"""M7 지표 대시보드 (`05 §13`, `02 §10`).

경계값이 이 파일의 본론이다. 비율 지표는 **분모가 0 일 때 `null`** 이어야 하고(0% 가 아니다),
`grade_accuracy[]` 는 표본 30 미만이면 숫자를 내리지 않는다 (D25). 실패를 은폐하는 구현은
전부 "표본이 적을 때 그럴듯한 숫자가 나오는" 모양을 하고 있어 여기서만 잡힌다.
"""

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import ANSWER_STATE_VERIFIED, GRADE_GREEN, GRADE_RED, GRADE_YELLOW
from app.services import event_service, metrics_service
from tests.helpers import API, create_actor, create_project, join_project
from tests.metrics_helpers import (
    emit,
    emit_card_handled,
    emit_graded,
    get_metrics,
    now,
    seed_answers,
)

pytestmark = pytest.mark.asyncio


async def _team(client: AsyncClient, domain: str):
    owner = await create_actor(client, f"owner@{domain}", name="담당자", timezone="UTC")
    project = await create_project(client, owner)
    asker = await create_actor(client, f"asker@{domain}", name="지수", timezone="UTC")
    await join_project(client, asker, project["invite_code"])
    return owner, asker, project


# --------------------------------------------------------------------------------------
# 자동응답률 · 절약 대기시간
# --------------------------------------------------------------------------------------
async def test_metrics_matches_injected_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """표본 이벤트 시나리오 → 각 지표 기대값 일치 (DoD 1)."""
    owner, asker, project = await _team(client, "expected.test")

    await emit_graded(db_session, project_id=project["id"], grade=GRADE_GREEN, count=6)
    await emit_graded(db_session, project_id=project["id"], grade=GRADE_YELLOW, count=2)
    await emit_graded(db_session, project_id=project["id"], grade=GRADE_RED, count=2)

    for verdict, count in (("correct", 4), ("different", 1)):
        for _ in range(count):
            await emit(
                db_session,
                project_id=project["id"],
                type=event_service.EVENT_FEEDBACK_CREATED,
                payload={"verdict": verdict},
            )

    await emit_card_handled(db_session, project_id=project["id"], viewed_at=now(), after_seconds=5)
    await emit_card_handled(db_session, project_id=project["id"], viewed_at=now(), after_seconds=90)
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["window_days"] == 30
    assert body["auto_answer_rate"] == {
        "value": 0.8,
        "target": metrics_service.TARGET_AUTO_ANSWER_RATE,
        "green": 6,
        "yellow": 2,
        "red": 2,
    }
    assert body["correction_rate"] == {
        "value": 0.2,
        "target": metrics_service.TARGET_CORRECTION_RATE,
        "correct": 4,
        "different": 1,
    }
    assert body["card_handle_30s_rate"] == {
        "value": 0.5,
        "target": metrics_service.TARGET_CARD_HANDLE_30S_RATE,
        "within_30s": 1,
        "viewed_cards": 2,
    }
    # 🟢+🟡 = 8건 × 기본 가정 24h.
    assert body["saved_wait_hours"] == {
        "value": 8 * 24,
        "assumption_hours": 24,
        "basis_count": 8,
    }


async def test_window_excludes_older_events(client: AsyncClient, db_session: AsyncSession) -> None:
    """`?days=` 창 밖의 이벤트는 세지 않는다."""
    owner, _, project = await _team(client, "window.test")

    await emit_graded(db_session, project_id=project["id"], grade=GRADE_GREEN, count=3)
    await emit_graded(
        db_session,
        project_id=project["id"],
        grade=GRADE_RED,
        count=5,
        created_at=now() - timedelta(days=40),
    )
    await db_session.commit()

    default_window = await get_metrics(client, owner, project["id"])
    assert default_window["auto_answer_rate"]["red"] == 0
    assert default_window["auto_answer_rate"]["value"] == 1.0

    wide_window = await get_metrics(client, owner, project["id"], days=60)
    assert wide_window["window_days"] == 60
    assert wide_window["auto_answer_rate"]["red"] == 5
    assert wide_window["auto_answer_rate"]["value"] == 0.375


async def test_auto_answer_rate_null_without_questions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """분모 0 은 "표본 없음"이다 — 0.0 이 아니라 `null` (`05 §13` 상단)."""
    owner, _, project = await _team(client, "empty.test")

    body = await get_metrics(client, owner, project["id"])

    assert body["auto_answer_rate"]["value"] is None
    assert body["saved_wait_hours"]["value"] == 0
    assert body["saved_wait_hours"]["basis_count"] == 0


