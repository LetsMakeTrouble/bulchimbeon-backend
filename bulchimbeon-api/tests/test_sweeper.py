"""만료 스위퍼 · 좀비 회수 (`06 §4`, D14·D23).

DoD (`prompts/05-notifications-sse.md`):
- **만료 스위퍼 (시간 주입)**: 살아 있는 카드가 **없으면** `expired` 전환 /
  **pending·deferred 카드가 걸려 있으면 `draft` 유지** — 두 케이스를 각각 단언한다
- **좀비 회수**: `processing` 으로 6분 방치된 질문 → `failed` + `reason='failed'` 카드 생성
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.notification import NOTIFICATION_ANSWER_FAILED
from app.models.question import (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_EXPIRED,
    QUESTION_STATUS_FAILED,
    QUESTION_STATUS_PROCESSING,
    Question,
)
from app.models.review_card import (
    CARD_REASON_FAILED,
    CARD_STATUS_DEFERRED,
    CARD_STATUS_PENDING,
    CARD_STATUS_RESOLVED,
)
from app.services import event_service, sweeper_service
from tests.notification_helpers import stored_notifications
from tests.pipeline_helpers import as_uuid, ask
from tests.review_helpers import (
    YELLOW_MARKER,
    answer_of,
    ask_until_card,
    build_team,
    card_for_question,
    seed_evidence,
)

# 발행 시점의 `expires_at` 을 과거로 되돌리기 위한 오프셋. 72h 를 실제로 기다릴 수 없다.
PAST = timedelta(hours=1)


async def _aged_draft(client: AsyncClient, db: AsyncSession, domain: str):
    """🟡 초안 하나를 만들고 `expires_at` 을 과거로 밀어 둔다."""
    team = await build_team(client, domain)
    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db, team, content_ko)
    question_id, card = await ask_until_card(client, db, team, content_ko)

    answer = await answer_of(db, question_id)
    answer.expires_at = datetime.now(UTC) - PAST
    await db.commit()
    return team, question_id, card, answer


# --- 만료 스위퍼 (D14) --------------------------------------------------------------------


async def test_sweeper_expires_a_draft_with_no_open_card(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """살아 있는 카드가 없으면 `expired` 로 내린다 + `answer.expired` 이벤트."""
    _, question_id, card, answer = await _aged_draft(client, db_session, "sweep-expire.test")

    # 카드를 해소해 "살아 있는 카드"를 없앤다 (스위퍼의 세 번째 조건).
    card.status = CARD_STATUS_RESOLVED
    await db_session.commit()

    expired = await sweeper_service.expire_stale_drafts(db_session)
    await db_session.commit()

    assert expired == [answer.id]
    db_session.expire_all()
    assert (await answer_of(db_session, question_id)).state == ANSWER_STATE_EXPIRED

    events = await db_session.scalars(
        select(Event).where(
            Event.entity_id == as_uuid(question_id),
            Event.type == event_service.EVENT_ANSWER_EXPIRED,
        )
    )
    recorded = list(events.all())
    assert len(recorded) == 1
    assert recorded[0].payload["answer_id"] == str(answer.id)
    assert recorded[0].actor_id is None, "스케줄러 행위이므로 system(NULL)이다"


@pytest.mark.parametrize("status", [CARD_STATUS_PENDING, CARD_STATUS_DEFERRED])
async def test_sweeper_keeps_a_draft_under_an_open_card(
    client: AsyncClient, db_session: AsyncSession, status: str
) -> None:
    """⛔ **살아 있는 카드 밑의 답변을 죽이지 않는다** (D14).

    이 조건을 빼면 담당자가 72h 이후에 지연 카드를 처리할 때 답변이 이미 `expired` 라
    "카드를 눌렀는데 409"가 난다 — 데모에서 밟히는 경로다.
    """
    _, question_id, card, _ = await _aged_draft(client, db_session, f"sweep-keep-{status}.test")

    card.status = status
    await db_session.commit()

    assert await sweeper_service.expire_stale_drafts(db_session) == []
    await db_session.commit()

    db_session.expire_all()
    assert (await answer_of(db_session, question_id)).state == ANSWER_STATE_DRAFT


async def test_sweeper_ignores_drafts_that_have_not_expired(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`expires_at` 이 미래면 대상이 아니다. 시각 주입으로 경계를 확인한다."""
    _, question_id, card, answer = await _aged_draft(client, db_session, "sweep-future.test")
    card.status = CARD_STATUS_RESOLVED
    await db_session.commit()

    # `expires_at` 직전 시각을 주입하면 아직 만료가 아니다.
    assert answer.expires_at is not None
    just_before = answer.expires_at - timedelta(seconds=1)
    assert await sweeper_service.expire_stale_drafts(db_session, now=just_before) == []

    db_session.expire_all()
    assert (await answer_of(db_session, question_id)).state == ANSWER_STATE_DRAFT


