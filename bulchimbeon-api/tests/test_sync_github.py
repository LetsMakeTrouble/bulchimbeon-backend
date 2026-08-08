"""GitHub 동기화 (`08 §3`) — 열거·변경 감지·파이프라인 재사용·실패 격리.

> ### 여기서 가짜인 것은 **GitHub 응답 하나**다
> `httpx.MockTransport` 로 응답만 갈아끼우므로 `path_glob` 매칭·sha 판정·에러 매핑·문서/버전
> 생성·인제스트·활성 전환·**재검토 연쇄**가 전부 실제 코드로 실행된다. 동기화를 통째로 목킹하면
> "동기화가 룰 5 를 태우는가"라는 M8 의 유일한 위험을 검증하지 못한다.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import (
    DOCUMENT_STATUS_DELETED,
    INGEST_STATUS_READY,
    Chunk,
    Document,
    DocumentVersion,
)
from app.models.integration import SYNC_STATUS_FAILED, SYNC_STATUS_OK, Integration
from app.models.question import (
    ANSWER_STATE_UNDER_REVIEW,
    ANSWER_STATE_VERIFIED,
    GRADE_YELLOW,
    QUESTION_STATUS_ANSWERED,
    Answer,
    AnswerCitation,
    Question,
)
from app.models.review_card import CARD_REASON_DOC_UPDATE, ReviewCard
from app.services import document_service, sse_manager
from app.services.pipeline import ingest
from app.services.sync.base import SyncError, git_blob_sha
from app.services.sync.github_sync import GithubSync, glob_to_regex, split_repo
from app.utils.storage import resolve_storage_path
from tests.documents_helpers import fetch_document
from tests.helpers import API
from tests.notification_helpers import dnd_window_excluding_now, inbox, patch_settings
from tests.pipeline_helpers import as_uuid
from tests.review_helpers import Team, build_team
from tests.sync_helpers import (
    REFUND_MD,
    REFUND_MD_V2,
    github_transport,
    install_github,
    list_documents,
    register_github,
    sync_events,
    trigger_sync,
)

DOC_PATH = "docs/refund-policy.md"
SOURCE_REF = f"acme/partner-docs:{DOC_PATH}"


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "github-sync.test")


def drain(queue) -> list:
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


async def sync_run_payload(client: AsyncClient, team: Team, integration_id: str) -> dict:
    events = await sync_events(client, team.owner, team.project_id, integration_id)
    runs = [event for event in events if event["type"] == "sync.run"]
    assert runs, f"sync.run 이벤트가 없다: {[event['type'] for event in events]}"
    return runs[-1]["payload"]


# --------------------------------------------------------------------------------------
# 순수 함수 — 글롭과 sha
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("docs/**/*.md", "docs/a.md", True),
        ("docs/**/*.md", "docs/policy/refund.md", True),
        ("docs/**/*.md", "readme.md", False),
        ("docs/**/*.md", "docs/a.txt", False),
        # ⚠️ `fnmatch` 였다면 `*` 가 `/` 까지 삼켜 아래가 True 가 된다 — 그게 이 함수의 존재 이유다.
        ("*.md", "docs/a.md", False),
        ("*.md", "a.md", True),
        ("**/*.md", "a.md", True),
        ("**/*.md", "deep/nested/a.md", True),
        ("docs/?.md", "docs/a.md", True),
        ("docs/?.md", "docs/ab.md", False),
    ],
)
def test_glob_matching_follows_git_semantics(pattern: str, path: str, matches: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is matches


def test_git_blob_sha_matches_git() -> None:
    """`git hash-object` 가 내는 값과 같아야 원격 sha 와 비교가 성립한다."""
    # `printf 'hello\n' | git hash-object --stdin`
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    # 빈 파일도 정의된 값이 있다.
    assert git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_split_repo_rejects_malformed_values() -> None:
    assert split_repo("acme/docs") == ("acme", "docs")
    with pytest.raises(SyncError):
        split_repo("acme")


# --------------------------------------------------------------------------------------
# 열거 (트리 API)
# --------------------------------------------------------------------------------------
async def test_listing_applies_the_glob_and_skips_non_files() -> None:
    provider = GithubSync(
        repo="acme/partner-docs",
        branch="main",
        path_glob="docs/**/*.md",
        transport=github_transport(
            {
                DOC_PATH: REFUND_MD,
                "docs/nested/shipping.md": b"# Shipping\n",
                "README.md": b"# root\n",  # 글롭 밖
                "docs/logo.png": b"\x89PNG",  # 파서가 없는 확장자
            }
        ),
    )
    try:
        refs = await provider.list_documents()
    finally:
        await provider.aclose()

    assert sorted(ref.source_ref for ref in refs) == [
        "acme/partner-docs:docs/nested/shipping.md",
        f"acme/partner-docs:{DOC_PATH}",
    ]
    # 제목은 경로 전체다 — 레포에 같은 파일명이 여럿일 때 목록에서 구분된다.
    assert {ref.title for ref in refs} == {DOC_PATH, "docs/nested/shipping.md"}
    # 저장 파일명은 디렉터리가 **버려진** 값이다 (`document_service.safe_filename`) —
    # 경로 탈출이 막히는 근거이며, 같은 이름이 겹쳐도 저장 위치가 버전마다 갈려 충돌하지 않는다.
    assert sorted(ref.filename for ref in refs) == ["refund-policy.md", "shipping.md"]


async def test_rate_limit_becomes_a_message_the_answerer_can_act_on() -> None:
    """403 + `x-ratelimit-remaining: 0` 은 "권한 없음"이 아니라 "한도 초과"다."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})

    provider = GithubSync(
        repo="acme/docs",
        branch="main",
        path_glob="**/*.md",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SyncError, match="한도"):
            await provider.list_documents()
    finally:
        await provider.aclose()


