"""Notion 동기화 (`08 §4`) — 블록 → 마크다운 변환과 `last_edited_time` 변경 감지.

변환은 순수 함수라 목킹 없이 그대로 검증한다. SDK 호출이 필요한 부분만
`tests/sync_helpers.StubNotionClient` 로 대체한다 (`08` 완료 기준의 "notion-client 목킹").

> ### 왜 변환을 이렇게까지 보는가
> 인제스트는 **마크다운 헤딩 하나로만** 문서 구조를 인식한다 (`06 §1` ②). 변환이 헤딩을
> 흘리면 청크의 `heading_path` 가 비고, 그러면 `05 §6` `citations[].heading_path` 도 비어
> 담당자가 근거 위치를 짚을 수 없다 — 화면에서야 드러나는 종류의 결함이다.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.services.sync.base import PartialListing
from app.services.sync.notion_sync import (
    NotionSync,
    page_title,
    parse_notion_time,
    render_block,
    rich_text_to_markdown,
)
from tests.documents_helpers import fetch_document
from tests.pipeline_helpers import as_uuid
from tests.review_helpers import Team, build_team
from tests.sync_helpers import (
    StubNotionClient,
    install_notion,
    list_documents,
    notion_page,
    register_notion,
    rich_text,
    sync_events,
    text_block,
    trigger_sync,
)

PAGE_ID = "page-refund"
OLD_TIME = "2026-01-01T00:00:00.000Z"


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "notion-sync.test")


def render(block: dict) -> list[str]:
    lines, _ = render_block(block)
    return lines


# --------------------------------------------------------------------------------------
# 인라인 (rich text)
# --------------------------------------------------------------------------------------
def test_annotations_become_markdown() -> None:
    spans = [
        rich_text("plain "),
        rich_text("bold", bold=True),
        rich_text(" and "),
        rich_text("italic", italic=True),
        rich_text(" and "),
        rich_text("gone", strikethrough=True),
    ]
    assert rich_text_to_markdown(spans) == "plain **bold** and *italic* and ~~gone~~"


def test_inline_code_stays_inside_emphasis() -> None:
    """`**`x`**` 순서라야 마크다운이 깨지지 않는다."""
    assert rich_text_to_markdown([rich_text("x", code=True, bold=True)]) == "**`x`**"


def test_links_are_preserved() -> None:
    assert rich_text_to_markdown([rich_text("docs", href="https://ex.com")]) == (
        "[docs](https://ex.com)"
    )


# --------------------------------------------------------------------------------------
# 블록 (`08 §4` — 헤딩·리스트·코드·테이블 기본만)
# --------------------------------------------------------------------------------------
def test_headings_become_markdown_headings() -> None:
    assert render(text_block("heading_1", "Refund Policy"))[0] == "# Refund Policy"
    assert render(text_block("heading_2", "Window"))[0] == "## Window"
    assert render(text_block("heading_3", "Japan"))[0] == "### Japan"


def test_list_items_and_todos() -> None:
    assert render(text_block("bulleted_list_item", "first")) == ["- first"]
    assert render(text_block("to_do", "done", checked=True)) == ["- [x] done"]
    assert render(text_block("to_do", "open", checked=False)) == ["- [ ] open"]


def test_numbered_items_count_up_and_reset_between_runs() -> None:
    """번호는 **연속된** 항목 안에서만 이어진다 — Notion 에서도 사이에 문단이 끼면 끊긴다."""
    first, counter = render_block(text_block("numbered_list_item", "one"), numbered=0)
    second, counter = render_block(text_block("numbered_list_item", "two"), numbered=counter)
    _, counter = render_block(text_block("paragraph", "사이에 낀 문단"), numbered=counter)
    third, _ = render_block(text_block("numbered_list_item", "again"), numbered=counter)

    assert first == ["1. one"]
    assert second == ["2. two"]
    assert third == ["1. again"]


def test_code_blocks_keep_their_body_verbatim() -> None:
    """코드 안의 `*` 를 강조로 바꾸면 원문이 훼손되고 인용이 원문과 달라진다."""
    block = {
        "id": "b",
        "type": "code",
        "code": {
            "language": "python",
            "rich_text": [rich_text("rate = a * b\nprint(rate)", bold=True)],
        },
    }
    assert render(block) == ["```python", "rate = a * b", "print(rate)", "```", ""]


def test_tables_become_markdown_tables() -> None:
    block = {
        "id": "t",
        "type": "table",
        "table": {"table_width": 2},
        "_rows": [
            {
                "type": "table_row",
                "table_row": {"cells": [[rich_text("Region")], [rich_text("Days")]]},
            },
            {"type": "table_row", "table_row": {"cells": [[rich_text("JP")], [rich_text("20")]]}},
            # 셀이 모자란 행도 표를 깨뜨리지 않는다.
            {"type": "table_row", "table_row": {"cells": [[rich_text("KR")]]}},
        ],
    }
    assert render(block) == [
        "| Region | Days |",
        "| --- | --- |",
        "| JP | 20 |",
        "| KR |  |",
        "",
    ]


def test_unsupported_blocks_contribute_nothing() -> None:
    """`08 §4` 범위 밖(이미지·임베드)은 본문에 넣지 않는다.

    근거 없는 문장을 만들지 않기 위해서다 (룰 6).
    """
    assert render({"id": "i", "type": "image", "image": {}}) == []


def test_page_title_reads_the_title_property_whatever_its_name_is() -> None:
    assert page_title(notion_page(PAGE_ID, "Refund Policy", OLD_TIME)) == "Refund Policy"
    assert page_title({"properties": {"Tags": {"type": "multi_select"}}}) == ""


def test_notion_timestamps_parse_to_aware_datetimes() -> None:
    parsed = parse_notion_time("2026-08-08T01:02:03.000Z")
    assert parsed is not None
    assert parsed == datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC)
    assert parse_notion_time("not-a-time") is None
    assert parse_notion_time("") is None


# --------------------------------------------------------------------------------------
# 블록 트리 → 문서 본문
# --------------------------------------------------------------------------------------
async def test_fetch_renders_the_whole_block_tree() -> None:
    blocks = {
        PAGE_ID: [
            text_block("heading_2", "Standard Window"),
            text_block("paragraph", "Refunds are accepted within 30 days."),
            {
                "id": "toggle-1",
                "type": "bulleted_list_item",
                "has_children": True,
                "bulleted_list_item": {"rich_text": [rich_text("Regions")]},
            },
        ],
        "toggle-1": [text_block("bulleted_list_item", "Japan: 20 days")],
    }
    stub = StubNotionClient({PAGE_ID: notion_page(PAGE_ID, "Refund Policy", OLD_TIME)}, blocks)
    provider = NotionSync(token="ntn_x", page_ids=[PAGE_ID], client=stub)

    refs = await provider.list_documents()
    markdown = (await provider.fetch(refs[0])).decode("utf-8")

    assert markdown.startswith("# Refund Policy\n")
    assert "## Standard Window" in markdown
    assert "- Regions" in markdown
    # 중첩 블록은 들여쓰기로 계층을 유지한다.
    assert "  - Japan: 20 days" in markdown


async def test_long_pages_follow_pagination_to_the_end() -> None:
    """`has_more` 를 무시하면 긴 문서의 **뒷부분이 통째로 사라진다** — 근거가 비는 결함이다.

    스텁이 한 번에 다 주면 페이지네이션 루프가 한 번도 실행되지 않아 이 보호가 초록으로
    지나간다. 그래서 블록 목록을 1건씩 쪼개 돌려준다.
    """
    blocks = {
        PAGE_ID: [
            text_block("paragraph", "first"),
            text_block("paragraph", "second"),
            text_block("paragraph", "third"),
        ]
    }
    stub = StubNotionClient(
        {PAGE_ID: notion_page(PAGE_ID, "Refund Policy", OLD_TIME)}, blocks, page_size=1
    )
    provider = NotionSync(token="ntn_x", page_ids=[PAGE_ID], client=stub)

    refs = await provider.list_documents()
    markdown = (await provider.fetch(refs[0])).decode("utf-8")

    assert "first" in markdown
    assert "second" in markdown
    assert "third" in markdown


async def test_one_missing_page_does_not_take_down_the_others() -> None:
    """`08 §6` 실패 격리 — 페이지 하나를 지우거나 공유를 풀면 그 한 건만 실패해야 한다.

    통째로 올리면 담당자가 config 를 직접 고치기 전까지 그 연동이 영구히 죽는다.
    """
    pages = {
        "good-1": notion_page("good-1", "Refund Policy", OLD_TIME),
        "good-2": notion_page("good-2", "Shipping Policy", OLD_TIME),
    }
    stub = StubNotionClient(pages, {}, missing={"gone"})
    provider = NotionSync(token="ntn_x", page_ids=["good-1", "gone", "good-2"], client=stub)

    with pytest.raises(PartialListing) as caught:
        await provider.list_documents()

    assert [ref.source_ref for ref in caught.value.refs] == ["good-1", "good-2"]
    assert [page_id for page_id, _ in caught.value.failures] == ["gone"]


async def test_sync_imports_the_good_pages_and_reports_the_missing_one(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """위 격리가 실행 결과까지 이어지는지 — 좋은 페이지는 들어오고 실패는 payload 에 남는다."""
    integration = await register_notion(
        client, team.owner, team.project_id, ["good-1", "gone", "good-2"]
    )
    install_notion(
        monkeypatch,
        {
            "good-1": notion_page("good-1", "Refund Policy", OLD_TIME),
            "good-2": notion_page("good-2", "Shipping Policy", OLD_TIME),
        },
        {
            "good-1": [text_block("paragraph", "Refunds are accepted within 30 days.")],
            "good-2": [text_block("paragraph", "Standard shipping takes five days.")],
        },
        missing={"gone"},
    )

    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    assert sorted(document["source_ref"] for document in documents) == ["good-1", "good-2"]

    events = await sync_events(client, team.owner, team.project_id, integration["id"])
    payload = events[-1]["payload"]
    assert payload["new_documents"] == 2
    assert [failure["source_ref"] for failure in payload["failed"]] == ["gone"]
    # 나머지가 들어왔으므로 실행 자체는 성공이다 (`08 §6`).
    assert payload["status"] == "ok"


async def test_unchanged_when_the_page_was_edited_before_we_pulled_it() -> None:
    """`08 §4` 변경 감지 — 기준점은 **우리가 그 페이지를 마지막으로 받아 온 시각**이다."""
    from pathlib import Path
    from uuid import uuid4

    from app.services.sync.base import StoredVersion

    stub = StubNotionClient(
        {PAGE_ID: notion_page(PAGE_ID, "Refund Policy", "2026-08-01T00:00:00.000Z")}, {}
    )
    provider = NotionSync(token="ntn_x", page_ids=[PAGE_ID], client=stub)
    ref = (await provider.list_documents())[0]

    pulled_later = StoredVersion(
        version_id=uuid4(), path=Path("/nonexistent"), created_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    pulled_earlier = StoredVersion(
        version_id=uuid4(), path=Path("/nonexistent"), created_at=datetime(2026, 7, 1, tzinfo=UTC)
    )

    assert await provider.is_unchanged(ref, pulled_later) is True
    assert await provider.is_unchanged(ref, pulled_earlier) is False


async def test_an_edit_in_the_same_minute_as_our_pull_counts_as_changed() -> None:
    """Notion 의 `last_edited_time` 은 분 단위로 절삭돼 오는 경우가 있다.

    그대로 비교하면 우리가 받아 온 **직후**의 편집이 우리 시각보다 앞서 찍혀 영원히
    "변경 없음"이 된다 — 낡은 근거가 그대로 남는 조용한 결함이다. 판정을 변경 쪽으로 기울이면
    최악이 "한 번 더 받아 오는 것"이고 그건 `runner._same_content` 가 걸러 낸다.
    """
    from pathlib import Path
    from uuid import uuid4

    from app.services.sync.base import StoredVersion

    stub = StubNotionClient(
        {PAGE_ID: notion_page(PAGE_ID, "Refund Policy", "2026-08-08T10:00:00.000Z")}, {}
    )
    provider = NotionSync(token="ntn_x", page_ids=[PAGE_ID], client=stub)
    ref = (await provider.list_documents())[0]

    same_minute = StoredVersion(
        version_id=uuid4(),
        path=Path("/nonexistent"),
        created_at=datetime(2026, 8, 8, 10, 0, 30, tzinfo=UTC),
    )

    assert await provider.is_unchanged(ref, same_minute) is False


# --------------------------------------------------------------------------------------
# 동기화 (문서/버전 파이프라인은 GitHub 과 같은 코드다)
# --------------------------------------------------------------------------------------
def _blocks(body: str) -> dict:
    return {
        PAGE_ID: [
            text_block("heading_2", "Standard Window"),
            text_block("paragraph", body),
        ]
    }


async def test_sync_creates_a_notion_document(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_notion(client, team.owner, team.project_id, [PAGE_ID])
    install_notion(
        monkeypatch,
        {PAGE_ID: notion_page(PAGE_ID, "Refund Policy", OLD_TIME)},
        _blocks("Refunds are accepted within 30 days of purchase."),
    )

    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    assert len(documents) == 1
    assert documents[0]["source_type"] == "notion"
    assert documents[0]["source_ref"] == PAGE_ID
    assert documents[0]["title"] == "Refund Policy"
    assert documents[0]["active_version"]["ingest_status"] == "ready"

    stored = (
        await db_session.scalars(select(Document).where(Document.source_ref == PAGE_ID))
    ).one()
    assert stored.project_id == as_uuid(team.project_id)


async def test_an_edited_page_with_identical_content_does_not_create_a_version(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """페이지를 열어 보기만 해도 `last_edited_time` 이 움직인다 — 내용이 같으면 새 버전을 만들지
    않는다. 만들면 인제스트 비용과 **재검토 연쇄**가 헛돌아 가짜 카드가 쌓인다."""
    integration = await register_notion(client, team.owner, team.project_id, [PAGE_ID])
    pages = {PAGE_ID: notion_page(PAGE_ID, "Refund Policy", OLD_TIME)}
    blocks = _blocks("Refunds are accepted within 30 days of purchase.")
    install_notion(monkeypatch, pages, blocks)
    await trigger_sync(client, team.owner, integration["id"])

    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    pages[PAGE_ID] = notion_page(PAGE_ID, "Refund Policy", future)
    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    detail = await fetch_document(client, team.owner, documents[0]["id"])
    assert len(detail["versions"]) == 1

    events = await sync_events(client, team.owner, team.project_id, integration["id"])
    assert events[-1]["payload"]["unchanged"] == 1


async def test_an_edited_page_with_new_content_becomes_a_new_version(
    client: AsyncClient, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = await register_notion(client, team.owner, team.project_id, [PAGE_ID])
    pages = {PAGE_ID: notion_page(PAGE_ID, "Refund Policy", OLD_TIME)}
    blocks = _blocks("Refunds are accepted within 30 days of purchase.")
    install_notion(monkeypatch, pages, blocks)
    await trigger_sync(client, team.owner, integration["id"])

    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    pages[PAGE_ID] = notion_page(PAGE_ID, "Refund Policy", future)
    blocks[PAGE_ID] = _blocks("Refunds in Japan follow a 20-day window.")[PAGE_ID]
    await trigger_sync(client, team.owner, integration["id"])

    documents = await list_documents(client, team.owner, team.project_id)
    assert len(documents) == 1, "변경은 새 문서가 아니라 새 버전이어야 한다 (룰 5)"
    assert documents[0]["active_version"]["version_no"] == 2

    events = await sync_events(client, team.owner, team.project_id, integration["id"])
    assert events[-1]["payload"]["new_versions"] == 1
