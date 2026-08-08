"""events 기록 — 모든 상태 변화의 단일 원천 (룰 4, `04 §5`).

M1 이 쓰는 타입은 `member.joined` · `answerer.transferred` 둘이고,
M2 가 `document.version_activated` 를 더한다.
타입 문자열은 `04 §5` 목록에서만 골라 쓴다 — 계약에 없는 타입을 새로 만들지 않는다.
커밋은 호출자(라우터)가 한다 — 이벤트가 그것을 일으킨 변경과 같은 트랜잭션에 묶여야 하기 때문이다.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event

EVENT_MEMBER_JOINED = "member.joined"
EVENT_ANSWERER_TRANSFERRED = "answerer.transferred"
EVENT_DOCUMENT_VERSION_ACTIVATED = "document.version_activated"

# M3 질문 파이프라인 (`04 §5`).
EVENT_QUESTION_CREATED = "question.created"
EVENT_QUESTION_STATUS_CHANGED = "question.status_changed"  # payload: from, to (`04 §6.1` 전 전이)
EVENT_QUESTION_GRADED = "question.graded"
EVENT_ANSWER_PUBLISHED = "answer.published"
EVENT_ANSWER_REUSED = "answer.reused"
# ⚠️ 재질문 즉답률(D26)의 **분모**를 만드는 이벤트다. 게이트 탈락 시 반드시 남긴다 —
#    이게 없으면 분모가 만들어지지 않아 지표가 항상 100% 로 보인다.
EVENT_ANSWER_REUSE_MISSED = "answer.reuse_missed"

# M4 확인 워크플로 (`04 §5`).
#
# ⚠️ 카드·공식 Q&A 이벤트는 **질문 스코프**로 남긴다 — `05 §13` 의 타임라인 조회가
#    `?entity_type=question&entity_id=q-9` 이고 그 표에 `card.*` · `official_qa.*` 가
#    함께 들어 있기 때문이다. 카드/Q&A 식별자는 payload 로 싣는다 (M3 파이프라인과 같은 규약).
EVENT_CARD_CREATED = "card.created"
EVENT_CARD_VIEWED = "card.viewed"  # 카드 처리 시간 지표의 시작점 (`05 §13`)
EVENT_CARD_APPROVED = "card.approved"
EVENT_CARD_EDITED = "card.edited"  # answer-option 확정도 이 타입이다 (`04 §5`)
EVENT_CARD_REJECTED = "card.rejected"
EVENT_CARD_DEFERRED = "card.deferred"
EVENT_CARD_KEPT = "card.kept"  # payload: bulk
EVENT_FEEDBACK_CREATED = "feedback.created"  # payload: verdict — 정정률의 원천
EVENT_OFFICIAL_QA_CREATED = "official_qa.created"
EVENT_OFFICIAL_QA_SUSPENDED = "official_qa.suspended"
EVENT_OFFICIAL_QA_ARCHIVED = "official_qa.archived"
# 문서 스코프다 — 한 번의 연쇄가 여러 질문에 걸치므로 질문에 붙일 수 없다.
EVENT_ANSWERS_REVIEW_CASCADE = "answers.review_cascade"  # payload: count

ENTITY_QUESTION = "question"
ENTITY_ANSWER = "answer"
ENTITY_DOCUMENT = "document"
ENTITY_DOCUMENT_VERSION = "document_version"


async def record_event(
    db: AsyncSession,
    *,
    project_id: UUID,
    type: str,
    actor_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """이벤트 한 건을 세션에 적재한다. `actor_id=None` 은 system 행위다."""
    event = Event(
        project_id=project_id,
        actor_id=actor_id,
        type=type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload or {},
    )
    db.add(event)
    return event
