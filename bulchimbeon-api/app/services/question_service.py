"""질문 조회·접수 서비스 (`05 §6`).

트랜잭션 경계는 라우터다 — 여기서는 `flush()` 까지만 한다 (M1·M2 와 같은 규약).

> ### 사용자 표시 문자열은 **수신자 `users.language`** 로 서버가 만든다 (`05 §1.5`)
> `disclaimer` · `held_info.message` · `failure_info.message` 가 그 대상이다.
> 프론트는 그대로 렌더한다. 반면 `error.message` 는 개발자용 한국어 고정이라 여기 없다.
"""

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, PipelineInProgress
from app.models.official_qa import OfficialQA
from app.models.project import ROLE_ANSWERER, ProjectMember
from app.models.question import (
    ANSWER_SOURCE_REUSED,
    ANSWER_STATE_DRAFT,
    GRADE_RED,
    QUESTION_MODE_CONVERSATION,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_FAILED,
    QUESTION_STATUS_HELD,
    QUESTION_STATUS_PROCESSING,
    Answer,
    Question,
)
from app.models.review_card import CARD_REASON_FAILED, CARD_REASON_RED, CARD_STATUS_PENDING
from app.models.user import User
from app.schemas.question import (
    AnswerOut,
    AskedBy,
    FailureInfo,
    FeedbackSummary,
    HeldInfo,
    OfficialQARef,
    QuestionCreate,
    QuestionDetail,
    QuestionListItem,
    QuestionListResponse,
    SimilarOfficialQA,
)
from app.services import (
    accuracy_service,
    answer_service,
    event_service,
    feedback_service,
    review_card_service,
)

# `05 §1.5` — 수신자 언어로 서버가 만드는 문자열. 코드 분기는 프론트가 하지 않는다.
_DISCLAIMER = {
    "ko": "참고용 답변입니다. 담당자 확인 전입니다.",
    "en": "This is a reference answer. It has not been confirmed by the answerer yet.",
}
_REUSED_DISCLAIMER = {
    "ko": "공식 확정 답변입니다.",
    "en": "This is a confirmed official answer.",
}
_HELD_MESSAGES = {
    "conflict": {
        "ko": "근거 문서가 충돌하여 담당자에게 전달했습니다.",
        "en": "The source documents contradict each other, so this was sent to the answerer.",
    },
    "no_evidence": {
        "ko": "근거 문서에서 답을 찾지 못해 담당자에게 전달했습니다.",
        "en": "No supporting evidence was found, so this was sent to the answerer.",
    },
    "low_confidence": {
        "ko": "답변의 신뢰도가 낮아 담당자에게 전달했습니다.",
        "en": "Confidence in the draft answer was too low, so this was sent to the answerer.",
    },
    "schema_failed": {
        "ko": "답변 생성이 형식 검증을 통과하지 못해 담당자에게 전달했습니다.",
        "en": "Answer generation failed validation, so this was sent to the answerer.",
    },
    "quota_exceeded": {
        "ko": "오늘의 AI 호출 한도를 넘겨 담당자에게 전달했습니다.",
        "en": "Today's AI call limit was reached, so this was sent to the answerer.",
    },
}
_FAILURE_MESSAGE = {
    "ko": "답변 생성에 실패했습니다. 담당자에게 전달했습니다.",
    "en": "Answer generation failed. This was sent to the answerer.",
}

# `05 §6` — 실패의 `reason` 은 화면 문구가 아니라 **로깅용 코드**다.
FAILURE_REASON = "pipeline_error"

# 카드가 어떤 이유로도 조회되지 않을 때의 표기. 🔴·실패는 카드를 **반드시 만들지만**
# (D23·룰 6 인박스 안전망) 계약서의 `card_status` 는 null 을 허용하지 않는다 (`05 §6`).
_FALLBACK_CARD_STATUS = CARD_STATUS_PENDING

_MAX_LIMIT = 100


def _localized(table: dict[str, str], language: str) -> str:
    return table.get(language, table["ko"])


async def create_question(
    db: AsyncSession, *, project_id: UUID, asker: User, payload: QuestionCreate
) -> Question:
    """질문 접수. 파이프라인은 라우터가 BackgroundTasks 로 띄운다 (D3 — 202 즉시 반환).

    대화모드는 AI 답변 대상이 아니므로 **`processing` 을 거치지 않고 즉시 `answered`** 다.
    파이프라인·좀비 회수의 대상 조건이 전부 `status='processing'` 이라, 상태만으로도
    모든 답변 트리거 경로에서 제외된다 (`pipeline/answer._pipeline` 의 mode 가드가 2중 방어).
    """
    conversation = payload.mode == QUESTION_MODE_CONVERSATION
    question = Question(
        project_id=project_id,
        asker_id=asker.id,
        content_ko=payload.content_ko,
        urgency=payload.urgency,
        suggest_urgent=False,  # ① 이 채운다 (`06 §2` ①)
        status=QUESTION_STATUS_ANSWERED if conversation else QUESTION_STATUS_PROCESSING,
        mode=payload.mode,
    )
    db.add(question)
    await db.flush()

    await event_service.record_event(
        db,
        project_id=project_id,
        type=event_service.EVENT_QUESTION_CREATED,
        actor_id=asker.id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={"urgency": question.urgency, "mode": question.mode},
    )
    return question