# --------------------------------------------------------------------------------------
# 신규 파일 → 새 문서 (`05 §5`) + 인제스트 파이프라인 재사용 (`08 §3` 3번)
# --------------------------------------------------------------------------------------
async def test_sync_creates_documents_that_went_through_the_real_ingest_pipeline(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD})

    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    assert len(documents) == 1
    document = documents[0]
    assert document["source_type"] == "github"
    assert document["source_ref"] == SOURCE_REF
    assert document["title"] == DOC_PATH

    # 활성 버전이 서 있고 인제스트가 끝났다 = M2 경로를 그대로 탔다.
    active = document["active_version"]
    assert active is not None
    assert active["ingest_status"] == INGEST_STATUS_READY
    assert active["version_no"] == 1

    # 청크가 실제로 만들어져 검색 대상이 됐다.
    chunks = (
        await db_session.scalars(
            select(Chunk).where(Chunk.document_version_id == as_uuid(active["id"]))
        )
    ).all()
    assert chunks, "인제스트가 청크를 만들지 않았다 — 근거 검색이 비게 된다"


async def test_sync_records_the_run_and_marks_the_integration(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`04 §5` 의 마지막 타입 `sync.run` + `last_synced_at`/`last_sync_status` (`08 §5`)."""
    integration = await register_github(client, team.owner, team.project_id)
    install_github(
        monkeypatch, {DOC_PATH: REFUND_MD, "docs/ship.md": b"# Shipping\n\nFive days.\n"}
    )

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["provider"] == "github"
    assert payload["status"] == "ok"
    assert payload["scanned"] == 2
    assert payload["new_documents"] == 2
    assert payload["new_versions"] == 0
    assert payload["failed"] == []

    row = await db_session.get(Integration, as_uuid(integration["id"]))
    await db_session.refresh(row)
    assert row is not None
    assert row.last_sync_status == SYNC_STATUS_OK
    assert row.last_synced_at is not None


async def test_sync_notifies_the_answerer_and_emits_sse(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`04 §4` `sync.completed` 알림 + `05 §12.3` SSE.

    SSE payload 는 `{integration_id, new_documents, new_versions}` 고정이다.
    """
    # DND 안에서 돌면 알림이 보류돼 알림함에 뜨지 않는다 (룰 6) — 창을 지금 밖으로 밀어 둔다.
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD})

    with sse_manager.subscribe(as_uuid(team.owner.id)) as queue:
        await trigger_sync(client, team.owner, integration["id"])
        messages = drain(queue)

    completed = [message for message in messages if message.event == "sync.completed"]
    assert len(completed) == 1
    assert completed[0].data == {
        "integration_id": integration["id"],
        "new_documents": 1,
        "new_versions": 0,
    }

    notifications = await inbox(client, team.owner)
    assert [item["type"] for item in notifications if item["type"].startswith("sync.")] == [
        "sync.completed"
    ]


