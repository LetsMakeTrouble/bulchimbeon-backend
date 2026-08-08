"""모델 등록 가드.

`app/models/__init__.py` 에 import 를 빠뜨리면 두 곳이 **조용히** 잘못 동작한다:
`alembic revision --autogenerate` 가 에러 없이 빈 리비전을 뱉고,
`tests/conftest.py` 의 `Base.metadata.create_all` 이 테이블을 만들지 않는다.
둘 다 초록으로 지나가므로 여기서 명시적으로 잡는다.
"""

from app.database import Base

# `04 §2` 중 M1 범위의 테이블.
M1_TABLES = {"users", "projects", "project_members", "guidelines", "events"}
# M2 가 더하는 테이블.
M2_TABLES = {"documents", "document_versions", "chunks"}
# M3 질문 파이프라인.
M3_TABLES = {"questions", "answers", "answer_citations", "official_qas"}
# M4 확인 워크플로.
M4_TABLES = {"review_cards", "feedbacks"}


def test_m1_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M1_TABLES


def test_m2_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M2_TABLES


def test_m3_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M3_TABLES


def test_m4_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M4_TABLES


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
