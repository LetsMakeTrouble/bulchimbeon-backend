"""questions.mode: 대화모드 / 질문모드

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-19 00:00:00.000000

프론트의 "대화모드 / 질문모드" 토글 지원. 대화모드 질문에는 AI 가 답변하지 않는다 —
접수 즉시 `answered` 가 되어 `processing` 을 거치지 않으므로 파이프라인·좀비 회수의
대상 조건(`status='processing'`)에 애초에 걸리지 않는다 (`pipeline/answer._pipeline`
의 mode 가드가 2중 방어).

기본값이 `question` 이라 기존 행·기존 클라이언트 호출은 동작이 바뀌지 않는다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("mode", sa.Text(), nullable=False, server_default=sa.text("'question'")),
    )
    op.create_check_constraint(
        "ck_questions_mode",
        "questions",
        "mode IN ('question', 'conversation')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_questions_mode", "questions", type_="check")
    op.drop_column("questions", "mode")
