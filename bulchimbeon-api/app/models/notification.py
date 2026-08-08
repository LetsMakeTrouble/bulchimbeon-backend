"""notifications (인앱 알림함) — `04 §2`, 타입 목록은 `04 §4` 가 정본이다.

> ### `deliver_after` 하나로 보류를 표현한다 (룰 6)
> **NULL = 즉시 발송**, 값 = 그 시각 이후 발송. 비긴급 카드 알림·DND 보류를 이 컬럼 하나로
> 표현하며 `pending_briefing` 같은 별도 플래그·상태를 만들지 않는다. 목록·미읽음 카운트는
> `deliver_after IS NULL OR deliver_after <= now()` 인 것만 노출하고(`05 §11`),
> 보류분의 flush 는 브리핑 스케줄러(M6)가 이 컬럼을 읽어 처리한다.

⚠️ **알림이 실패해도 질문은 사라지지 않는다** (룰 6 🛟). 최종 안전망은 담당자 인박스
(`review_cards`)이며 이 테이블은 그 위에 얹힌 통지 계층이다.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# `04 §4` 알림 타입. `05 §11` 의 type 어휘와 1:1 이며 **계약에 없는 타입을 만들지 않는다.**
NOTIFICATION_ANSWER_COMPLETED = "answer.completed"  # 질문자 — 파이프라인 완료(🔴 보류 안내 포함)
NOTIFICATION_ANSWER_FAILED = "answer.failed"  # 질문자 — 파이프라인 실패 (D23)
NOTIFICATION_ANSWER_VERIFIED = "answer.verified"  # 질문자 — 승인 확정
NOTIFICATION_ANSWER_CORRECTED = "answer.corrected"  # 질문자+담당자 — 수정 확정 (양쪽 언어, 룰 8)
NOTIFICATION_ANSWER_KEPT = "answer.kept"  # 질문자 — 원안 유지 + 사유
NOTIFICATION_ANSWER_REJECTED = "answer.rejected"  # 질문자 — 반려 + 사유
NOTIFICATION_CARD_CREATED = "card.created"  # 담당자 — urgent 만 즉시 (룰 6)
NOTIFICATION_BRIEFING_READY = "briefing.ready"  # 담당자 — 브리핑 시각 배치 (M6)
NOTIFICATION_DOC_REVIEW_NEEDED = "doc.review_needed"  # 담당자 — 재검토 N건 (룰 5)
NOTIFICATION_FEEDBACK_DIFFERENT = "feedback.different"  # 담당자 — 달랐다 접수
NOTIFICATION_SYNC_COMPLETED = "sync.completed"  # 담당자 — 연동 동기화 성공 (M8)
NOTIFICATION_SYNC_FAILED = "sync.failed"  # 담당자 — 연동 동기화 실패 (M8)

NOTIFICATION_TYPES = (
    NOTIFICATION_ANSWER_COMPLETED,
    NOTIFICATION_ANSWER_FAILED,
    NOTIFICATION_ANSWER_VERIFIED,
    NOTIFICATION_ANSWER_CORRECTED,
    NOTIFICATION_ANSWER_KEPT,
    NOTIFICATION_ANSWER_REJECTED,
    NOTIFICATION_CARD_CREATED,
    NOTIFICATION_BRIEFING_READY,
    NOTIFICATION_DOC_REVIEW_NEEDED,
    NOTIFICATION_FEEDBACK_DIFFERENT,
    NOTIFICATION_SYNC_COMPLETED,
    NOTIFICATION_SYNC_FAILED,
)

_TYPE_CHECK = ", ".join(f"'{value}'" for value in NOTIFICATION_TYPES)


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(f"type IN ({_TYPE_CHECK})", name="ck_notifications_type"),
        # `04 §7` — 알림함 조회(미읽음 필터)의 접근 경로.
        Index("ix_notifications_user_id_read_at", "user_id", "read_at"),
        # 브리핑 보류분 조회(`deliver_after <= now()`)가 함께 타는 인덱스 (`04 §7`).
        Index("ix_notifications_user_id_deliver_after", "user_id", "deliver_after"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)

    # `05 §1.5` — **수신자 `users.language` 로 서버가 만들어 저장한다.** 프론트는 그대로 렌더하며
    # 자체 번역하지 않는다. `error.message`(개발자용 한국어 고정)와 섞이지 않는 계열이다.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # 딥링크용 — `{project_id, question_id?, answer_id?, card_id?}` (`04 §2`, `05 §11`).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # **NULL = 즉시 발송** (모듈 독스트링). 값 = 그 시각 이후 발송.
    deliver_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
