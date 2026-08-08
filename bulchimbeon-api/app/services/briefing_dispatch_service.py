"""브리핑 발송 잡의 본체 — 보류 알림 flush + `briefing.ready` (`06 §4`, 결정 1.11).

> ### 중복 발송은 **락이 아니라 제약**으로 막는다 (TOCTOU 방지)
> ```sql
> INSERT INTO briefing_runs (id, project_id, run_date, sent_at)
> VALUES (:id, :pid, :run_date, now())
> ON CONFLICT (project_id, run_date) DO NOTHING;
> ```
> **`rowcount == 1` 일 때만 실제 발송**하고 `0` 이면 조기 반환한다. `SELECT` 로 "오늘 보냈나?"를
> 먼저 보고 분기하면 확인과 INSERT 사이가 벌어져 재시작·수동 트리거와 겹칠 때 두 번 나간다
> (`04 §7`, `models/briefing_run.py`). `run_date` 는 **담당자 `users.timezone` 기준 날짜**다.
>
> ⚠️ APScheduler 는 프로세스마다 중복 발화하므로 지금은 `--workers 1` 전제다 (룰 9).
> *확장 시점의 정답*(범위 밖): 스케줄러를 별도 프로세스로 분리하고 잡 단위 상호배제는
> `pg_try_advisory_lock`. 그때도 이 제약은 그대로 두 번째 방어선으로 남는다.

> ### "브리핑 시각에 도달했는가"는 `dnd.next_briefing_at` 으로 답할 수 없다
> 그 함수는 **항상 미래**를 돌려준다 — `defer` 기본 만기(D15)가 그것을 요구하기 때문이다.
> 여기서 필요한 판정은 "담당자 현지 날짜의 `briefing_hour` 를 이미 지났는가"이므로 현지 시각을
> 직접 본다. 공유하는 것은 그 함수가 아니라 `dnd.zone()` 과 `briefing_hour` 를 접는 방식이다.
>
> 판정을 **정각 일치가 아니라 "도달 이후"** 로 두는 이유: 잡이 매 정시에 돌더라도 재시작·지연된
> 틱 때문에 09:00 정각 체크를 놓칠 수 있는데, 정각 일치면 그날 브리핑이 조용히 통째로 빠진다.
> "도달 이후"로 두면 다음 틱이 그날의 브리핑을 마저 보내고, 하루 한 번이라는 보장은 시각 비교가
> 아니라 `UNIQUE(project_id, run_date)` 가 이미 하고 있다.

> ### 그 "도달 이후"가 **DND 한복판으로 새어 들어가면** 안 된다 (룰 6)
> "도달 이후"는 몇 시든 참이므로, 그날 브리핑을 놓친 채 서버가 현지 21:30 에 뜨면 첫 틱(기동
> +60분)인 22:30 에 `briefing.ready` 와 밀린 알림이 한꺼번에 터진다. 룰 6 은 "담당자의 방해
> 금지 시간에는 **어떤 즉시 알림도 보내지 않는다**"이다.
>
> 그래서 억제 대상은 **따라잡기 발송뿐**이다. 담당자가 `briefing_hour` 를 **스스로** DND 안에
> 넣어 뒀다면 그건 본인 선택이므로 그대로 보낸다 — 그 경우까지 막으면 브리핑이 영영 나가지
> 않는다. 판정은 "지금이 DND 안이고 **`briefing_hour` 자체는 DND 밖**"일 때만 건너뛴다.

시각을 인자로 받는 이유는 `sweeper_service` 와 같다 — 테스트가 브리핑 시각을 기다릴 수 없다.
"""

import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_SETTINGS
from app.models.briefing_run import BriefingRun
from app.models.project import Project
from app.models.user import User
from app.services import notification_service, sse_manager
from app.services.pipeline import dnd

logger = logging.getLogger(__name__)


async def dispatch_due_briefings(db: AsyncSession, *, now: datetime | None = None) -> list[UUID]:
    """브리핑 시각에 도달한 프로젝트에 브리핑을 발송한다. 돌려주는 값은 발송한 프로젝트 id 들이다.

    트랜잭션 경계는 호출자(스케줄러 잡)다 — 여기서는 `flush()` 까지만 한다. ⚠️ SSE 는 아웃박스라
    호출자가 커밋해야 실제로 나간다 (`sse_manager` 독스트링).
    """
    now = now or datetime.now(UTC)

    projects = list((await db.scalars(select(Project))).all())
    sent: list[UUID] = []
    for project in projects:
        if await _dispatch_project(db, project=project, now=now):
            sent.append(project.id)

    if sent:
        logger.info("브리핑 발송: 프로젝트 %s개", len(sent))
    return sent


