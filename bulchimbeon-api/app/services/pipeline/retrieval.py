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

import re
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import Select, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import (
    DOCUMENT_STATUS_ACTIVE,
    INGEST_STATUS_READY,
    Chunk,
    Document,
    DocumentVersion,
)
from app.models.official_qa import OFFICIAL_QA_STATUS_ACTIVE, OfficialQA
from app.utils.language import DEFAULT_LANGUAGE


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

# 섹션 단위로 접으면 개수가 줄므로 후보를 이 배수만큼 넉넉히 뽑는다.
_CANDIDATE_MULTIPLIER = 4

# 식별자로 볼 토큰 — 코드·경로·설정 키. 한국어 어절은 여기 걸리지 않는다(의도).
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")
_MIN_IDENTIFIER_LENGTH = 4


async def _apply_hnsw_settings(db: AsyncSession, ef_search: int) -> None:
    """검색 트랜잭션 한정 설정 (`04 §7`). `SET LOCAL` 이므로 트랜잭션이 끝나면 사라진다."""
    await db.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))


async def project_languages(db: AsyncSession, project_id: UUID) -> list[str]:
    """이 프로젝트의 **검색 대상 청크에 실제로 있는** 언어들.

    ⚠️ `projects.settings` 에 목록을 따로 두지 않는다. 설정 키는 계약서(`05 §3`)가 16개로
    닫아 두었고, 무엇보다 **문서를 올릴 때마다 목록이 어긋날 자리**가 생긴다. 청크에서
    끌어내면 업로드·삭제·버전 교체를 자동으로 따라간다.

    `NULL`(0013 이전 청크)은 기본 언어로 접는다.
    """
    rows = await db.scalars(
        select(Chunk.language)
        .where(Chunk.document_version_id.in_(active_versions_query(project_id)))
        .distinct()
    )
    found = {row or DEFAULT_LANGUAGE for row in rows}
    return sorted(found) if found else [DEFAULT_LANGUAGE]


def _evidence_columns(sim_expression: Any) -> Any:
    """근거 1건을 만들 컬럼 묶음. 벡터 검색과 키워드 보강이 **같은 모양**을 쓴다."""
    return (
        select(
            Chunk.id,
            Chunk.content,
            Chunk.meta,
            Document.id.label("document_id"),
            Document.title.label("doc_title"),
            DocumentVersion.id.label("document_version_id"),
            DocumentVersion.version_no,
            sim_expression.label("sim_raw"),
        )
        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
    )


def _to_evidence(row: Any) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=row.id,
        document_id=row.document_id,
        document_version_id=row.document_version_id,
        doc_title=row.doc_title,
        version_no=row.version_no,
        content=row.content,
        meta=row.meta or {},
        sim_raw=float(row.sim_raw),
    )


async def _run_search(
    db: AsyncSession,
    project_id: UUID,
    query_embedding: list[float],
    top_k: int,
    language: str | None = None,
) -> list[EvidenceChunk]:
    distance = Chunk.embedding.cosine_distance(query_embedding)

    stmt = (
        # ⚠️ `1 - distance` 가 거리→유사도 변환의 **유일한 지점**이다 (`04 §7`).
        _evidence_columns(literal(1.0) - distance)
        .where(Chunk.document_version_id.in_(active_versions_query(project_id)))
        .order_by(distance)  # 거리 오름차순 = 유사도 내림차순
        .limit(top_k)
    )
    if language is not None:
        # 기본 언어는 `NULL`(0013 이전 청크)도 함께 본다 — 마이그레이션 직후에도 검색이
        # 비지 않게 하기 위해서다.
        stmt = stmt.where(
            Chunk.language.is_(None) | (Chunk.language == language)
            if language == DEFAULT_LANGUAGE
            else Chunk.language == language
        )

    return [_to_evidence(row) for row in (await db.execute(stmt)).all()]


async def search_evidence(
    db: AsyncSession,
    *,
    project_id: UUID,
    query_embeddings: dict[str, list[float]],
    top_k: int,
    query_texts: dict[str, str] | None = None,
) -> list[EvidenceChunk]:
    """③ 근거 검색 — **언어별로 같은 언어끼리** 재고 합친다 (`06 §2` ③).

    `query_embeddings` 는 `{언어: 그 언어로 번역한 질문의 임베딩}` 이다. 한국어 문서는
    한국어 질의로, 영어 문서는 영어 질의로 잰다 — 임베딩은 같은 언어끼리 비교할 때 가장
    정확하고, 축이 하나뿐이면 반대쪽 언어가 통째로 손해를 본다.

    실측(2026-08-16): 한국어 청크를 영어 질의로 찾을 때 0.1914 였던 질문이 있다.
    `similarity_floor`(0.444) 아래라 강제 🔴 `no_evidence` 였는데, 청크 본문은 질문과
    거의 같은 문장이었다.

    ⛔ **언어별 결과를 sim 으로 그냥 합친다.** 같은 모델·같은 축이라 비교가 성립한다.
    언어마다 top_k 를 뽑아 합친 뒤 다시 top_k 로 자르므로, 한 언어가 좋은 근거를 독점하면
    그 언어가 자리를 다 가져간다 — 균등 배분하지 않는 것이 의도다. 근거는 언어가 아니라
    유사도로 뽑아야 한다.

    **0건 처리 순서**: 결과 0건 → `ef_search` 를 올려 **재조회 1회** → 그래도 0건일 때만
    호출자가 `no_evidence` 로 확정한다. 재조회 없이 곧바로 확정하면 HNSW post-filtering 때문에
    "문서는 있는데 근거가 없다"는 오판이 난다.
    """
    if not query_embeddings:
        return []

    # 후보를 넉넉히 뽑는다 — 뒤에서 섹션 단위로 접으면 개수가 줄기 때문이다.
    candidate_k = top_k * _CANDIDATE_MULTIPLIER

    for ef_search in (_EF_SEARCH_FIRST, _EF_SEARCH_RETRY):
        await _apply_hnsw_settings(db, ef_search)
        merged: list[EvidenceChunk] = []
        for language, embedding in query_embeddings.items():
            merged.extend(await _run_search(db, project_id, embedding, candidate_k, language))
        merged.extend(await _keyword_candidates(db, project_id, query_texts, candidate_k))
        if merged:
            return await _fold_into_sections(db, merged, top_k)

    return []


