"""크로스체크 — 질문자의 "맞았다 / 달랐다" (`05 §6`, `02` 룰 3·9, `06 §3`).

> ### 이것이 환각 방어 4겹의 마지막 겹이다 (`06 §7`)
> 담당자 확정 위에 질문자의 실사용 판정을 얹는다. "맞았다" 2건은 **승인 추천**일 뿐
> 자동 확정이 아니고, "달랐다"는 확정 답변이라도 재검토로 되돌린다.

⚠️ **유저당 1건**이다 (D12). 재제출은 verdict 가 달라도 409 이며 verdict 변경은 지원하지
않는다 — "생각이 바뀌었다"를 지원하면 `correct_count` 가 되감기며 승인 추천이 흔들린다.
"""

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DuplicateFeedback
from app.models.official_qa import OfficialQA
from app.models.question import ANSWER_STATE_UNDER_REVIEW, Answer, Question
from app.models.review_card import (
    CARD_REASON_FEEDBACK,
    FEEDBACK_CORRECT,
    FEEDBACK_DIFFERENT,
    RECOMMEND_APPROVE_CORRECT_COUNT,
    Feedback,
)
from app.models.user import User
from app.schemas.question import FeedbackCreate, FeedbackResponse, FeedbackSummary
from app.services import answer_service, event_service, official_qa_service, review_card_service

logger = logging.getLogger(__name__)

# `05 §1.5` — 사용자 표시 문자열은 **수신자 `users.language`** 로 서버가 만든다.
_MESSAGES = {
    FEEDBACK_CORRECT: {
        "ko": "확인 감사합니다.",
        "en": "Thanks for confirming.",
    },
    FEEDBACK_DIFFERENT: {
        "ko": "오류 신고됨, 재검토 중",
        "en": "Reported as incorrect. Under review.",
    },
}


async def submit(
    db: AsyncSession,
    *,
    question: Question,
    answer: Answer,
    user: User,
    payload: FeedbackCreate,
) -> FeedbackResponse:
    """크로스체크 접수 (`05 §6`).

    응답에 **갱신된 `feedback_summary` 를 항상 포함**한다 — 프론트 낙관적 업데이트의 전제이며
    `GET /questions/{id}` 재조회를 불필요하게 만든다.
    """
    # D12 — `draft`·`verified` 에서만 받는다. 그 외는 409 `FEEDBACK_NOT_ALLOWED`.
    answer_service.ensure_feedback_allowed(answer)
    await _ensure_first_feedback(db, answer.id, user.id)

    feedback = Feedback(
        answer_id=answer.id,
        user_id=user.id,
        verdict=payload.verdict,
        note=(payload.note or "").strip() or None,
        resolved=False,
    )
    db.add(feedback)
    await db.flush()

    await event_service.record_event(
        db,
        project_id=question.project_id,
        type=event_service.EVENT_FEEDBACK_CREATED,
        actor_id=user.id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={
            "feedback_id": str(feedback.id),
            "answer_id": str(answer.id),
            "verdict": feedback.verdict,
        },
    )

    if payload.verdict == FEEDBACK_CORRECT:
        await _on_correct(db, answer)
    else:
        await _on_different(db, question=question, answer=answer)

    summary = await summary_for_answer(db, answer.id, viewer_id=user.id)
    return FeedbackResponse(
        answer_id=answer.id,
        answer_state=answer.state,  # type: ignore[arg-type]
        message=_MESSAGES[payload.verdict].get(user.language, _MESSAGES[payload.verdict]["ko"]),
        feedback_summary=summary,
        recommend_approve=summary.correct >= RECOMMEND_APPROVE_CORRECT_COUNT,
    )


async def _ensure_first_feedback(db: AsyncSession, answer_id: UUID, user_id: UUID) -> None:
    existing = await db.scalar(
        select(Feedback.id).where(Feedback.answer_id == answer_id, Feedback.user_id == user_id)
    )
    if existing is not None:
        raise DuplicateFeedback()


