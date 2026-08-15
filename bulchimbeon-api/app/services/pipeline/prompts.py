"""파이프라인 프롬프트 (`06 §2`).

프롬프트를 서비스 로직에서 떼어 둔 이유는, ④ 의 시스템 프롬프트가 **환각 방어 1겹의 사양**
이기 때문이다 (`06 §7`). 문장 하나만 빠져도 방어가 조용히 약해지므로 한곳에서 관리한다.

⚠️ ① 의 user 프롬프트는 **한국어 원문 그대로**다. 장식을 붙이지 않는다 —
`FakeLLMProvider` 가 이 규약에 기대어 `content_en = "[en] {원문}"` 을 만들고,
테스트는 그 결과로 임베딩을 재현한다 (`fake_provider` 독스트링).
"""

from app.services.llm.fake_provider import LESSON_CORRECTED_PREFIX, SENTENCE_BLOCK_PREFIX
from app.services.pipeline.retrieval import EvidenceChunk

# `05 §6` `citations[].quote` — 원문 화면에서 하이라이트할 스니펫. 청크 전체를 싣지 않는다.
QUOTE_MAX_LENGTH = 500

TRANSLATE_SYSTEM = (
    "You translate a Korean question into English for an internal knowledge search, "
    "and flag whether it reads as urgent.\n"
    "- content_en: a faithful English translation. Keep proper nouns and any bracketed "
    "tokens exactly as they appear.\n"
    "- suggest_urgent: true only when the text mentions a deadline, an outage, or work "
    "that is blocked. This is a suggestion only — the asker decides."
)

# 프로젝트에 영어·질문자 언어 **말고 다른 언어**의 문서가 있을 때 쓴다 (`06 §2` ③).
# 한국어 질문 → 그 언어로 옮겨 같은 언어끼리 검색하기 위한 것이며, 화면에 보이지 않는다.
#
# ⚠️ 스키마는 `TranslationOut` 을 그대로 쓴다(`content_en` 필드에 해당 언어 문장이 담긴다).
#    필드명이 어긋나 보이지만 새 스키마를 만들면 ① 과 ③ 의 출력이 갈라지고, 이 문장은
#    저장되지 않고 임베딩 입력으로만 쓰이므로 이름값이 밖으로 새지 않는다.
SEARCH_TRANSLATE_SYSTEM = (
    "You translate a question into {language} so it can be matched against documents "
    "written in {language}.\n"
    "- content_en: a faithful translation into {language}. Keep identifiers, code tokens, "
    "and proper nouns exactly as they appear.\n"
    "- suggest_urgent: always false. This translation is for search only."
)

SAME_QUESTION_SYSTEM = (
    "You decide whether two questions ask for the same fact.\n"
    "Answer same_question=true only when a single answer would fully satisfy both. "
    "Differences in scope, region, product tier, or time window mean false."
)

# `06 §2` ④ 의 시스템 프롬프트 골격. **아래 규칙 문장은 사양이다 — 임의로 줄이지 않는다.**
ANSWER_SYSTEM_TEMPLATE = """You answer questions strictly from the provided evidence chunks.
Rules:
- Every sentence MUST cite at least one chunk id that supports it.
- If the evidence does not contain the answer, set not_answerable=true. Never use general knowledge.
- A chunk that merely mentions the topic without stating the requested fact does NOT count as \
evidence. In that case set not_answerable=true.
- If two chunks contradict each other on the asked point, set conflict=true and list both chunk ids.
- For each sentence, provide both text_en and its Korean translation text_ko.
- Use only the chunk ids given in [EVIDENCE]. Never invent an id.
- Put citations ONLY in chunk_ids. Never write a chunk id inside text_en or text_ko \
(no "(ch-1)", no footnote markers) — the reader sees sources as a separate list.
- Follow the project guidelines below. Apply the approved lessons below.
[GUIDELINES] {guidelines}
[APPROVED LESSONS] {lessons}"""

VERIFY_SYSTEM = (
    "You check whether each sentence is actually supported by the evidence cited for it.\n"
    "- supported=true only when the cited chunks state the claim. Paraphrase is fine; "
    "an unrelated or merely topical chunk is not.\n"
    "- Return exactly one verdict per sentence, using the sentence's index."
)

LESSON_SYSTEM = (
    "You extract one reusable lesson from an answerer's correction.\n"
    "You are given a question, the answer the system originally produced, and the answer the "
    "human expert confirmed instead.\n"
    "- lesson: ONE English sentence stating the general principle that would have produced "
    "the corrected answer.\n"
    "- Write a rule that also applies to other questions, not a summary of this one case.\n"
    "- No bullet, no quotes, no explanation — the sentence alone."
)

