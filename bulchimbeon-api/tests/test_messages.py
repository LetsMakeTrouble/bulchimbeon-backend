"""대화 메시지 (`05 §6.1`) — 사람 간 양방향 채널.

- **멤버면 역할 무관** POST/GET (담당자 발화가 이 채널의 존재 이유다). 비멤버는 403 `NOT_MEMBER`.
- 목록은 `05 §1.2` 봉투 + created_at 오름차순. sender 는 `{id, name, role}` 다.
- 빈/공백 content 는 요청 형식 오류다 — 이 리포는 전역 핸들러가 422 를 400
  `VALIDATION_ERROR` 로 통일한다 (`05 §1.4`, `main.py`).
- 커밋 후 SSE `message.created` 가 프로젝트 활성 멤버 전원(발신자 포함)에게 나간다.
- 룰 4 — `message.created` 이벤트가 events 에 남는다.
"""

import asyncio
from typing import Any
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.services import event_service
from tests.helpers import API, Actor, create_actor, error_code
from tests.notification_helpers import (
    event_named,
    issue_ticket,
    parse_sse,
    short_lived_access_token,
    wait_for_subscriber,
)
from tests.pipeline_helpers import as_uuid
from tests.review_helpers import Team, build_team


def _messages_url(project_id: str) -> str:
    return f"{API}/projects/{project_id}/messages"


