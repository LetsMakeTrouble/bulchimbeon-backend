"""동기화 오케스트레이션 (`08 §3`·`§5`·`§6`).

> ### 문서/버전이 만들어진 뒤부터는 **M2·M4 경로 그대로**다 (`08 §3` 3번, 룰 5)
> 신규 파일 → `create_synced_document`, 변경 파일 → `create_version_from_bytes`.
> 그다음은 업로드와 완전히 같은 `run_ingest(version_id, activate_on_ready=True)` 를 탄다 —
> 파싱·청킹·임베딩, `ready` 도달 시 활성 전환(3문 스왑), 그리고 **재검토 연쇄**까지.
> 여기에 별도 인제스트 경로를 만들면 "문서가 바뀌었는데 확정 답변이 재검토되지 않는" 구멍이
> 생긴다.

⚠️ **BackgroundTasks 규약** (`03 §2` 원칙 4): 진입점 `run_sync` 는 **UUID 두 개만** 받는다.
태스크 실행 시점에 요청 세션은 이미 닫혀 있으므로 ORM 객체·`AsyncSession` 을 넘기지 않는다.

⚠️ **네트워크 호출은 세션 밖에서 한다.** 조회 → (판정·다운로드) → 쓰기 로 단계를 나눠,
원격 응답을 기다리는 동안 DB 커넥션을 붙잡지 않는다. 파일이 수십 개면 그 차이가 커진다.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import TokenDecryptionError
from app.core.errors import AppError
from app.database import AsyncSessionLocal
from app.models.document import DOCUMENT_STATUS_DELETED, INGEST_STATUS_FAILED, DocumentVersion
from app.models.integration import (
    PROVIDER_GITHUB,
    PROVIDER_NOTION,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_OK,
    Integration,
)
from app.models.project import Project
from app.services import (
    document_service,
    event_service,
    integration_service,
    notification_service,
    sse_manager,
)
from app.services.pipeline.ingest import run_ingest
from app.services.sync.base import (
    PartialListing,
    RemoteRef,
    StoredVersion,
    SyncError,
    SyncOutcome,
    SyncProvider,
)
from app.services.sync.github_sync import GithubSync
from app.services.sync.notion_sync import NotionSync
from app.utils.storage import resolve_storage_path

logger = logging.getLogger(__name__)


def _default_session_factory() -> AsyncSession:
    return AsyncSessionLocal()


# ⚠️ 테스트가 갈아끼우는 지점이다 — `pipeline/ingest.py` 와 같은 이유(`03 §5.3` 롤백 격리).
session_factory: Callable[[], AsyncSession] = _default_session_factory

_GENERIC_FILE_ERROR = "가져오지 못했습니다."


@dataclass(frozen=True)
class _SyncContext:
    """세션 경계를 넘어 다니는 값은 전부 원시값이다 (`03 §2` 원칙 4)."""

    integration_id: UUID
    project_id: UUID
    provider: str
    actor_id: UUID
    answerer_id: UUID | None
    config: dict[str, Any]


def build_provider(provider: str, config: dict[str, Any]) -> SyncProvider:
    """평문 config → provider 인스턴스. 테스트가 목 클라이언트를 넣는 자리이기도 하다."""
    if provider == PROVIDER_GITHUB:
        return GithubSync(
            repo=str(config["repo"]),
            branch=str(config["branch"]),
            path_glob=str(config["path_glob"]),
            token=config.get("token"),
        )
    if provider == PROVIDER_NOTION:
        return NotionSync(token=str(config["token"]), page_ids=list(config["page_ids"]))
    raise SyncError(f"알 수 없는 연동 종류입니다: {provider}")


async def run_sync(integration_id: UUID, *, actor_id: UUID) -> None:
    """백그라운드 진입점 (`05 §5` `POST /integrations/{id}/sync`).

    예외를 밖으로 내보내지 않는다 — 백그라운드 태스크에서 터진 예외는 응답에 실을 곳이 없고,
    그 대신 `last_sync_status='failed'` 와 `sync.failed` 알림이 담당자에게 결과를 알린다.
    """
    context = await _load_context(integration_id, actor_id=actor_id)
    if context is None:
        logger.warning("동기화 대상 연동이 없다: %s", integration_id)
        return

    outcome = SyncOutcome()
    if not context.config:
        # `_load_context` 가 복호화에 실패했다는 뜻이다 (`core/crypto.py`). 여기서 끊고
        # **원인을 그대로** 남긴다 — provider 를 만들다 KeyError 로 죽게 두면 담당자에게
        # "동기화 중 오류" 라는 일반 문구만 남아 키가 바뀐 것을 알 수 없다.
        outcome.fatal = "저장된 연동 설정을 읽을 수 없습니다. 연동을 다시 등록해 주세요."
        await _finish(context, outcome)
        return

    provider: SyncProvider | None = None
    try:
        provider = build_provider(context.provider, context.config)
        refs = await _list_documents(provider, outcome)
        for ref in refs:
            await _sync_one(context, provider, ref, outcome)
    except SyncError as exc:
        outcome.fatal = str(exc)
    except Exception:
        logger.exception("동기화 실패: integration=%s", integration_id)
        outcome.fatal = "동기화 중 오류가 발생했습니다."
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception:
                # 클라이언트 정리 실패가 동기화 결과를 덮지 않게 한다.
                logger.warning("동기화 클라이언트 정리 실패", exc_info=True)

    await _finish(context, outcome)


async def _load_context(integration_id: UUID, *, actor_id: UUID) -> _SyncContext | None:
    async with session_factory() as db:
        integration = await db.get(Integration, integration_id)
        if integration is None:
            return None
        project = await db.get(Project, integration.project_id)
        try:
            config = integration_service.decrypted_config(integration)
        except TokenDecryptionError as exc:
            # 여기서 멈추지 않는다 — 빈 config 로 진행하면 원인이 "GitHub 401" 로 둔갑한다.
            # 빈 값을 넘기고 `_finish` 가 fatal 로 기록하도록 config 를 비워 둔다.
            logger.warning("연동 토큰 복호화 실패: integration=%s (%s)", integration_id, exc)
            config = {}
        return _SyncContext(
            integration_id=integration.id,
            project_id=integration.project_id,
            provider=integration.provider,
            actor_id=actor_id,
            answerer_id=project.answerer_id if project is not None else None,
            config=config,
        )


async def _list_documents(provider: SyncProvider, outcome: SyncOutcome) -> list[RemoteRef]:
    """열거. **일부만 되면 그만큼 처리하고 나머지는 실패로 남긴다** (`08 §6`).

    GitHub 은 트리가 잘린 경우, Notion 은 페이지 일부가 지워지거나 공유가 풀린 경우가 여기 온다.
    """
    try:
        refs = await provider.list_documents()
    except PartialListing as exc:
        for source_ref, message in exc.failures:
            outcome.fail(source_ref, message)
        refs = exc.refs
    outcome.scanned = len(refs)
    return refs


async def _sync_one(
    context: _SyncContext, provider: SyncProvider, ref: RemoteRef, outcome: SyncOutcome
) -> None:
    """파일 하나. **여기서 난 예외는 이 파일만 실패시킨다** (`08 §6` 실패 격리)."""
    try:
        _existing_id, deleted, stored = await _current_state(context, provider, ref)
        if deleted:
            # 담당자가 지운 문서(D20)를 동기화가 되살리지 않는다 — "지웠는데 다시 생긴다"가
            # 되고, 되살릴지는 담당자가 판단할 몫이다.
            outcome.skipped += 1
            return

        if stored is not None and not await _exists(stored.path):
            # 저장 파일이 사라졌다 (배포에서 `STORAGE_DIR` 볼륨이 날아간 경우가 대표적이다).
            # 변경 판정의 기준점이 없으므로 원격을 다시 받아 새 버전으로 세운다 — 내용이 같아도
            # 재검토 연쇄가 한 번 발화한다. 조용히 넘기지 않고 결과에 남긴다.
            logger.warning("저장 파일이 없어 다시 받아 온다: %s (%s)", ref.source_ref, stored.path)
            outcome.restored += 1
            stored = None

        if stored is not None and await provider.is_unchanged(ref, stored):
            if stored.ingest_failed:
                # 내용은 그대로인데 직전 인제스트가 실패해 **활성 버전이 서지 못한 상태**다
                # (`02 §5` 구현 노트). 그대로 두면 그 문서는 근거 검색에서 영영 빠진 채
                # 매 실행이 `unchanged` 로 보고돼 실패 목록에서도 사라진다.
                # 같은 버전을 다시 태운다 — 새 버전을 만들면 깨진 파일 하나가 버전을 쌓는다.
                await _repair(ref, stored, outcome)
                return
            outcome.unchanged += 1
            return

        content = await provider.fetch(ref)
        if stored is not None and await _same_content(stored.path, content):
            # 변경 신호는 왔는데 내용이 같은 경우다 (Notion 에서 흔하다 — 페이지를 열어
            # 보기만 해도 `last_edited_time` 이 움직인다). 새 버전을 만들면 인제스트 비용과
            # **재검토 연쇄**가 헛돌아 담당자에게 가짜 카드가 쌓인다.
            outcome.unchanged += 1
            return

        version_id, created_document = await _write_version(context, provider, ref, content)
        if version_id is None:
            # 겹쳐 돈 다른 실행이 이미 같은 내용을 넣었다 (`_insert_version`).
            outcome.unchanged += 1
            return

        # M2 인제스트 + 활성 전환 + M4 재검토 연쇄 (모듈 독스트링).
        await run_ingest(version_id, activate_on_ready=True)

        error = await _ingest_error(version_id)
        if error is not None:
            outcome.fail(ref.source_ref, error)
            return

        if created_document:
            outcome.new_documents += 1
        else:
            outcome.new_versions += 1
    except SyncError as exc:
        outcome.fail(ref.source_ref, str(exc))
    except AppError as exc:
        # 문서 상한(`03 §7`) 같은 도메인 거절. 메시지가 이미 담당자용 한국어다.
        outcome.fail(ref.source_ref, exc.message)
    except Exception:
        logger.exception("파일 동기화 실패: %s", ref.source_ref)
        outcome.fail(ref.source_ref, _GENERIC_FILE_ERROR)


async def _repair(ref: RemoteRef, stored: StoredVersion, outcome: SyncOutcome) -> None:
    """인제스트가 실패한 채 남은 **같은 버전**을 다시 태운다 (`_sync_one` 의 근거 참조).

    `run_ingest` 는 재인제스트 전에 그 버전의 청크를 비우므로(`pipeline/ingest.py`) 중복이
    쌓이지 않는다. 성공하면 `ready` 도달 시점에 활성 전환과 재검토 연쇄까지 그대로 이어진다.
    """
    await run_ingest(stored.version_id, activate_on_ready=True)
    error = await _ingest_error(stored.version_id)
    if error is not None:
        outcome.fail(ref.source_ref, error)
        return
    outcome.repaired += 1


async def _exists(path: Path) -> bool:
    """파일 존재 확인. 디스크 접근은 블로킹이므로 스레드로 밀어낸다 (룰 8)."""
    return await asyncio.get_running_loop().run_in_executor(None, path.exists)


async def _current_state(
    context: _SyncContext, provider: SyncProvider, ref: RemoteRef
) -> tuple[UUID | None, bool, StoredVersion | None]:
    """이미 가진 문서와 최신 버전을 **원시값으로** 뽑아 온다. 이후 판정은 세션 밖에서 한다."""
    async with session_factory() as db:
        document = await document_service.find_by_source_ref(
            db,
            project_id=context.project_id,
            source_type=provider.source_type,
            source_ref=ref.source_ref,
        )
        if document is None:
            return None, False, None
        if document.status == DOCUMENT_STATUS_DELETED:
            return document.id, True, None

        version = await document_service.latest_version(db, document.id)
        if version is None:
            return document.id, False, None
        return (
            document.id,
            False,
            StoredVersion(
                version_id=version.id,
                path=resolve_storage_path(version.storage_path),
                created_at=version.created_at,
                ingest_failed=version.ingest_status == INGEST_STATUS_FAILED,
            ),
        )


async def _write_version(
    context: _SyncContext, provider: SyncProvider, ref: RemoteRef, content: bytes
) -> tuple[UUID | None, bool]:
    """문서/버전을 만들고 **커밋한다**. 돌려주는 값은 `(version_id, 새 문서인가)` 다.
    `version_id` 가 `None` 이면 다른 실행이 이미 같은 내용을 넣어 둔 것이라 만들 것이 없다.

    커밋이 여기서 끝나야 하는 이유: 다음 단계인 `run_ingest` 는 자체 세션을 여는 독립
    태스크라(`03 §2` 원칙 4) 커밋되지 않은 행을 보지 못한다.

    ⚠️ **조회-후-INSERT 는 그 자체로 방어가 아니다.** 담당자가 동기화를 연달아 트리거하면 두
    실행이 조회와 INSERT 사이에서 겹친다. 진짜 방어는 `uq_documents_source_ref` 부분 UNIQUE
    이고(`04 §7`, 락이 아니라 제약 — 결정 1.11 과 같은 방식), 여기서는 그 제약에 걸렸을 때
    **새 문서가 아니라 새 버전 경로로 한 번 다시 시도**한다.
    """
    try:
        return await _insert_version(context, provider, ref, content)
    except IntegrityError:
        logger.info(
            "같은 원본 문서를 다른 실행이 먼저 만들었다 — 버전 경로로 다시 시도한다: %s",
            ref.source_ref,
        )
        return await _insert_version(context, provider, ref, content, after_conflict=True)


async def _insert_version(
    context: _SyncContext,
    provider: SyncProvider,
    ref: RemoteRef,
    content: bytes,
    *,
    after_conflict: bool = False,
) -> tuple[UUID | None, bool]:
    async with session_factory() as db:
        document = await document_service.find_by_source_ref(
            db,
            project_id=context.project_id,
            source_type=provider.source_type,
            source_ref=ref.source_ref,
        )
        if document is None:
            _, version = await document_service.create_synced_document(
                db,
                project_id=context.project_id,
                source_type=provider.source_type,
                source_ref=ref.source_ref,
                title=ref.title,
                uploaded_by=context.actor_id,
                filename=ref.filename,
                mime=ref.mime,
                payload=content,
            )
            created_document = True
        else:
            if after_conflict:
                # 먼저 들어온 실행이 넣은 것이 **우리가 넣으려던 바로 그 내용**이면 새 버전을
                # 만들지 않는다. 만들면 같은 내용의 버전이 둘이 되고 재검토 연쇄가 헛돌아
                # 담당자에게 가짜 카드가 간다.
                latest = await document_service.latest_version(db, document.id)
                if latest is not None and await _same_content(
                    resolve_storage_path(latest.storage_path), content
                ):
                    return None, False

            version = await document_service.create_version_from_bytes(
                db,
                document=document,
                uploaded_by=context.actor_id,
                filename=ref.filename,
                mime=ref.mime,
                payload=content,
            )
            created_document = False

        version_id = version.id
        await db.commit()
    return version_id, created_document


async def _ingest_error(version_id: UUID) -> str | None:
    """인제스트 결과 확인.

    `run_ingest` 는 예외를 삼키고 `ingest_status='failed'` 로만 남기므로(그쪽 독스트링),
    여기서 다시 읽지 않으면 **깨진 파일이 "성공"으로 집계된다**.
    """
    async with session_factory() as db:
        version = await db.get(DocumentVersion, version_id)
        if version is None or version.ingest_status != INGEST_STATUS_FAILED:
            return None
        return version.ingest_error or _GENERIC_FILE_ERROR


async def _same_content(path: Path, content: bytes) -> bool:
    """저장된 파일과 내용이 같은가. 파일 읽기는 블로킹이므로 스레드로 밀어낸다 (룰 8)."""

    def _work() -> bool:
        return path.exists() and path.read_bytes() == content

    return await asyncio.get_running_loop().run_in_executor(None, _work)


async def _finish(context: _SyncContext, outcome: SyncOutcome) -> None:
    """`08 §5` — `last_synced_at`/`last_sync_status` 갱신 + `sync.run` 이벤트 + 알림 + SSE."""
    succeeded = outcome.succeeded
    async with session_factory() as db:
        integration = await db.get(Integration, context.integration_id)
        if integration is not None:
            integration.last_synced_at = datetime.now(UTC)
            integration.last_sync_status = SYNC_STATUS_OK if succeeded else SYNC_STATUS_FAILED

        # `04 §5` 의 마지막 타입. 실패 목록이 사는 곳이 여기다 (사용자 결정 2026-08-08).
        await event_service.record_event(
            db,
            project_id=context.project_id,
            type=event_service.EVENT_SYNC_RUN,
            actor_id=context.actor_id,
            entity_type=event_service.ENTITY_INTEGRATION,
            entity_id=context.integration_id,
            payload=outcome.to_payload(provider=context.provider),
        )

        project = await db.get(Project, context.project_id)
        if project is not None:
            await notification_service.notify_sync_result(
                db,
                project=project,
                integration_id=context.integration_id,
                provider=context.provider,
                succeeded=succeeded,
                new_documents=outcome.new_documents,
                new_versions=outcome.new_versions,
            )
            # `05 §12.3` 은 sync 계열 SSE 를 `sync.completed` **하나만** 정의한다 —
            # 실패에는 대응 이벤트가 없으므로 알림함과 `last_sync_status` 가 그 경로다.
            if succeeded and context.answerer_id is not None:
                sse_manager.queue_sync_completed(
                    db,
                    answerer_id=context.answerer_id,
                    integration_id=context.integration_id,
                    new_documents=outcome.new_documents,
                    new_versions=outcome.new_versions,
                )
        await db.commit()

    logger.info(
        "동기화 완료: integration=%s status=%s 새문서=%s 새버전=%s 실패=%s",
        context.integration_id,
        "ok" if succeeded else "failed",
        outcome.new_documents,
        outcome.new_versions,
        len(outcome.failed),
    )