# --------------------------------------------------------------------------------------
# 변경 감지 (`08 §3` — 파일 SHA)
# --------------------------------------------------------------------------------------
async def test_syncing_twice_without_changes_creates_nothing(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_github(client, team.owner, team.project_id)
    files = {DOC_PATH: REFUND_MD}
    install_github(monkeypatch, files)

    await trigger_sync(client, team.owner, integration["id"])
    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["new_documents"] == 0
    assert payload["new_versions"] == 0
    assert payload["unchanged"] == 1

    documents = await list_documents(client, team.owner, team.project_id)
    detail = await fetch_document(client, team.owner, documents[0]["id"])
    assert len(detail["versions"]) == 1, "내용이 같은데 새 버전이 생겼다"


async def test_a_changed_file_becomes_a_new_active_version(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_github(client, team.owner, team.project_id)
    files = {DOC_PATH: REFUND_MD}
    install_github(monkeypatch, files)
    await trigger_sync(client, team.owner, integration["id"])

    files[DOC_PATH] = REFUND_MD_V2  # 원격이 바뀌었다 → sha 가 달라진다
    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["new_documents"] == 0
    assert payload["new_versions"] == 1

    documents = await list_documents(client, team.owner, team.project_id)
    assert len(documents) == 1, "변경은 새 문서가 아니라 새 버전이어야 한다 (룰 5)"
    assert documents[0]["active_version"]["version_no"] == 2
    detail = await fetch_document(client, team.owner, documents[0]["id"])
    assert len(detail["versions"]) == 2


# --------------------------------------------------------------------------------------
# ⭐ 동기화가 만든 새 버전이 재검토 연쇄를 일으킨다 (룰 5, `08` 완료 기준)
# --------------------------------------------------------------------------------------
async def test_a_synced_new_version_sends_confirmed_answers_back_to_review(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M8 의 유일한 진짜 위험: 별도 경로를 만들면 룰 5 가 **조용히** 깨진다 (`08 §3` 3번).

    확정 답변을 동기화 문서의 청크에 붙여 두고, 원격이 바뀌었을 때 그 답변이 `under_review` 로
    내려가고 `doc_update` 카드가 서는지를 본다.
    """
    integration = await register_github(client, team.owner, team.project_id)
    files = {DOC_PATH: REFUND_MD}
    install_github(monkeypatch, files)
    await trigger_sync(client, team.owner, integration["id"])

    document = (
        await db_session.scalars(select(Document).where(Document.source_ref == SOURCE_REF))
    ).one()
    versions = select(DocumentVersion.id).where(DocumentVersion.document_id == document.id)
    chunk = (
        await db_session.scalars(
            select(Chunk).where(Chunk.document_version_id.in_(versions)).limit(1)
        )
    ).first()
    assert chunk is not None

    question = Question(
        project_id=document.project_id,
        asker_id=as_uuid(team.asker.id),
        content_ko="환불 기한이 며칠인가요?",
        content_en="What is the refund window?",
        status=QUESTION_STATUS_ANSWERED,
    )
    db_session.add(question)
    await db_session.flush()
    answer = Answer(
        question_id=question.id,
        grade=GRADE_YELLOW,
        state=ANSWER_STATE_VERIFIED,
        content_ko="구매 후 30일입니다.",
        content_en="Within 30 days of purchase.",
    )
    db_session.add(answer)
    await db_session.flush()
    db_session.add(
        AnswerCitation(answer_id=answer.id, chunk_id=chunk.id, quote="30 days", similarity=0.9)
    )
    await db_session.commit()

    files[DOC_PATH] = REFUND_MD_V2
    await trigger_sync(client, team.owner, integration["id"])

    await db_session.refresh(answer)
    assert answer.state == ANSWER_STATE_UNDER_REVIEW, (
        "동기화가 만든 새 버전이 재검토 연쇄를 일으키지 않았다 — 룰 5 가 깨졌다"
    )

    card = (
        await db_session.scalars(
            select(ReviewCard).where(
                ReviewCard.question_id == question.id,
                ReviewCard.reason == CARD_REASON_DOC_UPDATE,
            )
        )
    ).first()
    assert card is not None, "재검토 카드가 서지 않으면 담당자가 이 사실을 알 수 없다"

    # 담당자에게 "확정 답변 N건 재검토" 알림도 함께 간다 (룰 5, `04 §4`).
    notifications = await inbox(client, team.owner)
    assert any(item["type"] == "doc.review_needed" for item in notifications)


# --------------------------------------------------------------------------------------
# 실패 격리 (`08 §6`)
# --------------------------------------------------------------------------------------
async def test_one_broken_file_does_not_stop_the_rest(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_github(client, team.owner, team.project_id)
    install_github(
        monkeypatch,
        {DOC_PATH: REFUND_MD, "docs/broken.md": b"# Broken\n"},
        content_status={"docs/broken.md": 404},
    )

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["new_documents"] == 1
    assert [failure["source_ref"] for failure in payload["failed"]] == [
        "acme/partner-docs:docs/broken.md"
    ]
    # 파일 하나가 실패해도 실행 자체는 성공이다 — 나머지가 들어왔기 때문이다.
    assert payload["status"] == "ok"

    documents = await list_documents(client, team.owner, team.project_id)
    assert [document["source_ref"] for document in documents] == [SOURCE_REF]


async def test_a_run_that_imported_nothing_is_reported_as_failed(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전부 실패하면 `ok` 라고 말하지 않는다 (`SyncOutcome.succeeded`)."""
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD}, content_status={DOC_PATH: 500})

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["status"] == "failed"

    row = await db_session.get(Integration, as_uuid(integration["id"]))
    await db_session.refresh(row)
    assert row is not None
    assert row.last_sync_status == SYNC_STATUS_FAILED

    notifications = await inbox(client, team.owner)
    assert any(item["type"] == "sync.failed" for item in notifications)


async def test_a_failed_enumeration_is_fatal_and_creates_nothing(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD}, tree_status=404)

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["status"] == "failed"
    assert "찾을 수 없습니다" in payload["fatal"]
    assert await list_documents(client, team.owner, team.project_id) == []


