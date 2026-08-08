"""근거 검색의 **범위**를 한 곳에 고정한다 (`06 §2` ③, D20·D24).

M2 는 범위만 만든다. 벡터 검색 본체(`1 - (embedding <=> :q)` · `hnsw.iterative_scan`)는
M3 에서 이 위에 얹는다 — **범위를 우회해서 `chunks` 를 직접 조회하지 말 것.**

검색 대상 3중 조건 (셋 중 하나라도 빠지면 규칙이 깨진다):

1. `documents.status = 'active'` — soft delete 된 문서의 청크는
   **물리 삭제하지 않되 검색에서 빠진다** (D20).
2. `document_versions.is_active` — 활성 버전 1개만. 구버전 청크는 남아 있지만 근거가 아니다 (룰 5).
3. `document_versions.ingest_status = 'ready'` — 인제스트가 끝나기 전 버전은
   검색 대상이 아니다 (`05 §12.3`).

⚠️ **`chunks` 와 `official_qas` 를 한 랭킹에 섞지 않는다** (D24). 공식 Q&A 는 재사용 판정
단계의 `similar_official_qa` 첨부 경로로만 노출된다.
"""

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import (
    DOCUMENT_STATUS_ACTIVE,
    INGEST_STATUS_READY,
    Chunk,
    Document,
    DocumentVersion,
)


def active_versions_query(project_id: UUID) -> Select[tuple[UUID]]:
    """검색 대상 버전 id 질의 (위 3중 조건)."""
    return (
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.project_id == project_id,
            Document.status == DOCUMENT_STATUS_ACTIVE,
            DocumentVersion.is_active.is_(True),
            DocumentVersion.ingest_status == INGEST_STATUS_READY,
        )
    )


async def active_version_ids(db: AsyncSession, project_id: UUID) -> list[UUID]:
    return list((await db.scalars(active_versions_query(project_id))).all())


def searchable_chunks_query(project_id: UUID) -> Select[tuple[Chunk]]:
    """검색 대상 청크 질의.

    M3 의 벡터 검색은 여기에 `order_by(Chunk.embedding.cosine_distance(:q))` 와
    `limit(retrieval_top_k)` 만 붙인다.
    ⚠️ 유사도는 `1 - (embedding <=> :q)` 다 — `cosine_distance()` 는 **거리**이며
    그대로 정렬 기준으로 쓰되 유사도로 읽으면 등급이 정확히 뒤집힌다 (`04 §7`).
    """
    return select(Chunk).where(Chunk.document_version_id.in_(active_versions_query(project_id)))


async def searchable_chunk_ids(db: AsyncSession, project_id: UUID) -> list[UUID]:
    """범위 검증용 — 테스트가 "soft delete 된 문서의 청크가 빠지는가"를 여기로 단언한다."""
    return list(
        (
            await db.scalars(
                select(Chunk.id)
                .where(Chunk.document_version_id.in_(active_versions_query(project_id)))
                .order_by(Chunk.seq)
            )
        ).all()
    )
