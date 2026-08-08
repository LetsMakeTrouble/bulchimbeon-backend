"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# ⚠️ 03 §5.4 ② — autogenerate 는 벡터 컬럼을 뱉지만 import 는 넣어주지 않아 실행 시 NameError 가 난다.
# 실측(M2, 2026-08-08): 실제로 뱉는 것은 `Vector(1536)` 이 아니라
# **`pgvector.sqlalchemy.vector.VECTOR(dim=1536)`** 라는 완전 경로다. `from ... import Vector`
# 만으로는 `pgvector` 이름이 바인딩되지 않으므로 아래 모듈 import 가 함께 있어야 한다.
# (쓰지 않는 리비전의 미사용 import 는 pyproject 의 per-file-ignores 로 처리한다.)
import pgvector.sqlalchemy
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
