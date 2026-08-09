"""M5 알림·SSE 테스트 헬퍼.

> ### ⚠️ DND 창은 **실행 시각 기준으로 계산한다**
> 기본값 `22:00~07:00` 을 그대로 두면 테스트가 **밤에 돌 때만** DND 안이 되어 결과가 뒤집힌다.
> "즉시 발송"을 단언하는 테스트가 밤 10시 이후 CI 에서만 깨지는 종류의 실패다.
> 그래서 지금 시각을 포함/제외하는 창을 그때그때 만들어 PATCH 로 밀어 넣는다.
>
> M9 가 같은 함정을 픽스처 층에서도 막았다 — `build_team` 이 `helpers.close_dnd_window` 로
> 창을 아예 닫아 둔다. 따라서 `dnd_window_excluding_now()` 는 이제 **방어적 중복**이고,
> `dnd_window_including_now()` 만이 필수다(닫힌 창을 다시 열어야 하므로).
> 중복을 남겨 두는 이유는 이 헬퍼를 쓰는 테스트가 "지금이 DND 밖"이라는 전제를 **자기 안에서**
> 밝히는 편이 읽기 쉽기 때문이다.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.notification import Notification
from app.services import sse_manager
from tests.helpers import API, Actor
from tests.pipeline_helpers import as_uuid

# 스트림 읽기 상한. 이벤트가 오지 않으면 테스트가 멈추지 말고 **실패**해야 한다.
STREAM_TIMEOUT_SECONDS = 10.0


def dnd_window_including_now() -> dict[str, str]:
    """지금이 반드시 DND 안이 되는 창 (담당자 타임존이 UTC 인 팀 기준)."""
    now = datetime.now(UTC)
    return {
        "dnd_start": (now - timedelta(hours=1)).strftime("%H:%M"),
        "dnd_end": (now + timedelta(hours=1)).strftime("%H:%M"),
    }


def dnd_window_excluding_now() -> dict[str, str]:
    """지금이 반드시 DND 밖이 되는 창."""
    now = datetime.now(UTC)
    return {
        "dnd_start": (now + timedelta(hours=2)).strftime("%H:%M"),
        "dnd_end": (now + timedelta(hours=3)).strftime("%H:%M"),
    }


async def patch_settings(
    client: AsyncClient, actor: Actor, project_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    response = await client.patch(
        f"{API}/projects/{project_id}/settings", json=settings, headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- 알림함 (`05 §11`) --------------------------------------------------------------------
async def inbox(
    client: AsyncClient, actor: Actor, *, unread_only: bool = False
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{API}/notifications", params={"unread_only": unread_only}, headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def unread_count(client: AsyncClient, actor: Actor) -> int:
    response = await client.get(f"{API}/notifications/unread-count", headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()["count"]


async def stored_notifications(
    db: AsyncSession, user_id: str, *, type: str | None = None
) -> list[Notification]:
    """**DB 직접 조회** — 보류 알림(`deliver_after` 미래)은 API 로는 보이지 않는다 (룰 6).

    "알림함에 안 뜨는데 레코드는 있다"를 단언해야 하므로 두 경로를 함께 본다.
    """
    stmt = select(Notification).where(Notification.user_id == as_uuid(user_id))
    if type is not None:
        stmt = stmt.where(Notification.type == type)
    rows = await db.scalars(stmt.order_by(Notification.created_at.asc()))
    return list(rows.all())


# --- SSE (`05 §12`) ----------------------------------------------------------------------
#
# > ### ⚠️ httpx `ASGITransport` 는 **응답을 전부 모은 뒤에** 돌려준다 (실측 httpx 0.28.1)
# > `handle_async_request` 가 `await self.app(...)` 로 앱을 끝까지 돌리고 `body_parts` 를
# > 합친 다음 `assert response_complete.is_set()` 을 한다. 즉 **끝나지 않는 스트림은
# > `client.stream(...)` 으로도 한 줄도 읽을 수 없고 테스트가 그대로 멈춘다.**
# >
# > 그래서 스트림을 **끝나게** 만들어 검증한다: access token 이 만료되면 서버가 스트림을
# > 종료하므로(`05 §12.2` 4번) 수명이 짧은 access token 으로 티켓을 받는다. 그 덕에
# > "토큰 만료 시 서버가 끊는다"는 계약까지 같은 테스트가 함께 확인한다.
# > 이벤트를 **스트림이 열려 있는 동안** 발행해야 하므로 요청은 `asyncio.Task` 로 띄우고
# > 그 사이에 질문을 접수한다 (같은 이벤트 루프에서 교대로 돈다).
STREAM_LIFETIME_SECONDS = 2


def short_lived_access_token(actor: Actor, seconds: int = STREAM_LIFETIME_SECONDS) -> str:
    """스트림 수명을 좌우하는 access token (`05 §12.2` 4번)."""
    return create_access_token(as_uuid(actor.id), expires_delta=timedelta(seconds=seconds))


async def issue_ticket(client: AsyncClient, actor: Actor, *, token: str | None = None) -> str:
    """`POST /sse/ticket` — `Authorization: Bearer` 로만 발급된다 (`05 §12.1`)."""
    headers = {"Authorization": f"Bearer {token}"} if token else actor.headers
    response = await client.post(f"{API}/sse/ticket", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == 60, "`05 §12.1` 이 TTL 60초를 못박았다"
    return body["ticket"]


async def wait_for_subscriber(user_id: str, *, timeout: float = STREAM_TIMEOUT_SECONDS) -> None:
    """스트림이 구독을 등록할 때까지 기다린다.

    구독 전에 이벤트를 발행하면 받을 사람이 없어 조용히 사라진다 — 실제 사용 순서(화면을
    열어 둔 채 질문한다)를 재현하려면 이 동기화가 필요하다.
    """
    async with asyncio.timeout(timeout):
        while sse_manager.subscriber_count(as_uuid(user_id)) == 0:
            await asyncio.sleep(0.01)


def parse_sse(body: str) -> list[dict[str, Any]]:
    """`event: {type}` + `data: {json}` 블록들을 파싱한다 (`05 §12.2`)."""
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        event: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        if event is None and not data_lines:
            continue
        events.append(
            {
                "event": event,
                "data": json.loads("\n".join(data_lines)) if data_lines else None,
            }
        )
    return events


def event_named(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """이름으로 하나 골라낸다.

    ⚠️ 순서를 단언하지 않는다 — `05 §12.3` 은 이벤트 순서를 규정하지 않고 한 커밋에서 여러
    건이 함께 나간다(예: `notification.created` + `answer.completed`).
    """
    found = [message for message in events if message["event"] == name]
    assert found, f"{name} 를 받지 못했다 — 받은 것: {[m['event'] for m in events]}"
    assert len(found) == 1, f"{name} 가 {len(found)}번 왔다"
    return found[0]
