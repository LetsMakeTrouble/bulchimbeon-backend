"""예약 작업 신뢰성 (운영 전환 항목 C).

지키는 것 셋이다.

1. **기동 직후 1회 실행** — `interval` 첫 발화가 "기동 + 주기"라 이게 없으면 재배포마다
   최대 60분(브리핑)·10분(만료) 공백이 생긴다.
2. **브리핑이 벽시계 기준** — `interval` 이면 기동 시각에 따라 확인 시각이 통째로 밀린다.
3. **실행 기록** — 돌았는지·실패했는지를 사후에 답할 수 있어야 한다.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scheduler
from app.models.job_run import (
    JOB_STATUS_FAILED,
    JOB_STATUS_OK,
    JOB_TRIGGER_INTERVAL,
    JOB_TRIGGER_STARTUP,
    JobRun,
)


async def _runs(db: AsyncSession) -> list[JobRun]:
    rows = await db.scalars(select(JobRun).order_by(JobRun.started_at))
    return list(rows)


def test_briefing_uses_a_wall_clock_trigger() -> None:
    """⛔ 브리핑은 `cron` 이다.

    `interval` 이면 첫 발화가 "기동 + 60분"이라 08:55 에 배포하면 09:00 브리핑을 09:55 에야
    본다. 재배포할 때마다 확인 시각이 밀리는 셈이라 운영에서 못 쓴다.
    """
    jobs = {job.id: job for job in scheduler.create_scheduler().get_jobs()}

    briefing = jobs[scheduler.JOB_BRIEFING]
    assert type(briefing.trigger).__name__ == "CronTrigger", (
        f"브리핑 트리거가 {type(briefing.trigger).__name__} 다 — 기동 시각에 끌려간다"
    )
    # 나머지 둘은 "얼마나 자주"가 중요하고 벽시계 정렬이 필요 없다.
    assert type(jobs[scheduler.JOB_EXPIRY_SWEEPER].trigger).__name__ == "IntervalTrigger"
    assert type(jobs[scheduler.JOB_ZOMBIE_RECOVERY].trigger).__name__ == "IntervalTrigger"


async def test_every_job_run_is_recorded(db_session: AsyncSession) -> None:
    """성공한 실행도 남는다 — "안 돌았다"와 "돌았는데 할 일이 0건이었다"는 다르다."""
    await scheduler.run_expiry_sweeper()

    runs = await _runs(db_session)
    assert len(runs) == 1
    run = runs[0]
    assert run.job_name == scheduler.JOB_EXPIRY_SWEEPER
    assert run.trigger == JOB_TRIGGER_INTERVAL
    assert run.status == JOB_STATUS_OK
    assert run.error is None
    assert run.finished_at >= run.started_at


async def test_a_failing_job_is_recorded_and_does_not_escape(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """잡이 터져도 예외가 스케줄러로 새지 않고, **실패가 기록된다.**

    잡 하나의 실패로 스케줄러가 죽으면 나머지 잡까지 함께 멈춘다. 그런데 조용히 삼키기만
    하면 "왜 안 돌았나"를 영영 알 수 없다 — 그래서 삼키고 **남긴다**.
    """

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("의도적 실패")

    monkeypatch.setattr(scheduler.sweeper_service, "expire_stale_drafts", boom)

    await scheduler.run_expiry_sweeper()  # 예외가 올라오면 이 줄에서 죽는다

    runs = await _runs(db_session)
    assert len(runs) == 1
    assert runs[0].status == JOB_STATUS_FAILED
    assert runs[0].error is not None
    assert "의도적 실패" in runs[0].error


async def test_startup_runs_all_three_jobs_once(db_session: AsyncSession) -> None:
    """기동 직후 세 잡이 **한 번씩** 돌고 `startup` 으로 표시된다.

    표시가 필요한 이유: 배포 직후 밀린 작업이 실제로 따라잡혔는지를 주기 발화와 구분해
    봐야 하기 때문이다.
    """
    await scheduler.run_startup_jobs()

    runs = await _runs(db_session)
    assert {run.job_name for run in runs} == {
        scheduler.JOB_ZOMBIE_RECOVERY,
        scheduler.JOB_EXPIRY_SWEEPER,
        scheduler.JOB_BRIEFING,
    }
    assert {run.trigger for run in runs} == {JOB_TRIGGER_STARTUP}

    # 좀비 회수가 먼저다 — 죽은 프로세스가 남긴 `processing` 을 정리해야 그 결과가
    # 브리핑에 반영된다.
    assert runs[0].job_name == scheduler.JOB_ZOMBIE_RECOVERY
    assert runs[-1].job_name == scheduler.JOB_BRIEFING


async def test_error_message_is_truncated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """스택이 붙은 긴 문자열이 테이블을 채우면 조회가 무거워진다."""

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("x" * (scheduler.MAX_ERROR_LENGTH * 3))

    monkeypatch.setattr(scheduler.sweeper_service, "recover_zombie_questions", boom)

    await scheduler.run_zombie_recovery()

    runs = await _runs(db_session)
    assert runs[0].error is not None
    assert len(runs[0].error) <= scheduler.MAX_ERROR_LENGTH
