"""답변 상태 가드 + 인용 조회 (`04 §6`, D12·D13, `05 §6`).

M3 은 답변을 **만들기만** 하고 확정·피드백 API 는 M4 가 붙인다. 그럼에도 상태 가드를
지금 두는 이유는, `expired` 가 **종착 상태**라는 사실이 파이프라인 쪽 규약(만료 스위퍼·
재사용 풀)과 한 몸이기 때문이다 — 판정을 M4 로 미루면 두 곳에 흩어진다.

⚠️ 이 모듈은 **의존 그래프의 바닥**이다. 질문 상세(`05 §6`)와 카드 상세(`05 §7`)가 같은
`citations[]` 스키마를 쓰는데 두 서비스가 서로를 부르면 순환 import 가 되므로 여기 둔다.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FeedbackNotAllowed, InvalidCardAction
from app.models.document import Chunk, Document, DocumentVersion
from app.models.question import (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_EXPIRED,
    ANSWER_STATE_VERIFIED,
    Answer,
    AnswerCitation,
)
from app.schemas.question import Citation

# `02` 룰 3 / D12 — 피드백은 `draft` · `verified` 에서만 받는다.
FEEDBACK_ALLOWED_STATES = (ANSWER_STATE_DRAFT, ANSWER_STATE_VERIFIED)


def ensure_confirmable(answer: Answer) -> None:
    """확정(승인·수정·원안유지) 가능 여부 (D13).

    **만료는 "확정 불가 + 상태 표기"다.** 승인·수정 대상에서 제외되며, 만료 답변의 카드를
    처리하려 하면 409 다. 스위퍼가 살아 있는 카드 밑에서 답변을 죽이지 않으므로(D14)
    이 경로에 도달하는 것은 "카드 없이 만료된 답변"뿐이다.

    ⚠️ 코드 선택 근거: `05 §1.4` 의 409 목록에 만료 전용 코드가 없다. 계약서에 없는 코드를
    새로 만들지 않는다는 규약(`05` 는 프론트와의 계약)에 따라, 의미가 가장 가까운
    `INVALID_CARD_ACTION`("이 카드에 유효하지 않은 액션")을 쓴다.
    """
    if answer.state == ANSWER_STATE_EXPIRED:
        raise InvalidCardAction("만료된 답변은 확정할 수 없습니다.")


def ensure_feedback_allowed(answer: Answer) -> None:
    """피드백 허용 상태 검사 (D12).

    `expired` · `rejected` · `under_review` 는 409 `FEEDBACK_NOT_ALLOWED` 다.
    """
    if answer.state not in FEEDBACK_ALLOWED_STATES:
        raise FeedbackNotAllowed()


async def citations_for_answer(db: AsyncSession, answer_id: UUID) -> list[Citation]:
    """`05 §6` `citations[]` — 열람 URL 을 만들 수 있도록 문서·버전 id 를 함께 싣는다.

    카드 상세(`05 §7`)의 `draft_answer.citations[]` 도 **동일 스키마**이므로 같은 함수를 쓴다.
    """
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
