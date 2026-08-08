"""확인 카드 큐 (`05 §7`) — 목록·상세·액션 매트릭스·중복 차단·권한.

액션 매트릭스는 **서버가 강제한다** (룰 5). 프론트가 표대로 버튼을 그리는 것은 UX 이고,
표 밖의 조합이 차단되는 것은 계약이다.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.question import ANSWER_STATE_EXPIRED, ANSWER_STATE_VERIFIED, Answer
from app.models.review_card import ReviewCard
from app.services import event_service, review_card_service
from tests.helpers import API, error_code
from tests.pipeline_helpers import as_uuid, ask
from tests.review_helpers import (
    GREEN_MARKER,
    RED_MARKER,
    YELLOW_MARKER,
    Team,
    act,
    answer_of,
    ask_until_card,
    build_team,
    card_detail,
    card_for_question,
    list_cards,
    question_detail,
    seed_evidence,
)


@pytest_asyncio.fixture
async def team(client: AsyncClient) -> Team:
    return await build_team(client, "cards.test")


# --------------------------------------------------------------------------------------
# 목록 — 큐 화면은 이 응답만으로 완성된다
# --------------------------------------------------------------------------------------
async def test_queue_item_carries_everything_the_row_needs(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"목록 아이템 계약. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    items = await list_cards(client, team)
    item = next(row for row in items if row["id"] == str(card.id))

    assert set(item) == {
        "id",
        "reason",
        "status",
        "is_urgent",
        "recommend_approve",
        "grade",
        "question_preview_en",
        "question_preview_ko",
        "created_at",
        "first_viewed_at",
    }
    assert item["reason"] == "yellow"
    assert item["grade"] == "yellow"
    assert item["question_preview_ko"].startswith("목록 아이템 계약")
    assert item["question_preview_en"] is not None
    assert item["first_viewed_at"] is None, "아직 안 본 카드 → NEW 뱃지"
    assert question_id


async def test_queue_sorts_by_recommend_then_urgent_then_oldest(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """정렬은 **승인 추천 → 긴급 → 오래된 순**이다 (`05 §7`)."""
    await seed_evidence(db_session, team, f"오래된 질문. {YELLOW_MARKER}")

    _, oldest = await ask_until_card(client, db_session, team, f"오래된 질문. {YELLOW_MARKER}")
    _, urgent = await ask_until_card(
        client, db_session, team, f"급한 질문. {YELLOW_MARKER}", urgency="urgent"
    )
    _, recommended = await ask_until_card(client, db_session, team, f"추천 질문. {YELLOW_MARKER}")

    # 승인 추천은 "맞았다" 2건으로만 켜진다 (룰 3) — 여기서는 정렬만 보므로 직접 세운다.
    recommended.recommend_approve = True
    await db_session.commit()

    order = [row["id"] for row in await list_cards(client, team)]
    assert order.index(str(recommended.id)) == 0
    assert order.index(str(urgent.id)) < order.index(str(oldest.id))


async def test_status_filter_narrows_the_queue(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"보류할 카드. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "defer", {})
    assert response.status_code == 200, response.text

    assert [row["id"] for row in await list_cards(client, team, status="deferred")] == [
        str(card.id)
    ]
    assert not await list_cards(client, team, status="pending")


# --------------------------------------------------------------------------------------
# 상세 — first_viewed_at 은 지표의 시작점이다
# --------------------------------------------------------------------------------------
async def test_detail_records_first_viewed_at_once(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"열람 시각 기록. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    first = await card_detail(client, team, str(card.id))
    second = await card_detail(client, team, str(card.id))

    assert first["first_viewed_at"] is not None
    assert second["first_viewed_at"] == first["first_viewed_at"], "두 번째 조회는 덮어쓰지 않는다"

    viewed = await db_session.scalars(
        select(Event).where(
            Event.entity_id == as_uuid(question_id),
            Event.type == event_service.EVENT_CARD_VIEWED,
        )
    )
    assert len(list(viewed)) == 1, "card.viewed 는 정확히 1회여야 한다 (처리 시간 지표의 분모)"


async def test_detail_is_a_superset_of_the_list_item(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"상세는 목록의 상위 집합이다. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    detail = await card_detail(client, team, str(card.id))

    assert detail["answer_id"] is not None
    assert detail["deferred_until"] is None
    assert detail["pending_feedbacks"] == []
    assert detail["question"]["content_ko"].startswith("상세는 목록의")
    # `draft_answer.citations[]` 는 `05 §6` 과 동일 스키마다 — 팝업에서 근거 원문을 연다.
    citation = detail["draft_answer"]["citations"][0]
    assert {"document_id", "document_version_id", "chunk_id", "quote", "similarity"} <= set(
        citation
    )


async def test_forced_red_card_shows_an_empty_draft(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """강제 🔴 은 발행할 문장이 없다 — 초안이 비어 있고 인용도 없다 (룰 1).

    `reason='failed'`(초안 자체가 `null`)와 구분된다. 담당자는 구조화된 질문을 보고 직접 쓴다.
    """
    content_ko = f"근거가 없는 질문. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    detail = await card_detail(client, team, str(card.id))

    assert detail["draft_answer"] == {"content_en": "", "citations": []}
    assert detail["question_struct"]["options"], "선택지는 그래도 실린다 (룰 8)"


# --------------------------------------------------------------------------------------
# 액션 매트릭스 (`05 §7.1`)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("marker", [GREEN_MARKER, YELLOW_MARKER])
async def test_keep_on_a_non_review_card_is_rejected(
    client: AsyncClient, db_session: AsyncSession, team: Team, marker: str
) -> None:
    """`keep` 은 재검토 카드(feedback·doc_update)에만 유효하다.

    근거는 `04 §6` 상태 전이다 — `under_review → verified` 의 경로가 "수정 저장 or 원안 유지"
    뿐이므로, 아직 재검토가 아닌 카드에는 "원안 유지"라는 개념이 없다.
    """
    content_ko = f"유지할 수 없는 카드. {marker}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "keep", {"reason_en": "Looks fine."})

    assert response.status_code == 409
    assert error_code(response) == "INVALID_CARD_ACTION"


async def test_approve_confirms_and_incorporates(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"승인할 답변. {GREEN_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "approve")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["answer"]["state"] == ANSWER_STATE_VERIFIED
    assert body["official_qa_id"] is not None

    answer = await answer_of(db_session, question_id)
    await db_session.refresh(answer)
    assert answer.verified_by == as_uuid(team.owner.id)
    assert answer.expires_at is None, "확정 답변은 만료 대상이 아니다"


async def test_green_card_sits_in_the_queue_without_notifying(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """🟢 도 카드를 만든다 (룰 6 인박스 안전망). 다만 **알림 대상이 아니다** (룰 1, `04 §4`).

    알림 발행 자체는 M5, 브리핑 제외 검증은 M6 범위다. 여기서는 그 둘이 공유할 판정을 단언한다.
    """
    content_ko = f"즉답 질문. {GREEN_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    assert card.reason == "green"
    assert str(card.id) in [row["id"] for row in await list_cards(client, team)]
    assert review_card_service.notifies_answerer(card) is False

    # 질문자에게는 정상 발행된 상태다 — 카드가 있다고 보류가 아니다.
    detail = await question_detail(client, team.asker, question_id)
    assert detail["status"] == "answered"
    assert detail["answer"]["grade"] == "green"


# --------------------------------------------------------------------------------------
# answer-option — "30초 컷" (`05 §7.2`)
# --------------------------------------------------------------------------------------
async def test_answer_option_confirms_with_the_selected_text(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"선택지로 답한다. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)
    options = card.question_struct["options"]

    response = await act(client, team, str(card.id), "answer-option", {"index": 1})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["selected_option"] == {"index": 1, "text": options[1]}
    assert body["answer"]["content_en"] == options[1]
    assert body["answer"]["state"] == ANSWER_STATE_VERIFIED

    # 동작은 edit 과 완전히 동일하다 — 편입·질문 전이까지 (`05 §7.2`).
    assert body["official_qa_id"] is not None
    assert (await question_detail(client, team.asker, question_id))["status"] == "answered"

    edited = await db_session.scalars(
        select(Event).where(
            Event.entity_id == as_uuid(question_id),
            Event.type == event_service.EVENT_CARD_EDITED,
        )
    )
    payloads = [event.payload for event in edited]
    assert payloads and payloads[0]["selected_option_index"] == 1


async def test_answer_option_rejects_an_out_of_range_index(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"범위 밖 선택지. {RED_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "answer-option", {"index": 99})

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


async def test_answer_option_is_red_only(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """선택지는 🔴 전달용 구조화(⑦)의 산물이므로 다른 등급에는 존재하지 않는다."""
    content_ko = f"선택지가 없는 카드. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "answer-option", {"index": 0})

    assert response.status_code == 409
    assert error_code(response) == "INVALID_CARD_ACTION"


# --------------------------------------------------------------------------------------
# 중복 차단 (기능 4.3)
# --------------------------------------------------------------------------------------
async def test_duplicate_resolution_returns_the_existing_resolution(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """409 body 로 프론트가 "내가 방금 한 것"과 "남이 이미 한 것"을 구분한다 (`05 §1.4`)."""
    content_ko = f"중복 처리. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    assert (await act(client, team, str(card.id), "approve")).status_code == 200

    retry = await act(client, team, str(card.id), "approve")
    assert retry.status_code == 409
    error = retry.json()["error"]

    assert error["code"] == "ALREADY_RESOLVED"
    assert error["resolution"] == "approved"
    assert error["resolved_at"] is not None
    assert error["resolved_by"] == {"id": team.owner.id, "name": "담당자"}


async def test_expired_answer_cannot_be_confirmed_through_its_card(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """`06 §5` 테스트 7 의 e2e — 만료는 **종착 상태**다 (D13).

    스위퍼는 살아 있는 카드 밑의 답변을 죽이지 않으므로(D14) 이 상황은 정상 경로에서 거의
    생기지 않지만, 생겼을 때 조용히 확정되면 만료의 의미가 사라진다.
    """
    content_ko = f"만료될 답변. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    answer = await answer_of(db_session, question_id)
    answer.state = ANSWER_STATE_EXPIRED
    await db_session.commit()

    response = await act(client, team, str(card.id), "approve")

    assert response.status_code == 409
    assert error_code(response) == "INVALID_CARD_ACTION"


# --------------------------------------------------------------------------------------
# defer (`05 §7.3`, D15)
# --------------------------------------------------------------------------------------
async def test_defer_without_until_falls_back_to_the_next_briefing_hour(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"출근 후 처리. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    response = await act(client, team, str(card.id), "defer", None)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "deferred"
    deferred_until = datetime.fromisoformat(body["deferred_until"])
    # 담당자 timezone 은 UTC 이고 기본 `briefing_hour` 는 9 시다 (`04 §3`).
    assert deferred_until.astimezone(UTC).hour == 9
    assert datetime.now(UTC) < deferred_until <= datetime.now(UTC) + timedelta(days=1)


async def test_defer_accepts_an_explicit_until(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"기한 지정. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    until = (datetime.now(UTC) + timedelta(hours=5)).replace(microsecond=0)
    response = await act(client, team, str(card.id), "defer", {"until": until.isoformat()})

    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["deferred_until"]) == until
    # 카드는 사라지지 않는다 (룰 9) — 큐에서 계속 보인다.
    assert [row["id"] for row in await list_cards(client, team, status="deferred")] == [
        str(card.id)
    ]


# --------------------------------------------------------------------------------------
# 실패 카드 (D23)
# --------------------------------------------------------------------------------------
@pytest_asyncio.fixture
async def failed_card(
    client: AsyncClient, db_session: AsyncSession, team: Team, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ReviewCard]:
    from app.services.pipeline import answer as answer_pipeline

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("의도적 실패")

    monkeypatch.setattr(answer_pipeline, "_pipeline", _boom)

    accepted = await ask(client, team.asker, team.project_id, "파이프라인이 죽는 질문")
    card = await card_for_question(db_session, accepted["question_id"])
    assert card is not None, "실패해도 카드는 만들어진다 (D23) — 없으면 질문이 유실된다"
    return accepted["question_id"], card


async def test_failed_card_has_no_answer_to_show(
    client: AsyncClient, team: Team, failed_card: tuple[str, ReviewCard]
) -> None:
    question_id, card = failed_card

    assert card.reason == "failed"
    assert card.answer_id is None

    detail = await card_detail(client, team, str(card.id))
    assert detail["answer_id"] is None
    assert detail["draft_answer"] is None
    assert detail["question_struct"] is None
    assert detail["grade"] is None

    asker_view = await question_detail(client, team.asker, question_id)
    assert asker_view["failure_info"]["card_status"] == "pending"


async def test_failed_card_cannot_be_approved_but_can_be_written(
    client: AsyncClient, db_session: AsyncSession, team: Team, failed_card: tuple[str, ReviewCard]
) -> None:
    """승인할 원안이 없다 — 담당자가 `edit` 으로 직접 쓴다 (`05 §7.1`)."""
    question_id, card = failed_card

    denied = await act(client, team, str(card.id), "approve")
    assert denied.status_code == 409
    assert error_code(denied) == "INVALID_CARD_ACTION"

    response = await act(
        client, team, str(card.id), "edit", {"content_en": "Japan uses a 20-day window."}
    )
    assert response.status_code == 200, response.text
    assert response.json()["answer"]["state"] == ANSWER_STATE_VERIFIED

    # `failed → answered` 전이 (`04 §6.1`). 재처리 API 가 없으므로 해소 경로는 카드뿐이다.
    asker_view = await question_detail(client, team.asker, question_id)
    assert asker_view["status"] == "answered"
    assert asker_view["answer"]["content_en"] == "Japan uses a 20-day window."

    answer = await db_session.scalar(
        select(Answer).where(Answer.question_id == as_uuid(question_id))
    )
    assert answer is not None
    assert answer.grade == "red", "담당자가 직접 쓴 답변은 AI 등급이 없다 — 🔴 로 남긴다"
    assert answer.matching_rate is None


# --------------------------------------------------------------------------------------
# 권한 (룰 5)
# --------------------------------------------------------------------------------------
async def test_asker_cannot_reach_the_queue(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    content_ko = f"질문자는 큐를 볼 수 없다. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    listing = await client.get(
        f"{API}/projects/{team.project_id}/review-cards", headers=team.asker.headers
    )
    detail = await client.get(f"{API}/review-cards/{card.id}", headers=team.asker.headers)
    action = await act(client, team, str(card.id), "approve", actor=team.asker)

    for response in (listing, detail, action):
        assert response.status_code == 403, response.text
        assert error_code(response) == "FORBIDDEN_ROLE"


async def test_cards_follow_the_answerer_handover(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    """담당자 교체 시 미처리 카드는 신규 담당자에게 따라간다 (기능 6.3).

    `review_cards` 가 `project_id` 스코프라 이관 UPDATE 가 없다 — 그래서 **누락될 수 없다**.
    """
    content_ko = f"이관될 카드. {YELLOW_MARKER}"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    transfer = await client.post(
        f"{API}/projects/{team.project_id}/transfer-answerer",
        json={"new_answerer_id": team.asker.id},
        headers=team.owner.headers,
    )
    assert transfer.status_code == 200, transfer.text

    new_queue = await client.get(
        f"{API}/projects/{team.project_id}/review-cards", headers=team.asker.headers
    )
    assert new_queue.status_code == 200
    assert [row["id"] for row in new_queue.json()["items"]] == [str(card.id)]

    old_queue = await client.get(
        f"{API}/projects/{team.project_id}/review-cards", headers=team.owner.headers
    )
    assert old_queue.status_code == 403, "구담당자는 질문자로 남는다 (D16)"
