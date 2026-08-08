"""질문 파이프라인 테스트 헬퍼.

> ### 왜 임베딩을 손으로 만드는가
> `FakeLLMProvider` 의 임베딩은 해시 기반이라 **두 텍스트 사이의 코사인이 사실상 0** 이다
> (1536차원 랜덤 단위 벡터). 그대로 두면 모든 테스트에서 S=0 이 되어 등급 분기를 볼 수 없다.
> 그래서 여기서 **목표 코사인을 정확히 갖는 벡터를 합성**해 청크에 심는다.
> 이렇게 하면 `1 - (embedding <=> :q)` 라는 SQL 산출식 자체가 테스트 대상이 된다 —
> 부호가 뒤집히면 즉시 드러난다 (`06 §5` 의 FakeLLM 한계에 대한 대응).
"""

import math
from typing import Any
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import (
    DOCUMENT_STATUS_ACTIVE,
    INGEST_STATUS_READY,
    SOURCE_TYPE_UPLOAD,
    Chunk,
    Document,
    DocumentVersion,
)
from app.models.official_qa import OFFICIAL_QA_STATUS_ACTIVE, OfficialQA
from app.models.project import Project
from app.models.question import (
    ANSWER_STATE_VERIFIED,
    GRADE_GREEN,
    QUESTION_STATUS_ANSWERED,
    Answer,
    Question,
)
from app.services.llm.fake_provider import deterministic_embedding
from tests.helpers import API, Actor

# `05 §6` — 파이프라인이 실제로 임베딩하는 것은 ① 이 만든 **영어 번역문**이다.
# FakeLLMProvider 의 규약이 `content_en = "[en] {원문}"` 이므로 테스트가 재현할 수 있다.
EN_PREFIX = "[en] "


def as_uuid(value: UUID | str) -> UUID:
    """API 응답의 id 는 문자열이다. DB 조회·INSERT 전에 UUID 로 되돌린다."""
    return value if isinstance(value, UUID) else UUID(value)


def question_english(content_ko: str) -> str:
    """파이프라인 ① 이 만들 `content_en` 을 미리 계산한다."""
    return f"{EN_PREFIX}{content_ko.strip()}"


def query_embedding_for(content_ko: str) -> list[float]:
    """질문이 검색에 쓸 벡터."""
    return deterministic_embedding(question_english(content_ko))


def embedding_with_cosine(content_ko: str, target: float) -> list[float]:
    """질문 벡터와 코사인이 정확히 `target` 인 단위 벡터를 만든다.

    `q` 에 직교하는 성분 `r'` 를 구해 `target·q + sqrt(1 - target²)·r'` 로 합성한다.
    ⚠️ pgvector 의 `vector` 는 **float4** 이므로 왕복하면 1e-6 수준의 오차가 남는다.
    단언은 `pytest.approx(abs=1e-5)` 정도로 둔다.
    """
    base = query_embedding_for(content_ko)
    noise = deterministic_embedding(f"{question_english(content_ko)}::orthogonal")

    projection = sum(a * b for a, b in zip(base, noise, strict=True))
    residual = [n - projection * b for n, b in zip(noise, base, strict=True)]
    norm = math.sqrt(sum(value * value for value in residual))
    unit_residual = [value / norm for value in residual]

    parallel = max(-1.0, min(1.0, target))
    orthogonal = math.sqrt(max(0.0, 1.0 - parallel * parallel))
    return [parallel * b + orthogonal * r for b, r in zip(base, unit_residual, strict=True)]


async def seed_document(
    db: AsyncSession,
    *,
    project_id: UUID | str,
    uploader_id: UUID | str,
    chunks: list[tuple[str, list[float]]],
    title: str = "Refund Policy",
    heading_path: list[str] | None = None,
    is_active: bool = True,
    document_status: str = DOCUMENT_STATUS_ACTIVE,
    ingest_status: str = INGEST_STATUS_READY,
) -> tuple[Document, DocumentVersion, list[Chunk]]:
    """검색 대상 청크를 직접 심는다 (업로드·인제스트를 거치지 않는다).

    M2 의 인제스트 경로는 이미 `test_ingest.py` 가 검증했다. 여기서는 **검색 범위 3중 조건**을
    테스트가 직접 조작할 수 있어야 하므로 행을 손으로 만든다.
    """
    document = Document(
        project_id=as_uuid(project_id),
        title=title,
        source_type=SOURCE_TYPE_UPLOAD,
        status=document_status,
    )
    db.add(document)
    await db.flush()

    version = DocumentVersion(
        document_id=document.id,
        version_no=1,
        original_filename=f"{title}.md",
        mime="text/markdown",
        storage_path=f"{project_id}/{title}.md",
        is_active=is_active,
        ingest_status=ingest_status,
        uploaded_by=as_uuid(uploader_id),
    )
    db.add(version)
    await db.flush()

    rows: list[Chunk] = []
    for seq, (content, embedding) in enumerate(chunks):
        chunk = Chunk(
            document_version_id=version.id,
            seq=seq,
            content=content,
            meta={"heading_path": heading_path or ["Refunds", "Standard"], "page_no": None},
            embedding=embedding,
        )
        db.add(chunk)
        rows.append(chunk)

    await db.flush()
    return document, version, rows


