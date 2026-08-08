"""integrations (외부 지식 원본 연결) — `04 §2`.

> ### 토큰은 **평문으로 저장하지 않는다** (`03 §7`)
> `config` 는 jsonb 이고 그 안의 비밀 필드(`token`)만 `INTEGRATION_ENCRYPTION_KEY` 로
> 대칭 암호화(Fernet)한 문자열로 들어간다. 암복호화의 유일한 구현은
> `app/core/crypto.py` 이고, 어느 필드가 비밀인지의 정의는
> `app/services/integration_service.py` 의 provider 스펙 한 곳뿐이다.
> 조회 응답에서는 `ntn_****` 로 마스킹된다 (`05 §5`).

> ### 동기화 **결과**는 이 테이블이 아니라 `events` 에 남는다 (사용자 결정 2026-08-08)
> `04 §2` 는 `last_sync_status` 를 `ok | failed + 에러 메시지 payload` 로 적었는데,
> 그 payload 를 담을 컬럼을 새로 만들지 않고 `sync.run` 이벤트의 payload 로 남긴다.
> 룰 4("모든 상태 변화는 events 에 기록 — 지표·타임라인의 단일 원천")와 같은 방향이고,
> `05 §13` 의 `GET /projects/{id}/events?entity_type=integration&entity_id=…` 로 그대로
> 조회된다. 여기 남는 것은 목록 화면이 쓰는 요약 둘(`last_synced_at`·`last_sync_status`)뿐이다.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# `04 §2` integrations.provider — `05 §5` 의 `{provider: "notion"|"github"}` 와 같은 어휘다.
PROVIDER_NOTION = "notion"
PROVIDER_GITHUB = "github"
PROVIDERS = (PROVIDER_NOTION, PROVIDER_GITHUB)

SYNC_STATUS_OK = "ok"
SYNC_STATUS_FAILED = "failed"
SYNC_STATUSES = (SYNC_STATUS_OK, SYNC_STATUS_FAILED)

_PROVIDER_CHECK = ", ".join(f"'{value}'" for value in PROVIDERS)
_SYNC_STATUS_CHECK = ", ".join(f"'{value}'" for value in SYNC_STATUSES)


class Integration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "integrations"
    __table_args__ = (
        CheckConstraint(f"provider IN ({_PROVIDER_CHECK})", name="ck_integrations_provider"),
        CheckConstraint(
            f"last_sync_status IN ({_SYNC_STATUS_CHECK})", name="ck_integrations_last_sync_status"
        ),
    )

    # ⚠️ `UNIQUE(project_id, provider)` 를 걸지 않는다. 한 프로젝트가 GitHub 레포 두 개를
    #    연결하는 것은 정상이고(`05 §5` 의 config 가 레포를 하나만 가리킨다), 제약을 걸면
    #    그 정상 사용이 막힌다.
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)

    # `05 §5` 의 provider 별 config. **토큰 필드는 Fernet 암호문**이다 (모듈 독스트링).
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # NULL = 한 번도 동기화하지 않았다.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(Text, nullable=True)
