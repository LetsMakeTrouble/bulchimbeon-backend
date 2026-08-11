"""LLM 사용량·비용 집계 (`llm_usage`).

"누가 얼마를 썼나"에 답하는 자리다. 적재는 `services/llm/usage` 가 하고 여기서는 읽기만 한다.

⚠️ **`reasoning_tokens` 를 합계에 더하지 않는다.** Responses API 의 `output_tokens` 에 이미
포함된 값이라 더하면 이중 계상이다. 별도 컬럼으로 두는 것은 "생각에 얼마나 썼나"를 따로
보기 위해서지 합산 대상이라서가 아니다.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage import LLMUsage
from app.models.user import User


@dataclass(frozen=True)
class UsageTotals:
    calls: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class UsageByActor:
    """`user_id` 가 NULL 인 행(스케줄러 경유 등)은 `user_id=None` 한 묶음으로 모인다."""

    user_id: UUID | None
    user_name: str | None
    totals: UsageTotals


@dataclass(frozen=True)
class UsageByStep:
    step: str
    totals: UsageTotals


def _totals(row: object) -> UsageTotals:
    return UsageTotals(
        calls=int(getattr(row, "calls", 0) or 0),
        input_tokens=int(getattr(row, "input_tokens", 0) or 0),
        output_tokens=int(getattr(row, "output_tokens", 0) or 0),
        reasoning_tokens=int(getattr(row, "reasoning_tokens", 0) or 0),
        cost_usd=Decimal(getattr(row, "cost_usd", 0) or 0),
    )


_AGGREGATES = (
    func.count().label("calls"),
    func.coalesce(func.sum(LLMUsage.input_tokens), 0).label("input_tokens"),
    func.coalesce(func.sum(LLMUsage.output_tokens), 0).label("output_tokens"),
    func.coalesce(func.sum(LLMUsage.reasoning_tokens), 0).label("reasoning_tokens"),
    func.coalesce(func.sum(LLMUsage.cost_usd), 0).label("cost_usd"),
)


def _scoped(project_id: UUID, since: datetime | None, until: datetime | None) -> list[object]:
    where: list[object] = [LLMUsage.project_id == project_id]
    if since is not None:
        where.append(LLMUsage.created_at >= since)
    if until is not None:
        where.append(LLMUsage.created_at < until)
    return where


async def project_totals(
    db: AsyncSession,
    *,
    project_id: UUID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageTotals:
    row = (await db.execute(select(*_AGGREGATES).where(*_scoped(project_id, since, until)))).one()
    return _totals(row)


async def by_actor(
    db: AsyncSession,
    *,
    project_id: UUID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[UsageByActor]:
    """계정별 사용량. 많이 쓴 순으로 돌려준다 — 비용을 볼 때 알고 싶은 순서다."""
    statement = (
        select(LLMUsage.user_id, User.name, *_AGGREGATES)
        .outerjoin(User, User.id == LLMUsage.user_id)
        .where(*_scoped(project_id, since, until))
        .group_by(LLMUsage.user_id, User.name)
        .order_by(func.coalesce(func.sum(LLMUsage.cost_usd), 0).desc())
    )
    return [
        UsageByActor(user_id=row.user_id, user_name=row.name, totals=_totals(row))
        for row in (await db.execute(statement)).all()
    ]


async def by_step(
    db: AsyncSession,
    *,
    project_id: UUID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[UsageByStep]:
    """단계별 사용량. 어느 단계가 비용을 먹는지 보는 용도다 (`03 §4.2` 의 단가 표와 같은 축)."""
    statement = (
        select(LLMUsage.step, *_AGGREGATES)
        .where(*_scoped(project_id, since, until))
        .group_by(LLMUsage.step)
        .order_by(func.coalesce(func.sum(LLMUsage.cost_usd), 0).desc())
    )
    return [
        UsageByStep(step=row.step, totals=_totals(row))
        for row in (await db.execute(statement)).all()
    ]
