"""브리핑 스케줄러 — 보류 알림 flush + `briefing.ready` (`06 §4`, 결정 1.11).

DoD (`prompts/06-briefing-lessons.md` 완료 기준):
- **시간 주입**: `briefing_hour` 도달 → 보류 알림 flush + `briefing.ready` 발송
- **중복 발송 방지**: 같은 `run_date` 로 **연속 2회 호출**해도 알림은 1건. 두 번째 호출이
  `ON CONFLICT DO NOTHING` 으로 `rowcount == 0` 이 되어 조기 반환하는지 단언한다
- `briefing_hour` 이전에는 발송하지 않는다
- 담당자 타임존이 다르면 발송 기준일·시각이 그 타임존을 따른다
- **밀린 브리핑이 DND 를 침범하지 않는다** (룰 6). 단, 담당자가 `briefing_hour` 를 스스로
  DND 안에 넣어 뒀다면 그건 본인 선택이므로 그대로 발송한다

> ### ⚠️ 시각은 전부 **절대값으로 주입**한다 (freezegun 을 쓰지 않는다)
> `build_team` 의 담당자는 `timezone="UTC"` 이고 기본 `briefing_hour` 는 9 다. 실제 시계에
> 기대면 **09:00 UTC 에 돌 때만 통과하는** 테스트가 된다 — `tests/notification_helpers.py`
> 독스트링이 DND 창으로 같은 부류의 실패를 적어 뒀다. 보류 알림의 `deliver_after` 도 발행
> 시점의 실제 시계로 계산되므로 주입 시각에 맞춰 되돌려 둔다.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.briefing_run import BriefingRun
from app.models.notification import (
    NOTIFICATION_BRIEFING_READY,
    NOTIFICATION_CARD_CREATED,
    Notification,
)
from app.models.user import User
from app.services import briefing_dispatch_service, sse_manager
from tests.notification_helpers import inbox, stored_notifications
from tests.pipeline_helpers import as_uuid, patch_project_settings
from tests.review_helpers import YELLOW_MARKER, Team, ask_until_card, build_team, seed_evidence

# 보류 알림이 풀리는 시각 — 담당자 타임존 UTC 기준 브리핑 시각(09:00)이다.
HELD_UNTIL = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
# 그 하루의 `briefing_hour` 를 지난 시각. "정각 일치"가 아니라 "도달 이후"가 판정 기준이다.
AT_BRIEFING_HOUR = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)
BEFORE_BRIEFING_HOUR = datetime(2026, 3, 4, 8, 59, tzinfo=UTC)
RUN_DATE = date(2026, 3, 4)

# 담당자 타임존이 UTC 가 아닐 때. 2026-03-04/05 는 미국 서머타임(3/8) 전이라 NY = UTC-5 다.
NEW_YORK = "America/New_York"
# UTC 로는 **3/5** 인데 뉴욕 현지로는 3/4 21:30 이다 — 발송 기준일이 갈리는 순간.
NEW_YORK_EVENING = datetime(2026, 3, 5, 2, 30, tzinfo=UTC)
# 뉴욕 현지 07:30 — 아직 `briefing_hour` 전이다.
NEW_YORK_MORNING = datetime(2026, 3, 4, 12, 30, tzinfo=UTC)


async def _team_with_held_notifications(
    client: AsyncClient, db: AsyncSession, domain: str, count: int
) -> tuple[Team, list[Notification]]:
    """비긴급 카드 알림 `count` 건을 **보류 상태로** 만들어 둔 팀 (룰 6).

    보류 알림은 알림함 API 로는 보이지 않으므로 DB 행을 직접 들고 다닌다.
    """
    team = await build_team(client, domain)
    for index in range(count):
        content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요? ({index})"
        await seed_evidence(db, team, content_ko, title=f"Refund Policy {index}")
        await ask_until_card(client, db, team, content_ko)

    held = await stored_notifications(db, team.owner.id, type=NOTIFICATION_CARD_CREATED)
    assert len(held) == count
    for notification in held:
        assert notification.deliver_after is not None, (
            "비긴급 카드 알림은 다음 브리핑까지 보류다 (룰 6)"
        )
        notification.deliver_after = HELD_UNTIL
    await db.commit()
    return team, held


async def _team_with_held_notification(
    client: AsyncClient, db: AsyncSession, domain: str
) -> tuple[Team, Notification]:
    team, held = await _team_with_held_notifications(client, db, domain, 1)
    return team, held[0]


def _drain(queue: asyncio.Queue[sse_manager.Message]) -> list[sse_manager.Message]:
    messages: list[sse_manager.Message] = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


async def _runs(db: AsyncSession, team: Team) -> list[BriefingRun]:
    rows = await db.scalars(
        select(BriefingRun).where(BriefingRun.project_id == as_uuid(team.project_id))
    )
    return list(rows.all())


async def test_briefing_hour_flushes_held_notifications_and_sends_ready(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`briefing_hour` 도달 → 보류 알림 flush + `briefing.ready` 알림 + SSE (`06 §4`)."""
    team, held = await _team_with_held_notification(client, db_session, "briefing-flush.test")

    with sse_manager.subscribe(as_uuid(team.owner.id)) as queue:
        sent = await briefing_dispatch_service.dispatch_due_briefings(
            db_session, now=AT_BRIEFING_HOUR
        )
        # ⚠️ SSE 는 아웃박스라 커밋해야 발행된다 (`sse_manager` 독스트링).
        await db_session.commit()
        events = _drain(queue)

    assert sent == [as_uuid(team.project_id)]

    # 보류가 풀렸다 — `deliver_after=NULL` 이 곧 "발행 완료"다.
    assert held.deliver_after is None

    ready = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_BRIEFING_READY)
    assert len(ready) == 1
    assert ready[0].deliver_after is None, "브리핑 알림 자신을 다음 브리핑까지 미루면 안 된다"
    assert ready[0].payload["date"] == RUN_DATE.isoformat()
    assert ready[0].title and ready[0].body, "`05 §1.5` — 문안은 서버가 만들어 저장한다"

    runs = await _runs(db_session, team)
    assert len(runs) == 1 and runs[0].run_date == RUN_DATE

    briefing_events = [m for m in events if m.event == sse_manager.SSE_BRIEFING_READY]
    assert len(briefing_events) == 1
    assert briefing_events[0].data == {
        "project_id": team.project_id,
        "date": RUN_DATE.isoformat(),
    }

    # `card.created` 묶음은 `briefing.ready` 하나로 요약한다 (`06 §4`) — 밀린 카드 알림마다
    # `notification.created` 를 내면 프론트가 같은 목록을 N 번 재조회한다 (`05 §12.2`).
    created = [m for m in events if m.event == sse_manager.SSE_NOTIFICATION_CREATED]
    assert [m.data["notification_id"] for m in created] == [str(ready[0].id)]