async def test_a_failed_ingest_is_retried_on_the_same_version_not_reported_as_unchanged(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """인제스트가 실패한 문서는 **활성 버전이 없어 근거 검색에서 빠진다** (`02 §5` 구현 노트).

    내용이 안 바뀌었다는 이유로 다음 실행이 `unchanged` 로 넘겨 버리면 그 문서는 영영 검색에
    들어오지 못하면서 실패 목록에서도 사라진다 — 조용한 누락이다. 같은 버전을 다시 태워야 한다
    (새 버전을 만들면 깨진 파일 하나가 동기화마다 버전을 쌓는다).
    """
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD})

    # 첫 실행만 임베딩이 죽는다 (일시적 장애).
    real_embed = ingest._embed_all
    calls = {"n": 0}

    async def flaky_embed(texts: list[str]) -> list[list[float]]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시적 임베딩 장애")
        return await real_embed(texts)

    monkeypatch.setattr(ingest, "_embed_all", flaky_embed)
    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    assert documents[0]["active_version"] is None, "인제스트 실패 버전은 활성이 되면 안 된다"
    first = await sync_run_payload(client, team, integration["id"])
    assert len(first["failed"]) == 1

    # 두 번째 실행 — 내용은 그대로다.
    await trigger_sync(client, team.owner, integration["id"])

    second = await sync_run_payload(client, team, integration["id"])
    assert second["repaired"] == 1
    assert second["unchanged"] == 0
    assert second["failed"] == []
    assert second["new_versions"] == 0, "복구는 새 버전을 만들지 않는다"

    documents = await list_documents(client, team.owner, team.project_id)
    assert documents[0]["active_version"]["version_no"] == 1
    assert documents[0]["active_version"]["ingest_status"] == INGEST_STATUS_READY
    detail = await fetch_document(client, team.owner, documents[0]["id"])
    assert len(detail["versions"]) == 1


async def test_a_missing_stored_file_is_refetched_and_recorded(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """변경 감지의 기준점은 저장된 파일이다 — 그게 사라지면 판정할 수 없다.

    배포에서 `STORAGE_DIR` 볼륨이 날아가면 실제로 이 상태가 된다. 다시 받아 오되 **조용히
    넘기지 않는다**: 내용이 같아도 새 버전이 서면서 재검토 연쇄가 한 번 발화하기 때문이다.
    """
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD})
    await trigger_sync(client, team.owner, integration["id"])

    version = (
        await db_session.scalars(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.source_ref == SOURCE_REF)
        )
    ).one()
    resolve_storage_path(version.storage_path).unlink()

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["restored"] == 1
    assert payload["new_versions"] == 1
    assert payload["unchanged"] == 0


async def test_an_unreadable_token_says_so_instead_of_blaming_github(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`INTEGRATION_ENCRYPTION_KEY` 가 바뀐 뒤의 동기화 (`core/crypto.py` 독스트링).

    일반 오류 문구로 덮으면 담당자가 "GitHub 이 이상하다"로 오해해 원인에 영원히 도달하지 못한다.
    """
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD})
    monkeypatch.setattr(settings, "integration_encryption_key", "동기화-전에-바뀐-키")

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["status"] == "failed"
    assert "다시 등록" in payload["fatal"]

    row = await db_session.get(Integration, as_uuid(integration["id"]))
    await db_session.refresh(row)
    assert row is not None
    assert row.last_sync_status == SYNC_STATUS_FAILED
    assert await list_documents(client, team.owner, team.project_id) == []


async def test_a_truncated_tree_is_reported_instead_of_silently_dropped(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조용히 자르지 않는다 — 잘린 채로 성공했다고 말하면 근거가 비어도 아무도 모른다."""
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD}, truncated=True)

    await trigger_sync(client, team.owner, integration["id"])

    payload = await sync_run_payload(client, team, integration["id"])
    assert any("잘라서" in failure["error"] for failure in payload["failed"])
    # 받아 온 만큼은 처리한다.
    assert payload["new_documents"] == 1


