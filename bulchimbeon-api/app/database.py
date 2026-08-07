"""async 엔진/세션과 SQLAlchemy 선언 베이스.

⚠️ pgvector 함정 3종 중 3번(`03 §5.4`): asyncpg 커넥션마다 `register_vector` 를 호출한다.
없으면 임베딩이 문자열로 왕복해 `<=>` 연산이 실패하거나 조용히 느려진다.
"""

from collections.abc import AsyncIterator

from pgvector.asyncpg import register_vector
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """모든 모델의 선언 베이스 (`04 §2`의 테이블과 1:1)."""


def create_engine(url: str, *, register_vector_codec: bool = True, **kwargs: object) -> AsyncEngine:
    """`register_vector` 가 설치된 async 엔진을 만든다.

    엔진을 새로 만드는 모든 경로(앱·테스트·마이그레이션)가 이 함수를 거쳐야
    vector 등록을 빠뜨리지 않는다.

    ⚠️ `register_vector_codec=False` 는 **확장이 아직 없는 DB에 붙는 경로 전용**이다.
    `register_vector` 는 `public.vector` 타입을 조회해 코덱을 등록하므로, 확장을 만들기 **전**에
    호출하면 `ValueError: unknown type: public.vector` 로 커넥션 자체가 실패한다.
    즉 확장을 만드는 마이그레이션(0001)과 테스트 부트스트랩은 코덱 없이 붙어야 한다.
    런타임 경로에서는 절대 끄지 않는다 — 끄면 임베딩이 문자열로 왕복해 `<=>` 가 깨진다 (03 §5.4 ③).
    """
    engine = create_async_engine(url, pool_pre_ping=True, **kwargs)

    if register_vector_codec:

        @event.listens_for(engine.sync_engine, "connect")
        def _register_vector_on_connect(dbapi_connection, connection_record) -> None:
            # SQLAlchemy 의 asyncpg 어댑터는 raw 커넥션을 받는 코루틴을 run_async 로 실행해 준다.
            dbapi_connection.run_async(register_vector)

    return engine


engine: AsyncEngine = create_engine(settings.database_url)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """요청 스코프 세션 의존성.

    ⚠️ BackgroundTasks 에는 이 세션을 넘기지 않는다 — `yield` 의존성이 백그라운드 태스크보다
    먼저 정리되므로 태스크 실행 시점에는 이미 닫혀 있다. 태스크에는 UUID 만 넘기고
    안에서 `AsyncSessionLocal()` 로 자체 세션을 연다 (`03 §2` 원칙 4).
    """
    async with AsyncSessionLocal() as session:
        yield session