async def seed_official_qa(
    db: AsyncSession,
    *,
    project_id: UUID | str,
    asker_id: UUID | str,
    question_embedding: list[float],
    question_ko: str = "기본 환불 기한은 며칠인가요?",
    question_en: str = "What is the standard refund window?",
    answer_ko: str = "구매 후 30일입니다.",
    answer_en: str = "Within 30 days of purchase.",
    status: str = OFFICIAL_QA_STATUS_ACTIVE,
) -> OfficialQA:
    """공식 Q&A 를 만든다.

    `official_qas.source_answer_id` 는 NOT NULL 이다 (`04 §2`) — 공식 Q&A 는 항상 확정된
    답변에서 파생되기 때문이다. 그래서 질문·답변 한 쌍을 먼저 만들어 붙인다.
    """
    origin_question = Question(
        project_id=as_uuid(project_id),
        asker_id=as_uuid(asker_id),
        content_ko=question_ko,
        content_en=question_en,
        status=QUESTION_STATUS_ANSWERED,
    )
    db.add(origin_question)
    await db.flush()

    origin_answer = Answer(
        question_id=origin_question.id,
        grade=GRADE_GREEN,
        state=ANSWER_STATE_VERIFIED,
        content_ko=answer_ko,
        content_en=answer_en,
    )
    db.add(origin_answer)
    await db.flush()

    official_qa = OfficialQA(
        project_id=as_uuid(project_id),
        question_ko=question_ko,
        question_en=question_en,
        answer_ko=answer_ko,
        answer_en=answer_en,
        question_embedding=question_embedding,
        source_answer_id=origin_answer.id,
        status=status,
    )
    db.add(official_qa)
    await db.flush()

    origin_answer.official_qa_id = official_qa.id
    await db.flush()
    return official_qa


async def load_project(db: AsyncSession, project_id: UUID | str) -> Project:
    project = await db.get(Project, as_uuid(project_id))
    assert project is not None
    return project


async def patch_project_settings(
    db: AsyncSession, project_id: UUID | str, **changes: Any
) -> Project:
    """`projects.settings` 부분 갱신 (임계값은 항상 여기서 로드된다 — 룰 3).

    파이프라인은 자체 세션에서 프로젝트를 다시 읽으므로 **커밋까지** 해 둔다.
    (테스트 세션의 커밋은 세이브포인트 해제일 뿐, 바깥 트랜잭션은 그대로 롤백된다.)
    """
    project = await load_project(db, project_id)
    project.settings = {**project.settings, **changes}
    await db.commit()
    return project


async def set_away_mode(db: AsyncSession, project_id: UUID | str, away_mode: bool) -> Project:
    project = await load_project(db, project_id)
    project.away_mode = away_mode
    await db.commit()
    return project


async def ask(
    client: AsyncClient,
    asker: Actor,
    project_id: str,
    content_ko: str,
    *,
    urgency: str = "normal",
) -> dict[str, Any]:
    """질문 접수 → 202. **BackgroundTasks 가 응답 사이클 안에서 끝나므로**
    이 await 가 돌아온 시점에는 파이프라인이 이미 완료돼 있다."""
    response = await client.post(
        f"{API}/projects/{project_id}/questions",
        json={"content_ko": content_ko, "urgency": urgency},
        headers=asker.headers,
    )
    assert response.status_code == 202, response.text
    return response.json()


async def ask_and_get(
    client: AsyncClient,
    asker: Actor,
    project_id: str,
    content_ko: str,
    *,
    urgency: str = "normal",
) -> dict[str, Any]:
    accepted = await ask(client, asker, project_id, content_ko, urgency=urgency)
    detail = await client.get(f"{API}/questions/{accepted['question_id']}", headers=asker.headers)
    assert detail.status_code == 200, detail.text
    return detail.json()
