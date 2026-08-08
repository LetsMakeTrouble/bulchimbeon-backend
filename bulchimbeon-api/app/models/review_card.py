"""review_cards(담당자 인박스) · feedbacks(크로스체크) (`04 §2`).

> ### 카드는 **최종 안전망**이다 (룰 6)
> 🟢 즉답도, 파이프라인 실패도 카드를 만든다. 알림이 실패해도·퇴근 모드를 꺼도·담당자가
> 교체돼도 인박스는 사라지지 않는다. 유일한 예외는 **재사용 답변**이다 — 담당자가 이미
> 확정한 원문을 그대로 내보낸 것이라 다시 확정받을 대상이 없다 (D11).

⚠️ `review_cards` 는 `project_id` 스코프다. 담당자 개인에게 붙지 않으므로 **담당자 교체
(D16)만으로 미처리 카드가 신규 담당자에게 따라간다** — 이관 UPDATE 가 따로 필요 없다.
"""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# --- review_cards (`04 §2`, `05 §7`) ----------------------------------------------------
# 🟢 과 파이프라인 실패도 카드를 만든다. `green` 은 큐에 적재되되 브리핑·알림에서 제외된다.
CARD_REASON_GREEN = "green"
CARD_REASON_YELLOW = "yellow"
CARD_REASON_RED = "red"
CARD_REASON_FEEDBACK = "feedback"
CARD_REASON_DOC_UPDATE = "doc_update"
CARD_REASON_FAILED = "failed"
CARD_REASONS = (
    CARD_REASON_GREEN,
    CARD_REASON_YELLOW,
    CARD_REASON_RED,
    CARD_REASON_FEEDBACK,
    CARD_REASON_DOC_UPDATE,
    CARD_REASON_FAILED,
)

# `05 §7.1` — 재검토 카드에만 `keep` 이 유효하다 (`04 §6` 의 under_review → verified 경로가
# "수정 저장 or 원안 유지"뿐이기 때문).
REVIEW_CARD_REASONS = (CARD_REASON_FEEDBACK, CARD_REASON_DOC_UPDATE)

CARD_STATUS_PENDING = "pending"
CARD_STATUS_DEFERRED = "deferred"
CARD_STATUS_RESOLVED = "resolved"
CARD_STATUSES = (CARD_STATUS_PENDING, CARD_STATUS_DEFERRED, CARD_STATUS_RESOLVED)

# 살아 있는 카드 = 만료 스위퍼가 그 밑의 답변을 죽이지 않는 상태다 (D14).
CARD_OPEN_STATUSES = (CARD_STATUS_PENDING, CARD_STATUS_DEFERRED)

CARD_RESOLUTION_APPROVED = "approved"
CARD_RESOLUTION_EDITED = "edited"
CARD_RESOLUTION_REJECTED = "rejected"
CARD_RESOLUTION_KEPT = "kept"  # 원안 유지 / 전체 유지(bulk-keep)
CARD_RESOLUTIONS = (
    CARD_RESOLUTION_APPROVED,
    CARD_RESOLUTION_EDITED,
    CARD_RESOLUTION_REJECTED,
    CARD_RESOLUTION_KEPT,
)


class ReviewCard(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_cards"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('green', 'yellow', 'red', 'feedback', 'doc_update', 'failed')",
            name="ck_review_cards_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'deferred', 'resolved')", name="ck_review_cards_status"
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('approved', 'edited', 'rejected', 'kept')",
            name="ck_review_cards_resolution",
        ),
        # `04 §7` — 큐 조회(`GET /projects/{id}/review-cards?status=`)의 접근 경로.
        Index("ix_review_cards_project_id_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("questions.id"), nullable=False, index=True
    )

    # `reason='failed'` 면 NULL 이다 — 답변이 만들어지기 전에 파이프라인이 죽었다 (D23).
    answer_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("answers.id"), nullable=True, index=True
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # `reason='doc_update'` 일 때 **재검토를 유발한 버전**. `bulk-keep` 의 묶음 키다 —
    # 이 값이 없으면 "문서 갱신 묶음 전체 유지"를 구현할 수 없다 (`05 §7`).
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=True
    )

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ⑦ 결과의 **복사본**이다 (`answers.question_struct` 에서 가져온다). 다시 생성하지 않는다.
    # `05 §7` — 🔴 전달용이므로 `reason='red'` 에만 채워진다.
    question_struct: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # "맞았다" 2건 누적 시 true. 브리핑 최상단·큐 정렬 1순위 (룰 3).
    recommend_approve: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # 카드 처리 시간 지표(`05 §13` `card_handle_30s_rate`)의 **시작점**. 상세 최초 조회 시각이다.
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # `05 §1.4` 의 409 `ALREADY_RESOLVED` body 가 `resolved_by: {id, name}` 를 요구한다.
    # 담당자는 교체될 수 있으므로(D16) "현재 담당자"로 대체할 수 없어 컬럼으로 남긴다.
    # (`04 §2` 반영, 사용자 승인 2026-08-08 — M3 의 `similar_official_qa_id` 와 같은 절차)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


# --- feedbacks (`04 §2`, `05 §6`) --------------------------------------------------------
FEEDBACK_CORRECT = "correct"
FEEDBACK_DIFFERENT = "different"
FEEDBACK_VERDICTS = (FEEDBACK_CORRECT, FEEDBACK_DIFFERENT)

# 룰 3 — "맞았다" 이만큼 쌓이면 카드가 승인 추천으로 승격된다. 자동 확정은 없다.
RECOMMEND_APPROVE_CORRECT_COUNT = 2


class Feedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """크로스체크 (`05 §6`).

    ⚠️ **유저당 1건**이다 (`04 §7` UNIQUE). 재제출은 verdict 가 달라도 409 이며
    verdict 변경은 지원하지 않는다 (D12).
    """

    __tablename__ = "feedbacks"
    __table_args__ = (
        UniqueConstraint("answer_id", "user_id", name="uq_feedbacks_answer_user"),
        CheckConstraint("verdict IN ('correct', 'different')", name="ck_feedbacks_verdict"),
    )

    answer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("answers.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    verdict: Mapped[str] = mapped_column(Text, nullable=False)

    # "달랐다"는 사유 한 줄이 필수다 (`05 §6`).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 담당자가 카드를 처리하면 미해소 피드백이 함께 resolved 된다 (룰 9 담당자 우선).
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