async def _dispatch_project(db: AsyncSession, *, project: Project, now: datetime) -> bool:
    """프로젝트 하나. 실제로 발송했으면 True."""
    answerer = await db.get(User, project.answerer_id)
    timezone_name = answerer.timezone if answerer is not None else dnd.FALLBACK_TIMEZONE

    # 룰 3 — 임계값은 `projects.settings` 에서 로드하고 기본값은 `DEFAULT_SETTINGS` 한 곳이다.
    def setting(key: str) -> Any:
        return project.settings.get(key, DEFAULT_SETTINGS[key])

    # 범위 밖 값을 `dnd.next_briefing_at` 과 **같은 방식으로** 접는다. 접는 방식이 갈리면 `defer`
    # 만기는 23시로, 여기는 "영영 발송 안 함"으로 조용히 어긋난다.
    briefing_hour = min(23, max(0, int(setting("briefing_hour"))))

    # ⚠️ 깨진 타임존을 UTC 로 떨어뜨리는 판정은 `dnd.zone` 하나만 쓴다. 여기서 따로 만들면
    #    DND 와 브리핑이 서로 다른 날짜를 볼 수 있다 (타임존 단일 원천, 룰 6).
    local = now.astimezone(dnd.zone(timezone_name))
    if local.hour < briefing_hour:
        return False

    if _is_catch_up_inside_dnd(
        local,
        timezone_name=timezone_name,
        briefing_hour=briefing_hour,
        dnd_start=str(setting("dnd_start")),
        dnd_end=str(setting("dnd_end")),
    ):
        return False

    run_date = local.date()
    if not await _claim_run(db, project_id=project.id, run_date=run_date, now=now):
        return False

    await notification_service.flush_pending(
        db, user_id=project.answerer_id, project_id=project.id, now=now
    )
    await notification_service.notify_briefing_ready(db, project=project, run_date=run_date)
    sse_manager.queue_briefing_ready(
        db,
        answerer_id=project.answerer_id,
        project_id=project.id,
        date=run_date.isoformat(),
    )
    await db.flush()
    return True


def _is_catch_up_inside_dnd(
    local: datetime,
    *,
    timezone_name: str,
    briefing_hour: int,
    dnd_start: str,
    dnd_end: str,
) -> bool:
    """지금 보내면 **DND 를 침범하는 따라잡기 발송**인가 (룰 6, 모듈 독스트링).

    두 조건이 함께 참일 때만 True 다.

    1. **지금이 DND 안이다.** 룰 6 은 이 구간에 어떤 즉시 알림도 금지한다 — `briefing.ready`
       하나가 아니라 `flush_pending` 이 함께 푸는 밀린 알림 전부가 이 구간에 쏟아진다.
    2. **`briefing_hour` 자체는 DND 밖이다.** 담당자가 브리핑 시각을 스스로 DND 안에 넣어
       뒀다면 그건 본인 선택이므로 억제 대상이 아니다. 이 조건이 없으면 그런 설정에서 브리핑이
       영영 나가지 않는다.

    억제된 발송은 **사라지지 않는다.** `briefing_runs` 를 아직 따내지 않았으므로 DND 가 끝난 뒤
    첫 틱이 그 시점의 `run_date` 로 보낸다 — 자정을 넘는 DND(기본 `22:00~07:00`)라면 다음 날
    몫으로 합쳐진다. 밀린 알림도 `deliver_after <= now` 가 계속 참이라 그때 함께 풀린다
    (`notification_service.flush_pending`). 룰 6 의 "인박스는 어떤 경우에도 사라지지 않는다"가
    지켜지는 근거가 이것이다.

    ⚠️ `local` 은 이미 담당자 타임존으로 변환된 시각이다. `briefing_hour` 판정 시각을 그 위에서
    만들어야 날짜가 어긋나지 않는다.
    """
    if not dnd.in_dnd_window(
        local, timezone_name=timezone_name, dnd_start=dnd_start, dnd_end=dnd_end
    ):
        return False

    briefing_moment = local.replace(hour=briefing_hour, minute=0, second=0, microsecond=0)
    return not dnd.in_dnd_window(
        briefing_moment, timezone_name=timezone_name, dnd_start=dnd_start, dnd_end=dnd_end
    )


async def _claim_run(db: AsyncSession, *, project_id: UUID, run_date: date, now: datetime) -> bool:
    """오늘 발송권을 **제약으로** 따낸다 (모듈 독스트링). `rowcount == 1` 일 때만 True."""
    result = await db.execute(
        insert(BriefingRun)
        .values(id=uuid4(), project_id=project_id, run_date=run_date, sent_at=now)
        .on_conflict_do_nothing(constraint="uq_briefing_runs_project_date")
    )
    return result.rowcount == 1
