"""연동 토큰 대칭 암호화 — **암복호화의 유일한 구현** (`03 §7`, `08 §1`).

`INTEGRATION_ENCRYPTION_KEY` 로 Fernet 암호화한 뒤 `integrations.config` 의 비밀 필드에
문자열로 넣는다. 어느 필드가 비밀인지의 정의는 `services/integration_service.py` 의
provider 스펙 한 곳이고, 여기는 "어떻게 감추는가"만 안다.

> ### env 값은 정식 Fernet 키가 **아니어도 받는다**
> `03 §4` 의 예시가 `INTEGRATION_ENCRYPTION_KEY=change-me-32bytes` 인데 이것은 Fernet 이
> 요구하는 형식(32바이트를 urlsafe base64 로 인코딩한 44자)이 아니다. 형식을 강제하면
> 문서대로 `.env` 를 만든 사람의 앱이 **기동조차 하지 못한다**. 그래서
> - 값이 이미 정식 Fernet 키면 그대로 쓰고,
> - 아니면 SHA-256 으로 32바이트를 뽑아 키로 삼는다(같은 문자열 → 항상 같은 키).
>
> ⚠️ **키가 바뀌면 이미 저장된 토큰은 복호화할 수 없다.** 그때는 연동을 다시 등록해야 하며,
> 그래서 배포(M9)에서도 로컬과 같은 값을 넣어야 한다. 복호화 실패는 조용히 넘기지 않고
> `TokenDecryptionError` 로 올린다 — 빈 토큰으로 동기화가 진행되면 원인이 "GitHub 401" 로
> 둔갑해 추적이 어려워진다.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

# 마스킹 표기 (`08 §1` — `ntn_****`). 접두사만 남기므로 길이도 새어 나가지 않는다.
MASK = "****"
_MAX_PREFIX_LENGTH = 8

# 키 문자열 → Fernet 인스턴스. 테스트가 `settings` 를 갈아끼우므로 값 자체를 캐시 키로 쓴다.
_fernet_cache: dict[str, Fernet] = {}


class TokenDecryptionError(Exception):
    """저장된 암호문을 현재 키로 풀 수 없다 — 대개 `INTEGRATION_ENCRYPTION_KEY` 가 바뀐 것이다."""


def _derive_key(raw: str) -> bytes:
    """env 값을 Fernet 키로 만든다 (모듈 독스트링 — 정식 키면 그대로, 아니면 SHA-256)."""
    candidate = raw.encode("utf-8")
    try:
        if len(base64.urlsafe_b64decode(candidate)) == 32:
            return candidate
    except (ValueError, TypeError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())


def _fernet() -> Fernet:
    raw = settings.integration_encryption_key
    cached = _fernet_cache.get(raw)
    if cached is None:
        cached = Fernet(_derive_key(raw))
        _fernet_cache[raw] = cached
    return cached


def encrypt(value: str) -> str:
    """평문 → 암호문 문자열. jsonb 에 그대로 들어간다."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """암호문 → 평문. 키가 다르거나 값이 손상됐으면 `TokenDecryptionError`."""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise TokenDecryptionError(
            "저장된 토큰을 복호화할 수 없습니다. INTEGRATION_ENCRYPTION_KEY 가 "
            "연동 등록 시점과 다르면 연동을 다시 등록해야 합니다."
        ) from exc


def mask(value: str) -> str:
    """조회 응답용 마스킹 (`05 §5` "토큰은 마스킹", `08 §1` `ntn_****`).

    `ntn_secret…` · `ghp_secret…` 처럼 접두사가 있는 토큰은 접두사만 남긴다 — 담당자가
    "어느 토큰을 넣었는지"를 알아보는 데는 그것으로 충분하고, 그 뒤는 길이조차 노출하지 않는다.
    """
    if not value:
        return ""
    prefix, separator, _ = value.partition("_")
    if separator and 1 <= len(prefix) <= _MAX_PREFIX_LENGTH:
        return f"{prefix}_{MASK}"
    return MASK
