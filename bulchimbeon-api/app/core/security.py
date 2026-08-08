"""비밀번호 해시와 JWT 발급·검증 (`03 §1`·`§7`).

- 해시: argon2 (`pwdlib[argon2]`).
- JWT: `python-jose[cryptography]>=3.4.0` — 하한 핀은 CVE-2024-33663 / CVE-2024-33664 때문이며
  대체 라이브러리 선택지를 두지 않는다 (`03 §1`).
- `sub` = user_id, access 30분 / refresh 14일 (만료는 `config.Settings` 에서 온다).

access 와 refresh 를 `type` 클레임으로 구분한다. 구분이 없으면 14일짜리 refresh 토큰을
Authorization 헤더에 그대로 넣어 30분 만료를 우회할 수 있다.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from jose import ExpiredSignatureError, JWTError, jwt
from pwdlib import PasswordHash

from app.config import settings
from app.core.errors import TokenExpired, Unauthorized

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"

# PasswordHash.recommended() 는 argon2 를 쓴다 (`04 §2` users.password_hash).
_password_hash = PasswordHash.recommended()


def hash_password(raw_password: str) -> str:
    return _password_hash.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    """검증 실패는 예외가 아니라 False 다.

    저장된 해시가 손상됐을 때 pwdlib 이 던지는 예외를 그대로 흘리면 로그인 실패가 500 이 된다.
    """
    try:
        return _password_hash.verify(raw_password, password_hash)
    except Exception:
        return False


def _create_token(user_id: UUID, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return jwt.encode(claims, settings.secret_key, algorithm=ALGORITHM)


def create_access_token(user_id: UUID, *, expires_delta: timedelta | None = None) -> str:
    """`expires_delta` 는 테스트가 만료 토큰을 만들기 위한 구멍이다(음수 허용)."""
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    return _create_token(user_id, ACCESS_TOKEN_TYPE, delta)


def create_refresh_token(user_id: UUID, *, expires_delta: timedelta | None = None) -> str:
    delta = expires_delta or timedelta(days=settings.refresh_token_expire_days)
    return _create_token(user_id, REFRESH_TOKEN_TYPE, delta)


def access_token_expires_at(token: str) -> datetime:
    """access token 의 만료 시각 — **SSE 스트림의 상한**이다 (`05 §12.2` 4번).

    티켓 TTL(60초)로 스트림 수명을 대신할 수 없고(1분마다 끊긴다), 무제한으로 두면 만료된
    자격증명으로 계속 수신하게 된다. 그래서 티켓 발급 시 이 값을 함께 적어 둔다.

    검증은 `decode_token` 이 이미 한 뒤에 부르는 것을 전제로 하지만, 여기서도 같은 예외
    체계를 통과시켜 두 경로가 어긋나지 않게 한다.
    """
    try:
        claims: dict[str, Any] = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except ExpiredSignatureError as exc:
        raise TokenExpired() from exc
    except JWTError as exc:
        raise Unauthorized() from exc

    exp = claims.get("exp")
    if not isinstance(exp, int | float):
        raise Unauthorized()
    return datetime.fromtimestamp(float(exp), tz=UTC)


def decode_token(token: str, *, expected_type: str) -> UUID:
    """토큰을 검증하고 user_id 를 돌려준다.

    만료는 401 `TOKEN_EXPIRED`, 그 밖의 실패는 401 `UNAUTHORIZED` 다 (`05 §1.4`).
    """
    try:
        claims: dict[str, Any] = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except ExpiredSignatureError as exc:
        # ⚠️ ExpiredSignatureError 는 JWTError 의 하위 클래스다 — 반드시 먼저 잡는다.
        raise TokenExpired() from exc
    except JWTError as exc:
        raise Unauthorized() from exc

    if claims.get("type") != expected_type:
        raise Unauthorized("토큰 종류가 올바르지 않습니다.")

    try:
        return UUID(str(claims.get("sub")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise Unauthorized() from exc
