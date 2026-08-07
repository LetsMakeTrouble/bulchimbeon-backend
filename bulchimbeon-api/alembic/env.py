"""Alembic 환경 — DB URL 과 metadata 는 앱 설정에서 가져온다.

alembic.ini 에 URL 을 복사해 두지 않는다. 비밀값이 파일로 새고, `.env` 와 어긋난 URL 로
마이그레이션이 엉뚱한 DB 에 걸린다.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection

from alembic import context
from app.config import settings
from app.database import Base, create_engine

# app.models 가 생기면 여기서 import 해 Base.metadata 에 등록한다 (autogenerate 대상).

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
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # ⚠️ 마이그레이션 엔진은 vector 코덱을 등록하지 않는다.
    # 확장을 만드는 것이 리비전 0001 자체라, 코덱을 켜면 그 리비전을 실행할 커넥션조차 열 수 없다
    # (`ValueError: unknown type: public.vector`).
    # 마이그레이션은 DDL 텍스트만 보내므로 코덱이 필요 없다.
    connectable = create_engine(
        _database_url(), register_vector_codec=False, poolclass=pool.NullPool
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
