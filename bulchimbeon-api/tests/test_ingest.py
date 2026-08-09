"""인제스트 파이프라인 · 청킹 · 검색 범위 (`06 §1`, `06 §2` ③, D20).

API 레벨 시나리오는 `test_documents.py` 에 있다. 여기서는 순수 함수와 훅을 직접 본다.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Chunk, Document, DocumentVersion
from app.services import sse_manager
from app.services.pipeline import ingest, retrieval
from app.utils.chunking import (
    MAX_CHUNK_TOKENS,
    OVERLAP_RATIO,
    chunk_pages,
    estimate_tokens,
)
from app.utils.parsing import ParsedPage
from tests.documents_helpers import latest_version, upload_document, upload_version
from tests.helpers import API, create_actor, create_project

# --- 청킹 (`06 §1` ②) --------------------------------------------------------------------


def test_heading_path_tracks_nesting_and_resets() -> None:
    text = "\n".join(
        [
            "# Refund Policy",
            "intro",
            "## Japan",
            "japan body",
            "### Partial",
            "partial body",
            "# Shipping",
            "shipping body",
        ]
    )

    chunks = chunk_pages([ParsedPage(text=text)])

    assert [chunk.heading_path for chunk in chunks] == [
        ["Refund Policy"],
        ["Refund Policy", "Japan"],
        ["Refund Policy", "Japan", "Partial"],
        ["Shipping"],
    ]


def test_long_section_splits_with_overlap() -> None:
    """800토큰 초과 시 15% 오버랩 고정 분할 (`06 §1` ②)."""
    body = " ".join(f"word{index}" for index in range(4000))
    chunks = chunk_pages([ParsedPage(text=f"# Long\n\n{body}")])

    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_tokens(chunk.content) <= MAX_CHUNK_TOKENS
        assert chunk.heading_path == ["Long"]

    # 인접한 두 청크는 겹치는 꼬리를 공유한다 — 경계에 걸친 문장이 유실되지 않게 하는 장치다.
    first_words = chunks[0].content.split()
    second_words = chunks[1].content.split()
    overlap = max(1, int(len(first_words) * OVERLAP_RATIO))
    assert first_words[-overlap:] == second_words[:overlap]


def test_page_no_is_preserved_per_page() -> None:
    chunks = chunk_pages(
        [ParsedPage(text="# A\n\nalpha", page_no=1), ParsedPage(text="## B\n\nbeta", page_no=2)]
    )

    assert [(chunk.page_no, chunk.heading_path) for chunk in chunks] == [
        (1, ["A"]),
        (2, ["A", "B"]),
    ]


def test_empty_pages_produce_no_chunks() -> None:
    assert chunk_pages([ParsedPage(text="   \n\n  ")]) == []


def test_heading_only_section_is_not_a_chunk() -> None:
    """제목 줄만 남은 섹션은 청크가 아니다 (사용자 결정 2026-08-08, M9 실측).

    문서 맨 위 `# 제목` 과 첫 `##` 사이에는 본문이 없다. 그대로 두면 18~32자짜리 "제목 청크"가
    문서마다 하나씩 생기는데, 이 청크는 **어떤 사실도 진술하지 않으므로** ⑤ 근거 검증에서
    문장을 뒷받침할 수 없으면서 `retrieval_top_k`(6) 자리 하나를 차지한다.
    시드 4개 문서에서 22청크가 아니라 26청크가 나오던 원인이 이것이다 (`08 §2`).

    ⚠️ 버려진 제목이 `heading_path` 에 남는 것은 **그것이 조상일 때뿐이다** — 아래
    `test_heading_only_sibling_disappears_entirely` 가 그 경계를 못박는다.
    """
    chunks = chunk_pages([ParsedPage(text="# Refund Policy v1\n\n## Japan\n\njapan body")])

    assert [chunk.content for chunk in chunks] == ["## Japan\n\njapan body"]
    # 버려진 H1 은 조상이므로 빵부스러기(`05 §6`)에 그대로 남는다.
    assert chunks[0].heading_path == ["Refund Policy v1", "Japan"]

    # 헤딩만 연달아 나와도 마찬가지다 — 본문이 하나도 없으면 청크가 하나도 없다.
    assert chunk_pages([ParsedPage(text="# A\n\n## B\n\n### C")]) == []


def test_heading_only_sibling_disappears_entirely() -> None:
    """본문 없는 **형제** 헤딩은 content 에서도 heading_path 에서도 사라진다.

    `_split_by_headings` 가 `del heading_stack[level - 1:]` 로 같은 레벨 이하를 걷어내기
    때문이다. 조상(`# T`)은 남지만 형제(`## Japan`)는 남지 않는다 — 제목만 있는 섹션을
    버리기로 한 결정(2026-08-08)의 **대가**이고, 이 테스트가 그 대가를 명시적으로 기록한다.
    """
    chunks = chunk_pages([ParsedPage(text="# T\n\n## Japan\n\n## Korea\n\nkorea body")])

    assert len(chunks) == 1
    assert chunks[0].heading_path == ["T", "Korea"], "형제였던 Japan 은 남지 않는다"
    assert "Japan" not in chunks[0].content


# --- SSE 훅 (`05 §12.3`) ------------------------------------------------------------------


@pytest.mark.parametrize("should_fail", [False, True])
async def test_document_ingested_event_is_published_for_ready_and_failed(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_llm_provider,
    should_fail: bool,
) -> None:
    """`document.ingested` 는 **ready·failed 양쪽에서** 발행된다.

    실패를 알리지 않으면 프론트가 `pending` 인 채로 영원히 폴링한다 (`05 §4`).

    ⚠️ M5 에서 수신자가 `project_id` → **담당자 `answerer_id`** 로 바뀌었다. SSE 구독은
    유저 단위이고(`05 §12.3` 의 "수신자" 열) 이 이벤트의 수신자는 담당자다.
    """
    published: list[dict] = []

    def _spy(*, answerer_id, document_id, version_id, status) -> None:
        published.append(
            {
                "answerer_id": str(answerer_id),
                "document_id": str(document_id),
                "version_id": str(version_id),
                "status": status,
            }
        )

    monkeypatch.setattr(sse_manager, "publish_document_ingested", _spy)

    owner = await create_actor(client, f"ssehook-{should_fail}@example.com")
    project = await create_project(client, owner)

    fake_llm_provider.embed_failure = "Refund Policy" if should_fail else None
    try:
        document = await upload_document(client, owner, project["id"], extension=".md")
    finally:
        fake_llm_provider.embed_failure = None

    assert published == [
        {
            # 프로젝트 생성자가 담당자다 (D1).
            "answerer_id": owner.id,
            "document_id": document["id"],
            "version_id": latest_version(document)["id"],
            "status": "failed" if should_fail else "ready",
        }
    ]


async def test_run_ingest_on_missing_version_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사라진 버전에는 발행할 대상이 없다 — 조용히 끝나야 한다."""
    from uuid import uuid4

    published: list[object] = []

    def _spy(**kwargs: object) -> None:
        published.append(kwargs)

    monkeypatch.setattr(sse_manager, "publish_document_ingested", _spy)

    await ingest.run_ingest(uuid4())

    assert published == []


