"""테스트 인프라 (`03 §5.3`).

- **testcontainers 미도입.** 이미 떠 있는 Postgres(`docker compose up -d db`)에
  `TEST_DATABASE_URL` 로 붙는다.
- 세션 스코프에서 테스트 DB 생성 → `CREATE EXTENSION vector` → `Base.metadata.create_all`.
- **테스트별 격리는 트랜잭션 롤백**이다. 테스트마다 DROP/CREATE 하지 않는다 —
  느리고 HNSW 인덱스 재생성 비용이 크다.

> ### ⚠️ 기존 테이블에 제약·인덱스를 **추가**했다면 테스트 DB 를 한 번 지워야 한다 (M8 실측)
> `Base.metadata.create_all` 은 테이블 단위로 `checkfirst` 한다. 테이블이 이미 있으면 통째로
> 건너뛰므로 **새 인덱스가 반영되지 않는다** — 새 테이블 추가는 멀쩡히 반영되기 때문에
> 눈치채기 어렵다. 그 상태에서는 제약을 검증하는 테스트가 "제약이 없어서" 실패하고,
> 반대로 제약에 기대는 코드가 통과해 버릴 수도 있다.
>
>     uv run python -c "import asyncio; from app.config import settings; \\
>       from tests.conftest import drop_database; \\
>       asyncio.run(drop_database(settings.test_database_url))"
>
> 마이그레이션 자체는 `test_migrations.py` 가 별도 스크래치 DB 에서 검증하므로 영향이 없다.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, create_engine, get_db
from app.services import llm, sse_manager, sse_stream_service, sse_ticket_service
from app.services.llm import usage as llm_usage
from app.services.llm.fake_provider import FakeLLMProvider
from app.services.pipeline import answer as answer_pipeline
from app.services.pipeline import ingest
from app.services.sync import runner as sync_runner

# ⚠️ `Base.metadata.create_all` 이 테이블을 만들려면 모델이 먼저 등록돼 있어야 한다.
# 빠뜨리면 테이블 없이 테스트가 돌다가 엉뚱한 곳에서 터진다 (또는 조용히 초록이다).
import app.models  # noqa: F401  # isort:skip
from app.main import app  # isort:skip

# 관리 작업(CREATE DATABASE)을 붙일 유지보수 DB. 대상 DB 자체에는 붙을 수 없다.
ADMIN_DATABASE = "postgres"


async def ensure_database_exists(url: str) -> None:
    """대상 DB 가 없으면 만든다.

    docker-compose 는 `POSTGRES_DB=bulchimbeon` 하나만 만들므로 테스트 DB 는 여기서 만든다.
    CI 의 `services:` 도 마찬가지라 이 경로가 로컬·CI 양쪽을 함께 해결한다.
    """
    target = make_url(url)
    admin_url = target.set(database=ADMIN_DATABASE)

    # CREATE DATABASE 는 트랜잭션 안에서 못 돈다.
    engine = create_engine(
        admin_url.render_as_string(hide_password=False),
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

    # `Base.metadata.create_all` 이 vector 컬럼을 만들려면 확장이 먼저 있어야 한다 (`03 §5.4` ①).
    bootstrap = create_engine(url, poolclass=NullPool)
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


@pytest.fixture(scope="session", autouse=True)
def storage_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """업로드 파일을 임시 디렉터리로 보낸다.

    기본값은 `PROJECT_ROOT/storage` 라(`03 §4`) 그대로 두면 테스트가 레포 안에 파일을 쌓는다.
    `STORAGE_DIR` 는 절대 경로여야 한다는 제약(`03 §5.2`)은 tmp 경로가 이미 만족한다.
    """
    original = settings.storage_dir
    settings.storage_dir = tmp_path_factory.mktemp("storage")
    try:
        yield settings.storage_dir
    finally:
        settings.storage_dir = original


@pytest.fixture(scope="session", autouse=True)
def fake_llm_provider() -> Iterator[FakeLLMProvider]:
    """모든 테스트를 FakeLLMProvider 로 고정한다 (룰 2 — **실 API 호출 테스트 금지**).

    `.env` 의 `LLM_PROVIDER` 가 무엇이든(기본 openai) 여기서 덮어쓴다. 세션 스코프인 이유는
    캐시된 인스턴스를 테스트마다 새로 만들 필요가 없기 때문이고, 호출 이력을 보고 싶은
    테스트는 `fake_llm_provider.embed_calls` 를 직접 읽는다.
    """
    original = settings.llm_provider
    settings.llm_provider = "fake"
    llm.reset_provider_cache()
    try:
        provider = llm.get_provider()
        assert isinstance(provider, FakeLLMProvider)
        yield provider
    finally:
        settings.llm_provider = original
        llm.reset_provider_cache()


@pytest_asyncio.fixture(autouse=True)
async def task_session_factory(db_connection: AsyncConnection) -> AsyncIterator[None]:
    """BackgroundTasks 가 여는 **자체 세션**을 테스트 트랜잭션에 물린다.

    운영에서 백그라운드 태스크(인제스트·질문 파이프라인)는 `AsyncSessionLocal()` 로 전역
    엔진의 새 세션을 연다 (`03 §2` 원칙 4). 테스트에서 그대로 두면 태스크가 **개발 DB** 에
    쓰고, 롤백 격리(`03 §5.3`)를 우회해 테스트가 서로를 오염시킨다.
    같은 커넥션에 세이브포인트로 물려 두면 태스크가 쓴 것도 테스트 종료 시 함께 롤백된다.

    ⚠️ 질문 파이프라인은 **실패 기록용 세션을 따로 연다**(예외가 난 세션 위에서는 커밋할 수
    없기 때문). 그것도 같은 팩토리를 거치므로 여기 한 곳만 갈아끼우면 된다.

    SSE 스트림도 같은 이유로 자체 세션을 쓴다 — 요청 세션을 스트림 수명(최대 30분) 동안
    붙잡으면 커넥션 풀이 마른다 (`services/sse_stream_service.py` 독스트링).
    """

    def factory() -> AsyncSession:
        return AsyncSession(
            bind=db_connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )

    originals = (
        ingest.session_factory,
        answer_pipeline.session_factory,
        sse_stream_service.session_factory,
        sync_runner.session_factory,
        llm_usage.session_factory,
    )
    ingest.session_factory = factory
    answer_pipeline.session_factory = factory
    sse_stream_service.session_factory = factory
    sync_runner.session_factory = factory
    llm_usage.session_factory = factory
    try:
        yield
    finally:
        (
            ingest.session_factory,
            answer_pipeline.session_factory,
            sse_stream_service.session_factory,
            sync_runner.session_factory,
            llm_usage.session_factory,
        ) = originals


@pytest.fixture(autouse=True)
def reset_sse_state() -> Iterator[None]:
    """SSE 티켓 저장소와 구독자 큐는 **프로세스 메모리**다 (룰 9 `--workers 1` 전제).

    비우지 않으면 앞선 테스트가 남긴 티켓·구독이 뒤 테스트에 섞인다.
    """
    sse_ticket_service.reset()
    sse_manager.reset()
    yield
    sse_ticket_service.reset()
    sse_manager.reset()


# ⚠️ 일일 호출 카운터를 비우는 픽스처는 없어졌다. 카운터가 `llm_usage` 행에서 파생되므로
#    테스트별 트랜잭션 롤백이 그대로 격리를 만든다 (`03 §5.3`).


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
