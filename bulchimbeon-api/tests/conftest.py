"""테스트 인프라 (`03 §5.3`).

- **testcontainers 미도입.** 이미 떠 있는 Postgres(`docker compose up -d db`)에
  `TEST_DATABASE_URL` 로 붙는다.
- 세션 스코프에서 테스트 DB 생성 → `CREATE EXTENSION vector` → `Base.metadata.create_all`.
- **테스트별 격리는 트랜잭션 롤백**이다. 테스트마다 DROP/CREATE 하지 않는다 —
  느리고 HNSW 인덱스 재생성 비용이 크다.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, create_engine, get_db
from app.main import app

# 관리 작업(CREATE DATABASE)을 붙일 유지보수 DB. 대상 DB 자체에는 붙을 수 없다.
ADMIN_DATABASE = "postgres"


async def ensure_database_exists(url: str) -> None:
    """대상 DB 가 없으면 만든다.

    docker-compose 는 `POSTGRES_DB=bulchimbeon` 하나만 만들므로 테스트 DB 는 여기서 만든다.
    CI 의 `services:` 도 마찬가지라 이 경로가 로컬·CI 양쪽을 함께 해결한다.
    """
    target = make_url(url)
    admin_url = target.set(database=ADMIN_DATABASE)

    # 확장이 없는 DB 이므로 vector 코덱을 켜지 않는다. CREATE DATABASE 는 트랜잭션 안에서 못 돈다.
    engine = create_engine(
        admin_url.render_as_string(hide_password=False),
        register_vector_codec=False,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await engine.dispose()


async def drop_database(url: str) -> None:
    """대상 DB 를 강제로 지운다 (마이그레이션 검증 테스트의 스크래치 DB 정리용)."""
    target = make_url(url)
    admin_url = target.set(database=ADMIN_DATABASE)

    engine = create_engine(
        admin_url.render_as_string(hide_password=False),
        register_vector_codec=False,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """테스트 DB 를 준비하고 엔진을 돌려준다."""
    url = settings.test_database_url
    await ensure_database_exists(url)

    # ⚠️ 확장을 만들기 전에는 vector 코덱을 등록할 수 없다 — register_vector 가
    # public.vector 타입을 조회하다 ValueError 로 커넥션을 못 연다 (app/database.py 참조).
    bootstrap = create_engine(url, register_vector_codec=False, poolclass=NullPool)
    try:
        async with bootstrap.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        await bootstrap.dispose()

    engine = create_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_connection(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """테스트 하나를 감싸는 바깥 트랜잭션. 끝나면 롤백해서 상태를 되돌린다."""
    conn = await db_engine.connect()
    trans = await conn.begin()
    try:
        yield conn
    finally:
        await trans.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """바깥 트랜잭션에 참여하는 세션.

    `join_transaction_mode="create_savepoint"` 라서 테스트 코드가 `commit()` 을 불러도
    세이브포인트만 확정되고 바깥 트랜잭션은 살아 있다 — 롤백 한 번으로 전부 되돌아간다.
    """
    session = AsyncSession(
        bind=db_connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        await session.close()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """앱의 DB 의존성을 테스트 세션으로 갈아끼운 httpx 클라이언트."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http_client:
            yield http_client
    finally:
        app.dependency_overrides.pop(get_db, None)
