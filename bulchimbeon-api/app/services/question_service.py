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
from app.models.document import Chunk, Document, DocumentVersion
from app.models.official_qa import OfficialQA
from app.models.project import ROLE_ANSWERER, ProjectMember
from app.models.question import (
    ANSWER_SOURCE_REUSED,
    GRADE_RED,
    QUESTION_STATUS_FAILED,
    QUESTION_STATUS_HELD,
    QUESTION_STATUS_PROCESSING,
    Answer,
    AnswerCitation,
    Question,
)
from app.models.user import User
from app.schemas.question import (
    AnswerOut,
    AskedBy,
    Citation,
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
from app.services import event_service

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

# M4 의 `review_cards` 가 생기기 전까지 카드 상태를 알 수 없다. 파이프라인이 🔴·실패로
# 끝나면 카드는 **반드시 만들어지므로**(D23·룰 6 인박스 안전망) `pending` 이 사실에 가깝다.
# TODO(M4): 실제 `review_cards.status` 를 읽는다.
_DEFAULT_CARD_STATUS = "pending"

_MAX_LIMIT = 100


def _localized(table: dict[str, str], language: str) -> str:
    return table.get(language, table["ko"])


async def create_question(
    db: AsyncSession, *, project_id: UUID, asker: User, payload: QuestionCreate
) -> Question:
    """질문 접수. 파이프라인은 라우터가 BackgroundTasks 로 띄운다 (D3 — 202 즉시 반환)."""
    question = Question(
        project_id=project_id,
        asker_id=asker.id,
        content_ko=payload.content_ko,
        urgency=payload.urgency,
        suggest_urgent=False,  # ① 이 채운다 (`06 §2` ①)
        status=QUESTION_STATUS_PROCESSING,
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
        payload={"urgency": question.urgency},
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
    return QuestionListResponse(
        items=[_to_list_item(question, answers.get(question.id)) for question in questions],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _answers_by_question(db: AsyncSession, question_ids: list[UUID]) -> dict[UUID, Answer]:
    if not question_ids:
        return {}
    rows = await db.scalars(select(Answer).where(Answer.question_id.in_(question_ids)))
    return {answer.question_id: answer for answer in rows}


def _to_list_item(question: Question, answer: Answer | None) -> QuestionListItem:
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
        grade=grade,
        matching_rate=answer.matching_rate if published and answer is not None else None,
        state=answer.state if published and answer is not None else None,
        created_at=question.created_at,
        feedback_summary=FeedbackSummary() if published else None,
    )


async def get_detail(db: AsyncSession, question: Question, viewer: User) -> QuestionDetail:
    """`05 §6` GET /questions/{id} — 🟢🟡 / 🔴 `held_info` / `failed` `failure_info` / reused."""
    asker = await db.get(User, question.asker_id)
    answer = await db.scalar(select(Answer).where(Answer.question_id == question.id))

    held_info: HeldInfo | None = None
    failure_info: FailureInfo | None = None
    answer_out: AnswerOut | None = None

    if question.status == QUESTION_STATUS_HELD and answer is not None:
        # 🔴 은 답변을 내보내지 않는다. 초안은 DB 에 남아 카드에서만 노출된다.
        held_info = HeldInfo(
            reason=answer.held_reason or "low_confidence",
            message=_localized(
                _HELD_MESSAGES[answer.held_reason or "low_confidence"], viewer.language
            ),
            card_status=_DEFAULT_CARD_STATUS,
        )
    elif question.status == QUESTION_STATUS_FAILED:
        failure_info = FailureInfo(
            reason=FAILURE_REASON,
            message=_localized(_FAILURE_MESSAGE, viewer.language),
            card_status=_DEFAULT_CARD_STATUS,
        )
    elif answer is not None and question.status != QUESTION_STATUS_PROCESSING:
        answer_out = await _to_answer_out(db, answer, viewer)

    return QuestionDetail(
        id=question.id,
        content_ko=question.content_ko,
        content_en=question.content_en,
        urgency=question.urgency,
        status=question.status,
        asked_by=AskedBy(id=question.asker_id, name=asker.name if asker else ""),
        answer=answer_out,
        similar_official_qa=await _similar_official_qa(db, answer),
        held_info=held_info,
        failure_info=failure_info,
    )


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


async def _to_answer_out(db: AsyncSession, answer: Answer, viewer: User) -> AnswerOut:
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
        disclaimer=_localized(_REUSED_DISCLAIMER if reused else _DISCLAIMER, viewer.language),
        citations=await _citations(db, answer.id),
        feedback_summary=FeedbackSummary(),  # TODO(M4): feedbacks 집계
        official_qa=official_qa_ref,
    )


async def _citations(db: AsyncSession, answer_id: UUID) -> list[Citation]:
    """`05 §6` `citations[]` — 열람 URL 을 만들 수 있도록 문서·버전 id 를 함께 싣는다."""
    rows = (
        await db.execute(
            select(
                AnswerCitation.id,
                AnswerCitation.chunk_id,
                AnswerCitation.quote,
                AnswerCitation.similarity,
                Chunk.meta,
                Document.id.label("document_id"),
                Document.title.label("doc_title"),
                DocumentVersion.id.label("document_version_id"),
                DocumentVersion.version_no,
            )
            .join(Chunk, Chunk.id == AnswerCitation.chunk_id)
            .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(AnswerCitation.answer_id == answer_id)
            .order_by(AnswerCitation.similarity.desc())
        )
    ).all()

    citations: list[Citation] = []
    for row in rows:
        meta = row.meta or {}
        heading_path = meta.get("heading_path")
        page_no = meta.get("page_no")
        citations.append(
            Citation(
                id=row.id,
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                doc_title=row.doc_title,
                version_no=row.version_no,
                heading_path=list(heading_path) if isinstance(heading_path, list) else [],
                page_no=page_no if isinstance(page_no, int) else None,
                quote=row.quote,
                similarity=row.similarity,
            )
        )
    return citations
