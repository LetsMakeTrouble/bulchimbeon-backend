"""events 기록 — 모든 상태 변화의 단일 원천 (룰 4, `04 §5`).

M1 이 쓰는 타입은 `member.joined` · `answerer.transferred` 둘이다.
타입 문자열은 `04 §5` 목록에서만 골라 쓴다 — 계약에 없는 타입을 새로 만들지 않는다.
커밋은 호출자(라우터)가 한다 — 이벤트가 그것을 일으킨 변경과 같은 트랜잭션에 묶여야 하기 때문이다.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event

EVENT_MEMBER_JOINED = "member.joined"
EVENT_ANSWERER_TRANSFERRED = "answerer.transferred"


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
