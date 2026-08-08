"""notifications

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notifications",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        # `05 §1.5` — 수신자 `users.language` 로 만들어 저장한다.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # **NULL = 즉시 발송**, 값 = 그 시각 이후 발송 (룰 6 비긴급 카드 알림·DND 보류).
        sa.Column("deliver_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
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
            "type IN ('answer.completed', 'answer.failed', 'answer.verified', "
            "'answer.corrected', 'answer.kept', 'answer.rejected', 'card.created', "
            "'briefing.ready', 'doc.review_needed', 'feedback.different', "
            "'sync.completed', 'sync.failed')",
            name="ck_notifications_type",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # `04 §7` — 알림함 조회(미읽음 필터).
    op.create_index(
        "ix_notifications_user_id_read_at", "notifications", ["user_id", "read_at"], unique=False
    )
    # 브리핑 보류분 조회(`deliver_after <= now()`)가 함께 타는 인덱스 (`04 §7`).
    op.create_index(
        "ix_notifications_user_id_deliver_after",
        "notifications",
        ["user_id", "deliver_after"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_notifications_user_id_deliver_after", table_name="notifications")
    op.drop_index("ix_notifications_user_id_read_at", table_name="notifications")
    op.drop_table("notifications")
