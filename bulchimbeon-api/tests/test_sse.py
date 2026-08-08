"""SSE 티켓 인증 · 스트림 (`05 §12`, `03 §7`).

DoD (`prompts/05-notifications-sse.md`):
- `POST /sse/ticket` → `GET /sse/stream?ticket=` 성공
- **만료·재사용 티켓 401**
- **access token 을 `?token=` 으로 넘기는 경로가 존재하지 않음**
- 파이프라인 완료 → 질문자 스트림에 `answer.completed` 수신
- 스트림 시작 시 `notification.unread_count` 가 1회

> ### ⚠️ 스트림은 **끝나게 만들어** 검증한다
> httpx `ASGITransport` 는 응답을 전부 모은 뒤에 돌려주므로(실측 httpx 0.28.1) 끝나지 않는
> 스트림은 한 줄도 읽을 수 없다. 수명이 짧은 access token 으로 티켓을 받아 서버가 스스로
> 끊게 하고(`05 §12.2` 4번), 그 사이에 이벤트를 발행한다. 근거와 방법은
> `tests/notification_helpers.py` 의 SSE 절 주석에 있다.
"""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.services import sse_manager, sse_ticket_service
from tests.helpers import API, error_code
from tests.notification_helpers import (
    dnd_window_excluding_now,
    event_named,
    issue_ticket,
    parse_sse,
    patch_settings,
    short_lived_access_token,
    wait_for_subscriber,
)
from tests.pipeline_helpers import as_uuid, ask
from tests.review_helpers import GREEN_MARKER, RED_MARKER, build_team, seed_evidence

STREAM = f"{API}/sse/stream"


async def _open_stream(client: AsyncClient, actor) -> "asyncio.Task[object]":
    """스트림을 백그라운드로 열고 구독이 등록될 때까지 기다린다."""
    ticket = await issue_ticket(client, actor, token=short_lived_access_token(actor))
    task = asyncio.create_task(client.get(STREAM, params={"ticket": ticket}))
    await wait_for_subscriber(actor.id)
    return task


async def _events(task: "asyncio.Task[object]") -> list[dict]:
    response = await task
    assert response.status_code == 200, response.text  # type: ignore[union-attr]
    return parse_sse(response.text)  # type: ignore[union-attr]


# --- 티켓 인증 (`05 §12.1`) ---------------------------------------------------------------


async def test_ticket_requires_authorization_header(client: AsyncClient) -> None:
    """티켓은 `Authorization: Bearer` 로만 발급된다 (`05 §12.1`)."""
    response = await client.post(f"{API}/sse/ticket")
    assert response.status_code == 401, response.text
    assert error_code(response) == "UNAUTHORIZED"


async def test_stream_accepts_a_fresh_ticket_and_pushes_unread_count(
    client: AsyncClient,
) -> None:
    """티켓 하나로 스트림이 열리고 **시작 시 미읽음 수가 1회** 온다 (`05 §12.2` 3번)."""
    team = await build_team(client, "sse-ticket.test")

    ticket = await issue_ticket(client, team.asker, token=short_lived_access_token(team.asker))
    response = await client.get(STREAM, params={"ticket": ticket})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    # 네이티브 SSE 가 프록시 버퍼링 대응 헤더를 붙인다 (`03 §1`) — 손으로 넣지 않는다.
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"

    events = parse_sse(response.text)
    assert [message["event"] for message in events] == ["notification.unread_count"]
    assert events[0]["data"] == {"count": 0}