async def test_briefing_is_sent_once_for_the_same_run_date(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """중복 발송 방지 — 같은 `run_date` 로 **연속 2회** 불러도 알림은 1건 (결정 1.11).

    두 번째 호출은 `INSERT … ON CONFLICT DO NOTHING` 이 `rowcount == 0` 을 돌려주므로
    발송 로직에 들어가지도 못하고 조기 반환한다.
    """
    team, _ = await _team_with_held_notification(client, db_session, "briefing-once.test")

    first = await briefing_dispatch_service.dispatch_due_briefings(db_session, now=AT_BRIEFING_HOUR)
    second = await briefing_dispatch_service.dispatch_due_briefings(
        db_session, now=AT_BRIEFING_HOUR + timedelta(minutes=30)
    )
    await db_session.commit()

    assert first == [as_uuid(team.project_id)]
    assert second == [], "이미 오늘 몫이 있으므로 조기 반환한다"

    # 조기 반환의 근거가 `rowcount == 0` 이라는 것을 직접 확인한다 (`SELECT` 선행 확인이 아니다).
    claimed = await briefing_dispatch_service._claim_run(
        db_session, project_id=as_uuid(team.project_id), run_date=RUN_DATE, now=AT_BRIEFING_HOUR
    )
    assert claimed is False

    ready = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_BRIEFING_READY)
    assert len(ready) == 1
    assert len(await _runs(db_session, team)) == 1


