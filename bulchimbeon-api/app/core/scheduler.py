"""APScheduler 잡 등록 (`03 §3`, `06 §4`).

> ### ⚠️ APScheduler 는 **프로세스마다 중복 발화한다 → `--workers 1` 전제** (룰 9)
> 워커를 2개로 늘리면 만료 스위퍼·좀비 회수가 워커 수만큼 돈다. 만료 스위퍼는 멱등이라
> (`state='draft'` 조건이 두 번째 실행을 걸러낸다) 피해가 없지만, M6 의 브리핑 발송은 알림이
> 두 번 나간다 — 그쪽은 **락이 아니라 제약**으로 막는다: `briefing_runs` 의
> `UNIQUE(project_id, run_date)` + `INSERT … ON CONFLICT DO NOTHING` → `rowcount == 1` 일 때만
> 발송 (`04 §2`, 결정 1.11).
> *확장 시점의 정답*(범위 밖): 스케줄러를 별도 프로세스로 분리하고 잡 단위 상호배제는
> `pg_try_advisory_lock`.

각 잡은 **자체 세션**을 연다 — 요청 스코프 세션이 없는 실행 경로이기 때문이다. 잡 하나가
터져도 스케줄러는 살아 있어야 하므로 예외를 잡아 기록만 남긴다.

> ### 세 잡은 전부 **상태 파생형**이다 (운영 전환 항목 C)
> 무엇을 할지가 DB 상태에서 매번 다시 계산된다 — 만료는 `expires_at < now`, 좀비는
> `processing` 정체, 브리핑은 `briefing_hour 도달 + 오늘 실행 없음`. 그래서 **다운타임
> 이후 자동으로 소급된다.** 할 일을 쌓아 두는 큐가 필요 없는 이유다.
>
> 종전에 빠져 있던 것은 큐가 아니라 셋이었다:
> 1. **기동 직후 실행이 없었다.** `interval` 의 첫 발화가 "기동 + 주기"라 재배포마다
>    최대 60분(브리핑)·10분(만료) 공백이 생겼다. → `run_startup_jobs()` 신설.
> 2. **브리핑이 기동 시각 기준이었다.** 재배포하면 확인 시각 자체가 밀렸다. → `cron` 으로
>    바꿔 매 정시에 본다.
> 3. **실행 기록이 없었다.** 돌았는지 알 방법이 로그뿐이었다. → `job_runs`.

> ### ⛔ 날짜를 넘긴 다운타임의 브리핑은 **보내지 않는다** (결정, 2026-08-11)
> 브리핑은 "오늘의 인박스 요약"이라 하루가 지나면 정보 가치가 없고, 소급 발송은
> `_is_catch_up_inside_dnd`(너무 늦은 소급을 막는 장치)와 정면으로 충돌한다.
> 대신 `job_runs` 에 실행 이력이 남으므로 "그날 브리핑이 왜 없었나"는 답할 수 있다.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.job_run import (
    JOB_STATUS_FAILED,
    JOB_STATUS_OK,
    JOB_TRIGGER_INTERVAL,
    JOB_TRIGGER_STARTUP,
    JobRun,
)
from app.services import briefing_dispatch_service, sweeper_service

logger = logging.getLogger(__name__)

# `06 §4` 주기표. 만료 스위퍼 10분 · 좀비 회수 5분 · 아침 브리핑 매 정시 체크.
EXPIRY_SWEEPER_MINUTES = 10
ZOMBIE_RECOVERY_MINUTES = 5

JOB_EXPIRY_SWEEPER = "expiry_sweeper"
JOB_ZOMBIE_RECOVERY = "zombie_recovery"
JOB_BRIEFING = "briefing"

# 오류 메시지를 통째로 넣지 않는다 — 스택이 붙은 문자열이 테이블을 채우면 조회가 무거워진다.
MAX_ERROR_LENGTH = 500


async def _record_run(
    *, job_name: str, trigger: str, started_at: datetime, processed: int, error: str | None
) -> None:
    """실행 기록을 **자체 세션**으로 남긴다.

    잡이 쓰던 세션에 얹으면 잡이 롤백될 때 실패 기록까지 사라진다 — 남겨야 할 이유가
    가장 큰 경우가 바로 그때다. 기록이 실패해도 잡을 죽이지 않는다.
    """
    try:
        async with session_factory() as db:
            db.add(
                JobRun(
                    job_name=job_name,
                    trigger=trigger,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status=JOB_STATUS_FAILED if error else JOB_STATUS_OK,
                    processed_count=processed,
                    error=error[:MAX_ERROR_LENGTH] if error else None,
                )
            )
            await db.commit()
    except Exception:
        logger.exception("job_runs 기록 실패: job=%s", job_name)


def _default_session_factory() -> "AsyncSession":
    return AsyncSessionLocal()


# ⚠️ 테스트가 갈아끼우는 지점 (`ingest.session_factory` 와 같은 이유).
session_factory: Callable[[], "AsyncSession"] = _default_session_factory


async def _run_job(
    job_name: str,
    work: Callable[[], Awaitable[int]],
    *,
    trigger: str = JOB_TRIGGER_INTERVAL,
) -> None:
    """잡 하나를 돌리고 결과를 기록한다. 예외는 여기서 멈춘다 — 잡 하나의 실패로
    스케줄러가 죽으면 나머지 잡까지 함께 멈춘다."""
    started_at = datetime.now(UTC)
    processed = 0
    error: str | None = None
    try:
        processed = await work()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("%s 잡 실패", job_name)
    await _record_run(
        job_name=job_name,
        trigger=trigger,
        started_at=started_at,
        processed=processed,
        error=error,
    )


async def _expiry_sweeper_work() -> int:
    """72h 무처리 초안을 `expired` 로 내린다 (D14). 살아 있는 카드 밑은 건드리지 않는다."""
    async with session_factory() as db:
        expired = await sweeper_service.expire_stale_drafts(db)
        await db.commit()
    if expired:
        logger.info("만료 스위퍼 잡: %s건", len(expired))
    return len(expired)


async def _zombie_recovery_work() -> int:
    """`processing` 에 멈춘 질문을 `failed` + 실패 카드로 회수한다 (D23)."""
    async with session_factory() as db:
        recovered = await sweeper_service.recover_zombie_questions(db)
        await db.commit()
    if recovered:
        logger.warning("좀비 회수 잡: %s건", len(recovered))
    return len(recovered)


async def _briefing_work() -> int:
    """담당자 `briefing_hour` 에 도달한 프로젝트에 브리핑을 보낸다 (`06 §4`).

    ⚠️ **커밋이 필수다.** `sse_manager` 는 아웃박스라 `briefing.ready` SSE 가 커밋 직후에야
    나간다 — 커밋을 빠뜨리면 알림함 레코드까지 통째로 사라지고 로그도 남지 않는다.
    """
    async with session_factory() as db:
        sent = await briefing_dispatch_service.dispatch_due_briefings(db)
        await db.commit()
    if sent:
        logger.info("브리핑 잡: 프로젝트 %s개", len(sent))
    return len(sent)


async def run_expiry_sweeper() -> None:
    await _run_job(JOB_EXPIRY_SWEEPER, _expiry_sweeper_work)


async def run_zombie_recovery() -> None:
    await _run_job(JOB_ZOMBIE_RECOVERY, _zombie_recovery_work)


async def run_briefing() -> None:
    await _run_job(JOB_BRIEFING, _briefing_work)


async def run_startup_jobs() -> None:
    """기동 직후 세 잡을 **1회씩** 돌린다 (운영 전환 항목 C).

    ⚠️ 이게 없으면 재배포마다 공백이 생긴다 — `interval` 의 첫 발화가 "기동 + 주기"이기
    때문이다. 잡이 전부 멱등이라(브리핑은 `UNIQUE(project_id, run_date)` 제약이 지킨다)
    한 번 더 도는 것 자체는 무해하다.

    순서는 **좀비 회수 → 만료 → 브리핑**이다. 죽은 프로세스가 남긴 `processing` 질문을
    먼저 정리해야 그 결과가 브리핑에 반영된다.
    """
    await _run_job(JOB_ZOMBIE_RECOVERY, _zombie_recovery_work, trigger=JOB_TRIGGER_STARTUP)
    await _run_job(JOB_EXPIRY_SWEEPER, _expiry_sweeper_work, trigger=JOB_TRIGGER_STARTUP)
    await _run_job(JOB_BRIEFING, _briefing_work, trigger=JOB_TRIGGER_STARTUP)


def create_scheduler() -> AsyncIOScheduler:
    """잡을 등록한 스케줄러를 돌려준다. 기동·정지는 앱 lifespan 이 한다 (`03 §3`)."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_expiry_sweeper,
        trigger="interval",
        minutes=EXPIRY_SWEEPER_MINUTES,
        id=JOB_EXPIRY_SWEEPER,
        replace_existing=True,
        # 프로세스가 멈췄다 깨어날 때 밀린 실행을 한 번으로 합친다.
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_zombie_recovery,
        trigger="interval",
        minutes=ZOMBIE_RECOVERY_MINUTES,
        id=JOB_ZOMBIE_RECOVERY,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_briefing,
        # ⚠️ `interval` 이 아니라 **`cron`** 이다. `interval` 은 첫 발화가 "기동 + 60분"이라
        #    재배포할 때마다 확인 시각이 통째로 밀렸다 — 08:55 에 배포하면 09:00 브리핑을
        #    09:55 에야 본다. `cron` 은 기동 시각과 무관하게 매 정시에 본다.
        #    발송 판정이 "정각 일치"가 아니라 "`briefing_hour` 도달 이후"라 정시에 한 번씩만
        #    확인해도 그날 브리핑은 반드시 나간다 (`briefing_dispatch_service`).
        trigger="cron",
        minute=0,
        id=JOB_BRIEFING,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
