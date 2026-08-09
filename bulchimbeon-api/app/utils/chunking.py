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

# `06 §1` ② — 800토큰 초과 시 분할, 오버랩 15%.
MAX_CHUNK_TOKENS = 800
OVERLAP_RATIO = 0.15

# 영문 경험칙. 실제보다 토큰을 적게 잡으면 상한을 넘기므로 나눗셈 쪽을 작게 잡는다.
CHARS_PER_TOKEN = 4

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class ChunkDraft:
    """DB 에 들어가기 전의 청크. `seq` 는 저장 직전에 부여한다 (`04 §2` chunks)."""

    content: str
    heading_path: list[str] = field(default_factory=list)
    page_no: int | None = None

    def to_meta(self) -> dict[str, object]:
        return {"heading_path": list(self.heading_path), "page_no": self.page_no}


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
            for body in _split_with_overlap(section_text):
                chunks.append(
                    ChunkDraft(content=body, heading_path=list(heading_path), page_no=page.page_no)
                )
    return chunks


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
