"""M4 DoD — 시나리오 B 전 구간 e2e (`08 §4`, `prompts/04-review-workflow.md`).

질문 → 🔴 → 카드 생성 → `edit`(영어) → ko 번역 확정 → 공식 Q&A 편입 →
**같은 질문 재접수 → 재사용 즉답**(확정 ko 원문 그대로, 재번역 안 됨).

> ### ⚠️ 질문자 관점 GET 이 이 파일의 핵심이다
> 카드 큐 쪽만 단언하면 **`held → answered` 전이 누락이 절대 드러나지 않는다.** 그 결함의
> 증상은 "담당자는 처리했는데 질문자 화면에는 영원히 보류 중"이며, 데모에서 처음 터진다.
"""

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_qa import OFFICIAL_QA_STATUS_ACTIVE
from app.models.question import (
    ANSWER_SOURCE_GENERATED,
    ANSWER_SOURCE_REUSED,
    ANSWER_STATE_VERIFIED,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_HELD,
)
from app.services.llm.fake_provider import FakeLLMProvider
from tests.pipeline_helpers import ask
from tests.review_helpers import (
    RED_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    card_detail,
    card_for_question,
    cards_for_question,
    official_qas_of,
    question_detail,
    seed_evidence,
)

# 일본 리전 조항이 없는 환불 정책 — 시나리오 B 의 전제다 (`08 §2`).
QUESTION_KO = f"환불 정책이 일본 리전에도 동일하게 적용되나요? {RED_MARKER}"
ANSWERER_INPUT_EN = (
    "The 30-day window applies to all regions except Japan, where local law requires 20 days."
)


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "scenario-b.test")


