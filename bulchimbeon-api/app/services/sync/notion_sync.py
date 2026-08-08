"""Notion 동기화 (`08 §4`) — `page_ids` 의 블록 트리를 마크다운으로 옮긴다.

> ### 마크다운으로 옮기는 이유
> 인제스트(`06 §1` ②)는 **마크다운 헤딩 하나로만** 헤딩 경로를 인식한다. Notion 블록을
> 그대로 이어 붙이면 문서 구조가 사라져 청크의 `heading_path` 가 비고, 그러면 `05 §6`
> `citations[].heading_path` 도 함께 빈다 — 담당자가 근거 위치를 짚을 수 없게 된다.
> DOCX 파서가 헤딩 스타일을 `##` 로 바꿔 주는 것(`utils/parsing.py`)과 같은 이유이며,
> 지원 범위도 `08 §4` 가 정한 대로 **헤딩·리스트·코드·테이블 기본만**이다.

> ### 변경 감지는 `last_edited_time` (`08 §4`)
> 우리가 그 페이지를 마지막으로 받아 온 시각 = **최신 버전의 `created_at`** 이다. 그보다 뒤에
> 편집됐으면 바뀐 것으로 본다. 연동 행에 커서를 두지 않는 이유는 `base.py` 독스트링에 있다.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from notion_client import AsyncClient
from notion_client.errors import APIResponseError, HTTPResponseError, RequestTimeoutError

from app.services.document_service import safe_filename
from app.services.sync.base import PartialListing, RemoteRef, StoredVersion, SyncError
from app.utils.parsing import MIME_MARKDOWN

logger = logging.getLogger(__name__)

SOURCE_TYPE = "notion"

_PAGE_SIZE = 100
# 블록 트리는 깊이 제한 없이 중첩될 수 있다. 순환은 없지만 깊은 트리에서 재귀가 폭주하지 않게
# 상한을 둔다 — 넘는 깊이는 내용이 아니라 구조라서 잘려도 근거 검색에 영향이 거의 없다.
_MAX_DEPTH = 6
_INDENT = "  "

_HEADING_PREFIX = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}

# `last_edited_time` 비교의 안전 여유 — `is_unchanged` 독스트링이 근거다.
# 분 단위 절삭을 흡수할 만큼이면 되고, 넉넉히 잡아도 대가는 "가끔 한 번 더 받아 오는 것"뿐이다.
_EDIT_TIME_TOLERANCE = timedelta(minutes=1)


class NotionSync:
    """`08 §4` 의 provider. `page_ids` 하나가 문서 하나다."""

    source_type = SOURCE_TYPE

    def __init__(
        self, *, token: str, page_ids: list[str], client: AsyncClient | None = None
    ) -> None:
        self._page_ids = page_ids
        self._owns_client = client is None
        self._client = client or AsyncClient(auth=token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- 열거 ---------------------------------------------------------------------------
    async def list_documents(self) -> list[RemoteRef]:
        """페이지 메타(제목·`last_edited_time`)만 읽는다. 블록 트리는 바뀐 것만 받는다.

        ⚠️ **페이지 하나의 실패가 나머지를 막지 않는다** (`08 §6`). GitHub 은 트리 호출 한 번이라
        열거가 전부 아니면 전무지만, 여기는 `page_ids` 를 한 건씩 조회한다 — 페이지 하나를
        지우거나 공유를 풀면 그 한 건만 실패해야 한다. 통째로 올리면 담당자가 config 를 직접
        고치기 전까지 그 연동이 영구히 죽는다.
        """
        refs: list[RemoteRef] = []
        failures: list[tuple[str, str]] = []

        for page_id in self._page_ids:
            try:
                page = await self._call(
                    self._client.pages.retrieve(page_id=page_id), what=f"page {page_id}"
                )
            except SyncError as exc:
                logger.warning("notion 페이지를 열거하지 못했다: %s (%s)", page_id, exc)
                failures.append((page_id, str(exc)))
                continue

            title = page_title(page) or page_id
            refs.append(
                RemoteRef(
                    source_ref=page_id,
                    title=title,
                    filename=safe_filename(title or page_id, ".md"),
                    mime=MIME_MARKDOWN,
                    revision=str(page.get("last_edited_time", "")),
                )
            )

        if failures:
            raise PartialListing(refs, failures)
        return refs

    # --- 변경 판정 ----------------------------------------------------------------------
    async def is_unchanged(self, ref: RemoteRef, stored: StoredVersion) -> bool:
        """`last_edited_time` 이 우리가 받아 온 시각보다 뒤가 아니면 변경 없음 (`08 §4`).

        ⚠️ **여유를 빼고 비교한다** (`_EDIT_TIME_TOLERANCE`). Notion 의 `last_edited_time` 은
        분 단위로 절삭돼 오는 경우가 있어, 우리가 받아 온 직후(같은 분 안에) 편집이 일어나면
        보고된 시각이 우리 시각보다 **앞서** 찍힌다. 그대로 비교하면 그 편집은 영원히 "변경 없음"이
        되어 낡은 근거가 그대로 남는다. 판정을 변경 쪽으로 기울이면 최악이 "한 번 더 받아 오는
        것"인데 그건 `runner._same_content` 가 걸러 새 버전으로 이어지지 않는다.
        """
        edited_at = parse_notion_time(ref.revision)
        if edited_at is None:
            # 시각을 못 읽으면 "안 바뀌었다"로 판정하지 않는다 — 갱신을 영영 놓치는 쪽이
            # 같은 내용을 한 번 더 받는 쪽보다 나쁘다. 내용이 같으면 runner 가 걸러낸다.
            return False
        return edited_at <= stored.created_at - _EDIT_TIME_TOLERANCE

    # --- 본문 ---------------------------------------------------------------------------
    async def fetch(self, ref: RemoteRef) -> bytes:
        """블록 트리를 마크다운으로 옮겨 UTF-8 바이트로 돌려준다."""
        lines = [f"# {ref.title}", ""]
        lines.extend(await self._render_children(ref.source_ref, depth=0))
        markdown = "\n".join(lines).rstrip() + "\n"
        return markdown.encode("utf-8")

    async def _render_children(self, block_id: str, *, depth: int) -> list[str]:
        if depth >= _MAX_DEPTH:
            # 조용히 버리지 않는다 — 잘린 줄은 검색에 영영 들어가지 못한다 (`08 §6` 의 정신).
            logger.warning(
                "notion 블록 깊이 상한(%s)에 걸려 하위 내용을 옮기지 않았다: block=%s",
                _MAX_DEPTH,
                block_id,
            )
            return []

        lines: list[str] = []
        numbered = 0
        indent = _INDENT * depth
        for block in await self._list_children(block_id):
            # 표만 자식을 **먼저** 모아 통째로 넘긴다 — 행 순서와 구분선이 한 덩어리라
            # 일반 재귀로 쪼개면 마크다운 표가 깨진다 (`_render_table` 독스트링).
            if block.get("type") == "table" and block.get("has_children"):
                block = {**block, "_rows": await self._list_children(str(block["id"]))}

            rendered, numbered = render_block(block, numbered=numbered)
            lines.extend(f"{indent}{line}" if line else "" for line in rendered)

            if block.get("has_children") and block.get("type") != "table":
                lines.extend(await self._render_children(str(block["id"]), depth=depth + 1))
        return lines

    async def _list_children(self, block_id: str) -> list[dict[str, Any]]:
        """페이지네이션을 끝까지 따라간다 — `has_more` 를 무시하면 긴 문서의 뒷부분이 사라진다."""
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload = await self._call(
                self._client.blocks.children.list(
                    block_id=block_id, start_cursor=cursor, page_size=_PAGE_SIZE
                ),
                what=f"block {block_id}",
            )
            blocks.extend(payload.get("results", []))
            if not payload.get("has_more"):
                return blocks
            cursor = payload.get("next_cursor")
            if cursor is None:
                return blocks

    async def _call(self, awaitable: Any, *, what: str) -> dict[str, Any]:
        """notion-client 예외를 담당자에게 보여줄 수 있는 문장으로 바꾼다 (`base.SyncError`)."""
        try:
            return await awaitable
        except APIResponseError as exc:
            if exc.status in (401, 403):
                raise SyncError(
                    "Notion 접근이 거부됐습니다. 토큰과 페이지 공유 설정을 확인해 주세요."
                ) from exc
            if exc.status == 404:
                raise SyncError(f"Notion 에서 찾을 수 없습니다: {what}") from exc
            logger.warning("notion 응답 오류 %s (%s)", exc.status, what)
            raise SyncError(f"Notion 응답 오류({exc.status})로 동기화하지 못했습니다.") from exc
        except RequestTimeoutError as exc:
            raise SyncError("Notion 응답이 지연돼 동기화하지 못했습니다.") from exc
        except HTTPResponseError as exc:
            logger.warning("notion HTTP 오류 (%s)", what)
            raise SyncError("Notion 통신 오류로 동기화하지 못했습니다.") from exc


# --------------------------------------------------------------------------------------
# 블록 → 마크다운 (`08 §4` — 헤딩·리스트·코드·테이블 기본만)
#
# 모듈 함수로 두는 이유: 변환이 네트워크와 무관한 순수 함수라 목킹 없이 그대로 테스트된다.
# --------------------------------------------------------------------------------------
def page_title(page: dict[str, Any]) -> str:
    """페이지 제목. `type == 'title'` 인 속성 하나가 정본이다 (이름은 DB 마다 다르다)."""
    for value in (page.get("properties") or {}).values():
        if isinstance(value, dict) and value.get("type") == "title":
            return rich_text_to_markdown(value.get("title") or [])
    return ""


def parse_notion_time(value: str) -> datetime | None:
    """`2026-08-08T01:02:03.000Z` → aware datetime. 못 읽으면 `None`."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("notion 시각을 읽지 못했다: %r", value)
        return None


