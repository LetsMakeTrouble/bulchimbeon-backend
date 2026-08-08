"""M7 학습 곡선 시계열 (`05 §13`).

"쓸수록 좋아진다"를 그래프 하나로 보여주는 데이터다. 그래서 **빈 날도 0 으로 채워야** 하고
(구멍 난 그래프는 하락으로 읽힌다) **`official_qas` 는 누적이어야 한다**(증분으로 그리면
지식이 쌓이는 곡선이 아니라 톱니가 된다).
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import GRADE_GREEN, GRADE_RED, GRADE_YELLOW
from app.services import event_service
from tests.helpers import API, create_actor, create_project, join_project
from tests.metrics_helpers import emit, emit_graded, get_timeseries, now

pytestmark = pytest.mark.asyncio


async def _team(client: AsyncClient, domain: str, *, timezone: str = "UTC"):
    owner = await create_actor(client, f"owner@{domain}", name="담당자", timezone=timezone)
    project = await create_project(client, owner)
    asker = await create_actor(client, f"asker@{domain}", name="지수", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    return owner, asker, project


def _dates(body: dict) -> list[date]:
    return [date.fromisoformat(item["date"]) for item in body["items"]]


def _item_on(body: dict, day: date) -> dict:
    return next(item for item in body["items"] if item["date"] == day.isoformat())


async def test_empty_buckets_are_filled_with_zero(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """질문 없는 날도 0 으로 채운다 (DoD 5) — 그래프에 구멍이 생기지 않게."""
    owner, _, project = await _team(client, "gaps.test")
    two_days_ago = now() - timedelta(days=2)
    await emit_graded(
        db_session, project_id=project["id"], grade=GRADE_GREEN, count=2, created_at=two_days_ago
    )
    await db_session.commit()

    body = await get_timeseries(client, owner, project["id"], days=5)

    dates = _dates(body)
    assert len(dates) == 5, "days=5 는 오늘을 포함한 5개 버킷이다"
    assert dates == sorted(dates)
    assert all((dates[i + 1] - dates[i]).days == 1 for i in range(len(dates) - 1))

    hit = _item_on(body, two_days_ago.date())
    assert hit["green"] == 2

    empty = [item for item in body["items"] if item["date"] != two_days_ago.date().isoformat()]
    assert all(item["green"] == 0 and item["questions"] == 0 for item in empty)


async def test_counts_questions_grades_reused_and_lessons(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """아이템의 각 필드가 제 이벤트를 센다 (`05 §13` 표)."""
    owner, _, project = await _team(client, "fields.test")
    yesterday = now() - timedelta(days=1)

    for _ in range(3):
        await emit(
            db_session,
            project_id=project["id"],
            type=event_service.EVENT_QUESTION_CREATED,
            created_at=yesterday,
            payload={"urgency": "normal"},
        )
    await emit_graded(
        db_session, project_id=project["id"], grade=GRADE_GREEN, count=1, created_at=yesterday
    )
    await emit_graded(
        db_session, project_id=project["id"], grade=GRADE_YELLOW, count=1, created_at=yesterday
    )
    await emit_graded(
        db_session, project_id=project["id"], grade=GRADE_RED, count=1, created_at=yesterday
    )
    await emit(
        db_session,
        project_id=project["id"],
        type=event_service.EVENT_ANSWER_REUSED,
        created_at=yesterday,
        payload={"similarity": 0.95},
    )
    await emit(
        db_session,
        project_id=project["id"],
        type=event_service.EVENT_LESSON_APPROVED,
        created_at=yesterday,
        payload={"lesson_id": "x"},
    )
    await db_session.commit()

    body = await get_timeseries(client, owner, project["id"], days=3)
    item = _item_on(body, yesterday.date())

    assert item["questions"] == 3
    assert (item["green"], item["yellow"], item["red"]) == (1, 1, 1)
    assert item["reused"] == 1
    assert item["lessons_approved"] == 1


async def test_official_qas_is_cumulative(client: AsyncClient, db_session: AsyncSession) -> None:
    """`official_qas` 는 **버킷 종료 시점 누적**이다 (DoD 5) — 증분이 아니다.

    증분으로 내리면 "지식이 쌓이는 곡선" 대신 톱니가 그려진다.
    """
    owner, _, project = await _team(client, "cumulative.test")
    today = now()

    for offset in (3, 3, 1):
        await emit(
            db_session,
            project_id=project["id"],
            type=event_service.EVENT_OFFICIAL_QA_CREATED,
            created_at=today - timedelta(days=offset),
            payload={"official_qa_id": "x"},
        )
    await db_session.commit()

    body = await get_timeseries(client, owner, project["id"], days=5)
    series = [item["official_qas"] for item in body["items"]]

    assert series == sorted(series), "누적값은 줄어들지 않는다"
    assert _item_on(body, (today - timedelta(days=4)).date())["official_qas"] == 0
    assert _item_on(body, (today - timedelta(days=3)).date())["official_qas"] == 2
    assert _item_on(body, (today - timedelta(days=2)).date())["official_qas"] == 2
    assert _item_on(body, (today - timedelta(days=1)).date())["official_qas"] == 3
    assert _item_on(body, today.date())["official_qas"] == 3


async def test_official_qas_includes_knowledge_from_before_the_window(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """창 이전에 쌓인 지식도 기저로 깔린다 — 곡선이 0 에서 다시 시작하면 안 된다."""
    owner, _, project = await _team(client, "baseline.test")
    await emit(
        db_session,
        project_id=project["id"],
        type=event_service.EVENT_OFFICIAL_QA_CREATED,
        created_at=now() - timedelta(days=90),
        payload={"official_qa_id": "old"},
    )
    await db_session.commit()

    body = await get_timeseries(client, owner, project["id"], days=3)

    assert all(item["official_qas"] == 1 for item in body["items"])


async def test_week_bucket(client: AsyncClient, db_session: AsyncSession) -> None:
    """`bucket=week` — 버킷 시작일은 **월요일**이다 (Postgres `date_trunc('week', …)` 경계).

    경계가 어긋나면 SQL 이 만든 키와 파이썬이 만든 키가 맞지 않아 **모든 버킷이 0** 이 된다.
    """
    owner, _, project = await _team(client, "week.test")
    ten_days_ago = now() - timedelta(days=10)
    await emit_graded(
        db_session, project_id=project["id"], grade=GRADE_GREEN, count=4, created_at=ten_days_ago
    )
    await db_session.commit()

    body = await get_timeseries(client, owner, project["id"], days=28, bucket="week")

    assert body["bucket"] == "week"
    dates = _dates(body)
    assert all(day.weekday() == 0 for day in dates), "주 버킷의 시작일은 월요일이다"
    assert all((dates[i + 1] - dates[i]).days == 7 for i in range(len(dates) - 1))

    monday = ten_days_ago.date() - timedelta(days=ten_days_ago.weekday())
    assert _item_on(body, monday)["green"] == 4
    assert sum(item["green"] for item in body["items"]) == 4


async def test_buckets_follow_answerer_timezone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """버킷 경계는 **담당자 `users.timezone`** 기준이다 (`04 §3`).

    어제 23:30 UTC 는 서울에서는 **오늘 08:30** 이다. 같은 순간이 두 프로젝트에서 하루 차이
    나는 버킷에 들어가야 한다 — UTC 로 굳어 있으면 여기서 잡힌다.
    """
    utc_owner, _, utc_project = await _team(client, "tz-utc.test", timezone="UTC")
    kst_owner, _, kst_project = await _team(client, "tz-kst.test", timezone="Asia/Seoul")

    instant = datetime.combine(
        now().date() - timedelta(days=1), time(hour=23, minute=30), tzinfo=UTC
    )
    for project in (utc_project, kst_project):
        await emit_graded(
            db_session, project_id=project["id"], grade=GRADE_GREEN, count=1, created_at=instant
        )
    await db_session.commit()

    utc_body = await get_timeseries(client, utc_owner, utc_project["id"], days=7)
    kst_body = await get_timeseries(client, kst_owner, kst_project["id"], days=7)

    utc_day = next(item for item in utc_body["items"] if item["green"] == 1)["date"]
    kst_day = next(item for item in kst_body["items"] if item["green"] == 1)["date"]

    assert date.fromisoformat(kst_day) - date.fromisoformat(utc_day) == timedelta(days=1)


async def test_timeseries_readable_by_asker(client: AsyncClient, db_session: AsyncSession) -> None:
    """시계열도 담당자 전용이 아니다 — 멤버면 본다 (`05 §13` 권한)."""
    _, asker, project = await _team(client, "ts-member-read.test")

    body = await get_timeseries(client, asker, project["id"], days=3)

    assert body["window_days"] == 3
    assert len(body["items"]) == 3


async def test_timeseries_denied_to_non_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    outsider = await create_actor(client, "outsider@ts-denied.test", name="외부인")
    _, _, project = await _team(client, "ts-denied.test")

    response = await client.get(
        f"{API}/projects/{project['id']}/metrics/timeseries", headers=outsider.headers
    )
    assert response.status_code == 403