async def test_scenario_b_red_to_edit_to_reuse(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    fake_llm_provider: FakeLLMProvider,
) -> None:
    await seed_evidence(db_session, team, QUESTION_KO)

    # --- ① 질문 → 🔴 보류 + 카드 ---------------------------------------------------------
    question_id, card = await ask_until_card(client, db_session, team, QUESTION_KO)

    assert card.reason == "red"
    assert card.status == "pending"
    # ⑦ 결과는 M3 이 이미 만들어 뒀다. 카드는 **복사**만 한다 (다시 생성하지 않는다).
    answer = await answer_of(db_session, question_id)
    assert answer.question_struct is not None
    assert card.question_struct == answer.question_struct

    asker_view = await question_detail(client, team.asker, question_id)
    assert asker_view["status"] == QUESTION_STATUS_HELD
    assert asker_view["answer"] is None, "🔴 은 질문자에게 발행하지 않는다"
    assert asker_view["held_info"]["reason"] == "no_evidence"
    assert asker_view["held_info"]["card_status"] == "pending"

    # --- ② 담당자 카드 상세 — 배경 → 질문 → 선택지 ----------------------------------------
    detail = await card_detail(client, team, str(card.id))
    assert detail["question_struct"]["options"], "🔴 카드에는 선택지가 실린다 (룰 8)"
    assert detail["draft_answer"] is not None
    assert detail["first_viewed_at"] is not None, "최초 조회 시각이 기록된다 (지표 시작점)"

    # --- ③ edit(영어) → ko 번역 확정 -----------------------------------------------------
    translate_calls_before = len(fake_llm_provider.translate_calls)
    response = await act(client, team, str(card.id), "edit", {"content_en": ANSWERER_INPUT_EN})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["answer"]["state"] == ANSWER_STATE_VERIFIED
    assert body["answer"]["content_en"] == ANSWERER_INPUT_EN
    confirmed_ko = body["answer"]["content_ko"]
    assert confirmed_ko, "서버가 ko 를 만들어 확정 원문으로 고정한다 (D5)"
    assert len(fake_llm_provider.translate_calls) == translate_calls_before + 1
    assert body["official_qa_id"] is not None, "확정은 공식 Q&A 로 편입된다 (`06 §3`)"
    assert body["lesson_candidate_id"] is None, "교훈 추출은 M6 범위다"
    assert body["resolved_feedbacks"] == 0

    # --- ④ 질문자 관점 — 여기가 데모에서 처음 터지는 지점이다 -------------------------------
    asker_view = await question_detail(client, team.asker, question_id)
    assert asker_view["status"] == QUESTION_STATUS_ANSWERED
    assert asker_view["answer"] is not None
    assert asker_view["answer"]["state"] == ANSWER_STATE_VERIFIED
    assert asker_view["answer"]["content_ko"] == confirmed_ko
    # `held_info` 는 이력용으로 남고 `card_status` 만 resolved 가 된다 (`05 §6`).
    assert asker_view["held_info"] is not None
    assert asker_view["held_info"]["card_status"] == "resolved"
    assert asker_view["answer"]["disclaimer"] is None, "확정 답변에 참고용 표기는 없다"

    # --- ⑤ 공식 Q&A 편입 확인 -------------------------------------------------------------
    official_qas = await official_qas_of(db_session, team.project_id)
    assert len(official_qas) == 1
    official_qa = official_qas[0]
    assert official_qa.status == OFFICIAL_QA_STATUS_ACTIVE
    assert official_qa.answer_ko == confirmed_ko
    assert official_qa.answer_en == ANSWERER_INPUT_EN
    assert official_qa.source_answer_id == answer.id

    # --- ⑥ 같은 질문 재접수 → 재사용 즉답 ---------------------------------------------------
    translate_calls_before = len(fake_llm_provider.translate_calls)
    accepted = await ask(client, team.asker, team.project_id, QUESTION_KO)
    reused_question_id = accepted["question_id"]

    reused_view = await question_detail(client, team.asker, reused_question_id)
    reused_answer = reused_view["answer"]

    assert reused_view["status"] == QUESTION_STATUS_ANSWERED
    assert reused_answer["source"] == ANSWER_SOURCE_REUSED
    assert reused_answer["grade"] == "green"
    assert reused_answer["state"] == ANSWER_STATE_VERIFIED, "재사용은 draft 를 거치지 않는다 (D11)"
    assert reused_answer["matching_rate"] is None, "원시 코사인 판정이라 % 를 표시하지 않는다"
    assert reused_answer["expires_at"] is None, "만료 스위퍼 대상이 아니다"
    assert reused_answer["disclaimer"] == "공식 확정 답변입니다."
    assert reused_answer["official_qa"]["id"] == str(official_qa.id)

    # ⚠️ 확정 당시의 한국어 원문 **그대로**다. 영어 저장본을 다시 번역하지 않는다 (룰 4·D5).
    assert reused_answer["content_ko"] == confirmed_ko
    assert len(fake_llm_provider.translate_calls) == translate_calls_before, (
        "재사용 경로에서 번역 호출이 일어나면 D5 위반이다"
    )

    # 재사용 답변은 카드를 만들지 않는다 (D11) — 담당자가 이미 확정한 원문이다.
    assert await card_for_question(db_session, reused_question_id) is None

    await db_session.refresh(official_qa)
    assert official_qa.reuse_count == 1


async def test_reject_keeps_the_question_held(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """반려로 해소하면 `status` 는 `held` 를 유지하고 `card_status` 만 resolved 다 (`04 §6.1`)."""
    content_ko = f"반려될 질문입니다. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(
        client, team, str(card.id), "reject", {"reason_en": "Out of scope for this project."}
    )
    assert response.status_code == 200, response.text
    assert response.json()["answer"]["state"] == "rejected"
    assert response.json()["official_qa_id"] is None, "반려는 공식 Q&A 에 편입하지 않는다"

    asker_view = await question_detail(client, team.asker, question_id)
    assert asker_view["status"] == QUESTION_STATUS_HELD
    assert asker_view["answer"] is None
    assert asker_view["held_info"]["card_status"] == "resolved"

    assert not await official_qas_of(db_session, team.project_id)


async def test_only_one_card_is_created_per_published_answer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """파이프라인은 답변 1건당 카드 1건을 만든다 — 큐가 중복으로 부풀지 않는다."""
    content_ko = f"카드는 하나만 생긴다. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, _ = await ask_until_card(client, db_session, team, content_ko)

    cards = await cards_for_question(db_session, question_id)
    assert len(cards) == 1
    assert cards[0].answer_id == (await answer_of(db_session, question_id)).id
    assert (await answer_of(db_session, question_id)).source == ANSWER_SOURCE_GENERATED
