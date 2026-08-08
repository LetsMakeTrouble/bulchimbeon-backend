"""M7 지표·타임라인 테스트 헬퍼.

> ### 왜 이벤트를 **직접 주입**하는가
> 지표는 `events` 테이블이 원천이다 (룰 4, `02 §10`). 기대값을 정확히 단언하려면 창 안에
> 어떤 이벤트가 몇 건 있는지를 테스트가 통제해야 하는데, 파이프라인을 태워 만들면
> "🟡 을 12건 만든다" 같은 조립에 시간이 다 들어가고 30초 경계·30일 창처럼 **시각이 핵심인**
> 케이스는 아예 만들 수 없다.
>
> 파이프라인이 실제로 이벤트를 남기는지는 M3~M6 테스트와 `test_events_timeline.py` 의
> 실주행 타임라인이 확인한다. 이 헬퍼는 **집계식**을 검증하기 위한 것이다.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.question import ANSWER_STATE_DRAFT, Answer, Question
from app.services import event_service
from tests.helpers import API, Actor


def as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(value)


async def emit(
    db: AsyncSession,
    *,
    project_id: str | UUID,
    type: str,
    created_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    actor_id: str | UUID | None = None,
    entity_id: str | UUID | None = None,
    entity_type: str | None = event_service.ENTITY_QUESTION,
) -> Event:
    """이벤트 한 건을 원하는 시각으로 심는다.

    `created_at` 을 주지 않으면 `clock_timestamp()` 가 찍힌다 — 그래야 같은 트랜잭션에서
    심은 이벤트들의 순서가 남는다 (`models/event.py`).
    """
    event = Event(
        project_id=as_uuid(project_id),
        actor_id=as_uuid(actor_id) if actor_id is not None else None,
        type=type,
        entity_type=entity_type,
        entity_id=as_uuid(entity_id) if entity_id is not None else None,
        payload=payload or {},
    )
    if created_at is not None:
        event.created_at = created_at
    db.add(event)
    await db.flush()
    return event


async def emit_graded(
    db: AsyncSession,
    *,
    project_id: str | UUID,
    grade: str,
    count: int = 1,
    created_at: datetime | None = None,
) -> None:
    """`question.graded` 를 `count` 건. 자동응답률·등급 분포·절약 대기시간의 원천이다."""
    for _ in range(count):
        await emit(
            db,
            project_id=project_id,
            type=event_service.EVENT_QUESTION_GRADED,
            created_at=created_at,
            entity_id=uuid4(),
            payload={"grade": grade, "matching_rate": None, "elapsed_ms": 1200},
        )


async def emit_card_handled(
    db: AsyncSession,
    *,
    project_id: str | UUID,
    viewed_at: datetime,
    after_seconds: float | None,
    action_type: str = event_service.EVENT_CARD_APPROVED,
) -> UUID:
    """카드 하나의 `card.viewed` + 액션. `after_seconds=None` 이면 아직 처리하지 않은 카드다.

    `after_seconds` 가 음수면 "열기 전에 이미 액션이 있었다"를 만든다 — 목록에서 바로
    `defer` 한 뒤 나중에 카드를 연 경우다.
    """
    card_id = uuid4()
    await emit(
        db,
        project_id=project_id,
        type=event_service.EVENT_CARD_VIEWED,
        created_at=viewed_at,
        entity_id=uuid4(),
        payload={"card_id": str(card_id), "reason": "yellow"},
    )
    if after_seconds is not None:
        await emit(
            db,
            project_id=project_id,
            type=action_type,
            created_at=viewed_at + timedelta(seconds=after_seconds),
            entity_id=uuid4(),
            payload={"card_id": str(card_id)},
        )
    return card_id


async def seed_answers(
    db: AsyncSession,
    *,
    project_id: str | UUID,
    asker_id: str | UUID,
    grade: str,
    count: int,
    state: str = ANSWER_STATE_DRAFT,
) -> None:
    """등급별 정확도(D25)의 **분모**를 만든다 — 원천이 `answers` 행이라 직접 심는다.

    `accuracy_service` 가 `answers` 를 세는 유일한 지표다(나머지는 events). 표본 30 경계를
    테스트하려면 30건 근처를 만들어야 하므로 파이프라인으로는 현실적이지 않다.
    """
    for index in range(count):
        question = Question(
            project_id=as_uuid(project_id),
            asker_id=as_uuid(asker_id),
            content_ko=f"표본 질문 {index}",
            status="answered",
        )
        db.add(question)
        await db.flush()
        db.add(
            Answer(
                question_id=question.id,
                grade=grade,
                matching_rate=None,
                search_score=None,
                grounding_score=None,
                sim_raw=None,
                held_reason=None,
                question_struct=None,
                state=state,
                content_ko="본문",
                content_en="body",
                expires_at=None,
            )
        )
    await db.flush()


async def get_metrics(
    client: AsyncClient, actor: Actor, project_id: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"{API}/projects/{project_id}/metrics", params=params or None, headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def get_timeseries(
    client: AsyncClient, actor: Actor, project_id: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"{API}/projects/{project_id}/metrics/timeseries",
        params=params or None,
        headers=actor.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def get_events(
    client: AsyncClient, actor: Actor, project_id: str, **params: Any
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{API}/projects/{project_id}/events", params=params or None, headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def now() -> datetime:
    return datetime.now(UTC)
