"""모델 등록 가드.

`app/models/__init__.py` 에 import 를 빠뜨리면 두 곳이 **조용히** 잘못 동작한다:
`alembic revision --autogenerate` 가 에러 없이 빈 리비전을 뱉고,
`tests/conftest.py` 의 `Base.metadata.create_all` 이 테이블을 만들지 않는다.
둘 다 초록으로 지나가므로 여기서 명시적으로 잡는다.
"""

from app.database import Base

# `04 §2` 중 M1 범위의 테이블.
M1_TABLES = {"users", "projects", "project_members", "guidelines", "events"}


def test_m1_tables_are_registered_on_metadata() -> None:
    assert set(Base.metadata.tables) >= M1_TABLES


def test_single_answerer_partial_unique_index_exists() -> None:
    """담당자 1명 강제는 이 부분 UNIQUE 인덱스가 한다 (`04 §7`, 룰 9)."""
    indexes = {index.name: index for index in Base.metadata.tables["project_members"].indexes}
    index = indexes["uq_project_members_single_answerer"]

    assert index.unique
    assert [column.name for column in index.columns] == ["project_id"]
    where = str(index.dialect_options["postgresql"]["where"])
    assert "answerer" in where and "active" in where