async def load_question(db: AsyncSession, question_id: UUID) -> Question:
    question = await db.get(Question, question_id)
    if question is None:
        raise NotFound()
    return question


async def patch_urgency(db: AsyncSession, question: Question, urgency: str) -> Question:
    """긴급도 변경 — **허용 창은 `status='processing'` 동안뿐**이다 (`05 §6`).

    파이프라인이 끝난 뒤에는 409 `PIPELINE_IN_PROGRESS` 이며 body 에 현재 `status` 를 싣는다.
    이미 만들어진 카드의 `is_urgent` 는 바뀌지 않는다.
    """
    if question.status != QUESTION_STATUS_PROCESSING:
        raise PipelineInProgress(
            "파이프라인이 끝난 질문의 긴급도는 변경할 수 없습니다.", status=question.status
        )
    question.urgency = urgency
    await db.flush()
    return question


def _list_query(project_id: UUID, member: ProjectMember, mine: bool | None) -> Select[tuple[UUID]]:
    """질문자는 기본 자기 것, 담당자는 전체 (`05 §6`)."""
    stmt = select(Question).where(Question.project_id == project_id)

    restrict_to_self = mine if mine is not None else member.role != ROLE_ANSWERER
    if restrict_to_self:
        stmt = stmt.where(Question.asker_id == member.user_id)
    return stmt


