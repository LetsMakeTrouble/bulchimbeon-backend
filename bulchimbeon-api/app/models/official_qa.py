"""official_qas — 확정 지식 (`04 §2`).

재사용 판정(`06 §2` ②)의 대상이며, **③ 근거 검색의 대상이 아니다** (D24).
`chunks` 와 한 랭킹으로 병합하지 않는다 — 인덱스는 둘이지만 쓰이는 단계가 다르다 (`04 §7`).

⚠️ `answer_ko` 는 **확정 당시의 한국어 원문이며 불변**이다 (룰 4·D5). 재사용 시 영어 저장본을
다시 번역하지 않는다.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import EMBEDDING_DIM_FIXED
from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

OFFICIAL_QA_STATUS_ACTIVE = "active"
OFFICIAL_QA_STATUS_UNDER_REVIEW = "under_review"
OFFICIAL_QA_STATUS_ARCHIVED = "archived"
OFFICIAL_QA_STATUSES = (
    OFFICIAL_QA_STATUS_ACTIVE,
    OFFICIAL_QA_STATUS_UNDER_REVIEW,
    OFFICIAL_QA_STATUS_ARCHIVED,
)


class OfficialQA(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "official_qas"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'under_review', 'archived')", name="ck_official_qas_status"
        ),
        # `04 §7` — 재사용 검색용 HNSW. 유사도는 `1 - (question_embedding <=> :q)` 다.
        Index(
            "ix_official_qas_question_embedding_hnsw",
            "question_embedding",
            postgresql_using="hnsw",
            postgresql_ops={"question_embedding": "vector_cosine_ops"},
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )

    question_ko: Mapped[str] = mapped_column(Text, nullable=False)
    question_en: Mapped[str] = mapped_column(Text, nullable=False)

    # 확정 한국어 원문 — 불변 (룰 4·D5).
    answer_ko: Mapped[str] = mapped_column(Text, nullable=False)
    answer_en: Mapped[str] = mapped_column(Text, nullable=False)

    question_embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM_FIXED), nullable=False
    )

    # 확정된 답변에서 편입될 때의 원천 (`06 §3`). **NULL = 담당자 직접 등록** (`05 §9` POST)
    # — 원천 질문·답변이 없는 유일한 경로다.
    source_answer_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("answers.id"), nullable=True
    )

    # `under_review` · `archived` 는 재사용·유사 첨부 대상에서 제외된다 (D7·D20).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))

    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # 재질문 즉답률의 분자 원천 (D26). 재사용이 성립할 때마다 증가한다.
    reuse_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
