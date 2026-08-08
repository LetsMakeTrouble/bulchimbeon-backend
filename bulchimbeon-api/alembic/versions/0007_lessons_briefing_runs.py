"""lessons, briefing_runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-08 17:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "lessons",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # 정규화 정의는 `app/utils/hashing.lesson_content_hash` 한 곳뿐이다 (`03 §5.2`).
        sa.Column("content_hash", sa.Text(), nullable=False),
        # `deleted` 는 물리 삭제가 아니라 해시를 보존하는 묘비다 (D8).
        sa.Column("status", sa.Text(), server_default=sa.text("'candidate'"), nullable=False),
        # 문서 갱신 시 true. 자동 삭제하지 않는다 (룰 5).
        sa.Column("needs_recheck", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_answer_id", sa.UUID(), nullable=True),
        # NULL = 승인된 뒤 한 번도 주입되지 않은 교훈 — 정리 제안(`05 §10`)의 기준.
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('candidate', 'approved', 'deleted')", name="ck_lessons_status"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["source_answer_id"], ["answers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # ⚠️ `project_id` 단독 인덱스는 만들지 않는다 — 아래 두 복합 인덱스가 모두 `project_id` 를
    # 선두 컬럼으로 쓰므로 그 접두사로 커버된다. 모델(`app/models/lesson.py`)도 선언하지 않으며,
    # 한쪽에만 두면 `alembic check` 가 드리프트로 잡는다.

    # 목록 조회(`05 §10` `?status=`)와 주입용 approved 로드(`06 §3`)가 함께 타는 경로.
    op.create_index(
        "ix_lessons_project_id_status", "lessons", ["project_id", "status"], unique=False
    )
    # 삭제 교훈 재생성 차단(D8)의 대조 경로 — 후보를 만들기 전에 매번 한 번씩 탄다.
    op.create_index(
        "ix_lessons_project_id_content_hash",
        "lessons",
        ["project_id", "content_hash"],
        unique=False,
    )

    op.create_table(
        "briefing_runs",
        sa.Column("project_id", sa.UUID(), nullable=False),
        # 담당자 `users.timezone` 기준 발송 대상 날짜 (`04 §2`).
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        # `04 §7` — 이 제약이 곧 중복 발송 방지 로직이다. `INSERT … ON CONFLICT DO NOTHING`
        # 후 `rowcount == 1` 일 때만 발송한다 (결정 1.11).
        sa.UniqueConstraint("project_id", "run_date", name="uq_briefing_runs_project_date"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("briefing_runs")

    op.drop_index("ix_lessons_project_id_content_hash", table_name="lessons")
    op.drop_index("ix_lessons_project_id_status", table_name="lessons")
    op.drop_table("lessons")
