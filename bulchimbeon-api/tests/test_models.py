"""모델 등록 가드 + 스키마 제약이 **실제로 강제되는지**.

`app/models/__init__.py` 에 import 를 빠뜨리면 두 곳이 **조용히** 잘못 동작한다:
`alembic revision --autogenerate` 가 에러 없이 빈 리비전을 뱉고,
`tests/conftest.py` 의 `Base.metadata.create_all` 이 테이블을 만들지 않는다.
둘 다 초록으로 지나가므로 여기서 명시적으로 잡는다.

메타데이터 단언만으로는 부족한 제약(중복 발송 방지·상태 CHECK)은 DB 에 실제로 INSERT 해
거절되는지까지 확인한다 — 앱 로직이 그 거절을 **정상 경로로** 쓰기 때문이다 (결정 1.11).
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base
from app.models.briefing_run import BriefingRun
from app.models.lesson import LESSON_STATUS_CANDIDATE, LESSON_STATUSES, Lesson
from app.schemas.lesson import LessonStatus
from app.utils.hashing import lesson_content_hash
from tests.helpers import create_actor, create_project

# `04 §2` 중 M1 범위의 테이블.
M1_TABLES = {"users", "projects", "project_members", "guidelines", "events"}
# M2 가 더하는 테이블.
M2_TABLES = {"documents", "document_versions", "chunks"}
# M3 질문 파이프라인.
M3_TABLES = {"questions", "answers", "answer_citations", "official_qas"}
# M4 확인 워크플로.
M4_TABLES = {"review_cards", "feedbacks"}
# M5 알림.
M5_TABLES = {"notifications"}
# M6 브리핑·교훈 메모리.
M6_TABLES = {"lessons", "briefing_runs"}


def test_m1_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M1_TABLES


def test_m2_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M2_TABLES


def test_m3_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M3_TABLES


def test_m4_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M4_TABLES


def test_m5_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M5_TABLES


def test_m6_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M6_TABLES


def test_single_active_version_partial_unique_index_exists() -> None:
    """활성 버전 1개 강제는 이 부분 UNIQUE 인덱스가 한다 (`04 §7`).

    ⚠️ 부분 UNIQUE 는 DEFERRABLE 이 아니다 — 전환은 반드시 3문 절차로 한다.
    """
    indexes = {index.name: index for index in Base.metadata.tables["document_versions"].indexes}
    index = indexes["uq_document_versions_single_active"]

    assert index.unique
    assert [column.name for column in index.columns] == ["document_id"]
    assert "is_active" in str(index.dialect_options["postgresql"]["where"])


def test_chunk_embedding_is_hnsw_cosine_and_1536() -> None:
    """`04 §7` — `USING hnsw (embedding vector_cosine_ops)`, 차원은 리터럴 1536 고정."""
    chunks = Base.metadata.tables["chunks"]
    assert chunks.columns["embedding"].type.dim == 1536

    index = {index.name: index for index in chunks.indexes}["ix_chunks_embedding_hnsw"]
    options = index.dialect_options["postgresql"]
    assert options["using"] == "hnsw"
    assert options["ops"] == {"embedding": "vector_cosine_ops"}


def test_feedback_is_unique_per_user_and_answer() -> None:
    """`04 §7` — 유저당 1건. 재제출은 409 이며 verdict 변경은 지원하지 않는다 (D12).

    앱 계층에서도 막지만 제약이 최종 방어선이다 — 동시 제출 두 건이 둘 다 조회를 통과하면
    "맞았다 2건"이 한 사람에게서 나와 승인 추천이 잘못 켜진다.
    """
    constraints = {
        constraint.name: constraint for constraint in Base.metadata.tables["feedbacks"].constraints
    }
    constraint = constraints["uq_feedbacks_answer_user"]

    assert [column.name for column in constraint.columns] == ["answer_id", "user_id"]


def test_single_answerer_partial_unique_index_exists() -> None:
    """담당자 1명 강제는 이 부분 UNIQUE 인덱스가 한다 (`04 §7`, 룰 9)."""
    indexes = {index.name: index for index in Base.metadata.tables["project_members"].indexes}
    index = indexes["uq_project_members_single_answerer"]

    assert index.unique
    assert [column.name for column in index.columns] == ["project_id"]
    where = str(index.dialect_options["postgresql"]["where"])
    assert "answerer" in where and "active" in where


def test_briefing_run_is_unique_per_project_and_run_date() -> None:
    """`04 §7` — 브리핑 중복 발송 방지 제약이 메타데이터에 있어야 한다 (결정 1.11)."""
    constraints = {
        constraint.name: constraint
        for constraint in Base.metadata.tables["briefing_runs"].constraints
    }
    constraint = constraints["uq_briefing_runs_project_date"]

    assert [column.name for column in constraint.columns] == ["project_id", "run_date"]


# --- M6 제약 실측 (`04 §7`) --------------------------------------------------------------


async def _project_id(client: AsyncClient, domain: str) -> UUID:
    """담당자 1명짜리 프로젝트를 만들고 id 만 돌려준다."""
    owner = await create_actor(client, f"owner@{domain}", name="담당자")
    project = await create_project(client, owner)
    return UUID(project["id"])


def _briefing_run_insert(project_id: UUID, run_date: date):
    """`04 §7` 의 발송 판정 SQL — 매번 새 `id` 로 만든다.

    같은 `id` 를 재사용하면 두 번째 INSERT 가 `(project_id, run_date)` 가 아니라 PK 에서
    충돌해 `ON CONFLICT DO NOTHING` 을 비껴간다 — 즉 이 테스트가 검증하려는 제약이 아니라
    엉뚱한 제약을 재는 셈이 된다.
    """
    return (
        pg_insert(BriefingRun)
        .values(
            id=uuid4(),
            project_id=project_id,
            run_date=run_date,
            sent_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["project_id", "run_date"])
    )


async def test_briefing_run_duplicate_is_rejected_by_the_constraint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """제약이 **실제로** 같은 날 두 번째 행을 거절하는지 (`04 §7`, 결정 1.11).

    락이 아니라 이 거절이 중복 발송 방지의 전부다. 제약이 빠지면 앱은 아무 예외도 못 보고
    조용히 두 번 발송한다.
    """
    project_id = await _project_id(client, "briefing-dup.test")
    run_date = date(2026, 8, 8)

    db_session.add(BriefingRun(project_id=project_id, run_date=run_date, sent_at=datetime.now(UTC)))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                BriefingRun(project_id=project_id, run_date=run_date, sent_at=datetime.now(UTC))
            )
            await db_session.flush()


async def test_briefing_run_on_conflict_do_nothing_yields_zero_rowcount(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """두 번째 호출은 `rowcount == 0` 이어야 한다 — 발송 로직의 조기 반환 조건이다.

    `SELECT` 로 "오늘 보냈나?"를 먼저 확인하는 방식은 확인과 INSERT 사이가 벌어지면 두 번
    나간다(TOCTOU). 그래서 판정을 이 한 문장에 맡긴다 (`prompts/06-briefing-lessons.md` 작업 3).
    """
    project_id = await _project_id(client, "briefing-conflict.test")
    run_date = date(2026, 8, 8)

    first = await db_session.execute(_briefing_run_insert(project_id, run_date))
    assert first.rowcount == 1

    second = await db_session.execute(_briefing_run_insert(project_id, run_date))
    assert second.rowcount == 0

    # 다음 날짜는 막히지 않는다 — 제약은 하루 단위다.
    tomorrow = await db_session.execute(_briefing_run_insert(project_id, date(2026, 8, 9)))
    assert tomorrow.rowcount == 1


async def test_lesson_status_check_rejects_unknown_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`04 §2` — status 는 `candidate` | `approved` | `deleted` 뿐이다."""
    project_id = await _project_id(client, "lesson-status.test")
    content = "일본 리전 환불 기한은 20일이다."

    db_session.add(
        Lesson(
            project_id=project_id,
            content=content,
            content_hash=lesson_content_hash(content),
            status=LESSON_STATUS_CANDIDATE,
        )
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Lesson(
                    project_id=project_id,
                    content=content,
                    content_hash=lesson_content_hash(content),
                    status="archived",
                )
            )
            await db_session.flush()


