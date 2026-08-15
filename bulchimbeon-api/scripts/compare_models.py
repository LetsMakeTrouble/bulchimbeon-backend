"""남은 네 모델 자리를 sol·terra·luna 로 비교한다 (`03 §4.1.4`, 스펙 AC-6·7).

`03 §4.1.1~4.1.3` 은 ⑤ 근거 검증 · 재사용 판정 · ① 질문 번역 세 자리를 이미 실측했다.
여기서 재는 것은 나머지 넷이다.

- `translate` — **담당자 확정문 en→ko**. 정답이 있다: 원문의 숫자·코드·고유명사가
  번역문에 살아남아야 하고 부정문이 긍정으로 뒤집히면 안 된다. 그래서 **숫자로 잰다**.
  이 자리는 오역을 잡아 줄 사람이 경로에 없다 (`03 §4.1` ⛔ 항목).
- `answer` — **④ 답변 생성**. 같은 근거·같은 질문을 주고 ⑤(sol 고정)로 채점한다.
  근거 밖 내용을 넣으면 G 가 떨어지고, 없는 청크 id 를 지어내면 그대로 잡힌다.
- `struct` / `lesson` — 정답이 없다. **숫자를 지어내지 않고** 세 모델 산출물을 나란히
  출력해 사람이 판단한다 (AC-7).

    uv run python scripts/compare_models.py translate
    uv run python scripts/compare_models.py answer --project <UUID>
    uv run python scripts/compare_models.py struct
    uv run python scripts/compare_models.py lesson

⚠️ 실 API 를 호출한다. `answer` 모드만 DB(근거 청크)가 필요하고 나머지는 필요 없다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_SETTINGS  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.services.llm import get_provider  # noqa: E402
from app.services.pipeline import prompts, retrieval  # noqa: E402
from app.services.pipeline.llm_schemas import (  # noqa: E402
    LessonOut,
    QuestionStructOut,
    SentencesOut,
    TextOut,
    VerdictsOut,
)
from app.utils.language import DEFAULT_LANGUAGE  # noqa: E402

MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

# ⑤ 채점자는 한 모델로 고정한다 — 채점자가 흔들리면 피채점자의 차이를 읽을 수 없다.
JUDGE_MODEL = "gpt-5.6-sol"

# `openai_provider.translate` 가 쓰는 것과 같은 문장이어야 측정이 의미를 갖는다.
ANSWER_TRANSLATE_SYSTEM = (
    "Translate the user's text from en to ko. "
    "Return only the translation, preserving meaning and tone. Do not add commentary."
)


class TranslateCase:
    """`must_keep` 는 번역문에 **그대로** 남아야 하는 토큰이다.

    숫자·통화코드·HTTP 상태·헤더명처럼 번역하면 안 되는 것들만 넣는다. `negated` 는
    원문이 부정문이라는 표시이고, 한국어 부정 표지가 하나도 없으면 뒤집힌 것으로 본다.
    """

    def __init__(self, text_en: str, must_keep: list[str], *, negated: bool = False) -> None:
        self.text_en = text_en
        self.must_keep = must_keep
        self.negated = negated


# 확정문에 실제로 나오는 형태를 골랐다 — 담당자가 영어로 써서 질문자가 한국어로 읽는 문장.
TRANSLATE_CASES = [
    TranslateCase(
        "Refunds are accepted within 30 days of purchase, and the shipping fee is refunded "
        "only when the item was defective.",
        ["30"],
    ),
    TranslateCase(
        "The platform does not perform currency conversion; the buyer is charged in the "
        "currency recorded on the order.",
        [],
        negated=True,
    ),
    TranslateCase(
        "Supported settlement currencies are KRW, USD, and JPY.",
        ["KRW", "USD", "JPY"],
    ),
    TranslateCase(
        "An `Idempotency-Key` reused with a different request body is rejected with `409`.",
        ["Idempotency-Key", "409"],
    ),
    TranslateCase(
        "Idempotency keys are ignored on `GET` and `DELETE` requests.",
        ["GET", "DELETE"],
        negated=True,
    ),
    TranslateCase(
        "Webhook signatures use HMAC-SHA256 and the timestamp must be within a 5-minute window.",
        ["HMAC-SHA256", "5"],
    ),
    TranslateCase(
        "Sandbox data is reset every Sunday at 00:00 UTC and is never migrated to production.",
        ["00:00", "UTC"],
        negated=True,
    ),
    TranslateCase(
        "Access tokens expire after 3600 seconds; refresh tokens are not issued.",
        ["3600"],
        negated=True,
    ),
]

# 한국어 부정 표지. 하나라도 있으면 부정이 살아 있다고 본다 (형태소 분석까지 가지 않는다).
NEGATION_MARKERS = ["않", "안 ", "없", "못", "무시", "아닙", "아니", "금지", "제외", "불가"]

STRUCT_CASE = {
    "question_en": "Is there a separate refund policy for the Japan region?",
    "held_reason": "no_evidence",
    "evidence": [
        "Refund Policy v1 > Standard Refunds: Customers may request a refund within 30 days "
        "of delivery. Refunds are issued to the original payment method within 5 business days.",
        "Refund Policy v1 > Shipping Fees: The shipping fee is refunded only when the item "
        "arrived damaged or the wrong item was shipped.",
    ],
}

LESSON_CASE = {
    "question_en": "Can a customer get a refund after 30 days if the item was defective?",
    "original_en": "No. Refunds are accepted only within 30 days of purchase.",
    "corrected_en": (
        "Yes. The 30-day window applies to change-of-mind returns. A defective item is "
        "handled as a warranty claim and has no 30-day limit, so route it to the support team."
    ),
}


async def _call(system: str, user: str, schema: type, model: str) -> dict:
    return await get_provider().complete_json(system, user, schema, model=model)


async def mode_translate() -> None:
    print("확정문 en→ko — 사실 보존률 (AC-6)\n")
    print("정답이 있는 항목만 센다: 번역하면 안 되는 토큰의 잔존 + 부정문의 부정 유지.\n")

    outputs: dict[str, list[str]] = {}
    scores: dict[str, tuple[int, int]] = {}

    for model in MODELS:
        kept = 0
        total = 0
        texts = []
        for case in TRANSLATE_CASES:
            result = await _call(ANSWER_TRANSLATE_SYSTEM, case.text_en, TextOut, model)
            text_ko = str(result["text"])
            texts.append(text_ko)
            for token in case.must_keep:
                total += 1
                kept += 1 if token in text_ko else 0
            if case.negated:
                total += 1
                kept += 1 if any(marker in text_ko for marker in NEGATION_MARKERS) else 0
        outputs[model] = texts
        scores[model] = (kept, total)

    print(f"{'모델':<16}{'보존':>8}{'전체':>8}{'보존률':>10}")
    for model in MODELS:
        kept, total = scores[model]
        print(f"{model:<16}{kept:>8}{total:>8}{kept / total * 100:>9.1f}%")

    print("\n놓친 항목:")
    any_miss = False
    for model in MODELS:
        for case, text_ko in zip(TRANSLATE_CASES, outputs[model], strict=True):
            missing = [token for token in case.must_keep if token not in text_ko]
            flipped = case.negated and not any(m in text_ko for m in NEGATION_MARKERS)
            if missing or flipped:
                any_miss = True
                label = "부정 뒤집힘" if flipped else f"누락 {missing}"
                print(f"  [{model}] {label}")
                print(f"      원문: {case.text_en[:70]}")
                print(f"      번역: {text_ko}")
    if not any_miss:
        print("  없음 — 세 모델 모두 만점이다.")


async def mode_answer(project_id: UUID) -> None:
    print("④ 답변 생성 — 같은 근거·같은 질문, ⑤(sol 고정)로 채점 (AC-6)\n")

    # 앞 3건은 근거가 명확한 정상 질문이고, 뒤 3건이 **함정**이다.
    # 첫 함정은 주제 자체가 문서 밖이라 쉽다. 나머지 둘이 판별력을 만든다 —
    # 주제는 문서에 있는데 **물어본 사실만 없다**. ④ 시스템 프롬프트의
    # "topic 만 언급한 청크는 근거가 아니다" 규칙을 실제로 지키는지 보는 자리다.
    questions = [
        "How long do I have to request a refund?",
        "Are idempotency keys applied to GET requests?",
        "How is a webhook signature verified?",
        "How many paid vacation days do employees get?",
        # 레이트 리밋은 분당 60회로 적혀 있지만 **초당** 한도는 어디에도 없다.
        "What is the per-second rate limit for the orders endpoint?",
        # 토큰 만료(24시간)는 있지만 **리프레시 토큰의 수명**은 없다(발급 자체를 안 한다).
        "How long is a refresh token valid for?",
    ]
    trap_from = 3

    async with AsyncSessionLocal() as db:
        evidence_sets = []
        for question_en in questions:
            vector = (await get_provider().embed([question_en]))[0]
            chunks = await retrieval.search_evidence(
                db,
                project_id=project_id,
                query_embeddings={DEFAULT_LANGUAGE: vector},
                top_k=DEFAULT_SETTINGS["retrieval_top_k"],
            )
            evidence_sets.append(chunks)

    print(f"{'모델':<16}{'평균 G':>9}{'환각 인용':>11}{'함정 회피':>11}")
    details: dict[str, list[str]] = {}

    for model in MODELS:
        g_values: list[int] = []
        invented = 0
        traps_avoided = 0
        notes: list[str] = []

        for index, (question_en, chunks) in enumerate(zip(questions, evidence_sets, strict=True)):
            aliases = {f"ch-{i + 1}": chunk for i, chunk in enumerate(chunks)}
            generated = await _call(
                prompts.ANSWER_SYSTEM_TEMPLATE.format(guidelines="(none)", lessons="(none)"),
                prompts.answer_user_prompt(
                    evidence=chunks, aliases=aliases, question_en=question_en
                ),
                SentencesOut,
                model,
            )
            if index >= trap_from:
                avoided = bool(generated["not_answerable"]) or not generated["sentences"]
                traps_avoided += 1 if avoided else 0
                mark = "회피" if avoided else "⚠️ 지어냄"
                notes.append(f"{mark} — {question_en}")
                if not avoided:
                    for sentence in generated["sentences"]:
                        notes.append(f"        → {sentence['text_en']}")
                continue

            sentences = generated["sentences"]
            if not sentences:
                g_values.append(0)
                continue

            for sentence in sentences:
                invented += sum(1 for alias in sentence["chunk_ids"] if alias not in aliases)

            # ⑤ 는 인용이 살아 있는 문장만 본다. 분모는 **생성 시점 원본 문장 수**로 고정한다.
            verifiable = [s for s in sentences if any(a in aliases for a in s["chunk_ids"])]
            if not verifiable:
                g_values.append(0)
                continue
            blocks = [
                (
                    position,
                    sentence["text_en"],
                    [aliases[a].content for a in sentence["chunk_ids"] if a in aliases],
                )
                for position, sentence in enumerate(verifiable, start=1)
            ]
            verdicts = await _call(
                prompts.VERIFY_SYSTEM,
                prompts.verify_user_prompt(blocks),
                VerdictsOut,
                JUDGE_MODEL,
            )
            supported = sum(1 for item in verdicts["verdicts"] if item["supported"])
            g_values.append(round(supported / len(sentences) * 100))

        average = sum(g_values) / len(g_values) if g_values else 0.0
        details[model] = notes
        traps = len(questions) - trap_from
        print(f"{model:<16}{average:>9.1f}{invented:>11}{f'{traps_avoided}/{traps}':>11}")

    print()
    for model in MODELS:
        for note in details[model]:
            print(f"  [{model}] {note}")


async def mode_struct() -> None:
    print("⑦ 카드 구조화 — 같은 입력, 세 모델 산출물 (AC-7: 사람이 판단한다)\n")
    print(f"질문: {STRUCT_CASE['question_en']}")
    print(f"보류 사유: {STRUCT_CASE['held_reason']}\n")

    user = (
        f"[QUESTION] {STRUCT_CASE['question_en']}\n"
        f"[WHY IT WAS HELD] {STRUCT_CASE['held_reason']}\n"
        "[EVIDENCE FOUND]\n" + "\n".join(f"- {item}" for item in STRUCT_CASE["evidence"])
    )

    for model in MODELS:
        result = await _call(prompts.STRUCT_SYSTEM, user, QuestionStructOut, model)
        print(f"── {model}")
        print(f"   background: {result['background']}")
        print(f"   question:   {result['question']}")
        for index, option in enumerate(result["options"], start=1):
            print(f"   option {index}:   {option}")
        print()


async def mode_lesson() -> None:
    print("교훈 추출 — 같은 입력, 세 모델 산출물 (AC-7: 사람이 판단한다)\n")
    print(f"질문:   {LESSON_CASE['question_en']}")
    print(f"원답:   {LESSON_CASE['original_en']}")
    print(f"수정답: {LESSON_CASE['corrected_en']}\n")

    user = prompts.lesson_user_prompt(
        question_en=LESSON_CASE["question_en"],
        original_en=LESSON_CASE["original_en"],
        corrected_en=LESSON_CASE["corrected_en"],
    )
    for model in MODELS:
        result = await _call(prompts.LESSON_SYSTEM, user, LessonOut, model)
        print(f"── {model}\n   {result['lesson']}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="모델 자리 비교 (sol·terra·luna)")
    parser.add_argument("mode", choices=["translate", "answer", "struct", "lesson"])
    parser.add_argument("--project", help="answer 모드에서 근거 청크를 가져올 프로젝트 UUID")
    args = parser.parse_args()

    if args.mode == "translate":
        await mode_translate()
    elif args.mode == "answer":
        if not args.project:
            raise SystemExit("answer 모드는 --project 가 필요하다 (데모 금지, 측정용 프로젝트)")
        await mode_answer(UUID(args.project))
    elif args.mode == "struct":
        await mode_struct()
    else:
        await mode_lesson()


if __name__ == "__main__":
    asyncio.run(main())
