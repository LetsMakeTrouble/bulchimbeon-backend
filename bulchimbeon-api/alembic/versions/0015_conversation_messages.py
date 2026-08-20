"""conversation_messages: 사람 간 양방향 대화 채널

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-20 00:00:00.000000

대화모드(0014)는 질문자 단방향 발화였다 - 담당자가 발화할 채널이 없었다
(POST /questions 는 asker 전용, 403). 사람 간 대화는 questions 와 분리된
별도 테이블로 쌓는다 (사용자 결정).

- project_id 는 CASCADE 다: 메시지는 프로젝트 밖에서 의미가 없다.
- append-only 라 updated_at 이 없다 (events 와 같은 규약).
- 목록 조회(created_at 오름차순)용 (project_id, created_at) 인덱스.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("sender_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # now() 가 아니라 clock_timestamp() 다 (`04 §7`, M7 실측).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_messages_project_created",
        "conversation_messages",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_project_created", table_name="conversation_messages")
    op.drop_table("conversation_messages")