# --- content_hash 정규화 (`03 §5.2`, D8) --------------------------------------------------


def test_lesson_content_hash_normalizes_case_whitespace_and_crlf() -> None:
    """세 입력이 **같은 해시**를 내야 한다 (`prompts/06-briefing-lessons.md` 완료 기준).

    이 셋이 갈라지면 담당자가 지운 교훈이 다음 수정에서 그대로 다시 후보로 올라온다 (D8).
    CRLF 는 특히 OS 별로 갈리는 축이라 `.gitattributes` 의 `*.md text eol=lf` 와 짝을 이룬다.
    """
    hashes = {
        lesson_content_hash("Japan is 20 days.\r\n"),
        lesson_content_hash("  japan  is 20 days. "),
        lesson_content_hash("Japan is 20 days."),
    }

    assert len(hashes) == 1


def test_lesson_content_hash_still_separates_different_lessons() -> None:
    """정규화가 서로 다른 교훈까지 접어버리면 차단이 과잉 적용된다."""
    assert lesson_content_hash("Japan is 20 days.") != lesson_content_hash("Japan is 30 days.")


def test_lesson_status_literal_matches_the_model_check() -> None:
    """스키마의 `Literal` 과 모델의 CHECK 대상이 같은 집합이어야 한다.

    한쪽만 늘리면 API 는 받아들이는데 DB 가 거절하거나(500), DB 는 받는데 스키마가 막는다.
    `test_notification_contract.py` 가 `NotificationType` 에 거는 것과 같은 가드다.
    """
    assert set(LessonStatus.__args__) == set(LESSON_STATUSES)
