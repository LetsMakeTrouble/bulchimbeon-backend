"""크로스체크 — 맞았다 / 달랐다 (`05 §6`, 룰 3·9, D12·D21·D22).

환각 방어 4겹의 마지막 겹이다 (`06 §7`). 담당자 확정 위에 질문자의 실사용 판정을 얹는다.
"""

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_qa import OFFICIAL_QA_STATUS_UNDER_REVIEW, OfficialQA
from app.models.question import (
    ANSWER_STATE_EXPIRED,
    ANSWER_STATE_UNDER_REVIEW,
    ANSWER_STATE_VERIFIED,
)
from app.models.review_card import Feedback
from tests.helpers import API, error_code
from tests.pipeline_helpers import as_uuid, ask
from tests.review_helpers import (
    GREEN_MARKER,
    RED_MARKER,
    YELLOW_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    card_detail,
    card_for_question,
    cards_for_question,
    feedback,
    question_detail,
    seed_evidence,
)


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "feedback.test")


async def _published_answer(
    client: AsyncClient, db_session: AsyncSession, team: Team, content_ko: str
) -> tuple[str, str]:
    """🟡 로 발행된 답변 하나. `(question_id, answer_id)`."""
    await seed_evidence(db_session, team, content_ko)
    question_id, _ = await ask_until_card(client, db_session, team, content_ko)
    answer = await answer_of(db_session, question_id)
    return question_id, str(answer.id)