# --- 검색 범위 (`06 §2` ③, D20·D24) -------------------------------------------------------


async def test_soft_deleted_document_chunks_leave_the_search_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """D20 — soft delete 된 문서의 청크는 물리 삭제되지 않되 **검색에서 빠진다**."""
    owner = await create_actor(client, "scope-delete@example.com")
    project = await create_project(client, owner)
    document = await upload_document(client, owner, project["id"], extension=".md")
    version_id = latest_version(document)["id"]

    db_session.expire_all()
    before = await retrieval.searchable_chunk_ids(db_session, project["id"])
    assert before, "인제스트된 활성 버전 청크가 범위에 있어야 한다"

    response = await client.delete(f"{API}/documents/{document['id']}", headers=owner.headers)
    assert response.status_code == 204, response.text

    db_session.expire_all()
    assert await retrieval.searchable_chunk_ids(db_session, project["id"]) == []

    # 그러나 행은 남아 있다 — citations 가 청크를 가리키기 때문이다.
    stored = list(
        (
            await db_session.scalars(
                select(Chunk.id).where(Chunk.document_version_id == version_id)
            )
        ).all()
    )
    assert len(stored) == len(before)


async def test_inactive_version_chunks_leave_the_search_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """룰 5 — 구버전 청크는 남지만 근거가 아니다. 활성 버전만 검색 대상이다."""
    owner = await create_actor(client, "scope-version@example.com")
    project = await create_project(client, owner)
    document = await upload_document(client, owner, project["id"], extension=".md")
    old_version_id = latest_version(document)["id"]

    updated = await upload_version(client, owner, document["id"], extension=".txt")
    new_version_id = latest_version(updated)["id"]

    db_session.expire_all()
    scope = set(await retrieval.searchable_chunk_ids(db_session, project["id"]))
    old_chunks = set(
        (
            await db_session.scalars(
                select(Chunk.id).where(Chunk.document_version_id == old_version_id)
            )
        ).all()
    )
    new_chunks = set(
        (
            await db_session.scalars(
                select(Chunk.id).where(Chunk.document_version_id == new_version_id)
            )
        ).all()
    )

    assert old_chunks and new_chunks
    assert scope == new_chunks
    assert not (scope & old_chunks)