async def _post_message(
    client: AsyncClient, team: Team, actor: Actor, content: str
) -> dict[str, Any]:
    response = await client.post(
        _messages_url(team.project_id), json={"content": content}, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# 발송 — 역할 무관 (질문 POST 와 다른 지점)
# --------------------------------------------------------------------------------------
async def test_asker_can_post_a_message(client: AsyncClient) -> None:
    team = await build_team(client, "msg-asker.test")

    body = await _post_message(client, team, team.asker, "일본 리전 건은 어떻게 되고 있나요?")

    assert body["content"] == "일본 리전 건은 어떻게 되고 있나요?"
    assert body["sender"] == {"id": team.asker.id, "name": "지수", "role": "asker"}
    assert body["created_at"] is not None
    UUID(body["id"])  # id 는 UUID 다


async def test_answerer_can_post_a_message(client: AsyncClient) -> None:
    """담당자 발화 채널 — POST /questions 는 asker 전용(403)이라 여기가 유일한 경로다."""
    team = await build_team(client, "msg-answerer.test")

    body = await _post_message(client, team, team.owner, "확인해 보겠습니다.")

    assert body["sender"] == {"id": team.owner.id, "name": "담당자", "role": "answerer"}


async def test_non_member_cannot_post_or_read(client: AsyncClient) -> None:
    """비멤버 403 `NOT_MEMBER` — 기존 멤버 확인 의존성 규약 그대로 (`core/deps.py`)."""
    team = await build_team(client, "msg-stranger.test")
    stranger = await create_actor(client, "stranger@msg-stranger.test")

    posted = await client.post(
        _messages_url(team.project_id), json={"content": "안녕하세요"}, headers=stranger.headers
    )
    assert posted.status_code == 403, posted.text
    assert error_code(posted) == "NOT_MEMBER"

    listed = await client.get(_messages_url(team.project_id), headers=stranger.headers)
    assert listed.status_code == 403, listed.text
    assert error_code(listed) == "NOT_MEMBER"


async def test_blank_content_is_a_validation_error(client: AsyncClient) -> None:
    """빈 문자열·공백만 거부. 전역 핸들러가 400 `VALIDATION_ERROR` 로 내린다 (`05 §1.4`)."""
    team = await build_team(client, "msg-blank.test")

    for content in ("", "   ", "\n\t "):
        response = await client.post(
            _messages_url(team.project_id), json={"content": content}, headers=team.asker.headers
        )
        assert response.status_code == 400, response.text
        assert error_code(response) == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# 목록 — 봉투·정렬·sender·페이지네이션
# --------------------------------------------------------------------------------------
async def test_list_returns_ascending_messages_with_sender(client: AsyncClient) -> None:
    team = await build_team(client, "msg-list.test")
    await _post_message(client, team, team.asker, "첫 번째")
    await _post_message(client, team, team.owner, "두 번째")
    await _post_message(client, team, team.asker2, "세 번째")

    response = await client.get(_messages_url(team.project_id), headers=team.owner.headers)
    assert response.status_code == 200, response.text
    body = response.json()

    # `05 §1.2` 봉투.
    assert body["total"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 0

    # created_at 오름차순 — 채팅 화면 순서다.
    assert [item["content"] for item in body["items"]] == ["첫 번째", "두 번째", "세 번째"]
    assert [item["sender"] for item in body["items"]] == [
        {"id": team.asker.id, "name": "지수", "role": "asker"},
        {"id": team.owner.id, "name": "담당자", "role": "answerer"},
        {"id": team.asker2.id, "name": "민호", "role": "asker"},
    ]


async def test_list_pagination(client: AsyncClient) -> None:
    team = await build_team(client, "msg-page.test")
    for index in range(5):
        await _post_message(client, team, team.asker, f"메시지 {index}")

    response = await client.get(
        _messages_url(team.project_id),
        params={"limit": 2, "offset": 2},
        headers=team.asker.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 2
    # 오름차순의 3·4번째 — offset 이 앞(오래된 쪽)에서부터 걷는다.
    assert [item["content"] for item in body["items"]] == ["메시지 2", "메시지 3"]


# --------------------------------------------------------------------------------------
# SSE `message.created` — 활성 멤버 전원 (발신자 포함)
# --------------------------------------------------------------------------------------
async def _open_stream(client: AsyncClient, actor: Actor) -> "asyncio.Task[object]":
    """test_sse 와 같은 방식 — 짧은 토큰으로 스트림을 끝나게 만들어 검증한다."""
    ticket = await issue_ticket(client, actor, token=short_lived_access_token(actor))
    task = asyncio.create_task(client.get(f"{API}/sse/stream", params={"ticket": ticket}))
    await wait_for_subscriber(actor.id)
    return task


async def _events_of(task: "asyncio.Task[object]") -> list[dict]:
    response = await task
    assert response.status_code == 200, response.text  # type: ignore[union-attr]
    return parse_sse(response.text)  # type: ignore[union-attr]


async def test_message_created_reaches_other_members(client: AsyncClient) -> None:
    """질문자의 발화가 담당자 스트림에 `message.created` 로 도착한다 (`05 §12.3`).

    ⚠️ 스트림은 한 번에 하나만 연다 — 테스트 세션들이 **한 커넥션**을 세이브포인트로 나눠
    쓰므로(conftest) 동시 스트림 둘은 세이브포인트 해제 순서가 꼬인다. 발신자 본인 수신은
    아래 테스트가 따로 본다.
    """
    team = await build_team(client, "msg-sse.test")

    owner_task = await _open_stream(client, team.owner)
    body = await _post_message(client, team, team.asker, "새 메시지입니다")

    expected = {"message_id": body["id"], "project_id": team.project_id}
    assert event_named(await _events_of(owner_task), "message.created")["data"] == expected


async def test_message_created_reaches_the_sender_too(client: AsyncClient) -> None:
    """**발신자 본인**도 받는다 — 다른 탭·기기 동기화, `card.resolved` 와 같은 규약."""
    team = await build_team(client, "msg-sse-self.test")

    sender_task = await _open_stream(client, team.asker)
    body = await _post_message(client, team, team.asker, "내 다른 탭에도 떠야 한다")

    expected = {"message_id": body["id"], "project_id": team.project_id}
    assert event_named(await _events_of(sender_task), "message.created")["data"] == expected


# --------------------------------------------------------------------------------------
# events 기록 (룰 4)
# --------------------------------------------------------------------------------------
async def test_message_created_event_is_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    team = await build_team(client, "msg-event.test")
    body = await _post_message(client, team, team.owner, "이력에 남아야 한다")

    event = await db_session.scalar(
        select(Event).where(
            Event.project_id == as_uuid(team.project_id),
            Event.type == event_service.EVENT_MESSAGE_CREATED,
        )
    )
    assert event is not None
    assert event.actor_id == as_uuid(team.owner.id)
    assert event.entity_type == "message"
    assert event.entity_id == UUID(body["id"])