async def test_briefing_is_not_sent_before_the_briefing_hour(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`briefing_hour` 이전에는 발송하지 않는다 — 보류 알림도 그대로 보류다."""
    team, held = await _team_with_held_notification(client, db_session, "briefing-early.test")

    assert (
        await briefing_dispatch_service.dispatch_due_briefings(db_session, now=BEFORE_BRIEFING_HOUR)
    ) == []
    await db_session.commit()

    assert held.deliver_after == HELD_UNTIL, "보류가 풀리면 안 된다"
    assert (
        await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_BRIEFING_READY)
        == []
    )
    assert await _runs(db_session, team) == []


async def test_briefing_hour_comes_from_project_settings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`briefing_hour` 는 `projects.settings` 에서 읽는다 (룰 3 — 9 를 박아 두지 않는다)."""
    team, _ = await _team_with_held_notification(client, db_session, "briefing-setting.test")
    await patch_project_settings(db_session, team.project_id, briefing_hour=6)

    early = datetime(2026, 3, 4, 6, 30, tzinfo=UTC)
    assert await briefing_dispatch_service.dispatch_due_briefings(db_session, now=early) == [
        as_uuid(team.project_id)
    ]
    await db_session.commit()

    runs = await _runs(db_session, team)
    assert len(runs) == 1 and runs[0].run_date == RUN_DATE


async def test_briefing_follows_the_answerer_timezone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """발송 기준일·시각은 **담당자 `users.timezone`** 을 따른다 (룰 6 타임존 단일 원천)."""
    team, held = await _team_with_held_notification(client, db_session, "briefing-tz.test")

    answerer = await db_session.get(User, as_uuid(team.owner.id))
    assert answerer is not None
    answerer.timezone = NEW_YORK
    answerer.language = "en"
    await db_session.commit()

    # 뉴욕 현지 07:30 — UTC 로는 12:30 이라 UTC 기준이었다면 이미 발송했을 시각이다.
    assert (
        await briefing_dispatch_service.dispatch_due_briefings(db_session, now=NEW_YORK_MORNING)
    ) == []

    sent = await briefing_dispatch_service.dispatch_due_briefings(db_session, now=NEW_YORK_EVENING)
    await db_session.commit()

    assert sent == [as_uuid(team.project_id)]
    assert held.deliver_after is None

    runs = await _runs(db_session, team)
    assert len(runs) == 1
    assert NEW_YORK_EVENING.date() == date(2026, 3, 5), "UTC 날짜와 갈리는 시각을 골랐다"
    assert runs[0].run_date == RUN_DATE, "UTC 날짜가 아니라 담당자 현지 날짜다"

    ready = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_BRIEFING_READY)
    assert len(ready) == 1
    assert ready[0].title.isascii(), "`05 §1.5` — 수신자 `users.language` 로 만든다"


