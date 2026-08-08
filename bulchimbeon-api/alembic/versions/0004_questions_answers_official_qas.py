"""questions, answers, answer_citations, official_qas

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-08 09:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# ⚠️ 03 §5.4 ② — 벡터 컬럼을 쓰는 리비전은 import 를 손으로 확인한다.
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "questions",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("asker_id", sa.UUID(), nullable=False),
        sa.Column("content_ko", sa.Text(), nullable=False),
        sa.Column("content_en", sa.Text(), nullable=True),
        sa.Column("urgency", sa.Text(), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("suggest_urgent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'processing'"), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("urgency IN ('normal', 'urgent')", name="ck_questions_urgency"),
        sa.CheckConstraint(
            "status IN ('processing', 'answered', 'held', 'failed')", name="ck_questions_status"
        ),
        sa.ForeignKeyConstraint(["asker_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_questions_project_id"), "questions", ["project_id"], unique=False)
    op.create_index(op.f("ix_questions_asker_id"), "questions", ["asker_id"], unique=False)

    # ⚠️ answers ↔ official_qas 는 순환 FK 다 (`04 §2`).
    #    answers 를 먼저 만들되 official_qas 를 가리키는 FK 는 나중에 ALTER 로 붙인다.
    op.create_table(
        "answers",
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("grade", sa.Text(), nullable=False),
        sa.Column("matching_rate", sa.Integer(), nullable=True),
        sa.Column("search_score", sa.Integer(), nullable=True),
        sa.Column("grounding_score", sa.Integer(), nullable=True),
        sa.Column("sim_raw", sa.Float(), nullable=True),
        sa.Column("held_reason", sa.Text(), nullable=True),
        sa.Column("question_struct", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("content_ko", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("content_en", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'generated'"), nullable=False),
        sa.Column("official_qa_id", sa.UUID(), nullable=True),
        sa.Column("similar_official_qa_id", sa.UUID(), nullable=True),
        sa.Column(
            "degraded_from_red", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("grade IN ('green', 'yellow', 'red')", name="ck_answers_grade"),
        sa.CheckConstraint(
            "state IN ('draft', 'verified', 'under_review', 'expired', 'rejected')",
            name="ck_answers_state",
        ),
        sa.CheckConstraint("source IN ('generated', 'reused')", name="ck_answers_source"),
        sa.CheckConstraint(
            "held_reason IS NULL OR held_reason IN "
            "('conflict', 'no_evidence', 'low_confidence', 'schema_failed', 'quota_exceeded')",
            name="ck_answers_held_reason",
        ),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"]),
        sa.ForeignKeyConstraint(["verified_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # `04 §7` — 질문당 1행. MVP 는 답변 재생성 API 를 제공하지 않는다.
        sa.UniqueConstraint("question_id", name="uq_answers_question"),
    )

    op.create_table(
        "official_qas",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("question_ko", sa.Text(), nullable=False),
        sa.Column("question_en", sa.Text(), nullable=False),
        sa.Column("answer_ko", sa.Text(), nullable=False),
        sa.Column("answer_en", sa.Text(), nullable=False),
        # 차원은 `vector(1536)` **리터럴 고정**이다 (`04` 상단).
        sa.Column("question_embedding", Vector(1536), nullable=False),
        sa.Column("source_answer_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("correct_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reuse_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'under_review', 'archived')", name="ck_official_qas_status"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["source_answer_id"], ["answers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_official_qas_project_id"), "official_qas", ["project_id"], unique=False
    )
    # `04 §7` — 재사용 검색용. 유사도는 `1 - (question_embedding <=> :q)`.
    op.create_index(
        "ix_official_qas_question_embedding_hnsw",
        "official_qas",
        ["question_embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"question_embedding": "vector_cosine_ops"},
    )

    # 순환 FK 의 나머지 절반.
    op.create_foreign_key(
        "fk_answers_official_qa_id", "answers", "official_qas", ["official_qa_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_answers_similar_official_qa_id",
        "answers",
        "official_qas",
        ["similar_official_qa_id"],
        ["id"],
    )

    op.create_table(
        "answer_citations",
        sa.Column("answer_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=True),
        # ⚠️ D24 이후 미사용. 항상 NULL 이다 (`04 §2`).
        sa.Column("official_qa_id", sa.UUID(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"]),
        sa.ForeignKeyConstraint(["official_qa_id"], ["official_qas.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_answer_citations_answer_id"), "answer_citations", ["answer_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_answer_citations_answer_id"), table_name="answer_citations")
    op.drop_table("answer_citations")

    op.drop_constraint("fk_answers_similar_official_qa_id", "answers", type_="foreignkey")
    op.drop_constraint("fk_answers_official_qa_id", "answers", type_="foreignkey")

    op.drop_index(
        "ix_official_qas_question_embedding_hnsw",
        table_name="official_qas",
        postgresql_using="hnsw",
        postgresql_ops={"question_embedding": "vector_cosine_ops"},
    )
    op.drop_index(op.f("ix_official_qas_project_id"), table_name="official_qas")
    op.drop_table("official_qas")

    op.drop_table("answers")

    op.drop_index(op.f("ix_questions_asker_id"), table_name="questions")
    op.drop_index(op.f("ix_questions_project_id"), table_name="questions")
    op.drop_table("questions")