async def test_sweeper_never_touches_reused_answers(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """재사용 답변은 `expires_at=NULL` 이라 애초에 대상이 아니다 (D11)."""
    team = await build_team(client, "sweep-reused.test")
    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)
    card.status = CARD_STATUS_RESOLVED

    answer = await answer_of(db_session, question_id)
    answer.expires_at = None
    await db_session.commit()

    assert await sweeper_service.expire_stale_drafts(db_session) == []


# --- 좀비 회수 (D23) ---------------------------------------------------------------------


async def test_zombie_recovery_fails_a_stuck_question(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`processing` 으로 6분 방치 → `failed` + `reason='failed'` 카드 + `answer.failed` 알림.

    프로세스가 죽어 파이프라인의 `except` 조차 타지 못한 경우의 안전망이다 (`03 §2`).
    """
    team = await build_team(client, "zombie.test")

    # 파이프라인을 돌리지 않고 `processing` 질문만 만든다 — 프로세스가 죽은 상태의 재현이다.
    question = Question(
        project_id=as_uuid(team.project_id),
        asker_id=as_uuid(team.asker.id),
        content_ko="환불 기한은 며칠인가요?",
        urgency="normal",
        suggest_urgent=False,
        status=QUESTION_STATUS_PROCESSING,
    )
    db_session.add(question)
    await db_session.flush()
    question_id = str(question.id)

    # `created_at` 은 server_default 라 직접 6분 전으로 돌린다.
    question.created_at = datetime.now(UTC) - timedelta(minutes=6)
    await db_session.commit()

    recovered = await sweeper_service.recover_zombie_questions(db_session)
    await db_session.commit()

    assert recovered == [as_uuid(question_id)]
    # ⚠️ id 를 먼저 붙잡아 둔다 — `expire_all()` 뒤에 ORM 속성을 읽으면 동기 컨텍스트에서
    #    lazy load 가 일어나 `MissingGreenlet` 이 난다.
    db_session.expire_all()
    reloaded = await db_session.get(Question, as_uuid(question_id))
    assert reloaded is not None and reloaded.status == QUESTION_STATUS_FAILED

    card = await card_for_question(db_session, question_id)
    assert card is not None
    assert card.reason == CARD_REASON_FAILED
    assert card.answer_id is None, "답변이 만들어지기 전에 죽었다 (D23)"

    notified = await stored_notifications(
        db_session, team.asker.id, type=NOTIFICATION_ANSWER_FAILED
    )
    assert len(notified) == 1


async def test_zombie_recovery_leaves_fresh_questions_alone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """5분이 지나지 않은 `processing` 은 정상 진행 중이다 — 죽이면 멀쩡한 질문이 사라진다."""
    team = await build_team(client, "zombie-fresh.test")
    question = Question(
        project_id=as_uuid(team.project_id),
        asker_id=as_uuid(team.asker.id),
        content_ko="환불 기한은 며칠인가요?",
        urgency="normal",
        suggest_urgent=False,
        status=QUESTION_STATUS_PROCESSING,
    )
    db_session.add(question)
    await db_session.flush()
    question_id = question.id
    await db_session.commit()

    assert await sweeper_service.recover_zombie_questions(db_session) == []
    db_session.expire_all()
    reloaded = await db_session.get(Question, question_id)
    assert reloaded is not None and reloaded.status == QUESTION_STATUS_PROCESSING


async def test_zombie_recovery_skips_questions_that_already_have_an_answer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """답변이 이미 있으면 대상이 아니다 — 발행된 답변을 실패로 덮으면 안 된다."""
    team = await build_team(client, "zombie-answered.test")
    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    accepted = await ask(client, team.asker, team.project_id, content_ko)

    question = await db_session.get(Question, as_uuid(accepted["question_id"]))
    assert question is not None
    # 상태 전이만 유실된 상황을 만든다 (답변 행은 이미 있다).
    question.status = QUESTION_STATUS_PROCESSING
    question.created_at = datetime.now(UTC) - timedelta(minutes=6)
    await db_session.commit()

    assert await sweeper_service.recover_zombie_questions(db_session) == []
