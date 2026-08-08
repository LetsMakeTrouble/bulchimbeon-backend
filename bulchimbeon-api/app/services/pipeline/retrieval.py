"""근거 검색 (`06 §2` ③, D20·D24) — 범위와 벡터 검색의 단일 원천.

검색 대상 3중 조건 (셋 중 하나라도 빠지면 규칙이 깨진다):

1. `documents.status = 'active'` — soft delete 된 문서의 청크는
   **물리 삭제하지 않되 검색에서 빠진다** (D20).
2. `document_versions.is_active` — 활성 버전 1개만. 구버전 청크는 남아 있지만 근거가 아니다 (룰 5).
3. `document_versions.ingest_status = 'ready'` — 인제스트가 끝나기 전 버전은
   검색 대상이 아니다 (`05 §12.3`).

⚠️ **`chunks` 와 `official_qas` 를 한 랭킹에 섞지 않는다** (D24). 공식 Q&A 는 재사용 판정
단계의 `similar_official_qa` 첨부 경로로만 노출된다. 두 검색이 이 파일에 함께 있는 것은
"임베딩 1회를 두 단계가 나눠 쓴다"는 사실 때문이며(`06 §0`), 랭킹은 끝까지 분리돼 있다.

⚠️ **`<=>` 는 거리다.** SQLAlchemy 헬퍼명이 `cosine_distance()` 인 것에 속지 말 것 —
유사도는 `1 - (embedding <=> :q)` 이며, 그대로 유사도로 쓰면 등급이 **정확히 뒤집힌다**
(`04 §7`).
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import (
    DOCUMENT_STATUS_ACTIVE,
    INGEST_STATUS_READY,
    Chunk,
    Document,
    DocumentVersion,
)
from app.models.official_qa import OFFICIAL_QA_STATUS_ACTIVE, OfficialQA


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

    벡터 검색은 여기에 `order_by(Chunk.embedding.cosine_distance(:q))` 와
    `limit(retrieval_top_k)` 만 붙인다 (`search_evidence`).
    ⚠️ 유사도는 `1 - (embedding <=> :q)` 다 — `cosine_distance()` 는 **거리**이며
    그대로 정렬 기준으로 쓰되 유사도로 읽으면 등급이 정확히 뒤집힌다 (`04 §7`).
    """
    return select(Chunk).where(Chunk.document_version_id.in_(active_versions_query(project_id)))


@dataclass(frozen=True)
class EvidenceChunk:
    """검색 결과 1건. `05 §6` `citations[]` 를 만들 재료를 전부 들고 있다."""

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    doc_title: str
    version_no: int
    content: str
    meta: dict[str, Any]
    sim_raw: float

    @property
    def heading_path(self) -> list[str]:
        value = self.meta.get("heading_path")
        return list(value) if isinstance(value, list) else []

    @property
    def page_no(self) -> int | None:
        value = self.meta.get("page_no")
        return value if isinstance(value, int) else None


# `06 §2` ③ — HNSW 는 인덱스 스캔 **이후** WHERE 를 적용하므로(post-filtering) 활성 버전
# 필터가 걸리면 top-k 가 조용히 0건이 될 수 있다. pgvector **0.8.0 이상** 필요.
_EF_SEARCH_FIRST = 100
_EF_SEARCH_RETRY = 400


async def _apply_hnsw_settings(db: AsyncSession, ef_search: int) -> None:
    """검색 트랜잭션 한정 설정 (`04 §7`). `SET LOCAL` 이므로 트랜잭션이 끝나면 사라진다."""
    await db.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))


async def _run_search(
    db: AsyncSession, project_id: UUID, query_embedding: list[float], top_k: int
) -> list[EvidenceChunk]:
    distance = Chunk.embedding.cosine_distance(query_embedding)

    stmt = (
        select(
            Chunk.id,
            Chunk.content,
            Chunk.meta,
            Document.id.label("document_id"),
            Document.title.label("doc_title"),
            DocumentVersion.id.label("document_version_id"),
            DocumentVersion.version_no,
            # ⚠️ 여기가 거리→유사도 변환의 **유일한 지점**이다 (`04 §7`).
            (literal(1.0) - distance).label("sim_raw"),
        )
        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Chunk.document_version_id.in_(active_versions_query(project_id)))
        .order_by(distance)  # 거리 오름차순 = 유사도 내림차순
        .limit(top_k)
    )

    rows = (await db.execute(stmt)).all()
    return [
        EvidenceChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            document_version_id=row.document_version_id,
            doc_title=row.doc_title,
            version_no=row.version_no,
            content=row.content,
            meta=row.meta or {},
            sim_raw=float(row.sim_raw),
        )
        for row in rows
    ]


async def search_evidence(
    db: AsyncSession, *, project_id: UUID, query_embedding: list[float], top_k: int
) -> list[EvidenceChunk]:
    """③ 근거 검색 — 활성 버전 청크만, `retrieval_top_k` 건 (`06 §2` ③).

    **0건 처리 순서**: 결과 0건 → `ef_search` 를 올려 **재조회 1회** → 그래도 0건일 때만
    호출자가 `no_evidence` 로 확정한다. 재조회 없이 곧바로 확정하면 HNSW post-filtering 때문에
    "문서는 있는데 근거가 없다"는 오판이 난다.
    """
    await _apply_hnsw_settings(db, _EF_SEARCH_FIRST)
    found = await _run_search(db, project_id, query_embedding, top_k)
    if found:
        return found

    await _apply_hnsw_settings(db, _EF_SEARCH_RETRY)
    return await _run_search(db, project_id, query_embedding, top_k)


async def search_official_qa(
    db: AsyncSession, *, project_id: UUID, query_embedding: list[float]
) -> tuple[OfficialQA, float] | None:
    """② 재사용 후보 검색 — top-1 공식 Q&A 와 **원시 코사인** (`06 §2` ②).

    ⚠️ `under_review` · `archived` 는 대상에서 제외한다 (D7·D20). 재검토 중인 지식을
    재사용하면 이미 틀렸다고 신고된 답을 다시 내보내게 된다.
    ⚠️ 여기서 나온 유사도는 **리스케일하지 않은 원시 코사인**이다. S 와 섞지 않는다 (룰 4).
    """
    distance = OfficialQA.question_embedding.cosine_distance(query_embedding)

    stmt = (
        select(OfficialQA, (literal(1.0) - distance).label("sim_raw"))
        .where(
            OfficialQA.project_id == project_id,
            OfficialQA.status == OFFICIAL_QA_STATUS_ACTIVE,
        )
        .order_by(distance)
        .limit(1)
    )

    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    return row[0], float(row.sim_raw)


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
