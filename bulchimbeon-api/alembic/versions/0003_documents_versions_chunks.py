"""documents, versions, chunks

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08 07:40:08.193066

"""

from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa

# ⚠️ 03 §5.4 ② — autogenerate 는 벡터 컬럼을 `pgvector.sqlalchemy.vector.VECTOR(dim=1536)` 로
# 뱉지만 import 는 넣어주지 않는다. 여기서는 템플릿이 넣어 준 `Vector(1536)` 로 손질했다
# (같은 타입이고 리비전 파일이 읽힌다). 손질을 빠뜨리면 실행 시 `NameError: pgvector` 다.
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `04 §7` — HNSW `iterative_scan` 은 pgvector 0.8.0 이상을 요구한다. M3 근거 검색이
# post-filtering 대응으로 이 기능에 의존하므로, 인덱스를 만들기 **전에** 여기서 막는다.
# 0.7 대 DB 에 배포하면 인덱스는 만들어지고 검색만 조용히 0건이 되는 최악의 형태로 실패한다.
MIN_PGVECTOR_VERSION = (0, 8, 0)


# 오프라인(`alembic upgrade head --sql`)에는 연결이 없어 버전을 물어볼 수 없다.
# 그렇다고 그냥 건너뛰면 **DDL 경로만 보호를 잃는다** — 운영 DB 를 DDL 로 만드는 쪽이
# 오히려 더 위험한 경로다. 그래서 같은 판정을 SQL 안에 심어 내보낸다.
_PGVECTOR_GUARD_SQL = """
DO $$
DECLARE ext_version text;
BEGIN
    SELECT extversion INTO ext_version FROM pg_extension WHERE extname = 'vector';
    IF ext_version IS NULL THEN
        RAISE EXCEPTION 'vector 확장이 없다. 리비전 0001 이 CREATE EXTENSION vector 를 수행한다.';
    END IF;
    IF (
        regexp_replace(split_part(ext_version, '.', 1), '[^0-9]', '', 'g')::int,
        regexp_replace(split_part(ext_version, '.', 2), '[^0-9]', '', 'g')::int
    ) < (0, 8) THEN
        RAISE EXCEPTION
            'pgvector % 은(는) 너무 낮다. 0.8.0 이상이 필요하다 (HNSW iterative_scan).',
            ext_version;
    END IF;
END $$;
"""


def _require_pgvector() -> None:
    if context.is_offline_mode():
        op.execute(_PGVECTOR_GUARD_SQL)
        return

    extversion = op.get_bind().scalar(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    if extversion is None:
        raise RuntimeError(
            "vector 확장이 없다. 리비전 0001 이 `CREATE EXTENSION vector` 를 수행한다 "
            "(`03 §5.4` ①)."
        )

    parts: list[int] = []
    for token in extversion.split(".")[:3]:
        digits = "".join(char for char in token if char.isdigit())
        parts.append(int(digits) if digits else 0)

    if tuple(parts) < MIN_PGVECTOR_VERSION:
        raise RuntimeError(
            f"pgvector {extversion} 은(는) 너무 낮다. "
            f"{'.'.join(str(part) for part in MIN_PGVECTOR_VERSION)} 이상이 필요하다 "
            "(HNSW `iterative_scan`, `04 §7`)."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _require_pgvector()

    op.create_table(
        "documents",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
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
            "source_type IN ('upload', 'notion', 'github')", name="ck_documents_source_type"
        ),
        sa.CheckConstraint("status IN ('active', 'deleted')", name="ck_documents_status"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_project_id"), "documents", ["project_id"], unique=False)
    op.create_table(
        "document_versions",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ingest_status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("ingest_error", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
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
            "ingest_status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_document_versions_ingest_status",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_document_no"),
    )
    op.create_index(
        op.f("ix_document_versions_document_id"),
        "document_versions",
        ["document_id"],
        unique=False,
    )
    # 활성 버전 1개 강제 (`04 §7`). ⚠️ 부분 UNIQUE 는 DEFERRABLE 이 아니다 —
    # 활성 전환은 반드시 한 트랜잭션 안의 3문 절차로 한다 (단일 UPDATE 는 행 순서에 따라 23505).
    op.create_index(
        "uq_document_versions_single_active",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "chunks",
        sa.Column("document_version_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # 차원은 `vector(1536)` **리터럴 고정**이다 (`04` 상단). EMBEDDING_DIM env 로 바꾸지 않는다.
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", "seq", name="uq_chunks_version_seq"),
    )
    op.create_index(
        op.f("ix_chunks_document_version_id"), "chunks", ["document_version_id"], unique=False
    )
    # `04 §7` — 검색은 `1 - (embedding <=> :q)`. `<=>` 는 거리이므로 cosine ops 를 쓴다.
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_chunks_embedding_hnsw",
        table_name="chunks",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index(op.f("ix_chunks_document_version_id"), table_name="chunks")
    op.drop_table("chunks")
    op.drop_index(
        "uq_document_versions_single_active",
        table_name="document_versions",
        postgresql_where=sa.text("is_active"),
    )
    op.drop_index(op.f("ix_document_versions_document_id"), table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index(op.f("ix_documents_project_id"), table_name="documents")
    op.drop_table("documents")
