"""async 엔진/세션과 SQLAlchemy 선언 베이스.

⚠️ **`03 §5.4` 함정 3번(`register_vector`)은 SQLAlchemy 경로에서 성립하지 않는다 — M2 실측.**
`pgvector.asyncpg.register_vector` 는 **raw asyncpg 전용**이다. 이 프로젝트는 벡터를
`pgvector.sqlalchemy.Vector` 컬럼 타입으로 다루며, 그 타입의 bind processor 가 이미
리스트를 `'[...]'` 문자열로 만들어 보낸다. 여기에 asyncpg 코덱까지 등록하면 코덱이 리스트를
기대하다 **모든 벡터 바인딩이 죽는다**:

    asyncpg.exceptions.DataError: invalid input for query argument $1:
    '[1.0, 0.0, ...]' (expected list or ndarray)

INSERT 뿐 아니라 `SELECT (:a)::vector <=> (:b)::vector` 같은 단순 캐스트도 함께 죽는다.
코덱 없이 붙으면 문자열이 그대로 Postgres 의 vector 파서로 들어가 정상 동작하며,
`<=>` 가 거리라는 성질도 그대로다(직교 벡터 = 1.0, M2 실측 2026-08-08).
raw asyncpg 로 직접 쿼리하는 스크립트를 새로 만든다면 **그쪽에서는** `register_vector` 가 필요하다.
"""

from collections.abc import AsyncIterator

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


def create_engine(url: str, **kwargs: object) -> AsyncEngine:
    """async 엔진을 만든다.

    엔진을 새로 만드는 모든 경로(앱·테스트·마이그레이션)가 이 함수를 거친다.
    벡터 코덱은 등록하지 않는다 — 이유는 모듈 독스트링 참조.
    """
    # ⚠️ 풀 크기를 **명시한다.** 생략하면 SQLAlchemy 기본값 5+10=15 가 실질 동시성 상한이
    #    되고, 파이프라인이 커넥션을 15초씩 붙잡으므로 동시 질문 15건에서 신규 요청까지
    #    막힌다 (`services/pipeline/concurrency.py`).
    #
    # 단 `poolclass` 를 지정한 호출(테스트의 `NullPool`)에는 붙이지 않는다 —
    # 크기 개념이 없는 풀이라 인자를 받으면 엔진 생성 자체가 죽는다.
    if "poolclass" not in kwargs:
        kwargs.setdefault("pool_size", settings.db_pool_size)
        kwargs.setdefault("max_overflow", settings.db_max_overflow)
    return create_async_engine(url, pool_pre_ping=True, **kwargs)


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
