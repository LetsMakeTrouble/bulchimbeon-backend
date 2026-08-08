"""알림함 · 긴급/브리핑 분기 · DND 보류 (`02` 룰 1·6·8, `04 §4`, `05 §11`).

DoD (`prompts/05-notifications-sse.md`):
- urgent 🔴 → 담당자 즉시 알림(`deliver_after IS NULL`)
- 비긴급 → **알림함 미노출 + `deliver_after` 설정됨** · 카드는 존재
- DND 중 urgent → 보류
- **🟢 카드 → 알림 없음**
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    NOTIFICATION_ANSWER_COMPLETED,
    NOTIFICATION_ANSWER_CORRECTED,
    NOTIFICATION_ANSWER_FAILED,
    NOTIFICATION_ANSWER_KEPT,
    NOTIFICATION_ANSWER_REJECTED,
    NOTIFICATION_ANSWER_VERIFIED,
    NOTIFICATION_CARD_CREATED,
    NOTIFICATION_DOC_REVIEW_NEEDED,
    NOTIFICATION_FEEDBACK_DIFFERENT,
)
from app.models.review_card import CARD_REASON_GREEN, CARD_REASON_RED
from app.services.llm import get_provider
from tests.helpers import API, create_actor, create_project, error_code, join_project
from tests.notification_helpers import (
    dnd_window_excluding_now,
    dnd_window_including_now,
    inbox,
    patch_settings,
    stored_notifications,
    unread_count,
)
from tests.pipeline_helpers import ask
from tests.review_helpers import (
    GREEN_MARKER,
    RED_MARKER,
    YELLOW_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    card_for_question,
    feedback,
    seed_evidence,
)

# --- 긴급 / 브리핑 분기 (룰 6) --------------------------------------------------------------


async def test_urgent_red_notifies_the_answerer_immediately(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """urgent 🔴 → 담당자 즉시 알림 (`deliver_after IS NULL`) + 알림함에 바로 노출."""
    team = await build_team(client, "urgent.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{RED_MARKER} 일본 리전 환불 기한도 30일인가요?"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko, urgency="urgent")
    assert card.reason == CARD_REASON_RED
    assert card.is_urgent is True

    stored = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_CARD_CREATED)
    assert len(stored) == 1
    assert stored[0].deliver_after is None, "긴급 알림은 보류하지 않는다 (룰 6)"

    items = await inbox(client, team.owner)
    assert [item["type"] for item in items] == [NOTIFICATION_CARD_CREATED]
    assert await unread_count(client, team.owner) == 1


async def test_non_urgent_card_notification_waits_for_the_briefing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """비긴급 → **알림함 미노출 + `deliver_after` 설정** · 카드는 즉시 적재 (룰 6 인박스 안전망)."""
    team = await build_team(client, "briefing.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{YELLOW_MARKER} 부분 환불도 30일 안에 신청해야 하나요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)
    assert card.is_urgent is False

    stored = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_CARD_CREATED)
    assert len(stored) == 1, "레코드는 만들어져 있어야 한다 — 브리핑이 이것을 flush 한다"
    assert stored[0].deliver_after is not None
    assert stored[0].deliver_after > datetime.now(UTC), "다음 브리핑 시각이어야 한다"

    # 발송 시각이 되지 않았으므로 목록·카운트 어디에도 나타나지 않는다 (`05 §11`).
    assert await inbox(client, team.owner) == []
    assert await unread_count(client, team.owner) == 0

    # 그래도 카드는 큐에 있다 — 알림이 늦어도 질문이 사라지지 않는다 (룰 6 🛟).
    assert await card_for_question(db_session, question_id) is not None


async def test_urgent_notification_is_held_during_dnd(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """DND 구간에서는 **긴급도 보류**한다 — "어떤 즉시 알림도 보내지 않는다" (룰 6).

    보류 시각은 DND **종료 이후**다. 브리핑까지 미루면 아침 9시 전에 깨어난 담당자가 긴급
    건을 못 본다.
    """
    team = await build_team(client, "dnd-hold.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_including_now())

    content_ko = f"{RED_MARKER} 지금 당장 환불 처리해야 하는데 기한이 지났나요?"
    await seed_evidence(db_session, team, content_ko)
    await ask_until_card(client, db_session, team, content_ko, urgency="urgent")

    stored = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_CARD_CREATED)
    assert len(stored) == 1
    assert stored[0].deliver_after is not None, "DND 중에는 긴급도 보류된다"
    assert await inbox(client, team.owner) == []


async def test_green_card_never_notifies_the_answerer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🟢 카드는 큐에만 적재된다 — **알림 대상이 아니다** (룰 1).

    질문자에게는 `answer.completed` 가 정상적으로 간다. 담당자 알림만 빠진다.
    """
    team = await build_team(client, "green-silent.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{GREEN_MARKER} 기본 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko, urgency="urgent")
    assert card.reason == CARD_REASON_GREEN, "🟢 도 카드는 만든다 (룰 6 안전망)"

    assert await stored_notifications(db_session, team.owner.id) == []
    assert [item["type"] for item in await inbox(client, team.asker)] == [
        NOTIFICATION_ANSWER_COMPLETED
    ]


# --- 질문자 알림 (`04 §4`) ----------------------------------------------------------------


async def test_held_question_notifies_the_asker_with_a_hold_message(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 도 `answer.completed` 다 — 발행이 아니라 **보류 안내**다 (`04 §4`).

    알리지 않으면 프론트가 `processing` 인 채로 영원히 폴링한다.
    """
    team = await build_team(client, "held-notify.test")
    content_ko = f"{RED_MARKER} 일본 리전 환불 정책이 있나요?"
    await seed_evidence(db_session, team, content_ko)
    await ask_until_card(client, db_session, team, content_ko)

    items = await inbox(client, team.asker)
    assert [item["type"] for item in items] == [NOTIFICATION_ANSWER_COMPLETED]
    assert "담당자" in items[0]["body"], "보류 안내 문구여야 한다"


async def test_pipeline_failure_notifies_the_asker(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """파이프라인 총 실패 → 질문자 `answer.failed` + 담당자 `card.created` (D23)."""
    team = await build_team(client, "failed-notify.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    # 근거 문서를 심지 않고 임베딩을 실패시켜 ② 이전에 죽인다.
    content_ko = "환불 기한이 며칠인가요?"
    provider = get_provider()
    provider.embed_failure = "[en]"
    try:
        accepted = await ask(client, team.asker, team.project_id, content_ko)
    finally:
        provider.embed_failure = None

    question_id = accepted["question_id"]
    assert [item["type"] for item in await inbox(client, team.asker)] == [
        NOTIFICATION_ANSWER_FAILED
    ]

    # 담당자에게는 `card.created` 가 간다. 이 질문은 비긴급이므로 브리핑까지 보류되고
    # (룰 6) 알림함에는 아직 뜨지 않는다 — 레코드로 확인한다.
    stored = await stored_notifications(db_session, team.owner.id, type=NOTIFICATION_CARD_CREATED)
    assert len(stored) == 1
    assert stored[0].deliver_after is not None

    card = await card_for_question(db_session, question_id)
    assert card is not None and card.reason == "failed"
    assert card.answer_id is None, "답변이 만들어지기 전에 죽었다 (D23)"


# --- 카드 처리 결과 (`04 §4`, `05 §7.1`) ---------------------------------------------------


async def test_approve_notifies_the_asker_that_the_answer_is_verified(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    team = await build_team(client, "approve-notify.test")
    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "approve")
    assert response.status_code == 200, response.text

    types = [item["type"] for item in await inbox(client, team.asker)]
    assert NOTIFICATION_ANSWER_VERIFIED in types


async def test_edit_sends_the_correction_in_both_languages(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """정정 알림은 질문자·담당자 **양쪽 언어**로 간다 (룰 8, `04 §4`).

    한 레코드에 두 언어를 담지 않는다 — 알림함은 수신자별 레코드이므로 **각자의 언어로 한 건씩**
    만들어야 프론트가 그대로 렌더할 수 있다 (`05 §1.5`).
    """
    owner = await create_actor(
        client, "owner@i18n.test", name="Mike", language="en", timezone="UTC"
    )
    project = await create_project(client, owner)
    asker = await create_actor(
        client, "asker@i18n.test", name="지수", language="ko", timezone="UTC"
    )
    await join_project(client, asker, project["invite_code"])
    team = Team(owner, asker, asker, project)
    await patch_settings(client, owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{RED_MARKER} 일본 리전 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(
        client,
        team,
        str(card.id),
        "edit",
        {"content_en": "Japan follows a 20-day refund window under local law."},
    )
    assert response.status_code == 200, response.text

    asker_notifications = [
        item for item in await inbox(client, asker) if item["type"] == NOTIFICATION_ANSWER_CORRECTED
    ]
    answerer_notifications = [
        item for item in await inbox(client, owner) if item["type"] == NOTIFICATION_ANSWER_CORRECTED
    ]
    assert len(asker_notifications) == 1
    assert len(answerer_notifications) == 1

    # 수신자 언어로 각각 만들어졌는지 — 같은 사건, 다른 문자열이다.
    assert asker_notifications[0]["title"] == "답변이 정정되었습니다"
    assert answerer_notifications[0]["title"] == "The answer was corrected"


async def test_keep_and_reject_carry_the_answerer_reason(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """유지·반려 사유가 질문자 알림 본문에 실린다 (`05 §7.1`, `05 §1.5`)."""
    team = await build_team(client, "reason-notify.test")
    content_ko = f"{YELLOW_MARKER} 부분 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    # `keep` 은 재검토 카드에만 유효하다 (`05 §7.1`). 먼저 확정해 원래 카드를 닫아야
    # "달랐다"가 새 `feedback` 카드를 만든다 — 살아 있는 카드가 있으면 기존 카드에 붙는다 (룰 9).
    response = await act(client, team, str(card.id), "approve")
    assert response.status_code == 200, response.text

    answer = await answer_of(db_session, question_id)
    response = await feedback(client, team.asker, str(answer.id), "different", "14일이라 들었어요")
    assert response.status_code == 200, response.text

    review_card = await card_for_question(db_session, question_id, reason="feedback")
    assert review_card is not None
    response = await act(
        client, team, str(review_card.id), "keep", {"reason_en": "The 30-day window is correct."}
    )
    assert response.status_code == 200, response.text

    kept = [
        item for item in await inbox(client, team.asker) if item["type"] == NOTIFICATION_ANSWER_KEPT
    ]
    assert len(kept) == 1
    assert "The 30-day window is correct." in kept[0]["body"]


async def test_reject_notifies_the_asker(client: AsyncClient, db_session: AsyncSession) -> None:
    team = await build_team(client, "reject-notify.test")
    content_ko = f"{YELLOW_MARKER} 환불은 언제까지 가능한가요?"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(
        client, team, str(card.id), "reject", {"reason_en": "Out of scope for this project."}
    )
    assert response.status_code == 200, response.text

    rejected = [
        item
        for item in await inbox(client, team.asker)
        if item["type"] == NOTIFICATION_ANSWER_REJECTED
    ]
    assert len(rejected) == 1
    assert "Out of scope for this project." in rejected[0]["body"]


# --- 피드백 · 문서 갱신 (`04 §4`) ----------------------------------------------------------


async def test_different_feedback_notifies_the_answerer_even_without_a_new_card(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """룰 9 — 살아 있는 카드가 있으면 새 카드는 안 생기지만 **알림은 보낸다**.

    "카드가 안 생겼으니 알릴 것도 없다"로 두면 담당자가 재검토 요청을 영영 모른다.
    """
    team = await build_team(client, "feedback-notify.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{YELLOW_MARKER} 환불 기한이 30일인가요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko, urgency="urgent")
    assert card.status == "pending", "이미 열려 있는 카드가 전제다 (룰 9)"

    answer = await answer_of(db_session, question_id)
    response = await feedback(client, team.asker, str(answer.id), "different", "14일이었어요")
    assert response.status_code == 200, response.text

    # 룰 9 — 기존 카드에 붙었으므로 `feedback` 카드는 만들어지지 않는다.
    assert await card_for_question(db_session, question_id, reason="feedback") is None

    notified = await stored_notifications(
        db_session, team.owner.id, type=NOTIFICATION_FEEDBACK_DIFFERENT
    )
    assert len(notified) == 1
    assert "14일이었어요" in notified[0].body
    # `04 §4` 의 "(urgent 준하여 브리핑 기본)" — `card.created` 와 같은 규칙이므로 urgent 질문의
    # 피드백은 즉시 발송이다.
    assert notified[0].deliver_after is None


async def test_document_activation_notifies_the_answerer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """룰 5 — 새 버전 활성화 **직후** "확정 답변 N건이 재검토 대상입니다"."""
    from tests.review_helpers import seed_new_version

    team = await build_team(client, "doc-notify.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{YELLOW_MARKER} 환불 기한은 며칠인가요?"
    document, _, _ = await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "approve")
    assert response.status_code == 200, response.text

    version = await seed_new_version(db_session, document=document, uploader_id=team.owner.id)
    response = await client.patch(
        f"{API}/documents/{document.id}/versions/{version.id}/activate",
        headers=team.owner.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_cascade_count"] == 1

    notified = await stored_notifications(
        db_session, team.owner.id, type=NOTIFICATION_DOC_REVIEW_NEEDED
    )
    assert len(notified) == 1
    assert "1건" in notified[0].body
    assert notified[0].payload["document_id"] == str(document.id)


async def test_reused_copy_asker_is_told_when_the_body_is_corrected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """D21 — 재사용 답변은 원본의 **사본**이다. 본문이 바뀐 사본의 질문자에게 정정 알림을 보낸다.

    이 경로가 없으면 담당자가 정정한 내용이 원본 질문자에게만 반영되고, 같은 질문을 나중에 한
    사람은 조용히 바뀐 답변을 모르고 지나간다.
    """
    team = await build_team(client, "reused-corrected.test")
    content_ko = f"{RED_MARKER} 일본 리전 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    origin_question_id, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "edit", {"content_en": "First ruling."})
    assert response.status_code == 200, response.text

    # 두 번째 질문자가 같은 질문을 하면 재사용 즉답을 받는다 (룰 4).
    reused_id = (
        await client.post(
            f"{API}/projects/{team.project_id}/questions",
            json={"content_ko": content_ko, "urgency": "normal"},
            headers=team.asker2.headers,
        )
    ).json()["question_id"]
    reused_answer = await answer_of(db_session, reused_id)
    assert reused_answer.source == "reused"

    # 원본에 "달랐다" → 담당자가 수정으로 해소 → 사본 본문까지 정정본으로 맞춰진다.
    origin_answer = await answer_of(db_session, origin_question_id)
    response = await feedback(client, team.asker, str(origin_answer.id), "different", "틀렸습니다")
    assert response.status_code == 200, response.text

    review_card = await card_for_question(db_session, origin_question_id, reason="feedback")
    assert review_card is not None
    response = await act(
        client, team, str(review_card.id), "edit", {"content_en": "Corrected ruling."}
    )
    assert response.status_code == 200, response.text

    corrected = [
        item
        for item in await inbox(client, team.asker2)
        if item["type"] == NOTIFICATION_ANSWER_CORRECTED
    ]
    assert len(corrected) == 1, "사본의 질문자도 정정을 알아야 한다 (D21)"
    assert corrected[0]["payload"]["question_id"] == reused_id


# --- 알림함 API (`05 §11`) ----------------------------------------------------------------


async def test_read_marks_only_my_notifications(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """읽음 처리는 내 것만 — 남의 id 는 **404 가 아니라 무시**한다 (멱등)."""
    team = await build_team(client, "read.test")
    content_ko = f"{GREEN_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    await ask_until_card(client, db_session, team, content_ko)

    items = await inbox(client, team.asker)
    assert len(items) == 1
    target = items[0]["id"]

    # 다른 유저가 같은 id 로 읽음 처리를 시도해도 아무 일도 일어나지 않는다.
    response = await client.post(
        f"{API}/notifications/read", json={"ids": [target]}, headers=team.asker2.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 0
    assert await unread_count(client, team.asker) == 1

    response = await client.post(
        f"{API}/notifications/read", json={"ids": [target]}, headers=team.asker.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1
    assert await unread_count(client, team.asker) == 0

    # 재요청은 멱등이다 (모바일 재시도).
    response = await client.post(
        f"{API}/notifications/read", json={"ids": [target]}, headers=team.asker.headers
    )
    assert response.json()["updated"] == 0

    assert await inbox(client, team.asker, unread_only=True) == []
    assert len(await inbox(client, team.asker)) == 1


async def test_notifications_require_authentication(client: AsyncClient) -> None:
    for method, path in (
        ("get", "/notifications"),
        ("get", "/notifications/unread-count"),
    ):
        response = await getattr(client, method)(f"{API}{path}")
        assert response.status_code == 401, response.text
        assert error_code(response) == "UNAUTHORIZED"


async def test_read_requires_at_least_one_id(client: AsyncClient) -> None:
    """`05 §11` 은 `{ids: [...]}` 를 받는다 — 빈 목록은 400 `VALIDATION_ERROR` 다."""
    team = await build_team(client, "read-validation.test")
    response = await client.post(
        f"{API}/notifications/read", json={"ids": []}, headers=team.asker.headers
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "VALIDATION_ERROR"


async def test_me_reports_unread_and_pending_counts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`05 §2` — `unread_notifications` · `pending_cards` · `unread_notifications_total`.

    ⚠️ `pending_cards` 는 담당자 프로젝트에서만 의미 있고 `asker` 에게는 항상 0 이다.
    """
    team = await build_team(client, "me-counts.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{RED_MARKER} 환불 기한이 지났는데 어떻게 하나요?"
    await seed_evidence(db_session, team, content_ko)
    await ask_until_card(client, db_session, team, content_ko, urgency="urgent")

    response = await client.get(f"{API}/auth/me", headers=team.owner.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    summary = next(item for item in body["projects"] if item["id"] == team.project_id)
    assert summary["unread_notifications"] == 1  # card.created (즉시 발송)
    assert summary["pending_cards"] == 1
    assert body["unread_notifications_total"] == 1

    response = await client.get(f"{API}/auth/me", headers=team.asker.headers)
    body = response.json()
    summary = next(item for item in body["projects"] if item["id"] == team.project_id)
    assert summary["unread_notifications"] == 1  # answer.completed
    assert summary["pending_cards"] == 0, "질문자에게는 항상 0 이다 (`05 §2`)"
