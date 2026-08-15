"""청킹 (`06 §1` ②) — 헤딩 경로 우선 분할 → 800토큰 초과 시 15% 오버랩 고정 분할.

`meta` 에 `heading_path`(예: `["Refund Policy", "Japan"]`)·`page_no` 를 남긴다.
프론트는 이 값으로 근거 원문 화면의 스크롤 앵커와 빵부스러기를 만든다 (`05 §6` citations 표).

⚠️ **토큰 수는 추정값이다.** tiktoken 을 의존성에 넣지 않았으므로(`03 §1` 스택 표에 없다)
영문 기준 경험칙 `4자 ≈ 1토큰`을 쓴다. 청크 경계가 임베딩 품질에 주는 영향은 완만하고,
상한을 넘겨 임베딩 API 가 거절하는 쪽이 훨씬 위험하므로 **보수적으로 추정**한다.
"""

import re
from dataclasses import dataclass, field

from app.utils.parsing import ParsedPage

# `06 §1` ② — 800토큰 초과 시 분할, 오버랩 15%. **바깥 상한**이다.
MAX_CHUNK_TOKENS = 800
OVERLAP_RATIO = 0.15

# 2차 분할 (`_split_section`) — 한 청크가 담는 **문장(또는 목록 항목) 수** 상한.
#
# ⚠️ 토큰이 아니라 문장 수로 잡는다. 토큰 추정은 영문 경험칙(4자≈1토큰)이라 한국어를
#    크게 과소평가해서, 토큰 목표로 자르면 **영어 섹션만 쪼개지고 한국어는 통째로 남는다.**
#    희석을 만드는 것은 바이트가 아니라 "한 청크에 든 사실의 개수"이므로 문장 수가 맞다.
#
# ⚠️ 키우면 사실 여럿이 한 청크로 뭉쳐 단일 항목 질문이 희석되고, 줄이면 문장이 잘게
#    흩어져 ④ 가 근거를 이어 붙이지 못한다. 바꿨으면 `probe_seed_docs.py --history` 로
#    차단선 미달 건수를 다시 재라.
MAX_UNITS_PER_CHUNK = 3

# 영문 경험칙. 실제보다 토큰을 적게 잡으면 상한을 넘기므로 나눗셈 쪽을 작게 잡는다.
CHARS_PER_TOKEN = 4

# 목록 항목(`- `, `* `, `1. `)은 한 줄이 한 단위다.
_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# 문장 경계 — 마침표·물음표·느낌표 뒤의 공백. 한국어 문장도 마침표로 끝난다.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class ChunkDraft:
    """DB 에 들어가기 전의 청크. `seq` 는 저장 직전에 부여한다 (`04 §2` chunks)."""

    content: str
    heading_path: list[str] = field(default_factory=list)
    page_no: int | None = None
    # ⚠️ **임베딩 대상은 `content` 가 아니라 이 값이다** — 헤딩 줄을 뺀 본문.
    # 이유는 `embedding_content` 아래 주석과 `docs/09-deploy-notes.md` 참조.
    # 기본값은 `content` 에서 헤딩 줄을 걷어낸 것이다(직접 만든 드래프트도 안전하게 동작).
    embedding_content: str | None = None

    def __post_init__(self) -> None:
        if self.embedding_content is None:
            self.embedding_content = strip_heading_lines(self.content) or self.content

    def to_meta(self) -> dict[str, object]:
        return {"heading_path": list(self.heading_path), "page_no": self.page_no}


def strip_heading_lines(text: str) -> str:
    """헤딩 줄을 걷어낸 본문. 임베딩 입력에만 쓴다.

    ⚠️ **`content` 에서는 절대 빼지 않는다** (`06 §2` ④ EVIDENCE 포맷이 요구한다).
    빼는 곳은 임베딩 입력 하나뿐이다.
    """
    lines = [line for line in text.splitlines() if not _HEADING_PATTERN.match(line)]
    return "\n".join(lines).strip()


