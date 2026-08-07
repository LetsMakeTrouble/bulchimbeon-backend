"""GET /health — DB ping 포함 (`03 §5.1`)."""

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app import __version__
from app.database import get_db
from app.main import app


async def test_health_returns_ok_with_db_ping(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "version": __version__}


@pytest.fixture
def _broken_db() -> Iterator[None]:
    """DB ping 이 실패하는 상황을 만든다."""

    class _FailingSession:
        async def execute(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("database is down")

    async def _override() -> AsyncIterator[_FailingSession]:
        yield _FailingSession()

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.usefixtures("_broken_db")
async def test_health_stays_200_when_db_is_down() -> None:
    """DB 가 죽어도 **200** 이다. 상태는 본문 `db` 필드로만 알린다.

    5xx 를 주면 배포 플랫폼 헬스체크가 `alembic upgrade head` 전 첫 부팅을 죽인다.
    확장이 없는 DB 에서는 런타임 엔진이 커넥션조차 못 열어 재시작 루프가 된다.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as broken_client:
        response = await broken_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "error", "version": __version__}
