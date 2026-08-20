"""conversation_messages — 사람 간 양방향 대화 채널 (`04 §2`, `05 §6.1`).

`questions` 와 **분리된 별도 테이블**이다 (사용자 결정). 대화모드 질문(`questions.mode`)은
질문자 단방향 발화로 그대로 두고, 담당자·질문자가 함께 쓰는 메시지는 여기 쌓인다.

append-only 라 `updated_at` 이 없다 — `events` 와 같은 규약 (`models/base.py`).
"""

import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class ConversationMessage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        # 목록 조회(`05 §6.1` — created_at 오름차순)가 타는 인덱스.
        Index("ix_conversation_messages_project_created", "project_id", "created_at"),
    )

    # 프로젝트가 지워지면 대화도 함께 사라진다 — 메시지는 프로젝트 밖에서 의미가 없다.
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
