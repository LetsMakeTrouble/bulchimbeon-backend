"""질문 1건당 단계별 토큰·단가 실측 (`03 §4.2`, 스펙 AC-12).

`daily_llm_call_limit` 은 호출 **수**만 세므로(`pipeline/quota.py`) 토큰과 비용은
어디에도 남지 않는다. 이 스크립트는 `openai_provider` 의 관측 훅(`usage_log`)을 켜고
실제 파이프라인을 돌려 **단계별 입력·출력 토큰**을 받아 단가를 곱한다.

⚠️ **실 API 를 호출한다** — 질문 1건당 3~4회, 임베딩 1회다. 데모 프로젝트에 대고
돌리지 말 것. 측정용 프로젝트를 따로 주고, 그 프로젝트에 질문·답변·이벤트가 실제로
쌓인다는 점을 감안한다.

    uv run python scripts/measure_tokens.py --project <UUID> --asker <이메일>

단계 이름은 `question.graded` 이벤트의 `steps` 에서 읽되 **`STEP_ORDER` 로 다시 정렬한다**.
⚠️ `events.payload` 는 `jsonb` 이고 jsonb 는 키 순서를 보존하지 않는다 — 길이순·사전순으로
재정렬하므로 `{translate, generate, verify}` 가 `verify, generate, translate` 로 나온다.
그대로 `usage_log` 와 짝지으면 단계 라벨이 통째로 어긋난다(모델 배정으로 들킨다).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal as session_factory  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.user import User  # noqa: E402
from app.schemas.question import QuestionCreate  # noqa: E402
from app.services import question_service  # noqa: E402
from app.services.llm import openai_provider  # noqa: E402
from app.services.pipeline import answer as answer_pipeline  # noqa: E402

# 1M 토큰당 USD. 모델 교체 시 여기만 고친다 — 측정 결과 표에 단가가 같이 찍히므로
# 표만 보고도 어떤 단가로 계산한 값인지 알 수 있다.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-terra": (2.50, 15.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "text-embedding-3-small": (0.02, 0.00),
}

# 파이프라인이 LLM 을 부르는 실제 순서 (`06 §1`). jsonb 가 흩뜨린 키를 여기 순서로 되돌린다.
STEP_ORDER = ["translate", "reuse_gate", "generate", "verify", "structure"]

# 기본 측정 질문 3건 — 등급 경로를 하나씩 태운다. 🔴 는 문서에 없는 주제를 물어
# `no_evidence` 를 유도하고, 그때만 `structure` 단계가 추가로 돈다.
DEFAULT_QUESTIONS = [
    ("green", "환불은 언제까지 신청할 수 있나요?"),
    ("yellow", "환불 처리 기간과 배송비 부담이 어떻게 되나요?"),
    ("red", "직원 연차 휴가는 며칠인가요?"),
]


def _price(model: str, usage: dict[str, int]) -> float:
    """USD. 미등록 모델은 0 으로 두고 표에 그대로 드러낸다(조용히 추정하지 않는다)."""
    rates = PRICING.get(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    # ⚠️ `reasoning_tokens` 는 `output_tokens` 에 이미 포함돼 있다 — 더하면 이중 계상이다.
    return (usage["input_tokens"] * in_rate + usage["output_tokens"] * out_rate) / 1_000_000


async def _measure_one(project: Project, asker: User, label: str, content_ko: str) -> dict:
    openai_provider.reset_usage_log()

    async with session_factory() as db:
        question = await question_service.create_question(
            db,
            project_id=project.id,
            asker=asker,
            payload=QuestionCreate(content_ko=content_ko, urgency="normal"),
        )
        question_id = question.id
        await db.commit()

    await answer_pipeline.run_answer_pipeline(question_id)

    async with session_factory() as db:
        event = await db.scalar(
            select(Event).where(Event.type == "question.graded", Event.entity_id == question_id)
        )

    payload = dict(event.payload) if event is not None else {}
    # 모르는 단계가 생기면 맨 뒤로 보내되 죽지는 않는다 — 측정이 목적이다.
    steps = sorted(
        payload.get("steps", {}).keys(),
        key=lambda name: STEP_ORDER.index(name) if name in STEP_ORDER else len(STEP_ORDER),
    )
    records = openai_provider.usage_log()

    # 임베딩은 파이프라인 단계가 아니라 검색 전처리다 — 분리해서 이름을 따로 붙인다.
    embeds = [r for r in records if r["model"] == settings.embedding_model]
    calls = [r for r in records if r["model"] != settings.embedding_model]

    rows = []
    for index, record in enumerate(calls):
        rows.append({"step": steps[index] if index < len(steps) else f"call{index + 1}", **record})
    for record in embeds:
        rows.append({"step": "embed(질문)", **record})

    return {
        "label": label,
        "question_id": str(question_id),
        "grade": payload.get("grade"),
        "matching_rate": payload.get("matching_rate"),
        "rows": rows,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="질문당 토큰·단가 실측")
    parser.add_argument("--project", required=True, help="측정용 프로젝트 UUID (데모 금지)")
    parser.add_argument("--asker", required=True, help="질문자 이메일")
    parser.add_argument(
        "--ask",
        action="append",
        default=[],
        metavar="한국어질문",
        help="측정할 질문을 직접 지정한다(반복 가능). 없으면 DEFAULT_QUESTIONS 를 쓴다.",
    )
    args = parser.parse_args()

    # 지정 질문은 기대 등급을 모른다 — 라벨은 순번으로 두고 실제 등급만 표에 찍는다.
    questions = (
        [(f"ask{index + 1}", text) for index, text in enumerate(args.ask)]
        if args.ask
        else DEFAULT_QUESTIONS
    )

    async with session_factory() as db:
        project = await db.get(Project, UUID(args.project))
        asker = await db.scalar(select(User).where(User.email == args.asker))

    if project is None:
        raise SystemExit(f"프로젝트를 찾지 못했다: {args.project}")
    if asker is None:
        raise SystemExit(f"사용자를 찾지 못했다: {args.asker}")

    print(f"측정 프로젝트: {project.name} ({project.id})")
    print(
        f"모델 배정: answer={settings.llm_model_answer} verify={settings.llm_model_verify} "
        f"struct={settings.llm_model_struct} translate={settings.llm_model_translate}"
    )
    print()

    results = []
    for label, content_ko in questions:
        results.append(await _measure_one(project, asker, label, content_ko))

    grand_total = 0.0
    for result in results:
        print(f"── {result['label']} → 실제 {result['grade']} (매칭률 {result['matching_rate']})")
        print(f"   {'단계':<14}{'모델':<18}{'입력':>8}{'출력':>8}{'추론':>8}{'USD':>12}")
        subtotal = 0.0
        for row in result["rows"]:
            cost = _price(row["model"], row)
            subtotal += cost
            print(
                f"   {row['step']:<14}{row['model']:<18}"
                f"{row['input_tokens']:>8}{row['output_tokens']:>8}"
                f"{row['reasoning_tokens']:>8}{cost:>12.6f}"
            )
        grand_total += subtotal
        print(f"   {'합계':<40}{subtotal:>12.6f} USD")
        print()

    average = grand_total / len(results) if results else 0.0
    print(f"질문 {len(results)}건 합계 {grand_total:.6f} USD · 질문당 평균 {average:.6f} USD")
    print(f"질문 123건 환산 ≈ {average * 123:.4f} USD (파이프라인만, 확정·교훈 단계 제외)")


if __name__ == "__main__":
    asyncio.run(main())