async def _on_correct(db: AsyncSession, answer: Answer) -> None:
    """맞았다 — 승인 추천 승격(룰 3) + 원본 지식으로의 신뢰도 환류(D22).

    D22 가 중요한 이유: 재사용 답변에 들어온 "맞았다"가 **원본 공식 Q&A** 의 신뢰도로
    돌아간다. 이 경로가 없으면 재사용될수록 원본에 대한 검증 이력이 쌓이지 않는다.
    """
    if answer.official_qa_id is not None:
        official_qa = await db.get(OfficialQA, answer.official_qa_id)
        if official_qa is not None:
            official_qa.correct_count += 1

    if await _correct_count(db, answer.id) < RECOMMEND_APPROVE_CORRECT_COUNT:
        return

    # 🟢 카드는 이때 비로소 브리핑에 등장한다 (룰 1·3). 자동 확정은 어떤 경로에도 없다.
    card = await review_card_service.open_card_for_answer(db, answer.id)
    if card is not None:
        card.recommend_approve = True
    await db.flush()


async def _on_different(db: AsyncSession, *, question: Question, answer: Answer) -> None:
    """달랐다 — 답변(및 연결 공식 Q&A) 재검토 전환 (룰 3, D7·D21).

    확정된 답변에도 "달랐다"를 누를 수 있다. 그 경우 해당 공식 Q&A 는 재사용을 멈추고,
    그것을 참조하는 재사용 답변까지 함께 내려간다 (D21).
    """
    answer.state = ANSWER_STATE_UNDER_REVIEW
    await db.flush()

    if answer.official_qa_id is not None:
        official_qa = await db.get(OfficialQA, answer.official_qa_id)
        if official_qa is not None:
            await official_qa_service.suspend(db, official_qa, project_id=question.project_id)

    # 룰 9 — 이미 열린 카드가 있으면 새로 만들지 않는다. 담당자가 그 카드를 열면
    # `pending_feedbacks` 로 신규 피드백이 보이고, 저장 시 함께 해소된다.
    # (카드를 하나 더 만들면 같은 답변에 대해 두 번 확정하는 경로가 생긴다.)
    if await review_card_service.open_card_for_answer(db, answer.id) is None:
        await review_card_service.create_card(
            db, question=question, answer=answer, reason=CARD_REASON_FEEDBACK
        )
    # TODO(M5): 담당자에게 `feedback.different` 알림 (`04 §4`).


async def _correct_count(db: AsyncSession, answer_id: UUID) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Feedback)
            .where(Feedback.answer_id == answer_id, Feedback.verdict == FEEDBACK_CORRECT)
        )
    ) or 0


async def summary_for_answer(
    db: AsyncSession, answer_id: UUID, *, viewer_id: UUID
) -> FeedbackSummary:
    summaries = await summaries_for_answers(db, [answer_id], viewer_id=viewer_id)
    return summaries.get(answer_id, FeedbackSummary())


async def summaries_for_answers(
    db: AsyncSession, answer_ids: list[UUID], *, viewer_id: UUID
) -> dict[UUID, FeedbackSummary]:
    """`05 §6` `feedback_summary` — 목록·상세가 공유한다.

    `my_feedback` 은 **조회자 기준**이다. 프론트가 "이미 피드백함" 버튼 상태를 이것으로 그린다.
    """
    if not answer_ids:
        return {}

    rows = (
        await db.execute(
            select(Feedback.answer_id, Feedback.verdict, Feedback.user_id).where(
                Feedback.answer_id.in_(answer_ids)
            )
        )
    ).all()

    summaries: dict[UUID, FeedbackSummary] = {
        answer_id: FeedbackSummary() for answer_id in answer_ids
    }
    for answer_id, verdict, user_id in rows:
        summary = summaries[answer_id]
        if verdict == FEEDBACK_CORRECT:
            summary.correct += 1
        else:
            summary.different += 1
        if user_id == viewer_id:
            summary.my_feedback = verdict
    return summaries
