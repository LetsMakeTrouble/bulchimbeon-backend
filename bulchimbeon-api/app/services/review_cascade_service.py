"""문서 갱신 재검토 연쇄 (`02` 룰 5, D9·D20, `06 §3`).

> ### 근거가 **바뀌는 것**과 **사라지는 것**은 답변 입장에서 같은 사건이다 (D20)
> 새 버전 활성 전환과 문서 soft delete 는 **동일한 연쇄**를 태운다. 차이는 파생된 공식 Q&A
> 처리뿐이다 — 갱신은 `under_review`(해소되면 돌아온다), 삭제는 `archived`(돌아오지 않는다).

연쇄 대상은 "그 문서의 청크를 근거로 **확정된** 답변"이다. `draft` 는 대상이 아니다 —
아직 지식이 아니고, 72h 뒤 스위퍼가 알아서 만료시킨다 (D14).
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Chunk, Document, DocumentVersion
from app.models.official_qa import OfficialQA
from app.models.project import Project
from app.models.question import (
    ANSWER_STATE_UNDER_REVIEW,
    ANSWER_STATE_VERIFIED,
    Answer,
    AnswerCitation,
    Question,
)
from app.models.review_card import CARD_REASON_DOC_UPDATE
from app.services import (
    event_service,
    lesson_service,
    notification_service,
    official_qa_service,
    review_card_service,
    sse_manager,
)

logger = logging.getLogger(__name__)


async def cascade_for_document(
    db: AsyncSession,
    *,
    document: Document,
    trigger_version_id: UUID | None,
    exclude_version_id: UUID | None = None,
    archive_official_qas: bool = False,
    actor_id: UUID | None = None,
) -> int:
    """재검토 연쇄를 실행하고 **영향받은 확정 답변 수**를 돌려준다.

    그 수가 `05 §4` 활성 전환 응답의 `review_cascade_count` 이며 담당자 알림 문구
    ("이 문서를 근거로 한 확정 답변 N건이 재검토 대상입니다")의 N 이다.

    `trigger_version_id` 는 카드의 묶음 키다 (`04 §2`). 활성 전환이면 **새로 활성화된 버전**,
    soft delete 면 사라지는 시점의 활성 버전이다 — 어느 쪽이든 "재검토를 유발한 근거"를
    가리키며, 이 값이 없으면 `bulk-keep` 으로 묶음을 한 번에 유지할 수 없다.
    """
    answers = await _verified_answers_citing(
        db, document_id=document.id, exclude_version_id=exclude_version_id
    )
    if not answers:
        return 0

    for answer in answers:
        answer.state = ANSWER_STATE_UNDER_REVIEW
        question = await db.get(Question, answer.question_id)
        if question is None:  # FK 가 보장한다.
            continue
        await review_card_service.create_card(
            db,
            question=question,
            answer=answer,
            reason=CARD_REASON_DOC_UPDATE,
            document_version_id=trigger_version_id,
        )
        # 재검토 전이를 질문자에게 알린다 (`05 §12.3` `answer.updated`). `04 §4` 에 재검토용
        # 알림 타입이 없으므로 이 이벤트가 통지 경로다.
        sse_manager.queue_answer_updated(
            db,
            asker_id=question.asker_id,
            question_id=question.id,
            answer_id=answer.id,
            state=ANSWER_STATE_UNDER_REVIEW,
        )
    await db.flush()

    answer_ids = [answer.id for answer in answers]
    await _cascade_official_qas(
        db,
        project_id=document.project_id,
        answer_ids=answer_ids,
        archive=archive_official_qas,
        actor_id=actor_id,
    )

    # 룰 5 — 이 답변들에서 나온 교훈은 **지우지 않고 재확인 표시만** 붙인다. 근거가 바뀐
    # 것과 원칙이 틀린 것은 다른 사건이고, 그 판단은 담당자 몫이다.
    flagged = await lesson_service.flag_needs_recheck(db, answer_ids=answer_ids)
    if flagged:
        logger.info("교훈 %s건에 needs_recheck 표시: document=%s", flagged, document.id)

    await event_service.record_event(
        db,
        project_id=document.project_id,
        type=event_service.EVENT_ANSWERS_REVIEW_CASCADE,
        actor_id=actor_id,
        entity_type=event_service.ENTITY_DOCUMENT,
        entity_id=document.id,
        payload={
            "count": len(answers),
            "document_version_id": str(trigger_version_id) if trigger_version_id else None,
            "archived_official_qas": archive_official_qas,
        },
    )
    # 담당자에게 "이 문서를 근거로 한 확정 답변 N건이 재검토 대상입니다" (룰 5, `04 §4`).
    # 룰 5 가 "직후"를 요구하므로 즉시 발송이며, DND 구간이면 함께 보류된다 (룰 6).
    project = await db.get(Project, document.project_id)
    if project is not None:
        await notification_service.notify_doc_review_needed(
            db,
            project=project,
            document_id=document.id,
            document_title=document.title,
            document_version_id=trigger_version_id,
            count=len(answers),
        )
    return len(answers)


async def _verified_answers_citing(
    db: AsyncSession, *, document_id: UUID, exclude_version_id: UUID | None
) -> list[Answer]:
    """이 문서의 청크를 근거로 **확정된** 답변들.

    ⚠️ 버전이 아니라 **문서** 단위로 잡는다. 답변은 생성 당시의 활성 버전을 인용하므로
    "이전 버전을 근거로 한 답변"과 "이 문서를 근거로 한 답변"은 새 버전을 제외하면 같은
    집합이고, 문서 기준이 과거 버전 이력이 여러 개일 때도 빠짐없이 걸린다.
    """
    versions = select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
    if exclude_version_id is not None:
        versions = versions.where(DocumentVersion.id != exclude_version_id)

    rows = await db.scalars(
        select(Answer)
        .distinct()
        .join(AnswerCitation, AnswerCitation.answer_id == Answer.id)
        .join(Chunk, Chunk.id == AnswerCitation.chunk_id)
        .where(
            Chunk.document_version_id.in_(versions),
            Answer.state == ANSWER_STATE_VERIFIED,
        )
    )
    return list(rows.all())


async def _cascade_official_qas(
    db: AsyncSession,
    *,
    project_id: UUID,
    answer_ids: list[UUID],
    archive: bool,
    actor_id: UUID | None,
) -> None:
    """파생된 공식 Q&A 처리.

    - 버전 갱신: `under_review` — 재사용을 멈추되 담당자가 해소하면 돌아온다 (D7).
    - soft delete: `archived` — 근거 문서가 사라졌으므로 되돌리지 않는다 (D20, `05 §9`).

    두 경우 모두 이 Q&A 를 참조하는 `source='reused'` 답변 처리는 `official_qa_service` 가
    맡는다 (D21).
    """
    rows = await db.scalars(select(OfficialQA).where(OfficialQA.source_answer_id.in_(answer_ids)))
    for official_qa in rows.all():
        if archive:
            await official_qa_service.archive(
                db, official_qa, project_id=project_id, actor_id=actor_id
            )
        else:
            await official_qa_service.suspend(db, official_qa, project_id=project_id)