async def test_failed_version_is_not_searchable(
    client: AsyncClient, db_session: AsyncSession, fake_llm_provider
) -> None:
    """인제스트가 실패한 버전은 활성이어도 검색 대상이 아니다."""
    owner = await create_actor(client, "scope-failed@example.com")
    project = await create_project(client, owner)

    fake_llm_provider.embed_failure = "Refund Policy"
    try:
        await upload_document(client, owner, project["id"], extension=".md")
    finally:
        fake_llm_provider.embed_failure = None

    db_session.expire_all()
    assert await retrieval.active_version_ids(db_session, project["id"]) == []


@pytest.mark.parametrize("ingest_status", ["pending", "processing", "failed", "ready"])
async def test_only_ready_versions_enter_the_search_scope(
    client: AsyncClient, db_session: AsyncSession, ingest_status: str
) -> None:
    """`05 §12.3` — `document.ingested` 가 오기 전까지 그 버전은 검색 대상이 아니다.

    `pending`·`processing` 은 API 로는 재현이 어렵다(인제스트가 즉시 끝난다). 상태를 직접
    써서 **네 상태 전부**를 밟는다 — 그래야 `ingest_status='ready'` 조건이 실제로 검증된다.
    """
    owner = await create_actor(client, f"scope-status-{ingest_status}@example.com")
    project = await create_project(client, owner)
    document = await upload_document(client, owner, project["id"], extension=".md")
    version_id = latest_version(document)["id"]

    db_session.expire_all()
    version = await db_session.get(DocumentVersion, version_id)
    assert version is not None
    version.ingest_status = ingest_status
    await db_session.flush()

    scope = await retrieval.active_version_ids(db_session, project["id"])
    if ingest_status == "ready":
        assert [str(value) for value in scope] == [version_id]
    else:
        assert scope == []


async def test_search_scope_is_limited_to_the_project(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """D24 — 프로젝트 외부 지식 금지. 다른 프로젝트의 청크가 섞이면 안 된다."""
    owner = await create_actor(client, "scope-project-a@example.com")
    project_a = await create_project(client, owner, name="A")
    project_b = await create_project(client, owner, name="B")
    await upload_document(client, owner, project_a["id"], extension=".md")
    await upload_document(client, owner, project_b["id"], extension=".txt")

    db_session.expire_all()
    scope_a = await retrieval.searchable_chunk_ids(db_session, project_a["id"])
    scope_b = await retrieval.searchable_chunk_ids(db_session, project_b["id"])

    assert scope_a and scope_b
    assert not (set(scope_a) & set(scope_b))

    versions_a = list(
        (
            await db_session.scalars(
                select(DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.project_id == project_a["id"])
            )
        ).all()
    )
    assert await retrieval.active_version_ids(db_session, project_a["id"]) == versions_a
