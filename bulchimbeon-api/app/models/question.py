"""questions · answers · answer_citations (`04 §2`).

상태 전이는 `04 §6`(답변)·`§6.1`(질문)이 정본이다. 여기서는 허용 값만 CHECK 로 못박는다.

⚠️ `answers` 는 `UNIQUE(question_id)` 다 — 질문당 1행이며 MVP 는 **답변 재생성 API 를
제공하지 않는다** (`04 §7`). 파이프라인이 두 번 돌아도 두 번째는 이 제약에서 막힌다.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# --- questions (`04 §2`·`§6.1`) ---------------------------------------------------------
URGENCY_NORMAL = "normal"
URGENCY_URGENT = "urgent"
URGENCIES = (URGENCY_NORMAL, URGENCY_URGENT)

QUESTION_STATUS_PROCESSING = "processing"
QUESTION_STATUS_ANSWERED = "answered"
QUESTION_STATUS_HELD = "held"
QUESTION_STATUS_FAILED = "failed"
QUESTION_STATUSES = (
    QUESTION_STATUS_PROCESSING,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_HELD,
    QUESTION_STATUS_FAILED,
)

# --- answers (`04 §2`·`§6`, `05 §1.3`) --------------------------------------------------
GRADE_GREEN = "green"
GRADE_YELLOW = "yellow"
GRADE_RED = "red"
GRADES = (GRADE_GREEN, GRADE_YELLOW, GRADE_RED)

ANSWER_STATE_DRAFT = "draft"
ANSWER_STATE_VERIFIED = "verified"
ANSWER_STATE_UNDER_REVIEW = "under_review"
ANSWER_STATE_EXPIRED = "expired"
ANSWER_STATE_REJECTED = "rejected"
ANSWER_STATES = (
    ANSWER_STATE_DRAFT,
    ANSWER_STATE_VERIFIED,
    ANSWER_STATE_UNDER_REVIEW,
    ANSWER_STATE_EXPIRED,
    ANSWER_STATE_REJECTED,
)

ANSWER_SOURCE_GENERATED = "generated"
ANSWER_SOURCE_REUSED = "reused"
ANSWER_SOURCES = (ANSWER_SOURCE_GENERATED, ANSWER_SOURCE_REUSED)

# 🔴 보류 사유 (`04 §2`, `05 §6`). 앞의 4종이 **강제 🔴**이며 DND 에서도 강등되지 않는다 (D2).
HELD_REASON_CONFLICT = "conflict"
HELD_REASON_NO_EVIDENCE = "no_evidence"
HELD_REASON_SCHEMA_FAILED = "schema_failed"
HELD_REASON_QUOTA_EXCEEDED = "quota_exceeded"
HELD_REASON_LOW_CONFIDENCE = "low_confidence"

FORCED_RED_REASONS = (
    HELD_REASON_CONFLICT,
    HELD_REASON_NO_EVIDENCE,
    HELD_REASON_SCHEMA_FAILED,
    HELD_REASON_QUOTA_EXCEEDED,
)
HELD_REASONS = (*FORCED_RED_REASONS, HELD_REASON_LOW_CONFIDENCE)


class Question(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint("urgency IN ('normal', 'urgent')", name="ck_questions_urgency"),
        CheckConstraint(
            "status IN ('processing', 'answered', 'held', 'failed')",
            name="ck_questions_status",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    asker_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    content_ko: Mapped[str] = mapped_column(Text, nullable=False)

    # ① 번역 결과. 파이프라인이 채우므로 접수 직후에는 NULL 이다.
    content_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 긴급 여부를 정하는 주체는 **질문자**다 (D10).
    urgency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))

    # ① 이 채우는 AI 제안 플래그. 결정권은 없다 (D10).
    suggest_urgent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'processing'"))


class Answer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "answers"
    __table_args__ = (
        # `04 §7` — 질문당 1행. 답변 재생성 API 가 없으므로 여기서 막는 것으로 충분하다.
        UniqueConstraint("question_id", name="uq_answers_question"),
        CheckConstraint("grade IN ('green', 'yellow', 'red')", name="ck_answers_grade"),
        CheckConstraint(
            "state IN ('draft', 'verified', 'under_review', 'expired', 'rejected')",
            name="ck_answers_state",
        ),
        CheckConstraint("source IN ('generated', 'reused')", name="ck_answers_source"),
        CheckConstraint(
            "held_reason IS NULL OR held_reason IN "
            "('conflict', 'no_evidence', 'low_confidence', 'schema_failed', 'quota_exceeded')",
            name="ck_answers_held_reason",
        ),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("questions.id"), nullable=False
    )

    grade: Mapped[str] = mapped_column(Text, nullable=False)

    # 🔴·재사용·데드라인 초과 경로에서는 산출되지 않는다 → NULL (`05 §6` 재사용 표).
    matching_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grounding_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 리스케일 **전** 원시 코사인 (top-1). 캘리브레이션 재산출의 근거로 남긴다 (`04 §2`).
    sim_raw: Mapped[float | None] = mapped_column(Float, nullable=True)

    held_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ⑦ 영어 구조화 `{background, question, options[]}`. M4 가 카드로 **복사**한다.
    question_struct: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))

    # 🔴 은 발행하지 않지만 초안은 남긴다 — 카드가 이것을 담당자에게 보여준다 (`04 §2`).
    content_ko: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    content_en: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'generated'"))

    # reused 원본 / (M4) 확정 시 편입된 Q&A. **D22 의 correct_count 증가 대상**이므로
    # "비슷한 확정 답변"을 여기에 넣지 않는다 — 그쪽은 similar_official_qa_id 다.
    #
    # ⚠️ `official_qas.source_answer_id` 가 이 테이블을 되짚으므로 두 테이블은 **순환 FK** 다.
    #    `use_alter=True` 가 없으면 `Base.metadata.create_all` 이 CircularDependencyError 로 죽고,
    #    그 경로를 쓰는 tests/conftest.py 가 통째로 멈춘다.
    official_qa_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("official_qas.id", use_alter=True, name="fk_answers_official_qa_id"),
        nullable=True,
    )

    # `similar_threshold` ~ `reuse_threshold` 구간에서 첨부되는 "비슷한 확정 답변" (룰 4, D24).
    # `05 §6` 의 `similar_official_qa` 필드가 이 값에서 나온다. 질문 임베딩을 저장하지 않으므로
    # 조회 시점에 다시 계산할 수 없어 파이프라인이 여기에 적어 둔다.
    similar_official_qa_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("official_qas.id", use_alter=True, name="fk_answers_similar_official_qa_id"),
        nullable=True,
    )

    degraded_from_red: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # 재사용 답변은 NULL 이다 — 만료 스위퍼 대상이 아니다 (D11).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class AnswerCitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """문장별 인용 (환각 방어 1겹의 증적, `06 §7`).

    ⚠️ `official_qa_id` 는 **D24 이후 미사용이며 항상 NULL** 이다 (`04 §2`).
    `05 §6` 의 `citations[]` 스키마에 대응 필드가 없다 — 공식 Q&A 는 근거로 인용되지 않는다.
    컬럼은 계약된 스키마이므로 남기되 채우지 않는다.
    """

    __tablename__ = "answer_citations"

    answer_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("answers.id"), nullable=False, index=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("chunks.id"), nullable=True
    )
    official_qa_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("official_qas.id"), nullable=True
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    similarity: Mapped[float] = mapped_column(Float, nullable=False)
