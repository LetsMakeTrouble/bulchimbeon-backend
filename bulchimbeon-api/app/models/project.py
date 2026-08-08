"""projects · project_members · guidelines (`04 §2`)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# `05 §1.3` — 전 화면 공통 enum.
ROLE_ANSWERER = "answerer"
ROLE_ASKER = "asker"
ROLES = (ROLE_ANSWERER, ROLE_ASKER)

MEMBER_STATUS_ACTIVE = "active"
MEMBER_STATUS_LEFT = "left"
MEMBER_STATUSES = (MEMBER_STATUS_ACTIVE, MEMBER_STATUS_LEFT)


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    invite_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)

    # 담당자 1명 (D1). 실제 강제는 project_members 의 부분 UNIQUE 인덱스가 하고,
    # 이 컬럼은 그 결과를 프로젝트 행에 캐시해 조회를 짧게 만든다.
    # 둘은 같은 트랜잭션에서 함께 갱신된다 (D16).
    answerer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    away_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # `04 §3` 스키마. 기본값의 유일한 정의 위치는 config.DEFAULT_SETTINGS 다 (룰 3) —
    # 여기에 server_default 로 값을 박으면 정의가 두 곳이 된다.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProjectMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        # 담당자 1명 강제 (`04 §7`, 룰 9).
        # ⚠️ 부분 UNIQUE 는 DEFERRABLE 이 아니다 — 역할 스왑은 단일 UPDATE 로 하지 말 것.
        #    M-1 실측(2026-08-07): 단일 UPDATE 는 행 처리 순서에 따라 **통과하기도 한다**.
        #    반드시 project_service.transfer_answerer 의 4문 절차를 쓴다 (D16, `04 §7`).
        Index(
            "uq_project_members_single_answerer",
            "project_id",
            unique=True,
            postgresql_where=text("role = 'answerer' AND status = 'active'"),
        ),
        CheckConstraint("role IN ('answerer', 'asker')", name="ck_project_members_role"),
        CheckConstraint("status IN ('active', 'left')", name="ck_project_members_status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)

    # 탈퇴는 행 삭제가 아니라 status 전환이다 — 기존 질문·답변·피드백을 보존한다 (D18).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Guideline(TimestampMixin, Base):
    """프로젝트당 0 또는 1행. 신규 프로젝트에는 행이 없고 최초 저장 시 생성된다 (`04 §1`)."""

    __tablename__ = "guidelines"

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), primary_key=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
