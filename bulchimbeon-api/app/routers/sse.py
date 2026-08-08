"""SSE 라우터 (`05 §12`) — 단명 티켓 발급 + 이벤트 스트림.

> ### ⛔ `?token=` 은 **존재하지 않는 경로**다 (`05 §12.1`, `03 §7`)
> 스트림은 `?ticket=` 만 받는다. access token 을 쿼리로 받는 파라미터를 만들지 않는다 —
> 쿼리스트링은 프록시 로그·리퍼러·브라우저 히스토리에 평문으로 남고 access token 은 30분짜리
> 전체 API 자격증명이다.

⚠️ **티켓 검증은 의존성에서 한다.** 제너레이터 본문에서 던지면 이미 스트리밍이 시작된 뒤라
401 응답을 만들 수 없다 — 계약서가 요구한 "만료·재사용 티켓 → 401 `UNAUTHORIZED`" 가
스트림 중간의 에러로 바뀐다.
"""

from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.core.deps import get_access_token_expiry, get_current_user
from app.models.user import User
from app.schemas.notification import SSETicketResponse
from app.services import sse_stream_service, sse_ticket_service
from app.services.sse_ticket_service import Ticket

router = APIRouter(prefix="/sse", tags=["sse"])


async def require_sse_ticket(
    # ⚠️ 필수 파라미터로 두지 않는다. 빠졌을 때 400 `VALIDATION_ERROR` 가 나가면 "티켓이라는
    #    파라미터가 있다"는 사실만 알려 주는 셈이고, 계약서의 인증 실패 코드는 401 이다.
    ticket: str | None = Query(default=None),
) -> Ticket:
    """티켓 검증 — **소진(연결 성립) 즉시 폐기**된다 (`05 §12.1`)."""
    return sse_ticket_service.consume(ticket)


@router.post("/ticket", response_model=SSETicketResponse)
async def create_ticket(
    user: User = Depends(get_current_user),
    access_expires_at: datetime = Depends(get_access_token_expiry),
) -> SSETicketResponse:
    """1회용 스트림 티켓 (`05 §12.1`) — `Authorization: Bearer` 헤더로 발급받는다.

    **재연결마다 새로 발급한다.** TTL 60초이며 스트림 성립 시 폐기된다.
    """
    ticket, expires_in = sse_ticket_service.issue(
        user_id=user.id, access_expires_at=access_expires_at
    )
    return SSETicketResponse(ticket=ticket, expires_in=expires_in)


@router.get("/stream", response_class=EventSourceResponse)
async def stream_events(
    ticket: Ticket = Depends(require_sse_ticket),
) -> AsyncIterator[ServerSentEvent]:
    """이벤트 스트림 (`05 §12.2`·`§12.3`).

    FastAPI 네이티브 SSE 를 쓴다 — `sse-starlette` 를 추가하지 않는다 (`03 §1`). 네이티브가
    `Cache-Control: no-cache` 와 **`X-Accel-Buffering: no`** 를 붙여 주므로 프록시 버퍼링
    대응을 손으로 하지 않는다.

    ⚠️ 본문은 서비스에 있다 — 스트림이 요청 세션을 붙잡지 않도록 자체 세션을 열고 닫아야 하고
    (`sse_stream_service` 독스트링), 그 세션 팩토리가 테스트의 교체 지점이다.
    """
    async for message in sse_stream_service.stream(ticket):
        yield message
