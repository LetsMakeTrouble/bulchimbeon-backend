"""실제 `seed/*.md` 를 그대로 검색 대상으로 심는 헬퍼 (`08 §2`).

> ### 왜 실제 시드 파일을 읽는가
> `tests/pipeline_helpers.seed_document` 는 청크 내용을 테스트가 지어낸다 — 등급 분기를 보는
> 데는 그걸로 충분하지만, `08 §3` Q11~Q13 이 검증하려는 것은 **확장된 시드 섹션이 실제로
> top-k 안에 들어오는가**다. 청크 23개를 실제 파일에서 만들어야 그 질문이 성립한다.
>
> 임베딩만 합성한다 — `FakeLLMProvider` 의 해시 임베딩은 두 텍스트 사이 코사인이 사실상
> 0 이라(`06 §5` 한계) 그대로 두면 23개 전부 무의미한 순위가 된다. 기대 근거 청크에만
> 목표 코사인을 심고 나머지는 해시 임베딩 그대로 두면, **정답 청크가 top-1 으로 올라오는지**가
> 실제 SQL 랭킹(`1 - (embedding <=> :q)`)으로 검증된다.
"""

from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import (
    DOCUMENT_STATUS_ACTIVE,
    INGEST_STATUS_READY,
    SOURCE_TYPE_UPLOAD,
    Chunk,
    Document,
    DocumentVersion,
)
from app.services.llm.fake_provider import deterministic_embedding
from app.utils.chunking import chunk_pages
from app.utils.parsing import MIME_MARKDOWN, parse_file

# ⚠️ 문서 목록·기대 청크 수는 **`scripts/seed.py` 가 정본**이다. 여기 복사해 두면 문서를
#    5개로 늘렸을 때 `test_seed.py` 는 잡아도 이 파일을 쓰는 회귀 테스트는 **옛 4개 세트를
#    계속 초록으로 통과시킨다** — `demo_questions.py` 가 질문 문안에 대해 없앤 함정과 같다.
from scripts.seed import EXPECTED_CHUNK_COUNT, SEED_DIR, SEED_DOCUMENTS
from tests.pipeline_helpers import as_uuid, embedding_with_cosine

__all__ = [
    "EXPECTED_CHUNK_COUNT",
    "SEED_DOCUMENTS",
    "SeedChunk",
    "plant_seed_corpus",
    "seed_corpus",
]


@dataclass(frozen=True)
class SeedChunk:
    doc_filename: str
    doc_title: str
    heading_path: tuple[str, ...]
    content: str


@lru_cache(maxsize=1)
def seed_corpus() -> tuple[SeedChunk, ...]:
    """`seed/*.md` 4개를 **운영과 같은 파서·청커로** 잘라 둔다."""
    chunks: list[SeedChunk] = []
    for filename, title in SEED_DOCUMENTS:
        pages = parse_file(SEED_DIR / filename, MIME_MARKDOWN)
        for draft in chunk_pages(pages):
            chunks.append(
                SeedChunk(
                    doc_filename=filename,
                    doc_title=title,
                    heading_path=tuple(draft.heading_path),
                    content=draft.content,
                )
            )
    return tuple(chunks)


async def plant_seed_corpus(
    db: AsyncSession,
    *,
    project_id: UUID | str,
    uploader_id: UUID | str,
    question_ko: str,
    boosts: dict[tuple[str, ...], float],
) -> dict[tuple[str, ...], UUID]:
    """시드 코퍼스 23청크를 문서 4개로 심는다. 돌려주는 값은 `heading_path` → `chunk_id`.

    `boosts` 에 있는 청크만 질문과 목표 코사인을 갖고, 나머지는 해시 임베딩(코사인 ≈ 0)이다.
    업로드·인제스트를 거치지 않는 이유는 `pipeline_helpers.seed_document` 와 같다 — 그 경로는
    `test_ingest.py` 가 이미 검증했고, 여기서 필요한 것은 **임베딩을 손으로 지정하는 것**이다.
    """
    corpus = seed_corpus()
    unknown = set(boosts) - {chunk.heading_path for chunk in corpus}
    assert not unknown, f"시드에 없는 heading_path 를 boost 했다: {unknown}"

    by_heading: dict[tuple[str, ...], UUID] = {}

    for filename, title in SEED_DOCUMENTS:
        document = Document(
            project_id=as_uuid(project_id),
            title=title,
            source_type=SOURCE_TYPE_UPLOAD,
            status=DOCUMENT_STATUS_ACTIVE,
        )
        db.add(document)
        await db.flush()

        version = DocumentVersion(
            document_id=document.id,
            version_no=1,
            original_filename=filename,
            mime=MIME_MARKDOWN,
            storage_path=f"{project_id}/{filename}",
            is_active=True,
            ingest_status=INGEST_STATUS_READY,
            uploaded_by=as_uuid(uploader_id),
        )
        db.add(version)
        await db.flush()

        for seq, item in enumerate(chunk for chunk in corpus if chunk.doc_filename == filename):
            target = boosts.get(item.heading_path)
            embedding = (
                embedding_with_cosine(question_ko, target)
                if target is not None
                else deterministic_embedding(item.content)
            )
            chunk = Chunk(
                document_version_id=version.id,
                seq=seq,
                content=item.content,
                meta={"heading_path": list(item.heading_path), "page_no": None},
                embedding=embedding,
            )
            db.add(chunk)
            await db.flush()
            by_heading[item.heading_path] = chunk.id

    await db.commit()
    return by_heading
