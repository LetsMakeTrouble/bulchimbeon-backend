"""문서 업로드·버전·활성 전환 API (`05 §4`).

M2 DoD:
- 4개 포맷 각각 업로드 → ready 전환 → chunks 존재·heading_path 확인
- 재업로드 시 version_no 증가·기존 버전 보존, **활성 전환이 3문 스왑으로 성공**하고 활성 버전 1개
- 20MB 초과·비허용 확장자 400
- `document.ingested` 가 ready·failed 양쪽에서 발행
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MAX_REQUEST_BODY_BYTES, MAX_UPLOAD_BYTES
from app.models.document import Chunk, DocumentVersion
from app.models.event import Event
from app.schemas.document import MAX_TITLE_LENGTH
from app.services.event_service import EVENT_DOCUMENT_VERSION_ACTIVATED
from app.services.pipeline.ingest import _GENERIC_INGEST_ERROR
from app.services.pipeline.retrieval import searchable_chunk_ids
from tests.documents_helpers import (
    MARKDOWN_SAMPLE,
    SAMPLES,
    fetch_document,
    latest_version,
    upload_document,
    upload_version,
    version_by_no,
)
from tests.helpers import (
    API,
    Actor,
    create_actor,
    create_project,
    error_code,
    join_project,
)
from tests.helpers import error_message as _error_message


async def _answerer_project(client: AsyncClient, prefix: str) -> tuple[Actor, dict]:
    owner = await create_actor(client, f"{prefix}@example.com")
    project = await create_project(client, owner)
    return owner, project


async def _active_version_ids(db: AsyncSession, document_id: str) -> list[str]:
    db.expire_all()
    rows = await db.scalars(
        select(DocumentVersion.id).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.is_active.is_(True),
        )
    )
    return [str(value) for value in rows.all()]


# --- 업로드 · 인제스트 -------------------------------------------------------------------


@pytest.mark.parametrize("extension", sorted(SAMPLES))
async def test_upload_each_format_reaches_ready_with_chunks(
    client: AsyncClient, db_session: AsyncSession, extension: str
) -> None:
    """4개 포맷 각각: 201 pending → 인제스트 → ready + 청크(heading_path·page_no)."""
    owner, project = await _answerer_project(client, f"fmt{extension.strip('.')}")

    created = await upload_document(client, owner, project["id"], extension=extension)

    # `05 §4` — 업로드 응답은 pending 으로 즉시 201 이다. 완료는 SSE/폴링으로 안다.
    assert created["versions"][0]["ingest_status"] == "pending"
    # `auto_activate=true` 는 "지금 활성화"가 아니라 "ready 에 도달하면 활성화"다 (`02 §5`).
    assert created["active_version"] is None

    detail = await client.get(f"{API}/documents/{created['id']}", headers=owner.headers)
    assert detail.status_code == 200, detail.text
    version = detail.json()["active_version"]
    assert version["ingest_status"] == "ready", detail.text
    assert version["ingest_error"] is None

    chunks = list(
        (
            await db_session.scalars(
                select(Chunk).where(Chunk.document_version_id == version["id"]).order_by(Chunk.seq)
            )
        ).all()
    )
    assert chunks, f"{extension}: 청크가 만들어지지 않았다"
    assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        # `04 §2` — meta 는 `{heading_path, page_no}` 다.
        assert set(chunk.meta) == {"heading_path", "page_no"}
        assert isinstance(chunk.meta["heading_path"], list)
        assert len(chunk.embedding) == 1536

    if extension == ".pdf":
        # PDF 만 page_no 를 갖는다 (`05 §6` citations 표). 헤딩 문법이 없어 경로는 빈 목록이다.
        assert chunks[0].meta["page_no"] == 1
    else:
        assert chunks[0].meta["page_no"] is None

    if extension in (".md", ".txt", ".docx"):
        # 헤딩 경로가 실제로 중첩돼야 한다 — 프론트가 빵부스러기로 그린다.
        paths = [chunk.meta["heading_path"] for chunk in chunks]
        assert any(len(path) >= 2 for path in paths), paths


async def test_markdown_heading_path_is_nested(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, project = await _answerer_project(client, "headingpath")
    created = await upload_document(client, owner, project["id"], extension=".md")

    chunks = list(
        (
            await db_session.scalars(
                select(Chunk)
                .where(Chunk.document_version_id == latest_version(created)["id"])
                .order_by(Chunk.seq)
            )
        ).all()
    )
    assert [chunk.meta["heading_path"] for chunk in chunks] == [
        ["Refund Policy"],
        ["Refund Policy", "Standard Refund Window"],
        ["Refund Policy", "Partial Refunds"],
    ]


async def test_upload_rejects_unsupported_extension(client: AsyncClient) -> None:
    owner, project = await _answerer_project(client, "badext")

    response = await client.post(
        f"{API}/projects/{project['id']}/documents",
        files={"file": ("notes.xlsx", b"binary", "application/vnd.ms-excel")},
        headers=owner.headers,
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "UNSUPPORTED_FILE_TYPE"


async def test_upload_rejects_oversized_file(client: AsyncClient) -> None:
    """`03 §7` — 파일당 20MB. 2차 방어선(`_read_capped`)이 잡는 구간이다."""
    owner, project = await _answerer_project(client, "toobig")

    response = await client.post(
        f"{API}/projects/{project['id']}/documents",
        files={"file": ("huge.md", b"a" * (MAX_UPLOAD_BYTES + 1), "text/markdown")},
        headers=owner.headers,
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"
    # ⚠️ 400 VALIDATION_ERROR 는 폼 검증 실패와도 겹친다. 어느 관문이 잡았는지를 문안으로 가른다 —
    #    이걸 안 보면 미들웨어를 통째로 지워도 테스트가 초록으로 통과한다 (뮤테이션으로 확인).
    assert "파일이 너무 큽니다" in _error_message(response)


async def test_oversized_body_is_rejected_before_form_parsing(client: AsyncClient) -> None:
    """⚠️ 라우터 안에서 재는 것만으로는 방어가 아니다.

    `File(...)` 의존성은 라우터 본문 **이전에** multipart 전체를 파싱하고, Starlette 은
    파일 파트에 상한을 걸지 않아 `SpooledTemporaryFile` 이 디스크로 롤오버된다.
    미들웨어가 `Content-Length` 만 보고 본문을 **한 바이트도 읽지 않고** 끊어야 한다
    (`app/core/upload_limit.py`). 여기서는 헤더만 위조해 그 경로를 직접 밟는다.
    """
    owner, project = await _answerer_project(client, "hugebody")

    response = await client.post(
        f"{API}/projects/{project['id']}/documents",
        content=b"x",
        headers={
            **owner.headers,
            "content-type": "multipart/form-data; boundary=----probe",
            "content-length": str(MAX_REQUEST_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"
    assert "요청 본문이 너무 큽니다" in _error_message(response)


async def test_oversized_streamed_body_is_rejected_without_content_length(
    client: AsyncClient,
) -> None:
    """`Content-Length` 를 안 보내면(chunked) **실제 수신 바이트**로 끊는다.

    ⚠️ 본문이 **정상적인 multipart 여야** 이 경로가 검증된다. 깨진 boundary 를 흘리면
    파서가 첫 청크에서 먼저 죽어 400 이 나오고, 그러면 미들웨어를 지워도 테스트가
    통과한다(뮤테이션으로 확인). 그래서 진짜 multipart 를 조립해 상한 너머까지 흘린다.
    """
    owner, project = await _answerer_project(client, "chunkedbody")
    boundary = "----bulchimbeonprobe"
    megabyte = b"a" * (1024 * 1024)

    async def _stream():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="huge.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode()
        # 상한을 넘길 때까지 흘린다. 끝맺음(closing boundary)에는 도달하지 못한다.
        for _ in range((MAX_REQUEST_BODY_BYTES // len(megabyte)) + 2):
            yield megabyte
        yield f"\r\n--{boundary}--\r\n".encode()

    response = await client.post(
        f"{API}/projects/{project['id']}/documents",
        content=_stream(),
        headers={
            **owner.headers,
            "content-type": f"multipart/form-data; boundary={boundary}",
        },
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"
    assert "요청 본문이 너무 큽니다" in _error_message(response)


async def test_upload_rejects_empty_file(client: AsyncClient) -> None:
    owner, project = await _answerer_project(client, "emptyfile")

    response = await client.post(
        f"{API}/projects/{project['id']}/documents",
        files={"file": ("empty.md", b"", "text/markdown")},
        headers=owner.headers,
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"


async def test_ingest_failure_marks_version_failed_without_leaking_internals(
    client: AsyncClient, db_session: AsyncSession, fake_llm_provider
) -> None:
    """파싱·임베딩 실패는 `ingest_status=failed` + 사유 저장이다 (`06 §1` ①).

    ⚠️ `ingest_error` 는 클라이언트에 그대로 내려가는 필드다. 내부 예외 문자열(경로·SDK
    메타데이터)이 실려 나가면 안 된다 (`03 §7`) — 원문은 로그에만 남는다.
    """
    owner, project = await _answerer_project(client, "ingestfail")

    fake_llm_provider.embed_failure = "Refund Policy"
    try:
        created = await upload_document(client, owner, project["id"], extension=".md")
    finally:
        fake_llm_provider.embed_failure = None

    db_session.expire_all()
    version = await db_session.get(DocumentVersion, latest_version(created)["id"])
    assert version is not None
    assert version.ingest_status == "failed"
    assert version.ingest_error == _GENERIC_INGEST_ERROR
    assert "fake embed failure" not in version.ingest_error

    chunk_count = await db_session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_version_id == version.id)
    )
    assert chunk_count == 0


async def test_parse_failure_keeps_the_user_facing_reason(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """반대로 `DocumentParseError` 는 우리가 쓴 한국어 문안이므로 그대로 보여 준다."""
    owner, project = await _answerer_project(client, "parsefail")

    # 확장자는 .pdf 인데 내용이 PDF 가 아니다 — mime 은 확장자로 정하므로 업로드는 통과한다.
    created = await upload_document(
        client,
        owner,
        project["id"],
        extension=".pdf",
        filename="broken.pdf",
        content=b"this is definitely not a pdf",
    )

    db_session.expire_all()
    version = await db_session.get(DocumentVersion, latest_version(created)["id"])
    assert version is not None
    assert version.ingest_status == "failed"
    assert version.ingest_error and "PDF 파싱 실패" in version.ingest_error


async def test_content_of_unparsable_version_is_422_not_500(client: AsyncClient) -> None:
    """`DocumentParseError` 를 그대로 두면 전역 핸들러가 500 을 낸다.

    계약서에 이미 있는 422 `PIPELINE_FAILED` 로 내려야 프론트가 분기할 수 있다 (`05 §1.4`).
    """
    owner, project = await _answerer_project(client, "brokencontent")
    created = await upload_document(
        client,
        owner,
        project["id"],
        extension=".pdf",
        filename="broken.pdf",
        content=b"this is definitely not a pdf",
    )

    response = await client.get(
        f"{API}/documents/{created['id']}/versions/{latest_version(created)['id']}/content",
        headers=owner.headers,
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "PIPELINE_FAILED"


async def test_title_longer_than_limit_is_rejected(client: AsyncClient) -> None:
    owner, project = await _answerer_project(client, "longtitle")

    response = await client.post(
        f"{API}/projects/{project['id']}/documents",
        files={"file": ("x.md", b"# x\n\nbody", "text/markdown")},
        data={"title": "a" * (MAX_TITLE_LENGTH + 1)},
        headers=owner.headers,
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"


# --- 버전 · 활성 전환 -------------------------------------------------------------------


async def test_reupload_creates_new_version_and_keeps_old(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """재업로드 = 새 버전. **기존 버전은 지우지 않는다** (룰 5)."""
    owner, project = await _answerer_project(client, "reupload")
    created = await upload_document(client, owner, project["id"], extension=".md")

    updated = await upload_version(
        client,
        owner,
        created["id"],
        extension=".md",
        content=(MARKDOWN_SAMPLE + "\n## Japan\n\nJapan requires 20 days.\n").encode(),
    )

    assert [version["version_no"] for version in updated["versions"]] == [2, 1]
    # 201 시점에는 **아직 구버전이 활성**이다 — 새 버전은 ready 에 도달해야 넘겨받는다.
    assert updated["active_version"]["version_no"] == 1

    fresh = await fetch_document(client, owner, created["id"])
    assert fresh["active_version"]["version_no"] == 2
    assert version_by_no(fresh, 1)["is_active"] is False

    # 구버전 청크도 남아 있다 — 물리 삭제는 어디에도 없다.
    old_chunks = await db_session.scalar(
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.document_version_id == version_by_no(fresh, 1)["id"])
    )
    assert old_chunks and old_chunks > 0


async def test_failed_new_version_does_not_take_down_the_previous_evidence(
    client: AsyncClient, db_session: AsyncSession, fake_llm_provider
) -> None:
    """⭐ 활성 전환을 `ready` 뒤로 미루는 **이유** 그 자체 (`02 §5` 구현 노트).

    업로드 시점에 활성화하면 인제스트 실패 시 신버전(`active`+`failed`)도
    구버전(`inactive`+`ready`)도 검색 범위에서 탈락해 **그 문서의 근거가 통째로 사라진다**.
    담당자가 멀쩡한 문서 위에 깨진 파일을 덮어 올리는 것만으로 벌어지는 일이다.
    """
    owner, project = await _answerer_project(client, "keepevidence")
    document = await upload_document(client, owner, project["id"], extension=".md")
    good_version_id = latest_version(document)["id"]

    db_session.expire_all()
    evidence_before = await searchable_chunk_ids(db_session, project["id"])
    assert evidence_before, "첫 버전이 근거로 잡혀 있어야 한다"

    # 두 번째 버전은 인제스트가 실패한다.
    fake_llm_provider.embed_failure = "Refund Policy"
    try:
        updated = await upload_version(client, owner, document["id"], extension=".md")
    finally:
        fake_llm_provider.embed_failure = None

    broken_version_id = latest_version(updated)["id"]
    assert broken_version_id != good_version_id

    fresh = await fetch_document(client, owner, document["id"])
    assert version_by_no(fresh, 2)["ingest_status"] == "failed"
    # 깨진 버전은 활성이 되지 못했고, 멀쩡한 v1 이 그대로 활성이다.
    assert version_by_no(fresh, 2)["is_active"] is False
    assert fresh["active_version"]["id"] == good_version_id

    db_session.expire_all()
    assert await searchable_chunk_ids(db_session, project["id"]) == evidence_before


async def test_activating_a_version_that_is_not_ready_is_rejected(
    client: AsyncClient, fake_llm_provider
) -> None:
    """활성인데 검색되지 않는 버전은 근거 공백을 만든다 — 수동 경로도 같은 관문을 지난다."""
    owner, project = await _answerer_project(client, "activatenotready")
    document = await upload_document(client, owner, project["id"], extension=".md")

    fake_llm_provider.embed_failure = "Refund Policy"
    try:
        updated = await upload_version(client, owner, document["id"], extension=".md")
    finally:
        fake_llm_provider.embed_failure = None

    response = await client.patch(
        f"{API}/documents/{document['id']}/versions/{latest_version(updated)['id']}/activate",
        headers=owner.headers,
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "PIPELINE_FAILED"


async def test_upload_with_auto_activate_false_leaves_version_inactive(
    client: AsyncClient,
) -> None:
    """활성화를 미루면 재검토 연쇄도 활성 전환 시점까지 미뤄진다 (`02 §5` 구현 노트).

    ⚠️ 201 시점에는 `auto_activate` 참/거짓 **둘 다** `active_version: null` 이다.
    두 경우가 갈리는 곳은 **인제스트가 끝난 뒤**이므로 거기서 단언한다 —
    201 만 보면 이 테스트는 아무것도 검증하지 못한다.
    """
    owner, project = await _answerer_project(client, "noautoactivate")

    created = await upload_document(
        client, owner, project["id"], extension=".md", auto_activate=False
    )
    assert created["active_version"] is None

    fresh = await fetch_document(client, owner, created["id"])
    assert version_by_no(fresh, 1)["ingest_status"] == "ready"  # 인제스트는 정상 완료했는데
    assert version_by_no(fresh, 1)["is_active"] is False  # 활성화는 되지 않았다
    assert fresh["active_version"] is None


async def test_activate_survives_both_swap_directions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M-1 실측 회귀 (`04 §7`) — 부분 UNIQUE 스왑은 **행 처리 순서에 따라** 결과가 갈린다.

    낮은→높은 방향만 시험하면 잘못된 단일 `UPDATE … CASE` 구현이 **초록으로 통과**한 뒤
    운영에서 간헐 `23505` 를 낸다. 앞뒤로 교체해 어느 순서가 불리하든 반드시 밟히게 한다.
    """
    owner, project = await _answerer_project(client, "swapboth")
    document = await upload_document(client, owner, project["id"], extension=".md")
    document = await upload_version(client, owner, document["id"], extension=".md")

    first = version_by_no(document, 1)["id"]
    second = version_by_no(document, 2)["id"]

    holder, challenger = second, first
    for _ in range(4):
        response = await client.patch(
            f"{API}/documents/{document['id']}/versions/{challenger}/activate",
            headers=owner.headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["review_cascade_count"] == 0  # TODO(M4) 이전에는 항상 0 이다.
        assert body["message"]

        # 활성 버전은 **항상 정확히 1개**다.
        assert await _active_version_ids(db_session, document["id"]) == [challenger]
        holder, challenger = challenger, holder


async def test_activate_records_event(client: AsyncClient, db_session: AsyncSession) -> None:
    """`04 §5` — `document.version_activated` (룰 4: 모든 상태 변화는 events 에)."""
    owner, project = await _answerer_project(client, "activateevent")
    document = await upload_document(client, owner, project["id"], extension=".md")

    events = list(
        (
            await db_session.scalars(
                select(Event).where(
                    Event.project_id == project["id"],
                    Event.type == EVENT_DOCUMENT_VERSION_ACTIVATED,
                )
            )
        ).all()
    )
    assert len(events) == 1  # 업로드 시 auto_activate 기본 true 가 이미 한 번 전환한다.
    assert events[0].payload["document_id"] == document["id"]
    assert events[0].payload["version_no"] == 1


async def test_activate_message_follows_user_language(client: AsyncClient) -> None:
    """`05 §1.5` — 서버가 만드는 표시 문자열은 수신자 `users.language` 로 만든다."""
    owner = await create_actor(client, "en-answerer@example.com", language="en")
    project = await create_project(client, owner)
    document = await upload_document(client, owner, project["id"], extension=".md")

    response = await client.patch(
        f"{API}/documents/{document['id']}/versions/{latest_version(document)['id']}/activate",
        headers=owner.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "No verified answers need review."


# --- 목록 · 상세 · 원문 · 삭제 -----------------------------------------------------------


async def test_list_documents_includes_active_version_summary(client: AsyncClient) -> None:
    owner, project = await _answerer_project(client, "listdocs")
    await upload_document(client, owner, project["id"], extension=".md", title="Refund Policy")

    response = await client.get(f"{API}/projects/{project['id']}/documents", headers=owner.headers)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Refund Policy"
    assert items[0]["active_version"]["ingest_status"] == "ready"


async def test_version_content_endpoint_returns_source_text(client: AsyncClient) -> None:
    """기능 2.2 — §6 `citations[]` 의 두 id 로 만드는 근거 열람 URL (`05 §4`)."""
    owner, project = await _answerer_project(client, "content")
    document = await upload_document(client, owner, project["id"], extension=".md")
    version = latest_version(document)

    response = await client.get(
        f"{API}/documents/{document['id']}/versions/{version['id']}/content",
        headers=owner.headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == document["id"]
    assert body["version_id"] == version["id"]
    assert body["version_no"] == 1
    assert body["mime"] == "text/markdown"
    assert "Refunds are accepted within 30 days of purchase." in body["content"]


async def test_soft_delete_hides_document_from_list_but_keeps_content(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """D20 — soft delete. 청크는 **물리 삭제하지 않는다**."""
    owner, project = await _answerer_project(client, "softdelete")
    document = await upload_document(client, owner, project["id"], extension=".md")
    version_id = latest_version(document)["id"]

    deleted = await client.delete(f"{API}/documents/{document['id']}", headers=owner.headers)
    assert deleted.status_code == 204, deleted.text

    listing = await client.get(f"{API}/projects/{project['id']}/documents", headers=owner.headers)
    assert listing.json()["items"] == []

    # 과거 답변의 citations 가 가리키는 원문은 계속 열려야 한다.
    content = await client.get(
        f"{API}/documents/{document['id']}/versions/{version_id}/content", headers=owner.headers
    )
    assert content.status_code == 200, content.text

    remaining = await db_session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_version_id == version_id)
    )
    assert remaining and remaining > 0

    # 삭제된 문서에는 쓰기가 막힌다 (되살리기는 MVP 범위 밖).
    activate = await client.patch(
        f"{API}/documents/{document['id']}/versions/{version_id}/activate", headers=owner.headers
    )
    assert activate.status_code == 404
    assert error_code(activate) == "NOT_FOUND"


async def test_soft_delete_is_idempotent(client: AsyncClient) -> None:
    owner, project = await _answerer_project(client, "deltwice")
    document = await upload_document(client, owner, project["id"], extension=".md")

    for _ in range(2):
        response = await client.delete(f"{API}/documents/{document['id']}", headers=owner.headers)
        assert response.status_code == 204, response.text


# --- 권한 (룰 5 — 서버가 강제) ------------------------------------------------------------


async def test_asker_cannot_write_documents(client: AsyncClient) -> None:
    """`05 §4` — 담당자 전용 쓰기, 멤버 읽기."""
    owner, project = await _answerer_project(client, "askerwrite")
    asker = await create_actor(client, "askerwrite-asker@example.com")
    await join_project(client, asker, project["invite_code"])

    document = await upload_document(client, owner, project["id"], extension=".md")

    upload = await client.post(
        f"{API}/projects/{project['id']}/documents",
        files={"file": ("x.md", b"# x\n\nbody", "text/markdown")},
        headers=asker.headers,
    )
    assert upload.status_code == 403
    assert error_code(upload) == "FORBIDDEN_ROLE"

    delete = await client.delete(f"{API}/documents/{document['id']}", headers=asker.headers)
    assert delete.status_code == 403
    assert error_code(delete) == "FORBIDDEN_ROLE"

    # 읽기는 된다.
    read = await client.get(f"{API}/documents/{document['id']}", headers=asker.headers)
    assert read.status_code == 200, read.text


async def test_non_member_gets_404_on_document(client: AsyncClient) -> None:
    """비멤버에게 403 을 주면 "그 id 의 문서가 존재한다"가 새어 나간다."""
    owner, project = await _answerer_project(client, "outsiderdoc")
    document = await upload_document(client, owner, project["id"], extension=".md")
    outsider = await create_actor(client, "outsiderdoc-other@example.com")

    response = await client.get(f"{API}/documents/{document['id']}", headers=outsider.headers)

    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"
