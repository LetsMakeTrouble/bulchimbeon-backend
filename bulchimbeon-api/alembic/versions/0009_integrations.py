"""integrations

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-08 23:59:00.000000

M8 외부 연동 (`04 §2` integrations).

`config` 안의 토큰은 **Fernet 암호문**으로 들어간다 (`03 §7`) — 컬럼 타입은 jsonb 그대로이고
암복호화는 `app/core/crypto.py` 가 유일한 구현이다.

동기화 결과(신규 문서 수·실패 목록)는 이 테이블이 아니라 `sync.run` **이벤트**의 payload 에
남는다 (사용자 결정 2026-08-08). 그래서 `04 §2` 가 말한 "에러 메시지 payload" 를 담을 컬럼이
여기 없다 — `app/models/integration.py` 독스트링이 근거다.

시각 기본값은 `clock_timestamp()` 다 (0008, `app/models/base.py`). `now()` 를 쓰면
같은 트랜잭션의 행들이 전부 동률이 되어 정렬이 무너진다.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "integrations",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        # 토큰 필드는 Fernet 암호문이다 (모듈 독스트링).
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # NULL = 한 번도 동기화하지 않았다.
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint("provider IN ('notion', 'github')", name="ck_integrations_provider"),
        sa.CheckConstraint(
            "last_sync_status IN ('ok', 'failed')", name="ck_integrations_last_sync_status"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # `GET /projects/{id}/integrations` 목록의 접근 경로 (`05 §5`).
    op.create_index("ix_integrations_project_id", "integrations", ["project_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_integrations_project_id", table_name="integrations")
    op.drop_table("integrations")