async def test_stream_start_reports_the_existing_unread_count(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """이미 쌓인 미읽음이 그 값으로 내려와야 한다.

    0 만 확인하면 "항상 0 을 보내는" 구현도 통과한다 — 재연결 뱃지 동기화가 이 값에 걸려 있다.
    """
    team = await build_team(client, "sse-unread.test")
    content_ko = f"{GREEN_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    await ask(client, team.asker, team.project_id, content_ko)

    ticket = await issue_ticket(client, team.asker, token=short_lived_access_token(team.asker))
    response = await client.get(STREAM, params={"ticket": ticket})

    events = parse_sse(response.text)
    assert event_named(events, "notification.unread_count")["data"] == {"count": 1}


async def test_ticket_cannot_be_reused(client: AsyncClient) -> None:
    """**검증 즉시 폐기**된다 — 재사용은 401 이다 (`05 §12.1`)."""
    team = await build_team(client, "sse-reuse.test")
    ticket = await issue_ticket(client, team.asker, token=short_lived_access_token(team.asker))

    first = await client.get(STREAM, params={"ticket": ticket})
    assert first.status_code == 200, first.text

    second = await client.get(STREAM, params={"ticket": ticket})
    assert second.status_code == 401, second.text
    assert error_code(second) == "UNAUTHORIZED"


async def test_expired_ticket_is_rejected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTL 60초를 넘긴 티켓은 401 이다 (`05 §12.1`)."""
    team = await build_team(client, "sse-expired.test")
    monkeypatch.setattr(sse_ticket_service, "TICKET_TTL_SECONDS", -1)

    issued = await client.post(f"{API}/sse/ticket", headers=team.asker.headers)
    assert issued.status_code == 200, issued.text

    response = await client.get(STREAM, params={"ticket": issued.json()["ticket"]})
    assert response.status_code == 401, response.text
    assert error_code(response) == "UNAUTHORIZED"


async def test_missing_and_unknown_tickets_are_rejected(client: AsyncClient) -> None:
    """티켓이 없거나 위조된 경우도 401 로 통일한다 — 실패 사유를 구분해 주지 않는다."""
    for params in ({}, {"ticket": "st_forged"}):
        response = await client.get(STREAM, params=params)
        assert response.status_code == 401, response.text
        assert error_code(response) == "UNAUTHORIZED"


async def test_access_token_cannot_be_passed_as_a_query_parameter(client: AsyncClient) -> None:
    """⛔ `?token=` 경로는 **존재하지 않는다** (`05 §12.1`, `03 §7`).

    쿼리스트링은 프록시 로그·리퍼러·브라우저 히스토리에 평문으로 남고 access token 은 30분짜리
    전체 API 자격증명이다. 계약(OpenAPI)과 실제 동작 **양쪽**을 단언한다 — 파라미터 목록만
    보면 라우터가 다른 이름으로 토큰을 받는 구현도 통과한다.
    """
    team = await build_team(client, "sse-token.test")

    parameters = app.openapi()["paths"][STREAM]["get"].get("parameters", [])
    names = {parameter["name"] for parameter in parameters}
    assert names == {"ticket"}, f"스트림은 티켓만 받는다 — 발견된 파라미터: {names}"

    response = await client.get(STREAM, params={"token": team.asker.access_token})
    assert response.status_code == 401, response.text
    assert error_code(response) == "UNAUTHORIZED"


async def test_stream_ends_when_the_access_token_expires(client: AsyncClient) -> None:
    """`05 §12.2` 4번 — **서버가 끊는다.** 프론트는 refresh 후 새 티켓으로 재연결한다.

    끊지 않으면 만료된 자격증명으로 계속 수신하게 된다. 응답이 끝났다는 사실 자체가 증거다.
    """
    team = await build_team(client, "sse-token-expiry.test")
    ticket = await issue_ticket(client, team.asker, token=short_lived_access_token(team.asker))

    async with asyncio.timeout(10):
        response = await client.get(STREAM, params={"ticket": ticket})
    assert response.status_code == 200, response.text
    assert sse_manager.subscriber_count(as_uuid(team.asker.id)) == 0, (
        "스트림이 끝나면 구독이 해제돼야 한다 — 남으면 프로세스 메모리가 계속 자란다"
    )


# --- 이벤트 수신 (`05 §12.3`) -------------------------------------------------------------


async def test_answer_completed_reaches_the_asker_stream(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """⛔ **절대 컷 불가** (결정 1.18) — 데모 4단계가 이 이벤트 하나에 걸려 있다.

    구독을 먼저 열고 질문을 접수한다. 순서를 바꾸면 파이프라인이 발행할 때 구독자가 없어
    이벤트가 사라지고, 실제 사용 순서(화면을 열어 둔 채 질문한다)와도 다르다.
    """
    team = await build_team(client, "sse-answer.test")
    content_ko = f"{GREEN_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)

    task = await _open_stream(client, team.asker)
    accepted = await ask(client, team.asker, team.project_id, content_ko)
    events = await _events(task)

    assert event_named(events, "answer.completed")["data"] == {
        "question_id": accepted["question_id"],
        "grade": "green",
        "status": "answered",
    }
    # 알림함 레코드가 생겼다는 신호도 함께 온다 (`05 §12.3` `notification.created`).
    assert event_named(events, "notification.created")["data"]["type"] == "answer.completed"


async def test_held_question_still_publishes_answer_completed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 보류에도 발행한다 — 알리지 않으면 프론트가 `processing` 으로 영원히 폴링한다."""
    team = await build_team(client, "sse-held.test")
    content_ko = f"{RED_MARKER} 일본 리전 환불 정책이 있나요?"
    await seed_evidence(db_session, team, content_ko)

    task = await _open_stream(client, team.asker)
    await ask(client, team.asker, team.project_id, content_ko)
    events = await _events(task)

    data = event_named(events, "answer.completed")["data"]
    assert data["grade"] == "red"
    assert data["status"] == "held"


async def test_card_created_reaches_the_answerer_stream(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`card.created` 의 수신자는 **담당자**다 (`05 §12.3`).

    같은 파이프라인 실행에서 질문자에게는 `answer.completed`, 담당자에게는 `card.created` 가
    간다 — 유저별로 큐가 갈려 있는지를 함께 확인한다.
    """
    team = await build_team(client, "sse-card.test")
    await patch_settings(client, team.owner, team.project_id, dnd_window_excluding_now())

    content_ko = f"{RED_MARKER} 지금 환불이 가능한가요?"
    await seed_evidence(db_session, team, content_ko)

    task = await _open_stream(client, team.owner)
    await ask(client, team.asker, team.project_id, content_ko, urgency="urgent")
    events = await _events(task)

    data = event_named(events, "card.created")["data"]
    assert data["project_id"] == team.project_id
    assert data["is_urgent"] is True
    # 담당자 스트림에 질문자용 이벤트가 섞이지 않는다.
    assert [message["event"] for message in events].count("answer.completed") == 0


async def test_answer_updated_reaches_the_asker_when_the_card_is_resolved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """확정·정정·반려·재검토는 `answer.updated` 로 질문자에게 간다 (`05 §12.3`)."""
    from tests.review_helpers import act, ask_until_card

    team = await build_team(client, "sse-updated.test")
    content_ko = f"{GREEN_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    question_id, card = await ask_until_card(client, db_session, team, content_ko)

    task = await _open_stream(client, team.asker)
    response = await act(client, team, str(card.id), "approve")
    assert response.status_code == 200, response.text
    events = await _events(task)

    data = event_named(events, "answer.updated")["data"]
    assert data["question_id"] == question_id
    assert data["state"] == "verified"


async def test_card_resolved_reaches_the_answerer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`card.resolved` 는 담당자의 **다른 기기 동기화**용이다 (`05 §12.3`)."""
    from tests.review_helpers import act, ask_until_card

    team = await build_team(client, "sse-resolved.test")
    content_ko = f"{GREEN_MARKER} 환불 기한은 며칠인가요?"
    await seed_evidence(db_session, team, content_ko)
    _, card = await ask_until_card(client, db_session, team, content_ko)

    task = await _open_stream(client, team.owner)
    response = await act(client, team, str(card.id), "approve")
    assert response.status_code == 200, response.text
    events = await _events(task)

    data = event_named(events, "card.resolved")["data"]
    assert data["card_id"] == str(card.id)
    assert data["resolution"] == "approved"
