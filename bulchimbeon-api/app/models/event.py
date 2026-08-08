"""events — 지표·타임라인의 단일 원천 (`04 §2`·`§5`, 룰 4).

append-only 다. 갱신하지 않으므로 `updated_at` 이 없다.
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class Event(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """⚠️ 이 테이블이 `models/base.py` 의 `clock_timestamp()` 기본값에 가장 많이 기댄다.

    파이프라인 한 번이 `question.graded` · `question.status_changed` · `answer.published` ·
    `card.created` 를 **같은 트랜잭션에서** 적재하므로, `created_at` 이 트랜잭션 시작 시각이면
    네 행이 전부 동률이 되어 `05 §13` 타임라인의 순서가 사라진다. 지표에도 직접 영향이
    있다 — 카드 처리 시간(`card_handle_30s_rate`)은 `card.viewed` → 액션 이벤트의 간격이다.
    """

    __tablename__ = "events"
    __table_args__ = (
        # `04 §7` — 프로젝트 타임라인·지표 조회가 이 인덱스를 탄다.
        Index("ix_events_project_created", "project_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )

    # NULL = system (스케줄러·파이프라인이 일으킨 변화).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # `04 §5` 의 타입 문자열. 계약서에 없는 타입을 새로 만들지 않는다.
    type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
