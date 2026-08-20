"""official_qas.source_answer_id nullable — 담당자 직접 등록 (`05 §9` POST)

Revision ID: 0016_official_qa_source_nullable
Revises: 0014
Create Date: 2026-08-20 00:00:00.000000

지금까지 공식 Q&A 는 카드 승인 편입으로만 생겼다 (`06 §3`). 담당자 직접 등록 경로가
생기면서 원천 답변이 없는 행이 가능해진다 — NULL 이 직접 등록의 표식이다.

⚠️ 리비전 ID 를 번호만("0015"/"0016")으로 두지 않는다 — 별도 브랜치(PR #1,
feat/conversation-messages)가 "0015" 를 쓰고 있어 번호가 충돌한다. 머지 순서가 정해지면
`down_revision` 을 그 시점의 head 로 맞춰야 멀티 헤드가 되지 않는다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_official_qa_source_nullable"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("official_qas", "source_answer_id", existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    # 직접 등록 행(NULL)이 있으면 실패한다 — 그 행들은 되돌릴 원천이 없으므로 수동 정리가 맞다.
    op.alter_column("official_qas", "source_answer_id", existing_type=sa.UUID(), nullable=False)