def rich_text_to_markdown(rich_text: list[dict[str, Any]]) -> str:
    """rich text 배열 → 마크다운 인라인. 굵게·기울임·취소선·인라인 코드·링크만 옮긴다."""
    parts: list[str] = []
    for span in rich_text:
        text = span.get("plain_text", "")
        if not text:
            continue
        annotations = span.get("annotations") or {}
        # 코드가 가장 안쪽이다 — `**`code`**` 순서라야 마크다운이 깨지지 않는다.
        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"
        href = span.get("href")
        if href:
            text = f"[{text}]({href})"
        parts.append(text)
    return "".join(parts)


def _text_of(block: dict[str, Any], block_type: str) -> str:
    body = block.get(block_type) or {}
    return rich_text_to_markdown(body.get("rich_text") or [])


def _render_table(block: dict[str, Any]) -> list[str]:
    """`table` 블록 — 자식 `table_row` 를 마크다운 표로 만든다.

    ⚠️ 행은 **이미 블록에 실려 있어야 한다.** `_render_children` 은 table 의 자식을 따로 훑지
    않고 이 함수에 맡긴다 — 표는 행 순서와 구분선이 한 덩어리라 재귀로 쪼개면 깨진다.
    """
    rows = block.get("_rows") or []
    if not rows:
        return []

    def cells(row: dict[str, Any]) -> list[str]:
        body = row.get("table_row") or {}
        return [rich_text_to_markdown(cell) for cell in body.get("cells") or []]

    header = cells(rows[0])
    if not header:
        return []

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows[1:]:
        values = cells(row)
        values += [""] * (len(header) - len(values))
        lines.append("| " + " | ".join(values[: len(header)]) + " |")
    lines.append("")
    return lines


