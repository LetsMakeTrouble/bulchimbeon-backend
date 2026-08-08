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
터져도 스케줄러는 살아 있어야 하므로 예외를 잡아 로그만 남긴다.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import AsyncSessionLocal
from app.services import briefing_dispatch_service, sweeper_service

logger = logging.getLogger(__name__)

# `06 §4` 주기표. 만료 스위퍼 10분 · 좀비 회수 5분 · 아침 브리핑 매 정시 체크.
EXPIRY_SWEEPER_MINUTES = 10
ZOMBIE_RECOVERY_MINUTES = 5
BRIEFING_MINUTES = 60

JOB_EXPIRY_SWEEPER = "expiry_sweeper"
JOB_ZOMBIE_RECOVERY = "zombie_recovery"
JOB_BRIEFING = "briefing"


async def run_expiry_sweeper() -> None:
    """72h 무처리 초안을 `expired` 로 내린다 (D14). 살아 있는 카드 밑은 건드리지 않는다."""
    try:
        async with AsyncSessionLocal() as db:
            expired = await sweeper_service.expire_stale_drafts(db)
            await db.commit()
        if expired:
            logger.info("만료 스위퍼 잡: %s건", len(expired))
    except Exception:
        # 잡 하나의 실패로 스케줄러가 멈추면 좀비 회수까지 함께 죽는다.
        logger.exception("만료 스위퍼 잡 실패")


async def run_zombie_recovery() -> None:
    """`processing` 에 멈춘 질문을 `failed` + 실패 카드로 회수한다 (D23)."""
    try:
        async with AsyncSessionLocal() as db:
            recovered = await sweeper_service.recover_zombie_questions(db)
            await db.commit()
        if recovered:
            logger.warning("좀비 회수 잡: %s건", len(recovered))
    except Exception:
        logger.exception("좀비 회수 잡 실패")


async def run_briefing() -> None:
    """담당자 `briefing_hour` 에 도달한 프로젝트에 브리핑을 보낸다 (`06 §4`).

    ⚠️ **커밋이 필수다.** `sse_manager` 는 아웃박스라 `briefing.ready` SSE 가 커밋 직후에야
    나간다 — 커밋을 빠뜨리면 알림함 레코드까지 통째로 사라지고 로그도 남지 않는다.
    """
    try:
        async with AsyncSessionLocal() as db:
            sent = await briefing_dispatch_service.dispatch_due_briefings(db)
            await db.commit()
        if sent:
            logger.info("브리핑 잡: 프로젝트 %s개", len(sent))
    except Exception:
        logger.exception("브리핑 잡 실패")


def create_scheduler() -> AsyncIOScheduler:
    """잡을 등록한 스케줄러를 돌려준다. 기동·정지는 앱 lifespan 이 한다 (`03 §3`).

    `interval` 트리거는 첫 실행이 **기동 + 주기 후**다. 재시작이 잦은 배포에서 매 부팅마다
    전체 스캔이 도는 것을 막아 주므로 그대로 둔다.
    """
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
        trigger="interval",
        # `06 §4` "매 정시 체크". 첫 실행이 기동 + 60분이라 틱이 정각에 딱 떨어지지 않지만,
        # 발송 판정이 "정각 일치"가 아니라 "`briefing_hour` 도달 이후"라 그날 브리핑은 늦어도
        # 다음 틱에 나간다 (`briefing_dispatch_service` 독스트링).
        minutes=BRIEFING_MINUTES,
        id=JOB_BRIEFING,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
