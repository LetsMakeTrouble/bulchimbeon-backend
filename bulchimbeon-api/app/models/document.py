"""documents · document_versions · chunks (`04 §2`).

⚠️ 벡터 컬럼은 `vector(1536)` **리터럴 고정**이다 (`04` 문서 상단).
`EMBEDDING_DIM` env 는 (a) 임베딩 응답 차원 검증 (b) OpenAI `dimensions` 파라미터 전달용이며
컬럼 차원을 바꾸는 스위치가 아니다 — `config.EMBEDDING_DIM_FIXED` 를 그대로 쓴다.
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import EMBEDDING_DIM_FIXED
from app.database import Base
from app.models.base import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

# `04 §2` documents.source_type — M2 는 upload 만 만든다. notion/github 는 M8.
SOURCE_TYPE_UPLOAD = "upload"
SOURCE_TYPE_NOTION = "notion"
SOURCE_TYPE_GITHUB = "github"
SOURCE_TYPES = (SOURCE_TYPE_UPLOAD, SOURCE_TYPE_NOTION, SOURCE_TYPE_GITHUB)

DOCUMENT_STATUS_ACTIVE = "active"
DOCUMENT_STATUS_DELETED = "deleted"  # soft delete (D20)
DOCUMENT_STATUSES = (DOCUMENT_STATUS_ACTIVE, DOCUMENT_STATUS_DELETED)

INGEST_STATUS_PENDING = "pending"
INGEST_STATUS_PROCESSING = "processing"
INGEST_STATUS_READY = "ready"
INGEST_STATUS_FAILED = "failed"
INGEST_STATUSES = (
    INGEST_STATUS_PENDING,
    INGEST_STATUS_PROCESSING,
    INGEST_STATUS_READY,
    INGEST_STATUS_FAILED,
)


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('upload', 'notion', 'github')", name="ck_documents_source_type"
        ),
        CheckConstraint("status IN ('active', 'deleted')", name="ck_documents_status"),
        # 외부 원본 1개 = 문서 1개 (`04 §7`, 사용자 결정 2026-08-08).
        # 동기화가 "이미 있나?" 조회와 INSERT 사이에서 겹치면 같은 파일이 문서 두 벌이 되고,
        # 그 뒤로는 한쪽만 갱신돼 다른 쪽이 **낡은 근거로 활성 상태를 유지**한다.
        # 락이 아니라 제약으로 막는다 — `briefing_runs` 의 중복 발송 방지와 같은 방식이다.
        # 업로드 문서는 `source_ref` 가 NULL 이라 대상 밖이다.
        Index(
            "uq_documents_source_ref",
            "project_id",
            "source_type",
            "source_ref",
            unique=True,
            postgresql_where=text("source_ref IS NOT NULL"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)

    # notion page_id / github `owner/repo:path` (M8). 업로드 문서는 NULL 이다.
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    # soft delete — 행도 청크도 지우지 않는다. 검색에서만 빠진다 (D20).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))


class DocumentVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_document_no"),
        # 활성 버전 1개 강제 (`04 §7`).
        # ⚠️ 부분 UNIQUE 는 DEFERRABLE 이 아니다 — 활성 전환을 단일 `UPDATE … CASE` 로 하지 말 것.
        #    M-1 실측(2026-08-07)에서 단일 UPDATE 는 **행 처리 순서에 따라 통과하기도 했다**.
        #    반드시 document_service.activate_version 의 3문 절차를 쓴다 (`04 §7`).
        Index(
            "uq_document_versions_single_active",
            "document_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "ingest_status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_document_versions_ingest_status",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)

    # `STORAGE_DIR` 기준 **상대 경로** (`04 §2`). 절대 경로를 저장하면 배포 컨테이너에서 깨진다.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ingest_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    ingest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class Chunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """인제스트 산출물. 갱신하지 않고 버전마다 새로 만든다 — `updated_at` 이 없다."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "seq", name="uq_chunks_version_seq"),
        # `04 §7` — 검색은 항상 `1 - (embedding <=> :q)` 로 유사도를 만든다. `<=>` 는 **거리**다.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("document_versions.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # `{heading_path: [...], page_no: int | null}` (`04 §2`, `06 §1`).
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM_FIXED), nullable=False)

    # `06 §2` ③ — 이 청크가 쓰인 언어. 질문을 **같은 언어로** 번역해 검색하기 위한 값이며
    # 인제스트가 문자 체계로 판별해 채운다 (`app/utils/language.py`).
    #
    # ⚠️ `NULL` 은 0013 이전에 들어온 청크다. 검색은 이를 기본 언어로 취급한다 —
    #    한국어 문서가 이미 있는 프로젝트는 재인제스트해야 효과를 본다.
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