async def test_saved_wait_hours_follows_project_settings(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`assumption_hours` 는 `projects.settings` 에서 온다 (DoD 4) — env 도 상수도 아니다.

    설정을 바꾸면 응답이 따라 바뀌어야 한다. 상수로 박아 두면 이 테스트가 잡는다.
    """
    owner, _, project = await _team(client, "assumption.test")
    await emit_graded(db_session, project_id=project["id"], grade=GRADE_GREEN, count=3)
    await emit_graded(db_session, project_id=project["id"], grade=GRADE_YELLOW, count=2)
    await db_session.commit()

    default_body = await get_metrics(client, owner, project["id"])
    assert default_body["saved_wait_hours"] == {
        "value": 5 * 24,
        "assumption_hours": 24,
        "basis_count": 5,
    }

    patched = await client.patch(
        f"{API}/projects/{project['id']}/settings",
        json={"saved_wait_assumption_hours": 8},
        headers=owner.headers,
    )
    assert patched.status_code == 200, patched.text

    body = await get_metrics(client, owner, project["id"])
    assert body["saved_wait_hours"] == {
        "value": 5 * 8,
        "assumption_hours": 8,
        "basis_count": 5,
    }
    # value 는 언제나 basis_count × assumption_hours 다.
    assert (
        body["saved_wait_hours"]["value"]
        == body["saved_wait_hours"]["basis_count"] * body["saved_wait_hours"]["assumption_hours"]
    )


# --------------------------------------------------------------------------------------
# 정정률
# --------------------------------------------------------------------------------------
async def test_correction_rate_null_without_feedback(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """피드백 0건 → `value: null` (DoD 2 경계). 정정률 0% 와 구분된다."""
    owner, _, project = await _team(client, "no-feedback.test")
    await emit_graded(db_session, project_id=project["id"], grade=GRADE_GREEN, count=3)
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["correction_rate"] == {
        "value": None,
        "target": metrics_service.TARGET_CORRECTION_RATE,
        "correct": 0,
        "different": 0,
    }


async def test_correction_rate_zero_when_all_correct(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "맞았다"만 있으면 정정률은 **0.0** 이다 — 이때만 0 이고, 표본 없음(null)과 다르다."""
    owner, _, project = await _team(client, "all-correct.test")
    for _ in range(3):
        await emit(
            db_session,
            project_id=project["id"],
            type=event_service.EVENT_FEEDBACK_CREATED,
            payload={"verdict": "correct"},
        )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])
    assert body["correction_rate"]["value"] == 0.0
    assert body["correction_rate"]["correct"] == 3