# --------------------------------------------------------------------------------------
# 기본 동작
# --------------------------------------------------------------------------------------
async def test_correct_returns_the_updated_summary(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """응답에 갱신된 `feedback_summary` 가 항상 들어 있다 — 재조회가 필요 없다 (`05 §6`)."""
    _, answer_id = await _published_answer(
        client, db_session, team, f"맞았다를 받을 답변. {YELLOW_MARKER}"
    )

    response = await feedback(client, team.asker, answer_id, "correct")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer_state"] == "draft", "맞았다는 상태를 바꾸지 않는다 — 자동 확정은 없다"
    assert body["feedback_summary"] == {"correct": 1, "different": 0, "my_feedback": "correct"}
    assert body["recommend_approve"] is False
    assert body["message"] == "확인 감사합니다."


async def test_different_requires_a_note(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    _, answer_id = await _published_answer(
        client, db_session, team, f"사유 없는 신고. {YELLOW_MARKER}"
    )

    response = await feedback(client, team.asker, answer_id, "different")

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


async def test_different_moves_the_answer_to_review_and_raises_a_card(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    question_id, answer_id = await _published_answer(
        client, db_session, team, f"틀린 답변. {YELLOW_MARKER}"
    )
    # 원래 카드를 먼저 처리해 둔다 — 살아 있는 카드가 있으면 새 카드를 만들지 않기 때문이다 (룰 9).
    original = await card_for_question(db_session, question_id)
    assert original is not None
    assert (await act(client, team, str(original.id), "approve")).status_code == 200

    response = await feedback(
        client, team.asker, answer_id, "different", "부분 환불은 14일이라고 들었어요"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer_state"] == ANSWER_STATE_UNDER_REVIEW
    assert body["message"] == "오류 신고됨, 재검토 중"
    assert body["feedback_summary"]["different"] == 1

    cards = await cards_for_question(db_session, question_id)
    assert [card.reason for card in cards] == ["yellow", "feedback"]
    assert cards[-1].status == "pending"

    # 질문자 화면에서 답변을 지우지 않는다 — "오류 신고됨, 재검토 중"을 덧붙일 뿐이다 (룰 3).
    detail = await question_detail(client, team.asker, question_id)
    assert detail["answer"]["state"] == ANSWER_STATE_UNDER_REVIEW
    assert detail["answer"]["content_ko"]


async def test_different_on_an_open_card_shows_up_as_pending_feedback(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """담당자가 처리 중인 카드에 "달랐다"가 들어오면 **카드를 하나 더 만들지 않는다** (룰 9).

    같은 답변에 살아 있는 카드가 둘이면 확정 경로가 둘이 되어 담당자 우선 원칙이 무너진다.
    신규 피드백은 그 카드의 `pending_feedbacks` 로 보이고 저장 시 함께 해소된다.
    """
    question_id, answer_id = await _published_answer(
        client, db_session, team, f"처리 중 신고. {YELLOW_MARKER}"
    )
    card = await card_for_question(db_session, question_id)
    assert card is not None

    assert (
        await feedback(client, team.asker, answer_id, "different", "다릅니다")
    ).status_code == 200

    assert len(await cards_for_question(db_session, question_id)) == 1

    detail = await card_detail(client, team, str(card.id))
    assert len(detail["pending_feedbacks"]) == 1
    assert detail["pending_feedbacks"][0]["verdict"] == "different"
    assert detail["pending_feedbacks"][0]["note"] == "다릅니다"
    assert detail["pending_feedbacks"][0]["user"]["name"] == "지수"

    # 담당자 저장이 항상 우선한다 — 미해소 피드백은 함께 resolved 된다.
    response = await act(client, team, str(card.id), "edit", {"content_en": "Corrected."})
    assert response.status_code == 200, response.text
    assert response.json()["resolved_feedbacks"] == 1

    rows = await db_session.scalars(
        select(Feedback).where(Feedback.answer_id == as_uuid(answer_id))
    )
    assert all(row.resolved for row in rows)


# --------------------------------------------------------------------------------------
# 승인 추천 (룰 3)
# --------------------------------------------------------------------------------------
async def test_two_corrects_promote_the_card_to_recommend_approve(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """맞았다 2건 → 승인 추천. **자동 확정은 없다** — 원탭 승인이 가능해질 뿐이다."""
    question_id, answer_id = await _published_answer(
        client, db_session, team, f"두 번 맞았다. {GREEN_MARKER}"
    )

    first = await feedback(client, team.asker, answer_id, "correct")
    assert first.json()["recommend_approve"] is False

    second = await feedback(client, team.asker2, answer_id, "correct")
    assert second.status_code == 200, second.text
    assert second.json()["recommend_approve"] is True
    assert second.json()["feedback_summary"]["correct"] == 2

    card = await card_for_question(db_session, question_id)
    assert card is not None
    await db_session.refresh(card)
    assert card.recommend_approve is True
    assert card.status == "pending", "추천일 뿐 확정이 아니다"

    answer = await answer_of(db_session, question_id)
    await db_session.refresh(answer)
    assert answer.state == "draft"


async def test_same_user_cannot_submit_twice(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """유저당 1건이며 verdict 변경은 지원하지 않는다 (D12)."""
    _, answer_id = await _published_answer(
        client, db_session, team, f"재제출 차단. {YELLOW_MARKER}"
    )
    assert (await feedback(client, team.asker, answer_id, "correct")).status_code == 200

    retry = await feedback(client, team.asker, answer_id, "different", "역시 아닌 것 같아요")

    assert retry.status_code == 409
    assert error_code(retry) == "DUPLICATE_FEEDBACK"


# --------------------------------------------------------------------------------------
# 허용 상태 (D12)
# --------------------------------------------------------------------------------------
async def test_feedback_is_blocked_on_non_feedbackable_states(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`under_review`·`rejected`·`expired` 는 409 `FEEDBACK_NOT_ALLOWED` 다."""
    question_id, answer_id = await _published_answer(
        client, db_session, team, f"상태 가드. {YELLOW_MARKER}"
    )
    card = await card_for_question(db_session, question_id)
    assert card is not None

    # under_review — 다른 질문자의 "달랐다"로 내려간 상태
    assert (
        await feedback(client, team.asker2, answer_id, "different", "아닙니다")
    ).status_code == 200
    blocked = await feedback(client, team.asker, answer_id, "correct")
    assert blocked.status_code == 409
    assert error_code(blocked) == "FEEDBACK_NOT_ALLOWED"

    # rejected — 담당자가 반려한 상태
    assert (
        await act(client, team, str(card.id), "reject", {"reason_en": "Not applicable."})
    ).status_code == 200
    blocked = await feedback(client, team.asker, answer_id, "correct")
    assert blocked.status_code == 409
    assert error_code(blocked) == "FEEDBACK_NOT_ALLOWED"


async def test_feedback_is_blocked_on_an_expired_answer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """만료는 종착 상태다 — 피드백도 받지 않는다 (D12·D13)."""
    question_id, answer_id = await _published_answer(
        client, db_session, team, f"만료된 답변. {YELLOW_MARKER}"
    )
    answer = await answer_of(db_session, question_id)
    answer.state = ANSWER_STATE_EXPIRED
    await db_session.commit()

    response = await feedback(client, team.asker, answer_id, "correct")

    assert response.status_code == 409
    assert error_code(response) == "FEEDBACK_NOT_ALLOWED"


async def test_different_then_keep_restores_the_answer(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """재검토를 **원안 유지**로 해소하는 경로 (룰 3 — 담당자 택1 중 하나).

    질문자가 틀렸다고 봤지만 담당자가 원안이 맞다고 판단한 경우다. 답변은 확정으로 돌아가고
    사유는 질문자에게 전달된다 (M5 알림).
    """
    question_id, answer_id = await _published_answer(
        client, db_session, team, f"유지로 해소할 신고. {YELLOW_MARKER}"
    )
    original = await card_for_question(db_session, question_id)
    assert original is not None
    assert (await act(client, team, str(original.id), "approve")).status_code == 200

    assert (
        await feedback(client, team.asker, answer_id, "different", "다른 것 같아요")
    ).status_code == 200

    review_card = await card_for_question(db_session, question_id, reason="feedback")
    assert review_card is not None
    response = await act(
        client,
        team,
        str(review_card.id),
        "keep",
        {"reason_en": "The original answer is correct as written."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["answer"]["state"] == ANSWER_STATE_VERIFIED
    assert response.json()["resolved_feedbacks"] == 1, "미해소 피드백이 함께 해소된다 (룰 9)"

    detail = await question_detail(client, team.asker, question_id)
    assert detail["answer"]["state"] == ANSWER_STATE_VERIFIED
    assert detail["answer"]["feedback_summary"]["different"] == 1, "신고 이력은 남는다"


async def test_answerer_cannot_cross_check(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """확정은 담당자, 크로스체크는 질문자다 (룰 3)."""
    _, answer_id = await _published_answer(
        client, db_session, team, f"담당자는 피드백하지 않는다. {YELLOW_MARKER}"
    )

    response = await feedback(client, team.owner, answer_id, "correct")

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN_ROLE"


# --------------------------------------------------------------------------------------
# 지식으로의 환류 (D22·D7·D21)
# --------------------------------------------------------------------------------------
async def test_correct_on_a_reused_answer_credits_the_source_knowledge(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """재사용 답변에 들어온 "맞았다"가 **원본 공식 Q&A** 의 신뢰도로 환류된다 (D22).

    이 경로가 없으면 재사용될수록 원본에 대한 검증 이력이 쌓이지 않는다.
    """
    content_ko = f"환불 정책이 일본에도 적용되나요? {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)
    assert (
        await act(client, team, str(card.id), "edit", {"content_en": "Japan uses 20 days."})
    ).status_code == 200

    reused = await ask(client, team.asker, team.project_id, content_ko)
    reused_answer = await answer_of(db_session, reused["question_id"])
    assert reused_answer.source == "reused"

    response = await feedback(client, team.asker, str(reused_answer.id), "correct")
    assert response.status_code == 200, response.text

    official_qa = await db_session.scalar(
        select(OfficialQA).where(OfficialQA.id == reused_answer.official_qa_id)
    )
    assert official_qa is not None
    await db_session.refresh(official_qa)
    assert official_qa.correct_count == 1


async def test_different_on_a_verified_answer_suspends_its_knowledge(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """확정 답변에도 "달랐다"를 누를 수 있고, 그 지식은 재사용을 멈춘다 (룰 3 ⚠️, D7).

    ⚠️ 재사용으로 퍼진 답변도 함께 내려간다 (D21) — 원본과 사본이 따로 놀면 안 된다.
    """
    content_ko = f"D21 연쇄를 볼 질문. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    origin_question_id, card = await ask_until_card(client, db_session, team, content_ko)
    assert (
        await act(client, team, str(card.id), "edit", {"content_en": "Original ruling."})
    ).status_code == 200

    reused = await ask(client, team.asker2, team.project_id, content_ko)
    reused_answer = await answer_of(db_session, reused["question_id"])
    assert reused_answer.state == ANSWER_STATE_VERIFIED

    origin_answer = await answer_of(db_session, origin_question_id)
    response = await feedback(
        client, team.asker, str(origin_answer.id), "different", "일본은 20일이 아니에요"
    )
    assert response.status_code == 200, response.text

    official_qa = await db_session.get(OfficialQA, origin_answer.official_qa_id)
    assert official_qa is not None
    await db_session.refresh(official_qa)
    assert official_qa.status == OFFICIAL_QA_STATUS_UNDER_REVIEW

    await db_session.refresh(reused_answer)
    assert reused_answer.state == ANSWER_STATE_UNDER_REVIEW

    # 재검토 중인 지식은 재사용 후보에서 빠진다 — 같은 질문이 다시 와도 생성 경로로 간다.
    third = await ask(client, team.asker2, team.project_id, content_ko)
    third_answer = await answer_of(db_session, third["question_id"])
    assert third_answer.source == "generated"


async def test_asker_can_see_their_own_verdict_in_the_question_list(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`my_feedback` 은 **조회자 기준**이다 — 프론트의 버튼 상태가 여기서 나온다."""
    _, answer_id = await _published_answer(
        client, db_session, team, f"내 피드백 표시. {YELLOW_MARKER}"
    )
    assert (await feedback(client, team.asker, answer_id, "correct")).status_code == 200

    mine = await client.get(
        f"{API}/projects/{team.project_id}/questions", headers=team.asker.headers
    )
    summary = mine.json()["items"][0]["feedback_summary"]
    assert summary == {"correct": 1, "different": 0, "my_feedback": "correct"}

    others = await client.get(
        f"{API}/projects/{team.project_id}/questions?mine=false",
        headers=team.asker2.headers,
    )
    other_summary = others.json()["items"][0]["feedback_summary"]
    assert other_summary == {"correct": 1, "different": 0, "my_feedback": None}
