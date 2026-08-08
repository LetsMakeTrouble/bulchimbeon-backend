"""review_cards, feedbacks

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-08 11:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "review_cards",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=False),
        # `reason='failed'` 면 NULL (D23).
        sa.Column("answer_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        # `reason='doc_update'` 의 묶음 키 — bulk-keep 이 이 값으로 묶는다 (`05 §7`).
        sa.Column("document_version_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("question_struct", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "recommend_approve", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_urgent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deferred_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # `05 §1.4` 409 ALREADY_RESOLVED body 의 `resolved_by` (`04 §2` 반영, 사용자 승인).
        sa.Column("resolved_by", sa.UUID(), nullable=True),
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
            "reason IN ('green', 'yellow', 'red', 'feedback', 'doc_update', 'failed')",
            name="ck_review_cards_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'deferred', 'resolved')", name="ck_review_cards_status"
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('approved', 'edited', 'rejected', 'kept')",
            name="ck_review_cards_resolution",
        ),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_review_cards_question_id"), "review_cards", ["question_id"], unique=False
    )
    op.create_index(op.f("ix_review_cards_answer_id"), "review_cards", ["answer_id"], unique=False)
    # `04 §7` — 큐 조회의 접근 경로.
    op.create_index(
        "ix_review_cards_project_id_status", "review_cards", ["project_id", "status"], unique=False
    )

    op.create_table(
        "feedbacks",
        sa.Column("answer_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.CheckConstraint("verdict IN ('correct', 'different')", name="ck_feedbacks_verdict"),
        sa.ForeignKeyConstraint(["answer_id"], ["answers.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # `04 §7` — 유저당 1건. 재제출은 409 이며 verdict 변경은 지원하지 않는다 (D12).
        sa.UniqueConstraint("answer_id", "user_id", name="uq_feedbacks_answer_user"),
    )
    op.create_index(op.f("ix_feedbacks_answer_id"), "feedbacks", ["answer_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_feedbacks_answer_id"), table_name="feedbacks")
    op.drop_table("feedbacks")

    op.drop_index("ix_review_cards_project_id_status", table_name="review_cards")
    op.drop_index(op.f("ix_review_cards_answer_id"), table_name="review_cards")
    op.drop_index(op.f("ix_review_cards_question_id"), table_name="review_cards")
    op.drop_table("review_cards")
