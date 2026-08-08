"""공식 Q&A — 확정 지식의 편입·재검토·아카이브 (`06 §3`, `05 §9`).

> ### 지식 환류의 한 바퀴
> 담당자 확정 → **편입**(`incorporate`) → 재사용(`06 §2` ②) → 문제 발생 시 **재검토**
> (`suspend`) → 해소되면 **복귀**(`restore`). 원본 문서가 사라지면 **아카이브**(`archive`).

⚠️ `answer_ko` 가 "불변"이라는 말은 **서버가 영어 저장본을 다시 번역하지 않는다**는 뜻이다
(룰 4·D5). 담당자가 재검토 카드에서 새로 써 넣는 것은 재번역이 아니라 새로운 확정이므로
같은 행의 내용을 갱신한다 — `official_qas.status` 에 `under_review → active` 경로가 있는 것
자체가 "행이 살아남아 돌아온다"는 전제다 (D7).

> ### D21 — 재사용으로 퍼진 답변은 원본과 따로 놀지 않는다
> Q&A 가 내려가면 그것을 참조하는 `source='reused'` 답변도 함께 내려가고, 올라오면 함께
> 올라오며 **본문도 원본의 현재 값으로 맞춘다**. 사본이 원본보다 오래된 채로 `verified` 를
> 유지하면 담당자가 정정한 내용이 일부 질문자에게만 반영되지 않는다.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.notification import NOTIFICATION_ANSWER_CORRECTED
from app.models.official_qa import (
    OFFICIAL_QA_STATUS_ACTIVE,
    OFFICIAL_QA_STATUS_ARCHIVED,
    OFFICIAL_QA_STATUS_UNDER_REVIEW,
    OfficialQA,
)
from app.models.question import (
    ANSWER_SOURCE_REUSED,
    ANSWER_STATE_UNDER_REVIEW,
    ANSWER_STATE_VERIFIED,
    Answer,
    Question,
)
from app.schemas.official_qa import (
    OfficialQAArchived,
    OfficialQADetail,
    OfficialQAListItem,
    OfficialQAListResponse,
)
from app.services import event_service, notification_service, sse_manager
from app.services.llm import get_provider

logger = logging.getLogger(__name__)

_MAX_LIMIT = 100


async def incorporate(
    db: AsyncSession, *, question: Question, answer: Answer, project_id: UUID
) -> OfficialQA | None:
    """확정된 답변을 공식 Q&A 로 편입한다 (`06 §3`).

    돌려주는 값이 `05 §7.1` 응답의 `official_qa_id` 다. **`None` 이 정상인 경우가 둘** 있다:

    1. 확정한 본문이 비어 있다 — 강제 🔴 4종은 초안이 `""` 다. 빈 문자열을 지식으로 넣으면
       다음 유사 질문이 **빈 답변을 즉답으로** 받는다 (`06 §2` ②). 카드 액션 자체는
       매트릭스대로 성공시키고 편입만 건너뛴다 (`05 §7.1` — 해당 없으면 `null`).
    2. 연결된 Q&A 가 이미 `archived` 다 — 근거 문서가 삭제된 지식이며 MVP 에서는 되돌리지
       않는다 (`05 §9`).
    """
    if not answer.content_ko.strip():
        logger.info("편입할 본문이 비어 있어 공식 Q&A 를 만들지 않는다: answer=%s", answer.id)
        return None

    question_en = question.content_en or question.content_ko

    if answer.official_qa_id is not None:
        official_qa = await db.get(OfficialQA, answer.official_qa_id)
        if official_qa is None:
            logger.warning("연결된 공식 Q&A 가 사라졌다: answer=%s", answer.id)
        elif official_qa.status == OFFICIAL_QA_STATUS_ARCHIVED:
            return official_qa
        else:
            official_qa.answer_ko = answer.content_ko
            official_qa.answer_en = answer.content_en
            official_qa.question_ko = question.content_ko
            official_qa.question_en = question_en
            await restore(db, official_qa, project_id=project_id)
            return official_qa

    # 질문 임베딩은 ② 재사용 검색이 질문의 **영어 번역문**을 임베딩하는 것과 같은 축이어야
    # 한다 (`06 §2` ②). 다른 텍스트를 넣으면 재질문이 영원히 재사용되지 않는다.
    embedding = (await get_provider().embed([question_en]))[0]

    official_qa = OfficialQA(
        project_id=project_id,
        question_ko=question.content_ko,
        question_en=question_en,
        answer_ko=answer.content_ko,
        answer_en=answer.content_en,
        question_embedding=embedding,
        source_answer_id=answer.id,
        status=OFFICIAL_QA_STATUS_ACTIVE,
    )
    db.add(official_qa)
    await db.flush()

    answer.official_qa_id = official_qa.id
    await db.flush()

    await event_service.record_event(
        db,
        project_id=project_id,
        type=event_service.EVENT_OFFICIAL_QA_CREATED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=question.id,
        payload={"official_qa_id": str(official_qa.id), "answer_id": str(answer.id)},
    )
    return official_qa


async def suspend(db: AsyncSession, official_qa: OfficialQA, *, project_id: UUID) -> int:
    """공식 Q&A 를 재검토로 내린다 (D7). 돌려주는 값은 **함께 내려간 재사용 답변 수**다.

    `under_review` 동안 재사용은 멈춘다 (`retrieval.search_official_qa` 의 status 조건).
    """
    if official_qa.status == OFFICIAL_QA_STATUS_UNDER_REVIEW:
        return 0

    official_qa.status = OFFICIAL_QA_STATUS_UNDER_REVIEW
    await db.flush()

    await event_service.record_event(
        db,
        project_id=project_id,
        type=event_service.EVENT_OFFICIAL_QA_SUSPENDED,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=(await _source_question_id(db, official_qa)),
        payload={"official_qa_id": str(official_qa.id)},
    )

    # D21 — 재사용으로 퍼진 답변도 함께 내린다.
    reused = await _reused_answers(db, official_qa.id)
    for answer in reused:
        if answer.state == ANSWER_STATE_VERIFIED:
            answer.state = ANSWER_STATE_UNDER_REVIEW
            await _notify_answer_updated(db, answer)
    await db.flush()
    return len(reused)


async def restore(db: AsyncSession, official_qa: OfficialQA, *, project_id: UUID) -> int:
    """재검토가 해소되어 다시 재사용 가능해진다. 돌려주는 값은 함께 복귀한 재사용 답변 수다.

    ⚠️ 재사용 답변의 본문을 **현재 원문으로 맞춘다** (D21). 담당자가 수정으로 해소했다면
    그 정정이 이미 재사용된 답변에도 반영돼야 한다 — 재번역이 아니라 확정 원문의 복사다.
    """
    official_qa.status = OFFICIAL_QA_STATUS_ACTIVE
    await db.flush()

    reused = await _reused_answers(db, official_qa.id)
    for answer in reused:
        # **본문이 실제로 바뀐 사본에만** 정정 알림을 보낸다 (D21). 유지(`keep`)로 해소된
        # 경우는 원문이 그대로이므로 "정정되었습니다"가 거짓이 된다.
        corrected = answer.content_ko != official_qa.answer_ko
        restored = answer.state == ANSWER_STATE_UNDER_REVIEW

        answer.content_ko = official_qa.answer_ko
        answer.content_en = official_qa.answer_en
        if restored:
            answer.state = ANSWER_STATE_VERIFIED

        # 바뀐 게 없으면 알리지 않는다 — 재확정마다 모든 사본에 갱신 신호를 뿌리면
        # 프론트가 무의미한 재조회를 반복한다 (`05 §12.2` 는 수신 시 전량 재조회를 규정한다).
        if not (corrected or restored):
            continue

        await _notify_answer_updated(db, answer)
        if corrected:
            question = await db.get(Question, answer.question_id)
            if question is not None:
                await notification_service.notify_answer_resolved(
                    db,
                    question=question,
                    answer=answer,
                    type=NOTIFICATION_ANSWER_CORRECTED,
                )
    await db.flush()
    return len(reused)


async def _notify_answer_updated(db: AsyncSession, answer: Answer) -> None:
    """`05 §12.3` `answer.updated` — 재검토·복귀 전이를 질문자에게 알린다.

    `04 §4` 의 알림 타입 어휘에는 "재검토로 내려갔다"에 해당하는 것이 없다(§11 이 어휘를 닫아
    두었고 계약에 없는 타입을 만들지 않는다). §12.3 이 `answer.updated` 의 수신자를 질문자로,
    사유를 "확정/정정/반려/**재검토**"로 명시하므로 이 이벤트가 그 통지 경로다.
    """
    question = await db.get(Question, answer.question_id)
    if question is None:  # FK 가 보장한다.
        return
    sse_manager.queue_answer_updated(
        db,
        asker_id=question.asker_id,
        question_id=question.id,
        answer_id=answer.id,
        state=answer.state,
    )


async def archive(
    db: AsyncSession, official_qa: OfficialQA, *, project_id: UUID, actor_id: UUID | None = None
) -> datetime:
    """`archived` 전이 (`04 §2`) — 물리 삭제가 아니다. 돌려주는 값은 `05 §9` 의 `archived_at`.

    경로는 둘뿐이다: `DELETE /official-qas/{id}` 와 원본 문서 soft delete (D20).
    재사용·유사 첨부·검색에서 완전히 빠지며 **MVP 에서는 되돌리지 않는다**.
    이미 이 Q&A 를 근거로 발행된 답변은 그대로 남는다 (`05 §9` 이력 보존).

    ⚠️ 시각을 `updated_at` 에서 읽지 않는다 — `onupdate=func.now()` 는 서버 표현식이라
    flush 직후 속성 접근이 동기 IO(`MissingGreenlet`)를 부른다. 여기서 찍어 돌려준다.
    """
    archived_at = datetime.now(UTC)
    if official_qa.status == OFFICIAL_QA_STATUS_ARCHIVED:
        return archived_at

    official_qa.status = OFFICIAL_QA_STATUS_ARCHIVED
    await db.flush()

    await event_service.record_event(
        db,
        project_id=project_id,
        type=event_service.EVENT_OFFICIAL_QA_ARCHIVED,
        actor_id=actor_id,
        entity_type=event_service.ENTITY_QUESTION,
        entity_id=(await _source_question_id(db, official_qa)),
        payload={"official_qa_id": str(official_qa.id)},
    )
    return archived_at


async def _reused_answers(db: AsyncSession, official_qa_id: UUID) -> list[Answer]:
    """이 Q&A 를 `official_qa_id` 로 참조하는 **재사용 답변**들 (D21).

    ⚠️ `source='reused'` 로 좁힌다. 확정 시 편입으로 같은 값이 채워진 원본 답변(`generated`)은
    사본이 아니라 원천이므로 대상이 아니다.
    """
    rows = await db.scalars(
        select(Answer).where(
            Answer.official_qa_id == official_qa_id,
            Answer.source == ANSWER_SOURCE_REUSED,
        )
    )
    return list(rows.all())


async def _source_question_id(db: AsyncSession, official_qa: OfficialQA) -> UUID | None:
    """이벤트를 붙일 질문 (`05 §13` 타임라인은 질문 스코프로 조회된다)."""
    return await db.scalar(
        select(Answer.question_id).where(Answer.id == official_qa.source_answer_id)
    )


# --- 조회 API (`05 §9`) ------------------------------------------------------------------
def _visible_query(project_id: UUID) -> Select[tuple[OfficialQA]]:
    """`archived` 는 목록·검색에 기본으로 나타나지 않는다 (`05 §9`)."""
    return select(OfficialQA).where(
        OfficialQA.project_id == project_id,
        OfficialQA.status != OFFICIAL_QA_STATUS_ARCHIVED,
    )


async def list_official_qas(
    db: AsyncSession,
    *,
    project_id: UUID,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> OfficialQAListResponse:
    """목록/검색 — `query` 는 **키워드 매칭**이다 (`05 §9`).

    벡터 검색이 아니다. 재사용 판정(`06 §2` ②)만 임베딩을 쓰며, 여기는 담당자가 확정 지식을
    눈으로 훑는 화면이다.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)

    stmt = _visible_query(project_id)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                OfficialQA.question_ko.ilike(pattern),
                OfficialQA.question_en.ilike(pattern),
                OfficialQA.answer_ko.ilike(pattern),
                OfficialQA.answer_en.ilike(pattern),
            )
        )

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(
        stmt.order_by(OfficialQA.created_at.desc(), OfficialQA.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return OfficialQAListResponse(
        items=[_to_list_item(row) for row in rows.all()],
        total=total,
        limit=limit,
        offset=offset,
    )


async def load_official_qa(db: AsyncSession, official_qa_id: UUID) -> OfficialQA:
    official_qa = await db.get(OfficialQA, official_qa_id)
    if official_qa is None:
        raise NotFound()
    return official_qa


async def to_detail(db: AsyncSession, official_qa: OfficialQA) -> OfficialQADetail:
    source_question_id = await _source_question_id(db, official_qa)
    return OfficialQADetail(
        **_to_list_item(official_qa).model_dump(),
        source_answer_id=official_qa.source_answer_id,
        # 편입은 항상 확정된 답변에서 파생되므로(`04 §2`) 질문이 없을 수 없다.
        source_question_id=source_question_id,  # type: ignore[arg-type]
    )


def _to_list_item(official_qa: OfficialQA) -> OfficialQAListItem:
    return OfficialQAListItem(
        id=official_qa.id,
        question_ko=official_qa.question_ko,
        question_en=official_qa.question_en,
        answer_ko=official_qa.answer_ko,
        answer_en=official_qa.answer_en,
        status=official_qa.status,  # type: ignore[arg-type]
        correct_count=official_qa.correct_count,
        reuse_count=official_qa.reuse_count,
        created_at=official_qa.created_at,
    )


def archived_response(official_qa: OfficialQA, archived_at: datetime) -> OfficialQAArchived:
    return OfficialQAArchived(
        id=official_qa.id,
        status=official_qa.status,  # type: ignore[arg-type]
        archived_at=archived_at,
    )
