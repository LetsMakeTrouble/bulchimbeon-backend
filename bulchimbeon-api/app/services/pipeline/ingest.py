"""인제스트 파이프라인 (`06 §1`) — 파싱 → 청킹 → 임베딩 → `ingest_status=ready`.

⚠️ **BackgroundTasks 규약** (`03 §2` 원칙 4, 위반 시 `MissingGreenlet` / 세션 조기 종료)
- 진입점 `run_ingest` 는 **`version_id: UUID` 하나만** 받는다. ORM 객체·`AsyncSession` 금지 —
  FastAPI 0.106.0 부터 `yield` 의존성이 백그라운드 태스크보다 **먼저** 정리되므로
  요청 세션은 태스크 실행 시점에 이미 닫혀 있다.
- 태스크 안에서 자체 세션을 연다. **실패를 기록할 때는 또 다른 새 세션**을 쓴다 —
  예외가 난 세션은 이미 롤백 대상이라 그 위에서 커밋할 수 없다.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import EMBEDDING_DIM_FIXED
from app.database import AsyncSessionLocal
from app.models.document import (
    INGEST_STATUS_FAILED,
    INGEST_STATUS_PROCESSING,
    INGEST_STATUS_READY,
    Chunk,
    Document,
    DocumentVersion,
)
from app.models.project import Project
from app.services import document_service, sse_manager
from app.services.llm import get_provider
from app.utils.chunking import ChunkDraft, chunk_pages
from app.utils.language import detect as detect_language
from app.utils.parsing import DocumentParseError, parse_file
from app.utils.storage import resolve_storage_path

logger = logging.getLogger(__name__)

# `06 §1` ③ — 임베딩은 100개 단위 배치로 부른다.
EMBED_BATCH_SIZE = 100

# `ingest_error` 는 **클라이언트에 그대로 내려가는 필드**다 (`05 §4` 목록·상세의 버전 요약).
# ⚠️ 예외 문자열을 그대로 넣지 않는다. 자르는 것만으로는 유출을 막지 못한다 —
#    `FileNotFoundError` 는 STORAGE_DIR 절대 경로를, SDK 예외는 요청 메타데이터를 문자열에
#    담는다 (`03 §7` 내부 정보 노출 금지. `main.py` 의 전역 핸들러도 같은 이유로 내부 사정을
#    응답에서 뺀다). 사용자에게는 아래 문안만 보이고, 원문은 `logger.exception` 에만 남는다.
MAX_INGEST_ERROR_LENGTH = 500
_GENERIC_INGEST_ERROR = "문서 처리에 실패했습니다. 파일을 확인한 뒤 다시 업로드해 주세요."


def _user_facing_error(exc: Exception) -> str:
    """`DocumentParseError` 만 사용자용 문안이다 — 우리가 직접 쓴 한국어 메시지다.

    그 외(임베딩 실패·IO 오류·SDK 예외 등)는 내부 사정이므로 일반 문구로 덮는다.
    """
    if isinstance(exc, DocumentParseError):
        return str(exc)[:MAX_INGEST_ERROR_LENGTH] or _GENERIC_INGEST_ERROR
    return _GENERIC_INGEST_ERROR


def _default_session_factory() -> AsyncSession:
    return AsyncSessionLocal()


# ⚠️ 테스트가 갈아끼우는 **유일한 지점**이다. 테스트 세션은 바깥 트랜잭션에 물린 커넥션을
#    공유해야 하므로(`03 §5.3` 롤백 격리) 전역 엔진의 세션을 그대로 쓸 수 없다.
session_factory: Callable[[], AsyncSession] = _default_session_factory


@dataclass(frozen=True)
class _VersionRef:
    """SSE 발행에 필요한 식별자. 세션이 닫힌 뒤에도 쓰이므로 ORM 객체를 들고 다니지 않는다.

    `answerer_id` 를 함께 담는 이유: `document.ingested` 의 수신자는 **담당자**이고
    (`05 §12.3`) SSE 구독은 유저 단위인데, 발행 시점에는 세션이 닫혀 조회할 수 없다.
    """

    project_id: UUID
    document_id: UUID
    version_id: UUID
    answerer_id: UUID | None


async def run_ingest(version_id: UUID, *, activate_on_ready: bool = True) -> None:
    """백그라운드 진입점. **UUID 와 불리언만 받는다** (`03 §2` 원칙 4).

    ORM 객체·`AsyncSession` 을 넘기지 않는다는 규약의 요지는 "태스크 실행 시점에 이미 닫힌
    요청 스코프 자원을 붙잡지 말 것"이다. 불변 원시값은 그 위험이 없다.

    `activate_on_ready` 는 업로드의 `auto_activate` 가 그대로 내려온 것이다.
    **활성 전환은 `ready` 에 도달한 뒤에 한다** — 업로드 시점에 활성화하면 인제스트가
    실패했을 때 신버전(`active` + `failed`)도 구버전(`inactive` + `ready`)도 검색 범위에서
    탈락해 그 문서의 근거가 통째로 사라진다 (`02 §5` 구현 노트).

    완료·실패 **양쪽 모두** SSE `document.ingested` 를 발행한다 (`05 §12.3`).
    실패를 알리지 않으면 프론트가 `pending` 상태로 영원히 폴링한다.
    """
    ref: _VersionRef | None = None
    status = INGEST_STATUS_READY

    try:
        async with session_factory() as db:
            ref = await _ingest(db, version_id, activate_on_ready=activate_on_ready)
            await db.commit()
    except Exception as exc:
        logger.exception("ingest 실패: version=%s", version_id)
        status = INGEST_STATUS_FAILED
        try:
            # ⚠️ 실패 기록은 반드시 **새 세션**이다 (`03 §2` 원칙 4).
            async with session_factory() as db:
                ref = await _mark_failed(db, version_id, exc)
                await db.commit()
        except Exception:
            logger.exception("ingest 실패 기록마저 실패: version=%s", version_id)

    if ref is None:
        # 버전이 사라졌거나 이미 처리 중이었다 — 발행할 대상이 없다.
        return

    sse_manager.publish_document_ingested(
        answerer_id=ref.answerer_id,
        document_id=ref.document_id,
        version_id=ref.version_id,
        status=status,
    )


async def _ingest(
    db: AsyncSession, version_id: UUID, *, activate_on_ready: bool
) -> _VersionRef | None:
    row = await db.execute(
        select(DocumentVersion, Document)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentVersion.id == version_id)
    )
    found = row.first()
    if found is None:
        logger.warning("ingest 대상 버전이 없다: %s", version_id)
        return None

    version, document = found
    ref = await _version_ref(db, version=version, document=document)

    # 진행 상태를 먼저 커밋한다 — 임베딩 배치는 수 초가 걸리고, 그동안 목록 조회가
    # `pending` 이 아니라 `processing` 을 보게 해야 "멈춘 건지 도는 건지"가 구분된다.
    version.ingest_status = INGEST_STATUS_PROCESSING
    version.ingest_error = None
    await db.commit()

    drafts = await _parse_and_chunk(resolve_storage_path(version.storage_path), version.mime)
    if not drafts:
        # ⚠️ 원인이 둘이다. "텍스트가 아예 없다"(스캔 PDF·빈 파일)와 "제목 줄뿐이고 본문이
        #    없다"(`utils/chunking._has_prose` 가 전부 버린 경우)는 담당자가 취할 조치가
        #    다르므로 문안에 둘 다 적는다. 이 문자열은 `ingest_error` 로 그대로 내려간다 (`05 §4`).
        raise DocumentParseError(
            "문서에서 근거로 쓸 본문을 찾지 못했습니다. "
            "(스캔 PDF 이거나, 내용이 비어 있거나, 제목 줄 외에 본문이 없습니다)"
        )

    # ⚠️ **`content` 가 아니라 `embedding_content` 를 임베딩한다** — 헤딩 줄을 뺀 본문이다.
    # M-1 캘리브레이션(`probe_calibration.py`)이 헤딩 없는 본문으로 임계값 5종을 뽑았는데
    # 여기서 헤딩까지 임베딩하면 **측정한 것과 다른 것을 운영**하게 된다. 실제로 배포 대조에서
    # 유사도가 가운데로 몰려(높은 쪽 하락·낮은 쪽 상승) Q8 이 `similarity_floor` 를 0.008 차이로
    # 넘겨 강제 🔴 이 풀렸다 — 시나리오 B 가 통째로 재현되지 않았다.
    # 헤딩은 `content` 와 `meta.heading_path` 에 그대로 남으므로 EVIDENCE 표시는 영향이 없다.
    embeddings = await _embed_all([draft.embedding_content or draft.content for draft in drafts])

    # 재인제스트(실패 후 재시도)가 청크를 중복 적재하지 않게 먼저 비운다.
    await db.execute(delete(Chunk).where(Chunk.document_version_id == version.id))

    for seq, (draft, embedding) in enumerate(zip(drafts, embeddings, strict=True)):
        db.add(
            Chunk(
                document_version_id=version.id,
                seq=seq,
                content=draft.content,
                meta=draft.to_meta(),
                embedding=embedding,
                # 임베딩에 넣은 것과 **같은 텍스트**로 판별한다 — 헤딩 줄이 붙은 `content` 로
                # 재면 영문 제목 한 줄 때문에 한국어 본문이 `en` 으로 기울 수 있다.
                language=detect_language(draft.embedding_content or draft.content),
            )
        )

    version.ingest_status = INGEST_STATUS_READY
    version.ingest_error = None
    await db.flush()

    if activate_on_ready:
        # `auto_activate=True` 의 실제 실행 지점이다 (`02 §5` 구현 노트).
        # 여기서 비로소 근거가 교체되므로 M4 재검토 연쇄도 이 경로에서 딱 한 번 발화한다.
        # ⚠️ 3문 스왑은 document_service 가 유일한 구현이다 — 여기에 복사하지 않는다 (`04 §7`).
        await document_service.activate_version(
            db, document=document, version=version, actor_id=version.uploaded_by
        )

    return ref


async def _parse_and_chunk(path: Path, mime: str) -> list[ChunkDraft]:
    """동기 파싱·청킹을 스레드로 밀어낸다 (룰 8)."""

    def _work() -> list[ChunkDraft]:
        return chunk_pages(parse_file(path, mime))

    if not path.exists():
        raise DocumentParseError(f"업로드 파일을 찾을 수 없습니다: {path.name}")

    return await asyncio.get_running_loop().run_in_executor(None, _work)


async def _embed_all(texts: list[str]) -> list[list[float]]:
    """`06 §1` ③ — 100개 단위 배치. 응답 차원은 컬럼 고정값으로 검증한다 (`04` 상단)."""
    provider = get_provider()
    vectors: list[list[float]] = []

    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        result = await provider.embed(batch)
        if len(result) != len(batch):
            raise ValueError(f"임베딩 개수 불일치: 요청 {len(batch)} / 응답 {len(result)}")
        vectors.extend(result)

    for vector in vectors:
        if len(vector) != EMBEDDING_DIM_FIXED:
            raise ValueError(
                f"임베딩 차원 불일치: 컬럼은 vector({EMBEDDING_DIM_FIXED}) 고정인데 "
                f"{len(vector)} 이 왔다."
            )
    return vectors


async def _mark_failed(db: AsyncSession, version_id: UUID, exc: Exception) -> _VersionRef | None:
    row = await db.execute(
        select(DocumentVersion, Document)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentVersion.id == version_id)
    )
    found = row.first()
    if found is None:
        return None

    version, document = found
    version.ingest_status = INGEST_STATUS_FAILED
    version.ingest_error = _user_facing_error(exc)
    await db.flush()
    return await _version_ref(db, version=version, document=document)


async def _version_ref(
    db: AsyncSession, *, version: DocumentVersion, document: Document
) -> _VersionRef:
    """SSE 수신자(담당자)까지 미리 담아 둔다 — 발행 시점에는 세션이 닫혀 있다."""
    project = await db.get(Project, document.project_id)
    return _VersionRef(
        project_id=document.project_id,
        document_id=document.id,
        version_id=version.id,
        answerer_id=project.answerer_id if project is not None else None,
    )
