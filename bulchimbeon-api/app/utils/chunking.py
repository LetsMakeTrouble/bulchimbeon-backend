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


def _split_by_headings(text: str, heading_stack: list[str]) -> list[tuple[str, list[str]]]:
    """마크다운 헤딩으로 섹션을 나눈다. `heading_stack` 은 호출 간에 이어진다(제자리 갱신).

    헤딩 줄 자체도 섹션 본문 앞에 남긴다 — 청크만 읽는 LLM 이 "무엇에 대한 문단인지"를
    알아야 근거로 쓸 수 있기 때문이다 (`06 §2` ④ EVIDENCE 포맷).
    """
    sections: list[tuple[str, list[str]]] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
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
