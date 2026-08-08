"""모델 공통 믹스인.

`04` 문서 상단 — 모든 PK 는 UUID, 모든 테이블에 `created_at`, 갱신되는 테이블에 `updated_at`.
append-only 인 `events` 는 `TimestampMixin` 대신 `CreatedAtMixin` 만 쓴다.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """UUID PK. 기본값은 파이썬 쪽에서 만든다 — INSERT 전에 id 를 알아야 하는 경로가 많다."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
