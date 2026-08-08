"""SSE 단명 티켓 (`05 §12.1`, `03 §7`).

> ### ⛔ access token 을 URL 에 싣지 않는다
> `EventSource` 는 헤더를 실을 수 없어 쿼리로 인증해야 한다. 그렇다고 access token 을 쿼리에
> 넣으면 **프록시·리버스프록시 액세스 로그·브라우저 히스토리·리퍼러에 평문으로 남고**, 그것은
> 30분 수명의 **전체 API 자격증명**이다. 대신 **TTL 60초 · 1회용 · SSE 전용** 티켓을 쓴다 —
> 유출돼도 만료·소진 후에는 무가치하다.

> ### 스트림 수명은 access token 이 정한다 (`05 §12.2` 4번)
> 티켓 발급 시 그 요청의 access token 만료 시각을 함께 적어 둔다. 스트림은 그 시각에 서버가
> 끊고, 프론트는 `POST /auth/refresh` 후 **새 티켓**으로 재연결한다. 티켓 TTL(60초)로 스트림
> 수명을 대신하면 1분마다 끊기고, 무제한으로 두면 만료된 자격증명으로 계속 수신하게 된다.

⚠️ 저장소는 **인메모리**다 — SSE 구독자 큐와 같은 `--workers 1` 전제 위에 있다 (룰 9).
워커가 2개면 다른 워커가 발급한 티켓을 검증할 수 없다. 확장 시점의 정답은 Redis(TTL 키) 또는
서명된 단명 토큰(별도 키·짧은 exp)이다.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.errors import Unauthorized

logger = logging.getLogger(__name__)

# `05 §12.1` 이 `expires_in: 60` 으로 못박은 값이다. `projects.settings` 의 임계값 목록
# (`04 §3`)에 없으므로 프로젝트 설정으로 두지 않는다 — 계약된 상수다.
TICKET_TTL_SECONDS = 60

_TICKET_BYTES = 24
_TICKET_PREFIX = "st_"

# 티켓 문자열이 URL 로 오가므로, 로그에 그대로 남기지 않는다 (`03 §7` 토큰 로그 금지).
_LOG_PREFIX_LENGTH = 6


@dataclass(frozen=True)
class Ticket:
    user_id: UUID
    expires_at: datetime
    # 이 티켓을 발급받을 때 쓴 access token 의 만료 시각 = 스트림의 상한이다.
    access_expires_at: datetime


_tickets: dict[str, Ticket] = {}


def issue(*, user_id: UUID, access_expires_at: datetime) -> tuple[str, int]:
    """1회용 티켓을 발급한다. 돌려주는 값은 `(ticket, expires_in)` 이다 (`05 §12.1`)."""
    _purge_expired()

    ticket = f"{_TICKET_PREFIX}{secrets.token_urlsafe(_TICKET_BYTES)}"
    _tickets[ticket] = Ticket(
        user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=TICKET_TTL_SECONDS),
        access_expires_at=access_expires_at,
    )
    return ticket, TICKET_TTL_SECONDS


def consume(ticket: str | None) -> Ticket:
    """검증 **즉시 폐기**한다. 만료·재사용·위조는 전부 401 `UNAUTHORIZED` 다 (`05 §12.1`).

    ⚠️ 실패 사유를 구분해 응답하지 않는다 — "그 티켓은 있었지만 만료됐다"와 "그런 티켓은
    없다"를 구분해 주면 티켓 추측 공격에 신호를 준다.
    """
    if not ticket:
        raise Unauthorized()

    found = _tickets.pop(ticket, None)  # 소진 = 즉시 폐기. 재사용은 여기서 자동으로 막힌다.
    if found is None:
        logger.info("sse 티켓 검증 실패(없음·재사용): %s…", ticket[:_LOG_PREFIX_LENGTH])
        raise Unauthorized()

    if found.expires_at <= datetime.now(UTC):
        logger.info("sse 티켓 검증 실패(만료): %s…", ticket[:_LOG_PREFIX_LENGTH])
        raise Unauthorized()
    return found


def reset() -> None:
    """테스트용 — 프로세스 메모리를 비운다."""
    _tickets.clear()


def _purge_expired() -> None:
    """발급 때마다 만료분을 걷어낸다 — 소진되지 않은 티켓이 메모리에 쌓이지 않게."""
    now = datetime.now(UTC)
    for ticket in [key for key, value in _tickets.items() if value.expires_at <= now]:
        _tickets.pop(ticket, None)
