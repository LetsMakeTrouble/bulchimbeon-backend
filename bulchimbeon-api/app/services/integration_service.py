"""연동 등록·조회·해제 (`05 §5`, `04 §2`).

커밋은 라우터가 한다 — 요청 하나 = 트랜잭션 하나. 여기서는 `flush()` 까지만 한다.

> ### 토큰은 **들어올 때 암호화하고 나갈 때 마스킹한다**
> 평문 토큰이 사는 곳은 딱 둘이다: 요청 본문과, 동기화가 도는 순간의 메모리.
> DB 에는 Fernet 암호문만 들어가고(`core/crypto.py`), 응답에는 `ntn_****` 만 나간다.
> 어느 필드가 비밀인지는 `schemas/integration.SECRET_FIELDS` 한 곳이 정의한다 —
> provider 를 늘릴 때 그 표만 고치면 암호화·마스킹·복호화가 함께 따라온다.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import MASK, TokenDecryptionError, decrypt, encrypt, mask
from app.core.errors import NotFound
from app.models.integration import Integration
from app.schemas.integration import (
    SECRET_FIELDS,
    IntegrationCreate,
    IntegrationListResponse,
    IntegrationOut,
)

logger = logging.getLogger(__name__)


def _secret_fields(provider: str) -> tuple[str, ...]:
    return SECRET_FIELDS.get(provider, ())


def _encrypted_config(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    """저장용 config — 비밀 필드만 암호문으로 바꾼다.

    `None` 은 그대로 둔다: GitHub 공개 레포는 토큰 없이 동작하고(`05 §5`), 빈 값을 암호화하면
    "토큰이 있다"와 "없다"가 구분되지 않는다.
    """
    stored = dict(config)
    for field in _secret_fields(provider):
        value = stored.get(field)
        if value:
            stored[field] = encrypt(str(value))
    return stored


def decrypted_config(integration: Integration) -> dict[str, Any]:
    """동기화가 쓰는 평문 config. **응답에 실지 않는다** — 이 값은 프로세스 안에서만 돈다."""
    plain = dict(integration.config)
    for field in _secret_fields(integration.provider):
        value = plain.get(field)
        if value:
            plain[field] = decrypt(str(value))
    return plain


def masked_config(integration: Integration) -> dict[str, Any]:
    """응답용 config (`05 §5` "토큰은 마스킹", `08 §1` `ntn_****`).

    ⚠️ **암호문이 아니라 평문을 마스킹한다.** 암호문에는 `ntn_`·`ghp_` 접두사가 없으므로
    그대로 자르면 어느 토큰을 넣었는지 알아볼 수 없는 `****` 만 남는다 — 계약서가 접두사를
    보여 주기로 한 이유(담당자가 자기가 넣은 값을 식별한다)가 사라진다.

    복호화가 실패하면(키가 바뀐 경우) 접두사 없이 `****` 로 내려간다. 목록 조회가 500 으로
    죽으면 담당자가 **연동을 다시 등록하는 화면 자체에 못 들어간다** — 복구 경로를 막지 않는다.
    """
    shown = dict(integration.config)
    for field in _secret_fields(integration.provider):
        value = shown.get(field)
        if not value:
            shown[field] = None
            continue
        try:
            shown[field] = mask(decrypt(str(value)))
        except TokenDecryptionError:
            logger.warning("마스킹용 복호화 실패: integration=%s", integration.id)
            shown[field] = MASK
    return shown


def to_out(integration: Integration) -> IntegrationOut:
    return IntegrationOut(
        id=integration.id,
        provider=integration.provider,  # type: ignore[arg-type]
        config=masked_config(integration),
        last_synced_at=integration.last_synced_at,
        last_sync_status=integration.last_sync_status,  # type: ignore[arg-type]
        created_at=integration.created_at,
    )


async def create(db: AsyncSession, *, project_id: UUID, payload: IntegrationCreate) -> Integration:
    """연결 등록 (`05 §5`).

    config 는 스키마가 provider 별로 이미 검증했다 — 여기서는 기본값이 채워진 정규형
    (`parsed_config().model_dump()`)을 저장한다. 요청이 `branch` 를 생략했을 때 DB 에
    `main` 이 남아야 동기화가 매번 같은 기본값을 다시 추측하지 않는다.
    """
    normalized = payload.parsed_config().model_dump()
    integration = Integration(
        project_id=project_id,
        provider=payload.provider,
        config=_encrypted_config(payload.provider, normalized),
    )
    db.add(integration)
    await db.flush()
    await db.refresh(integration)  # created_at 은 server_default 다.
    return integration


async def list_for_project(db: AsyncSession, project_id: UUID) -> IntegrationListResponse:
    """목록 (`05 §5`) — 오래된 순. 토큰은 마스킹된다."""
    rows = await db.scalars(
        select(Integration)
        .where(Integration.project_id == project_id)
        .order_by(Integration.created_at.asc())
    )
    return IntegrationListResponse(items=[to_out(integration) for integration in rows.all()])


async def load(db: AsyncSession, integration_id: UUID) -> Integration:
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise NotFound()
    return integration


async def delete(db: AsyncSession, integration: Integration) -> None:
    """해제 (`05 §5`).

    ⚠️ **동기화로 만들어진 문서는 지우지 않는다.** 그 문서를 근거로 확정된 답변의
    `citations[]` 가 청크를 가리키고 있고(D20 이 soft delete 를 택한 것과 같은 이유),
    연결을 끊는 것은 "앞으로 더 가져오지 않는다"는 뜻이지 "지금까지 가져온 지식을 버린다"가
    아니다. 문서를 치우는 것은 담당자가 `DELETE /documents/{id}` 로 따로 판단한다.
    """
    await db.delete(integration)
    await db.flush()