# --------------------------------------------------------------------------------------
# 같은 원본 = 문서 하나 (`04 §7` 부분 UNIQUE, 사용자 결정 2026-08-08)
# --------------------------------------------------------------------------------------
async def test_the_database_refuses_a_second_document_for_the_same_source(
    db_session: AsyncSession, team: Team
) -> None:
    """조회-후-INSERT 는 방어가 아니다 — 진짜 방어는 제약이다 (`04 §7`).

    두 벌이 생기면 조회가 항상 한쪽만 집으므로 나머지가 **낡은 내용으로 활성 상태를 유지**하고,
    룰 6("근거 문서 내용만")이 낡은 근거를 인용하는 형태로 조용히 깨진다.
    """

    def build() -> Document:
        return Document(
            project_id=as_uuid(team.project_id),
            title=DOC_PATH,
            source_type="github",
            source_ref=SOURCE_REF,
            status="active",
        )

    db_session.add(build())
    await db_session.flush()

    db_session.add(build())
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_uploads_are_not_affected_by_the_source_ref_constraint(
    db_session: AsyncSession, team: Team
) -> None:
    """업로드 문서는 `source_ref` 가 NULL 이라 부분 UNIQUE 의 대상이 아니다 (`04 §7`).

    이게 깨지면 같은 프로젝트에 문서를 두 개 올릴 수 없게 된다 — M2 가 통째로 막힌다.
    """
    for _ in range(2):
        db_session.add(
            Document(
                project_id=as_uuid(team.project_id),
                title="Refund Policy",
                source_type="upload",
                source_ref=None,
                status="active",
            )
        )
    await db_session.flush()  # 예외가 나지 않아야 한다


async def test_a_concurrent_run_that_already_wrote_the_same_content_creates_nothing(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """제약에 걸린 실행은 죽지 않고 **버전 경로로 재시도**한다 (`runner._write_version`).

    겹쳐 돈 다른 실행이 이미 같은 내용을 넣었으면 새 버전도 만들지 않는다 — 만들면 같은 내용의
    버전이 둘이 되고 재검토 연쇄가 헛돌아 담당자에게 가짜 카드가 간다.

    조회가 낡은 값을 보는 구간(다른 실행이 INSERT 했지만 우리 조회가 그 전에 일어난 상태)을
    `find_by_source_ref` 를 두 번만 `None` 으로 만들어 재현한다.
    """
    integration = await register_github(client, team.owner, team.project_id)
    install_github(monkeypatch, {DOC_PATH: REFUND_MD})
    await trigger_sync(client, team.owner, integration["id"])  # 문서가 이미 있다

    real_find = document_service.find_by_source_ref
    calls = {"n": 0}

    async def stale_find(db, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:  # _current_state 1회 + _insert_version 첫 시도 1회
            return None
        return await real_find(db, **kwargs)

    monkeypatch.setattr(document_service, "find_by_source_ref", stale_find)
    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    assert len(documents) == 1, "제약이 두 번째 문서를 막았어야 한다"
    detail = await fetch_document(client, team.owner, documents[0]["id"])
    assert len(detail["versions"]) == 1, "같은 내용인데 새 버전이 생겼다"

    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["unchanged"] == 1
    assert payload["failed"] == []


# --------------------------------------------------------------------------------------
# 담당자가 지운 문서 (D20)
# --------------------------------------------------------------------------------------
async def test_sync_does_not_resurrect_a_deleted_document(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_github(client, team.owner, team.project_id)
    files = {DOC_PATH: REFUND_MD}
    install_github(monkeypatch, files)
    await trigger_sync(client, team.owner, integration["id"])

    document = (
        await db_session.scalars(select(Document).where(Document.source_ref == SOURCE_REF))
    ).one()
    deleted = await client.delete(f"{API}/documents/{document.id}", headers=team.owner.headers)
    assert deleted.status_code == 204, deleted.text

    files[DOC_PATH] = REFUND_MD_V2
    await trigger_sync(client, team.owner, integration["id"])

    await db_session.refresh(document)
    assert document.status == DOCUMENT_STATUS_DELETED
    payload = await sync_run_payload(client, team, integration["id"])
    assert payload["skipped"] == 1
    assert payload["new_documents"] == 0
    assert payload["new_versions"] == 0