async def _keyword_candidates(
    db: AsyncSession,
    project_id: UUID,
    query_texts: dict[str, str] | None,
    top_k: int,
) -> list[EvidenceChunk]:
    """식별자 매칭 보강 — 임베딩이 뭉개는 것을 **글자 그대로** 찾는다.

    `VITE_API_BASE_URL` · `/inbox` · `s_ceil` 같은 토큰은 벡터 공간에서 주변 단어에 묻힌다.
    실측(2026-08-16)에서 차단선 아래로 떨어진 질문의 상당수가 이 유형이었다 — 답이 문서
    안에 있는데 `no_evidence` 로 떨어졌다.

    ⚠️ **점수(S)를 여기서 만들지 않는다.** 이 함수가 하는 일은 후보에 넣어 주는 것뿐이고,
    `sim_raw` 는 그대로 벡터 코사인이다(키워드로만 걸린 청크는 0.0). 등급의 정의를
    건드리면 80/50 임계값의 의미가 바뀐다 (룰 1). 그래서 "찾긴 찾았는데 🟡" 은 나와도,
    **근거가 있는데 `no_evidence` 로 떨어지는 일은 준다.**

    ⚠️ 한국어 어절은 대상이 아니다 — 조사가 붙어 글자 그대로 일치하지 않는다. 이 함수의
    목적은 식별자이지 자연어 검색이 아니다.
    """
    terms = {
        term
        for text in (query_texts or {}).values()
        for term in _IDENTIFIER_PATTERN.findall(text)
        if len(term) >= _MIN_IDENTIFIER_LENGTH
    }
    if not terms:
        return []

    stmt = (
        _evidence_columns(literal(0.0))
        .where(
            Chunk.document_version_id.in_(active_versions_query(project_id)),
            or_(*(Chunk.content.ilike(f"%{term}%") for term in sorted(terms))),
        )
        .limit(top_k)
    )
    return [_to_evidence(row) for row in (await db.execute(stmt)).all()]


async def _fold_into_sections(
    db: AsyncSession, candidates: list[EvidenceChunk], top_k: int
) -> list[EvidenceChunk]:
    """**작게 찾고 크게 답한다** — 청크로 찾되 ④ 에는 그 청크가 속한 섹션 전체를 넘긴다.

    청킹을 문장 단위로 잘게 나눈 뒤(`utils/chunking.MAX_UNITS_PER_CHUNK`) 검색은 좋아졌지만
    (차단선 미달 26 → 8건), 그대로 두면 ④ 가 받는 근거가 문장 세 개짜리 조각이 된다.
    매칭률은 `min(S, G)` 이라 **G 가 떨어지면 S 를 올린 보람이 없다.**

    그래서 순위는 잘게 매기고 본문은 굵게 돌려준다:
    - 같은 (버전, `heading_path`) 조각들을 하나로 접는다 — 중복이 자리를 먹던 것도 사라진다
    - `sim_raw` 는 그 섹션에서 **가장 잘 맞은 조각의 값**이다 (S 의 의미가 그대로다)
    - `chunk_id` 도 그 조각이다 — 인용은 여전히 문장 단위로 정확히 가리킨다
    """
    best: dict[tuple[UUID, tuple[str, ...]], EvidenceChunk] = {}
    for chunk in candidates:
        key = (chunk.document_version_id, tuple(chunk.heading_path))
        current = best.get(key)
        if current is None or chunk.sim_raw > current.sim_raw:
            best[key] = chunk

    picked = sorted(best.values(), key=lambda chunk: chunk.sim_raw, reverse=True)[:top_k]
    if not picked:
        return []

    rows = (
        await db.execute(
            select(Chunk.document_version_id, Chunk.meta, Chunk.content)
            .where(Chunk.document_version_id.in_({chunk.document_version_id for chunk in picked}))
            .order_by(Chunk.document_version_id, Chunk.seq)
        )
    ).all()

    sections: dict[tuple[UUID, tuple[str, ...]], list[str]] = {}
    for version_id, meta, content in rows:
        heading_path = meta.get("heading_path") if isinstance(meta, dict) else None
        sections.setdefault((version_id, tuple(heading_path or [])), []).append(content)

    expanded: list[EvidenceChunk] = []
    for chunk in picked:
        pieces = sections.get((chunk.document_version_id, tuple(chunk.heading_path)))
        expanded.append(chunk if not pieces else replace(chunk, content=_join_pieces(pieces)))
    return expanded


def _join_pieces(pieces: list[str]) -> str:
    """섹션 조각을 잇는다. 창끼리 한 단위 겹치므로 **중복 줄을 접는다.**"""
    seen: set[str] = set()
    lines: list[str] = []
    for piece in pieces:
        for line in piece.splitlines():
            marker = line.strip()
            if marker and marker in seen:
                continue
            if marker:
                seen.add(marker)
            lines.append(line)
    return "\n".join(lines).strip()


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