async def list_questions(
    db: AsyncSession,
    *,
    project_id: UUID,
    member: ProjectMember,
    mine: bool | None = None,
    status: str | None = None,
    grade: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> QuestionListResponse:
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    stmt = _list_query(project_id, member, mine)
    if status is not None:
        stmt = stmt.where(Question.status == status)
    if grade is not None:
        # 등급은 답변에 있다. `held` 질문도 초안 행이 있으므로 조인으로 걸러진다.
        stmt = stmt.join(Answer, Answer.question_id == Question.id).where(Answer.grade == grade)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    questions = list(
        (
            await db.scalars(
                stmt.order_by(Question.created_at.desc(), Question.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )

    answers = await _answers_by_question(db, [question.id for question in questions])
    summaries = await feedback_service.summaries_for_answers(
        db, [answer.id for answer in answers.values()], viewer_id=member.user_id
    )
    return QuestionListResponse(
        items=[
            _to_list_item(question, answers.get(question.id), summaries) for question in questions
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _answers_by_question(db: AsyncSession, question_ids: list[UUID]) -> dict[UUID, Answer]:
    if not question_ids:
        return {}
    rows = await db.scalars(select(Answer).where(Answer.question_id.in_(question_ids)))
    return {answer.question_id: answer for answer in rows}


def _to_list_item(
    question: Question,
    answer: Answer | None,
    summaries: dict[UUID, FeedbackSummary],
) -> QuestionListItem:
    """`05 §6` 목록 아이템 표.

    - `grade`: `processing` 이면 null, `held` 면 `"red"`.
    - `matching_rate`: `held`·`processing`·재사용이면 null.
    - `state`·`feedback_summary`: **발행된 답변이 없으면 null** — 🔴 초안은 카드에서만 노출된다.
    """
    published = answer is not None and question.status not in (
        QUESTION_STATUS_HELD,
        QUESTION_STATUS_PROCESSING,
        QUESTION_STATUS_FAILED,
    )

    if question.status == QUESTION_STATUS_HELD:
        grade: str | None = GRADE_RED
    elif answer is not None and question.status != QUESTION_STATUS_PROCESSING:
        grade = answer.grade
    else:
        grade = None

    return QuestionListItem(
        id=question.id,
        content_ko=question.content_ko,
        status=question.status,
        mode=question.mode,
        grade=grade,
        matching_rate=answer.matching_rate if published and answer is not None else None,
        state=answer.state if published and answer is not None else None,
        created_at=question.created_at,
        feedback_summary=(
            summaries.get(answer.id, FeedbackSummary())
            if published and answer is not None
            else None
        ),
    )


async def get_detail(db: AsyncSession, question: Question, viewer: User) -> QuestionDetail:
    """`05 §6` GET /questions/{id} — 🟢🟡 / 🔴 `held_info` / `failed` `failure_info` / reused.

    > ### `held_info` 는 해소된 뒤에도 남는다 (`05 §6` 프론트 필수 처리)
    > 담당자 확정으로 보류가 풀리면 `status` 는 `answered` 가 되고 `answer` 가 채워지지만,
    > `held_info` 는 **이력용으로 유지**하되 `card_status` 만 `resolved` 로 바뀐다. 프론트는
    > `held_info != null && status == "answered"` 를 "보류였다가 담당자가 답한 질문"으로
    > 렌더한다 — `held_info` 가 있다고 보류로 표시하지 않는다.
    """
    asker = await db.get(User, question.asker_id)
    answer = await db.scalar(select(Answer).where(Answer.question_id == question.id))

    held_info: HeldInfo | None = None
    failure_info: FailureInfo | None = None
    answer_out: AnswerOut | None = None

    if answer is not None and answer.held_reason is not None:
        # 🔴 로 보류됐던 사실은 답변 행의 `held_reason` 이 원천이다. DND 강등으로 🟡 이 된
        # 답변은 `held_reason` 이 비어 있으므로(`06 §2` ⑥) 여기 걸리지 않는다.
        held_info = HeldInfo(
            reason=answer.held_reason,
            message=_localized(_HELD_MESSAGES[answer.held_reason], viewer.language),
            card_status=await _card_status(db, question.id, (CARD_REASON_RED,)),
        )

    if question.status == QUESTION_STATUS_FAILED:
        failure_info = FailureInfo(
            reason=FAILURE_REASON,
            message=_localized(_FAILURE_MESSAGE, viewer.language),
            card_status=await _card_status(db, question.id, (CARD_REASON_FAILED,)),
        )
    elif question.status == QUESTION_STATUS_ANSWERED and answer is not None:
        # 🔴 보류(`held`)·처리 중에는 답변을 내보내지 않는다. 초안은 카드에서만 노출된다.
        answer_out = await _to_answer_out(db, question, answer, viewer)

    return QuestionDetail(
        id=question.id,
        content_ko=question.content_ko,
        content_en=question.content_en,
        urgency=question.urgency,
        status=question.status,
        mode=question.mode,
        asked_by=AskedBy(id=question.asker_id, name=asker.name if asker else ""),
        answer=answer_out,
        similar_official_qa=await _similar_official_qa(db, answer),
        held_info=held_info,
        failure_info=failure_info,
    )


async def _card_status(db: AsyncSession, question_id: UUID, reasons: tuple[str, ...]) -> str:
    """`held_info`·`failure_info` 의 `card_status` (`05 §6`)."""
    status = await review_card_service.card_status_for_question(db, question_id, reasons)
    return status or _FALLBACK_CARD_STATUS


async def _similar_official_qa(db: AsyncSession, answer: Answer | None) -> SimilarOfficialQA | None:
    """`similar_threshold` ~ `reuse_threshold` 구간에서만 채워진다 (룰 4, D24).

    ⚠️ `similarity` 는 **원시 코사인**이다. `matching_rate` 와 축이 다르므로 % 로 읽지 않는다.
    """
    if answer is None or answer.similar_official_qa_id is None:
        return None

    official_qa = await db.get(OfficialQA, answer.similar_official_qa_id)
    if official_qa is None:
        return None
    return SimilarOfficialQA(
        id=official_qa.id,
        question_ko=official_qa.question_ko,
        answer_ko=official_qa.answer_ko,
        # 첨부 판정에 쓴 유사도는 이 답변의 top-1 원시 코사인과 같은 축이다.
        similarity=answer.sim_raw if answer.sim_raw is not None else 0.0,
    )


async def _to_answer_out(
    db: AsyncSession, question: Question, answer: Answer, viewer: User
) -> AnswerOut:
    reused = answer.source == ANSWER_SOURCE_REUSED

    official_qa_ref: OfficialQARef | None = None
    if reused and answer.official_qa_id is not None:
        official_qa = await db.get(OfficialQA, answer.official_qa_id)
        if official_qa is not None:
            official_qa_ref = OfficialQARef(
                id=official_qa.id,
                question_ko=official_qa.question_ko,
                reuse_count=official_qa.reuse_count,
            )

    return AnswerOut(
        id=answer.id,
        grade=answer.grade,
        state=answer.state,
        matching_rate=answer.matching_rate,
        search_score=answer.search_score,
        grounding_score=answer.grounding_score,
        content_ko=answer.content_ko,
        content_en=answer.content_en,
        source=answer.source,
        degraded_from_red=answer.degraded_from_red,
        expires_at=answer.expires_at,
        disclaimer=_disclaimer(answer, viewer.language),
        citations=await answer_service.citations_for_answer(db, answer.id),
        feedback_summary=await feedback_service.summary_for_answer(
            db, answer.id, viewer_id=viewer.id
        ),
        official_qa=official_qa_ref,
        # `05 §6` — 이 답변 등급의 실측 정확도 (D25). 표본 30건 미만이면 숫자 대신 "표본 부족"이다.
        accuracy_context=await accuracy_service.for_grade(
            db,
            project_id=question.project_id,
            grade=answer.grade,
            language=viewer.language,
        ),
    )


def _disclaimer(answer: Answer, language: str) -> str | None:
    """`05 §6` — 참고용 표기는 **미확정 상태에만** 붙는다.

    담당자가 확정한 답변에는 disclaimer 가 없고(`05 §6` held → answered 예시가 `null`),
    재사용 답변만 "공식 확정 답변입니다."를 단다.
    """
    if answer.source == ANSWER_SOURCE_REUSED:
        return _localized(_REUSED_DISCLAIMER, language)
    if answer.state == ANSWER_STATE_DRAFT:
        return _localized(_DISCLAIMER, language)
    return None
