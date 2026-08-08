"""SSE 스트림 본체 (`05 §12.2`).

> ### ⚠️ 스트림은 요청 세션을 붙잡지 않는다
> 스트림은 access token 만료까지(기본 30분) 살아 있는데, FastAPI 의 `yield` 의존성은
> **스트리밍이 끝날 때까지** 정리되지 않는다(`fastapi/routing.py` — "The stack outlives the
> streaming response"). `Depends(get_db)` 로 세션을 받으면 SSE 클라이언트 수만큼 커넥션이
> 풀에서 빠져나가 **접속자 몇 명으로 앱 전체가 멈춘다.**
> 그래서 시작 시 미읽음 수를 읽는 데만 **자체 세션을 열고 즉시 닫는다.** 이후 루프는 DB 를
> 건드리지 않는다 (인메모리 큐만 본다).

> ### 재연결은 예외가 아니라 정상 동작이다 (`05 §12.2`)
> Railway 는 SSE 를 15분에 강제 종료한다. **서버는 재연결을 전제로 설계한다** — 재생(replay)
> 계약이 없고, 프론트는 재연결 시 구독 리소스를 전량 재조회한다. 서버가 보장하는 것은
> 딱 하나, **스트림 시작 시 미읽음 수 1회 push** 다(뱃지를 맞출 유일한 수단).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from fastapi.sse import ServerSentEvent
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.services import notification_service, sse_manager
from app.services.sse_ticket_service import Ticket

logger = logging.getLogger(__name__)

# `05 §12.2` 5번은 "15초마다 ping" 이다. 14초로 두는 이유는 FastAPI 네이티브 keepalive 가
# 15초에 `: ping` **주석**을 끼워 넣기 때문이다 — 우리가 먼저 `event: ping` 을 내보내야
# 프론트가 관측 가능한 형태로 받는다 (`sse_manager.SSE_PING` 주석).
PING_INTERVAL_SECONDS = 14.0


def _default_session_factory() -> AsyncSession:
    return AsyncSessionLocal()


# ⚠️ 테스트가 갈아끼우는 유일한 지점이다 (`ingest.session_factory`·`answer.session_factory` 와
#    같은 이유 — 테스트 세션은 바깥 트랜잭션에 물린 커넥션을 공유해야 롤백 격리가 성립한다).
session_factory: Callable[[], AsyncSession] = _default_session_factory


async def stream(ticket: Ticket) -> AsyncIterator[ServerSentEvent]:
    """티켓 하나에 대응하는 이벤트 스트림.

    ⚠️ **구독을 먼저 열고 미읽음 수를 읽는다.** 순서를 바꾸면 그 사이에 생긴 알림이 카운트에도
    안 잡히고 이벤트로도 오지 않아 뱃지가 영구히 어긋난다. 반대 순서면 최악이 "한 번 더 세는
    것"인데, 프론트는 `notification.created` 를 받고 카운트를 재조회하므로 곧 맞춰진다.
    """
    with sse_manager.subscribe(ticket.user_id) as queue:
        async with session_factory() as db:
            unread = await notification_service.unread_count(db, ticket.user_id)

        # `05 §12.2` 3번 — 스트림 시작 시 1회. 재연결 후 뱃지 동기화의 유일한 수단이다.
        yield ServerSentEvent(
            event=sse_manager.SSE_NOTIFICATION_UNREAD_COUNT, data={"count": unread}
        )

        while True:
            remaining = (ticket.access_expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                # `05 §12.2` 4번 — access token 이 만료되면 **서버가 끊는다.** 프론트는 refresh
                # 후 새 티켓으로 재연결한다. 끊지 않으면 만료된 자격증명으로 계속 수신한다.
                logger.info("sse 스트림 종료(access token 만료): user=%s", ticket.user_id)
                return

            wait = min(PING_INTERVAL_SECONDS, remaining)
            message = await _next_message(queue, wait)
            if message is not None:
                yield ServerSentEvent(event=message.event, data=message.data)
                continue

            if wait < PING_INTERVAL_SECONDS:
                # 토큰 만료 때문에 짧아진 대기였다 — 곧 끊을 스트림에 하트비트를 보내지 않는다.
                continue
            yield ServerSentEvent(event=sse_manager.SSE_PING, data={})


async def _next_message(
    queue: "asyncio.Queue[sse_manager.Message]", timeout: float
) -> sse_manager.Message | None:
    """다음 이벤트를 기다린다. 시간이 다 되면 `None`(= ping 을 보낼 차례).

    ⚠️ 타임아웃과 `put_nowait` 가 경합하면 `await queue.get()` 이 취소되면서 이미 넘겨진 항목이
    사라질 수 있다. 취소 직후 `get_nowait` 로 한 번 더 확인해 그 창을 닫는다 — 유실은
    재연결로도 복구되지 않는 구간(연결이 살아 있으므로 프론트가 재조회하지 않는다)이다.
    """
    try:
        async with asyncio.timeout(timeout):
            return await queue.get()
    except TimeoutError:
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
