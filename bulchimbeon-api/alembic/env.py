"""Alembic 환경 — DB URL 과 metadata 는 앱 설정에서 가져온다.

alembic.ini 에 URL 을 복사해 두지 않는다. 비밀값이 파일로 새고, `.env` 와 어긋난 URL 로
마이그레이션이 엉뚱한 DB 에 걸린다.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection

# ⚠️ autogenerate 대상 등록. `app.models` 를 import 해야 Base.metadata 에 테이블이 붙는다.
# 빠뜨리면 `alembic revision --autogenerate` 가 **에러 없이 빈 리비전**을 뱉는다.
# 개별 모델이 아니라 패키지를 import 한다 — 새 모델은 app/models/__init__.py 에만 추가하면 된다.
import app.models  # noqa: F401
from alembic import context
from app.config import settings
from app.database import Base, create_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """`-x database_url=...` 로 덮어쓸 수 있게 둔다 (마이그레이션 검증 테스트가 쓴다)."""
    return context.get_x_argument(as_dictionary=True).get("database_url") or settings.database_url


def run_migrations_offline() -> None:
    """URL 만으로 SQL 을 찍는 오프라인 모드."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 온라인 경로와 같은 설정을 준다. 오프라인에서는 비교할 DB 가 없어 실효는 없지만,
        # 두 경로의 설정이 갈리면 "어느 쪽에서 돌렸느냐"에 따라 결과가 달라 보인다.
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # `compare_server_default=True` — 기본값 표현식의 드리프트까지 `alembic check` 가 잡는다.
    # 끄면 모델이 `clock_timestamp()` 인데 DB 는 `now()` 인 상태가 **초록으로 지나간다**
    # (M7 0008 이 정확히 그 종류의 리비전이다). 새 테이블을 `now()` 로 만들어도 마찬가지다.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