def estimate_tokens(text: str) -> int:
    """보수적 토큰 추정 — 문자 수 기준과 공백 분리 단어 수 중 큰 값."""
    return max((len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN, len(text.split()))


def chunk_pages(pages: list[ParsedPage]) -> list[ChunkDraft]:
    """파싱 산출물을 청크 목록으로 만든다.

    헤딩 경로는 **페이지를 넘어 이어진다** — PDF 는 페이지 단위로 파싱되지만 섹션은
    페이지 경계에서 끊기지 않기 때문이다.
    """
    chunks: list[ChunkDraft] = []
    heading_stack: list[str] = []

    for page in pages:
        for section_text, heading_path in _split_by_headings(page.text, heading_stack):
            # ⚠️ `content` 는 **종전 그대로** 둔다 (헤딩 줄 포함, 오버랩 경계도 그대로).
            #    달라지는 것은 `embedding_content` 하나뿐이다.
            heading_line = _leading_heading(section_text)
            for body in _split_section(section_text, heading_line):
                chunks.append(
                    ChunkDraft(
                        content=body,
                        heading_path=list(heading_path),
                        page_no=page.page_no,
                        embedding_content=_embedding_body(body, heading_line),
                    )
                )
    return chunks


def _leading_heading(section_text: str) -> str:
    """섹션 첫 줄이 헤딩이면 그 줄, 아니면 빈 문자열."""
    lines = section_text.splitlines()
    if lines and _HEADING_PATTERN.match(lines[0]):
        return lines[0]
    return ""


def _embedding_body(body: str, heading_line: str) -> str:
    """임베딩 입력 — 헤딩 줄을 걷어낸 본문.

    보통은 줄 단위로 걷어내면 끝이다. 다만 `_split_with_overlap` 은 800토큰 초과 섹션을
    **공백으로 이어 붙여** 나누므로 줄바꿈이 사라진다. 그 경우 창 전체가 헤딩 한 줄처럼
    보여 줄 단위 제거가 본문을 통째로 지워 버리므로, 알려진 헤딩 문자열을 접두사로만 뗀다.
    """
    stripped = strip_heading_lines(body)
    if stripped:
        return stripped

    if heading_line:
        normalized = " ".join(heading_line.split())
        if body.startswith(normalized):
            remainder = body[len(normalized) :].strip()
            if remainder:
                return remainder

    # 걷어낼 게 없거나 걷어내면 빈 문자열이 되는 경우 — 원문을 그대로 임베딩한다.
    return body


def _has_prose(body: str) -> bool:
    """헤딩 줄 말고 실제 문장이 한 줄이라도 있는가.

    ⚠️ **제목 줄만 남은 섹션은 청크가 아니다** (사용자 결정 2026-08-08, M9 실측).
    문서 맨 위의 `# Refund Policy v1` 과 첫 `##` 사이에는 본문이 없는데, 그대로 두면
    18자짜리 "제목 청크"가 하나 생긴다. 이 청크는 어떤 사실도 진술하지 않으므로
    ⑤ 근거 검증에서 문장을 뒷받침할 수 없고, 그러면서 `retrieval_top_k`(6) 자리 하나를
    차지한다. 시드 4개 문서 기준으로 문서(`08 §2`)가 말하는 22청크 대신 26청크가 나오던
    원인이 이것이다.

    ⚠️ **버려진 제목이 `heading_path` 에 남는 것은 그것이 조상일 때뿐이다.**
    `_split_by_headings` 는 `del heading_stack[level - 1:]` 로 같은 레벨 이하를 걷어내므로,
    본문 없는 `## Japan` 다음에 형제인 `## Korea` 가 오면 **`Japan` 은 content 에서도
    heading_path 에서도 완전히 사라진다.** 시드 4개 문서에서는 본문 없는 섹션이 H1 넷뿐이고
    전부 조상이라 해당 사항이 없다.
    """
    return any(line.strip() and not _HEADING_PATTERN.match(line) for line in body.splitlines())


def _split_by_headings(text: str, heading_stack: list[str]) -> list[tuple[str, list[str]]]:
    """마크다운 헤딩으로 섹션을 나눈다. `heading_stack` 은 호출 간에 이어진다(제자리 갱신).

    헤딩 줄 자체도 섹션 본문 앞에 남긴다 — 청크만 읽는 LLM 이 "무엇에 대한 문단인지"를
    알아야 근거로 쓸 수 있기 때문이다 (`06 §2` ④ EVIDENCE 포맷).
    다만 헤딩 **밖에** 아무것도 없는 섹션은 버린다 (`_has_prose`). 버려도 그 제목이
    **하위 섹션의 조상이라면** `heading_path` 에 그대로 남는다 — 하위 섹션이 없는 형제
    헤딩은 사라진다 (`_has_prose` 독스트링).
    """
    sections: list[tuple[str, list[str]]] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body and _has_prose(body):
            sections.append((body, list(heading_stack)))

    for line in text.splitlines():
        match = _HEADING_PATTERN.match(line)
        if match is None:
            buffer.append(line)
            continue

        flush()
        level = len(match.group(1))
        title = match.group(2).strip()
        # 같은 레벨 이하를 걷어내고 현재 헤딩을 올린다.
        # H1 > H3 처럼 레벨을 건너뛰어도 빈 칸을 채우지 않는다 — heading_path 는 화면에 그대로
        # 찍히는 빵부스러기라서(`05 §6`) 빈 문자열이 섞이면 그게 곧 UI 버그다.
        del heading_stack[level - 1 :]
        heading_stack.append(title)
        buffer.append(line)

    flush()
    return sections


def _split_section(section_text: str, heading_line: str) -> list[str]:
    """섹션을 청크 본문들로 나눈다 — **바깥 상한 → 안쪽 목표** 두 겹이다.

    바깥은 종전 그대로 `MAX_CHUNK_TOKENS`(800) 하드 상한이고, 안쪽이 이번에 더한
    `TARGET_CHUNK_TOKENS` 목표 분할이다.

    ### 왜 한 겹 더 나누나 (실측 2026-08-16)
    섹션 하나가 사실을 여럿 담고 있으면, 그중 **하나만 묻는 질문이 나머지에 희석된다.**
    화면 경로 12개가 한 문단에 있는 코퍼스에서 "설정 화면 경로"를 물었을 때 0.336 이
    나왔다 — 답이 그 문단 안에 있는데도 `similarity_floor`(0.444)에 걸렸다.
    같은 코퍼스에서 토큰 수명 한 문장을 **독립 섹션으로 빼자 0.42 → 0.77** 이 됐다.
    그때는 사람이 문서를 고쳐서 얻은 효과였고, 이 함수는 그것을 자동으로 한다.

    ⛔ **문서를 고치라고 요구하지 않는다.** "검색이 잘 되게 문서를 쪼개 두세요"는 이
    제품이 없애려는 종류의 숙제다. 쪼개는 일은 우리가 한다.
    """
    pieces: list[str] = []
    for piece in _split_with_overlap(section_text):
        pieces.extend(_pack_units(piece, heading_line))
    return pieces


def _units(body: str) -> list[str]:
    """분할 단위 — 목록 항목은 한 줄이 하나, 산문은 문장 하나.

    ⚠️ 문장 경계는 마침표·물음표·느낌표 뒤의 공백이다. 한국어 문장도 마침표로 끝나므로
    같은 규칙으로 잘린다. 헤딩 줄은 단위에서 뺀다 — 창마다 다시 붙일 것이기 때문이다.
    """
    units: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or _HEADING_PATTERN.match(line):
            continue
        if _LIST_ITEM_PATTERN.match(stripped):
            units.append(stripped)
            continue
        units.extend(part.strip() for part in _SENTENCE_END.split(stripped) if part.strip())
    return units


def _pack_units(piece: str, heading_line: str) -> list[str]:
    """단위를 `TARGET_CHUNK_TOKENS` 안쪽 창으로 묶는다. 창 사이는 한 단위 겹친다.

    ⚠️ **헤딩 줄을 창마다 앞에 붙인다.** 청크만 읽는 LLM 이 "무엇에 대한 문단인지" 알아야
    근거로 쓸 수 있고(`06 §2` ④), 화면 빵부스러기도 여기서 나온다. 임베딩 입력에서는
    그 줄이 다시 걷힌다 (`_embedding_body`) — 종전 규칙 그대로다.
    """
    units = _units(piece)
    if not units:
        return [piece]

    windows: list[list[str]] = []
    current: list[str] = []
    for unit in units:
        if len(current) >= MAX_UNITS_PER_CHUNK:
            windows.append(current)
            # 경계에 걸친 문장이 어느 창에서도 온전히 검색되지 않는 것을 막는다.
            current = [current[-1]]
        current.append(unit)
    if current:
        windows.append(current)

    if len(windows) <= 1:
        return [piece]  # 목표 안쪽이면 종전 그대로 둔다 (불필요한 변형 금지).

    prefix = f"{heading_line}\n" if heading_line else ""
    return [prefix + "\n".join(window) for window in windows]


def _split_with_overlap(text: str) -> list[str]:
    """`MAX_CHUNK_TOKENS` 를 넘으면 15% 오버랩으로 나눈다.

    오버랩이 없으면 경계에 걸친 문장이 어느 청크에서도 온전히 검색되지 않는다.
    """
    if estimate_tokens(text) <= MAX_CHUNK_TOKENS:
        return [text]

    words = text.split()
    if not words:
        return []

    windows: list[str] = []
    start = 0
    while start < len(words):
        end = start
        current = 0
        while end < len(words):
            # 단어 사이 공백 1자를 포함해 누적한다.
            addition = estimate_tokens(words[end]) + (1 if end > start else 0)
            if current + addition > MAX_CHUNK_TOKENS and end > start:
                break
            current += addition
            end += 1

        windows.append(" ".join(words[start:end]))
        if end >= len(words):
            break

        overlap = max(1, int((end - start) * OVERLAP_RATIO))
        # 오버랩이 창 전체를 덮으면 진행하지 못한다 — 최소 한 단어는 전진시킨다.
        start = max(start + 1, end - overlap)

    return windows