def render_block(block: dict[str, Any], *, numbered: int = 0) -> tuple[list[str], int]:
    """블록 하나 → 마크다운 줄들. 두 번째 값은 다음 블록에 넘길 번호 매기기 카운터다.

    번호 목록은 연속된 `numbered_list_item` 안에서만 이어진다 — 사이에 다른 블록이 끼면
    Notion 에서도 목록이 끊기므로 1부터 다시 시작한다.
    """
    block_type = str(block.get("type", ""))

    if block_type in _HEADING_PREFIX:
        return [f"{_HEADING_PREFIX[block_type]} {_text_of(block, block_type)}", ""], 0
    if block_type == "paragraph":
        text = _text_of(block, block_type)
        return ([text, ""] if text else [""]), 0
    if block_type == "bulleted_list_item":
        return [f"- {_text_of(block, block_type)}"], 0
    if block_type == "numbered_list_item":
        numbered += 1
        return [f"{numbered}. {_text_of(block, block_type)}"], numbered
    if block_type == "to_do":
        done = (block.get("to_do") or {}).get("checked")
        return [f"- [{'x' if done else ' '}] {_text_of(block, block_type)}"], 0
    if block_type == "quote":
        return [f"> {_text_of(block, block_type)}", ""], 0
    if block_type == "callout":
        return [f"> {_text_of(block, block_type)}", ""], 0
    if block_type == "toggle":
        # 접힌 내용은 자식 블록으로 따라온다 — 제목만 문단으로 남긴다.
        return [_text_of(block, block_type), ""], 0
    if block_type == "code":
        body = block.get("code") or {}
        language = body.get("language") or ""
        # ⚠️ 코드 본문은 rich text 변환을 태우지 않는다. 코드 안의 `*` 를 강조로 바꾸면
        #    원문이 훼손되고, 그 상태로 청크가 되어 근거 인용이 원문과 달라진다.
        content = "".join(span.get("plain_text", "") for span in body.get("rich_text") or [])
        return [f"```{language}", *content.split("\n"), "```", ""], 0
    if block_type == "divider":
        return ["---", ""], 0
    if block_type == "table":
        return _render_table(block), 0
    if block_type == "table_row":
        # 표 바깥의 행은 있을 수 없다 (`table` 이 통째로 처리한다).
        return [], 0

    # 이미지·임베드·자식 페이지 등. `08 §4` 의 "기본만" 범위 밖이라 본문에 넣지 않는다 —
    # 근거 없는 문장을 만들지 않기 위해서다 (룰 6).
    logger.debug("변환하지 않는 notion 블록 타입: %s", block_type)
    return [], 0
