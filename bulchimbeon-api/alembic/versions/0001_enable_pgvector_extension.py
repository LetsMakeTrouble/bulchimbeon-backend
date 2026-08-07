"""enable pgvector extension

Revision ID: 0001
Revises:
Create Date: 2026-08-07 15:52:30.263008

⚠️ 03 §5.4 ① — 최초 마이그레이션의 **첫 줄**이 CREATE EXTENSION 이어야 한다.
이후 리비전이 `vector` 타입 컬럼을 만들 때 확장이 없으면 UndefinedObject 로 죽는다.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# ⚠️ 03 §5.4 ② — autogenerate 는 `Vector(1536)` 를 뱉지만 import 는 넣어주지 않는다.
# 이 import 가 없으면 리비전 실행 시 NameError 가 난다. script.py.mako 가 자동으로 넣어준다.
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Downgrade schema."""
    # base 리비전이므로 여기까지 내려오면 vector 컬럼을 가진 테이블은 이미 전부 사라진 상태다.
    op.execute("DROP EXTENSION IF EXISTS vector")
