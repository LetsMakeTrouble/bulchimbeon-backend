"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# ⚠️ 03 §5.4 ② — autogenerate 는 `Vector(1536)` 를 뱉지만 import 는 넣어주지 않는다.
# 여기서 미리 넣지 않으면 리비전 실행 시 NameError 가 난다.
# (쓰지 않는 리비전의 미사용 import 는 pyproject 의 per-file-ignores 로 처리한다.)
from pgvector.sqlalchemy import Vector
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