STRUCT_SYSTEM = (
    "You restructure a question for a busy human expert who will answer it in one pass.\n"
    "Write in English, in this order: background, then the question, then options.\n"
    "- background: what was searched, what the evidence said, and where it fell short "
    "(including any contradiction found).\n"
    "- question: one sentence the expert can answer directly.\n"
    "- options: 2-4 concrete answers the expert can pick from."
)


def guidelines_block(content: str | None) -> str:
    return content.strip() if content and content.strip() else "(none)"


def lessons_block(lessons: list[str]) -> str:
    """`[APPROVED LESSONS]` (`06 §3`).

    ⚠️ 들어오는 것은 **`approved` 교훈뿐**이다 (룰 7 — 승인 전 교훈은 어디에도 쓰지 않는다).
    후보를 여기 섞으면 담당자가 승인한 적 없는 원칙이 답변을 바꾸게 된다. 그 필터는
    `lesson_service.load_for_prompt` 가 쥐고 있고 이 함수는 렌더링만 한다.
    """
    lines = [f"- {lesson.strip()}" for lesson in lessons if lesson.strip()]
    return "\n".join(lines) if lines else "(none)"


def lesson_user_prompt(*, question_en: str, original_en: str, corrected_en: str) -> str:
    """교훈 추출 입력 — 원답 vs 수정답 + 질문 (`06 §3`).

    ⚠️ 수정답을 **마지막**에 둔다. `FakeLLMProvider` 가 `LESSON_CORRECTED_PREFIX` 뒤를
    잘라 결정적 교훈을 만들기 때문이다 (⑤ 의 `SENTENCE_BLOCK_PREFIX` 와 같은 공유 계약).
    강제 🔴 의 초안은 빈 문자열이라 `original_en` 이 `""` 일 수 있다 — 그것도 "원답이
    없었다"는 유효한 차이다.
    """
    return (
        f"[QUESTION] {question_en}\n"
        f"[ORIGINAL ANSWER] {original_en or '(none — the system held the question)'}\n"
        f"{LESSON_CORRECTED_PREFIX}{corrected_en}"
    )


def evidence_block(evidence: list[EvidenceChunk], aliases: dict[str, EvidenceChunk]) -> str:
    """`[EVIDENCE]` 블록.

    ⚠️ **UUID 를 모델에 노출하지 않는다** (`06 §2` ④). 지역 별칭(`ch-1`)만 보여 주고
    별칭↔UUID 매핑은 서버가 들고 있다가 `answer_citations` 저장 시 복원한다.
    모델이 UUID 를 지어낼 여지를 없애고 토큰도 아낀다.
    """
    lines: list[str] = []
    for alias, chunk in aliases.items():
        heading = " > ".join(chunk.heading_path) or "(no heading)"
        lines.append(
            f"[{alias}] doc={chunk.doc_title} v{chunk.version_no} | {heading}\n{chunk.content}"
        )
    return "\n\n".join(lines)


def answer_user_prompt(
    *, evidence: list[EvidenceChunk], aliases: dict[str, EvidenceChunk], question_en: str
) -> str:
    return f"[EVIDENCE]\n{evidence_block(evidence, aliases)}\n\n[QUESTION] {question_en}"


def verify_user_prompt(blocks: list[tuple[int, str, list[str]]]) -> str:
    """⑤ 입력 — 생성된 문장 + **각 문장이 인용한 청크 원문** (`06 §2` ⑤).

    `blocks` 는 `(index, text_en, [인용 청크 원문])` 이다. `index` 는 ④ 결과의 1-based
    순번이 아니라 **이 프롬프트 안에서의 순번**이며, 호출자가 원본 순번으로 되돌린다 —
    인용이 비어 ④ 에서 이미 자동 false 가 된 문장은 여기 실리지 않기 때문이다.
    """
    parts: list[str] = []
    for index, text_en, quotes in blocks:
        evidence = "\n".join(f"- {quote}" for quote in quotes) or "- (none)"
        parts.append(f"{SENTENCE_BLOCK_PREFIX}{index}]\ntext_en: {text_en}\nevidence:\n{evidence}")
    return "\n\n".join(parts)


def struct_user_prompt(*, question_en: str, held_reason: str, evidence: list[EvidenceChunk]) -> str:
    summary = "\n".join(
        f"- {chunk.doc_title} v{chunk.version_no}: {chunk.content[:QUOTE_MAX_LENGTH]}"
        for chunk in evidence
    )
    return (
        f"[QUESTION] {question_en}\n"
        f"[WHY IT WAS HELD] {held_reason}\n"
        f"[EVIDENCE FOUND]\n{summary or '- (none)'}"
    )