# --------------------------------------------------------------------------------------
# 카드 처리 30초
# --------------------------------------------------------------------------------------
async def test_card_handle_rate_null_without_views(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`viewed_cards == 0` → `value: null` (DoD 2 경계).

    분모는 **`first_viewed_at` 이 기록된 카드 수**다. 카드가 있어도 아무도 열지 않았으면
    측정 자체가 시작되지 않았다는 뜻이다.
    """
    owner, _, project = await _team(client, "unviewed.test")
    await emit(
        db_session,
        project_id=project["id"],
        type=event_service.EVENT_CARD_CREATED,
        payload={"card_id": "00000000-0000-0000-0000-000000000001", "reason": "yellow"},
    )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["card_handle_30s_rate"] == {
        "value": None,
        "target": metrics_service.TARGET_CARD_HANDLE_30S_RATE,
        "within_30s": 0,
        "viewed_cards": 0,
    }


async def test_card_handle_30s_boundary(client: AsyncClient, db_session: AsyncSession) -> None:
    """30초 경계 — 29초는 분자에, 31초는 분모에만.

    아직 처리하지 않은 카드도 분모에 남는다("열어 놓고 안 했다"가 곧 이 지표의 실패다).
    """
    owner, _, project = await _team(client, "boundary.test")
    viewed_at = now() - timedelta(minutes=5)

    await emit_card_handled(
        db_session, project_id=project["id"], viewed_at=viewed_at, after_seconds=29
    )
    await emit_card_handled(
        db_session, project_id=project["id"], viewed_at=viewed_at, after_seconds=31
    )
    await emit_card_handled(
        db_session, project_id=project["id"], viewed_at=viewed_at, after_seconds=None
    )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["card_handle_30s_rate"]["viewed_cards"] == 3
    assert body["card_handle_30s_rate"]["within_30s"] == 1


async def test_card_handle_ignores_action_before_view(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """열람 **이전**의 액션은 세지 않는다.

    목록에서 바로 `defer` 한 뒤 나중에 카드를 열어 처리한 카드가 이 모양이다. 액션 시각을
    `min()` 으로만 집으면 간격이 음수가 되어 30초 이내로 통과하고, 지표가 부풀려진다.
    """
    owner, _, project = await _team(client, "before-view.test")
    viewed_at = now() - timedelta(minutes=5)

    await emit_card_handled(
        db_session,
        project_id=project["id"],
        viewed_at=viewed_at,
        after_seconds=-600,
        action_type=event_service.EVENT_CARD_DEFERRED,
    )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["card_handle_30s_rate"]["viewed_cards"] == 1
    assert body["card_handle_30s_rate"]["within_30s"] == 0


# --------------------------------------------------------------------------------------
# 재질문 즉답률 (D26) — 이 마일스톤에서 가장 틀리기 쉬운 지표
# --------------------------------------------------------------------------------------
async def test_requestion_instant_rate_denominator_includes_reuse_missed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`reuse_missed` 2건 + `reused` 1건 → `value == 1/3` (DoD 3).

    `reuse_missed` 를 무시한 구현이면 여기서 1.0 이 나와 잡힌다 — 재사용이 1건만 일어나도
    100% 가 되는 지표는 실패를 은폐한다.
    """
    owner, _, project = await _team(client, "requestion.test")

    await emit(
        db_session,
        project_id=project["id"],
        type=event_service.EVENT_ANSWER_REUSED,
        payload={"similarity": 0.94},
    )
    for similarity in (0.88, 0.86):
        await emit(
            db_session,
            project_id=project["id"],
            type=event_service.EVENT_ANSWER_REUSE_MISSED,
            payload={"best_similarity": similarity},
        )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["requestion_instant_rate"] == {
        "value": round(1 / 3, 4),
        "target": metrics_service.TARGET_REQUESTION_INSTANT_RATE,
        "reused": 1,
        "reuse_missed": 2,
    }


async def test_requestion_instant_rate_null_without_candidates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`reused=0, reuse_missed=0` → `value: null` (DoD 2 경계).

    재사용 후보가 한 번도 없었다는 뜻이지 즉답률 0% 가 아니다.
    """
    owner, _, project = await _team(client, "no-reuse.test")
    await emit_graded(db_session, project_id=project["id"], grade=GRADE_GREEN, count=2)
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])

    assert body["requestion_instant_rate"] == {
        "value": None,
        "target": metrics_service.TARGET_REQUESTION_INSTANT_RATE,
        "reused": 0,
        "reuse_missed": 0,
    }


# --------------------------------------------------------------------------------------
# 등급별 실측 정확도 (D25)
# --------------------------------------------------------------------------------------
async def test_grade_accuracy_is_per_grade_and_never_summed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🟢·🟡·🔴 을 각각 낸다 — 합산하면 🟢 표본이 🔴 의 실패를 덮는다."""
    owner, asker, project = await _team(client, "per-grade.test")

    body = await get_metrics(client, owner, project["id"])

    assert [item["grade"] for item in body["grade_accuracy"]] == [
        GRADE_GREEN,
        GRADE_YELLOW,
        GRADE_RED,
    ]
    assert all(item["window_days"] == 30 for item in body["grade_accuracy"])


async def test_grade_accuracy_insufficient_below_30_samples(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """표본 29건 → `sufficient:false` · `verified_rate:null` · `message` (DoD 2 경계).

    데모 규모에서 "표본 부족"이 뜨는 것은 버그가 아니라 D25 가 의도한 동작이다.
    """
    owner, asker, project = await _team(client, "sample29.test")
    await seed_answers(
        db_session,
        project_id=project["id"],
        asker_id=asker.id,
        grade=GRADE_GREEN,
        count=29,
        state=ANSWER_STATE_VERIFIED,
    )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])
    green = next(item for item in body["grade_accuracy"] if item["grade"] == GRADE_GREEN)

    assert green["sample"] == 29
    assert green["sufficient"] is False
    assert green["verified_rate"] is None
    assert green["message"]


async def test_grade_accuracy_sufficient_at_30_samples(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """표본 30건이면 숫자를 낸다 — 경계는 "미만"이다 (D25)."""
    owner, asker, project = await _team(client, "sample30.test")
    await seed_answers(
        db_session,
        project_id=project["id"],
        asker_id=asker.id,
        grade=GRADE_YELLOW,
        count=30,
        state=ANSWER_STATE_VERIFIED,
    )
    await db_session.commit()

    body = await get_metrics(client, owner, project["id"])
    yellow = next(item for item in body["grade_accuracy"] if item["grade"] == GRADE_YELLOW)

    assert yellow["sample"] == 30
    assert yellow["sufficient"] is True
    assert yellow["verified_rate"] == 1.0
    assert yellow["message"] is None


# --------------------------------------------------------------------------------------
# 권한 (`05 §13` — 멤버 조회 가능)
# --------------------------------------------------------------------------------------
async def test_metrics_readable_by_asker(client: AsyncClient, db_session: AsyncSession) -> None:
    """지표는 담당자 전용이 아니다 — 멤버면 본다."""
    _, asker, project = await _team(client, "member-read.test")
    body = await get_metrics(client, asker, project["id"])
    assert body["window_days"] == 30


async def test_metrics_denied_to_non_member(client: AsyncClient, db_session: AsyncSession) -> None:
    outsider = await create_actor(client, "outsider@denied.test", name="외부인")
    _, _, project = await _team(client, "denied.test")

    response = await client.get(f"{API}/projects/{project['id']}/metrics", headers=outsider.headers)
    assert response.status_code == 403