# --------------------------------------------------------------------------------------
# 밀린 브리핑이 DND 를 침범하지 않는다 (룰 6)
# --------------------------------------------------------------------------------------
async def test_catch_up_briefing_is_suppressed_inside_the_dnd_window(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "도달 이후" 판정이 DND 한복판으로 새어 들어가면 안 된다 (룰 6).

    재현: `briefing_hour=9`, DND `22:00~07:00`, 그날 브리핑 미발송인 채로 서버가 현지 21:30 에
    뜬다. APScheduler `interval` 첫 실행은 기동 +60분이라 첫 틱이 22:30 이고, `22 >= 9` 는
    참이므로 **`briefing.ready` 와 밀린 알림이 DND 한가운데서 한꺼번에 터진다.**

    룰 6 은 "담당자의 **방해 금지 시간에는 어떤 즉시 알림도 보내지 않는다**"이다. 발송권
    (`briefing_runs`)을 아직 따내지 않았으므로 브리핑은 사라지지 않고 뒤로 미뤄질 뿐이다.
    """
    team, held = await _team_with_held_notifications(client, db_session, "briefing-dnd.test", 1)
    inside_dnd = datetime(2026, 3, 4, 22, 30, tzinfo=UTC)

    # 기본 DND(`22:00~07:00`) 그대로다 — 룰 3 대로 `projects.settings` 에서 읽는지도 함께 본다.
    sent = await briefing_dispatch_service.dispatch_due_briefings(db_session, now=inside_dnd)
    await db_session.commit()

    assert sent == [], "브리핑 시각은 지났지만 지금은 DND 안이다"
    assert held[0].deliver_after == HELD_UNTIL, "밀린 알림도 그대로 보류다"
    assert (
        await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_BRIEFING_READY)
        == []
    )
    assert await _runs(db_session, team) == [], (
        "발송권을 따내지 않아야 DND 가 끝난 뒤 그 몫이 나갈 수 있다"
    )


async def test_briefing_hour_inside_the_dnd_window_still_fires(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """담당자가 `briefing_hour` 를 **스스로** DND 안에 넣었으면 그대로 보낸다.

    억제 대상은 따라잡기 발송뿐이다. 이 경우까지 막으면 그 설정에서 브리핑이 **영영** 나가지
    않는다 — DND 를 피하려다 룰 6 의 🛟("인박스는 사라지지 않는다")를 반대편에서 깨는 셈이다.
    """
    team, held = await _team_with_held_notifications(client, db_session, "briefing-dnd-own.test", 1)
    # 기본 DND `22:00~07:00` 안으로 브리핑 시각을 옮긴다 (룰 3 — 값은 `projects.settings`).
    await patch_project_settings(db_session, team.project_id, briefing_hour=23)

    inside_own_window = datetime(2026, 3, 4, 23, 30, tzinfo=UTC)
    sent = await briefing_dispatch_service.dispatch_due_briefings(db_session, now=inside_own_window)
    await db_session.commit()

    assert sent == [as_uuid(team.project_id)]
    assert held[0].deliver_after is None, "보류가 풀렸다"

    runs = await _runs(db_session, team)
    assert len(runs) == 1 and runs[0].run_date == RUN_DATE


# --------------------------------------------------------------------------------------
# flush 는 밀린 알림을 **합치지 않는다** (룰 6 🛟)
# --------------------------------------------------------------------------------------
async def test_flush_keeps_every_held_notification_as_its_own_record(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """보류 3건 → flush → **레코드 3건이 그대로** 남는다.

    N=1 로만 덮으면 "밀린 카드 알림을 `briefing.ready` 하나로 갈음하고 지운다"는 구현이 그대로
    통과한다. `06 §4` 가 요약하라고 한 것은 **SSE 이벤트**이지 알림 레코드가 아니다 —
    담당자가 아침에 여는 인박스에서 밤사이 쌓인 카드가 사라지면 룰 6 의 🛟 가 무너진다.
    """
    team, held = await _team_with_held_notifications(client, db_session, "briefing-flush-n.test", 3)
    held_ids = {notification.id for notification in held}

    with sse_manager.subscribe(as_uuid(team.owner.id)) as queue:
        sent = await briefing_dispatch_service.dispatch_due_briefings(
            db_session, now=AT_BRIEFING_HOUR
        )
        await db_session.commit()
        events = _drain(queue)

    assert sent == [as_uuid(team.project_id)]

    stored = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_CARD_CREATED)
    assert len(stored) == 3, "병합하거나 지우지 않는다 — 레코드 수가 그대로다"
    assert {notification.id for notification in stored} == held_ids, "같은 행이 그대로 남는다"
    assert all(notification.deliver_after is None for notification in stored), (
        "`deliver_after=NULL` 이 곧 발행 완료 표시다"
    )

    # 알림함에도 3건이 각각 선다 — 담당자가 아침에 여는 화면이 여기다 (`05 §11`).
    delivered = await inbox(client, team.owner)
    assert sum(1 for item in delivered if item["type"] == NOTIFICATION_CARD_CREATED) == 3
    assert {item["id"] for item in delivered if item["type"] == NOTIFICATION_CARD_CREATED} == {
        str(notification_id) for notification_id in held_ids
    }

    # 요약되는 것은 SSE 뿐이다 (`06 §4`) — `notification.created` 는 `briefing.ready` 한 건.
    ready = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_BRIEFING_READY)
    created = [m for m in events if m.event == sse_manager.SSE_NOTIFICATION_CREATED]
    assert [m.data["notification_id"] for m in created] == [str(ready[0].id)]
