"""마이그레이션 검증 — 빈 DB 에서 `alembic upgrade head` 가 통과하는지 (`03 §5.3`).

느리므로 `@pytest.mark.slow` 로 분리한다. CI 는 이 테스트만 따로 돌린다.
"""

import asyncio
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.config import PROJECT_ROOT, settings
from app.database import create_engine
from tests.conftest import drop_database, ensure_database_exists

# 테스트 DB 를 재사용하면 이미 확장·테이블이 있어 "빈 DB에서 통과" 를 확인할 수 없다.
SCRATCH_DATABASE = "bulchimbeon_migrations_test"
# 아래 두 테스트도 각자 빈 DB 에서 시작해야 한다 — 스키마를 내렸다 올리거나 드리프트를
# 재는 작업이라 서로의 전제를 깨기 때문에 DB 를 나눠 쓴다.
CHECK_DATABASE = "bulchimbeon_migrations_check_test"
ROUNDTRIP_DATABASE = "bulchimbeon_migrations_roundtrip_test"


def _scratch_url(database: str = SCRATCH_DATABASE) -> str:
    return (
        make_url(settings.test_database_url)
        .set(database=database)
        .render_as_string(hide_password=False)
    )


async def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """alembic 을 서브프로세스로 돌린다.

    `env.py` 가 `asyncio.run()` 을 부르므로 인프로세스로는 못 돌린다 — 이미 도는 이벤트 루프
    안에서 또 `asyncio.run()` 을 부르면 터진다.
    """
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "-x", f"database_url={url}", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return set(rows)


@pytest.mark.slow
async def test_alembic_upgrade_head_on_empty_database() -> None:
    url = _scratch_url()

    await drop_database(url)
    await ensure_database_exists(url)
    try:
        result = await _alembic(url, "upgrade", "head")
        assert result.returncode == 0, f"alembic upgrade head 실패:\n{result.stderr}"

        # 03 §5.4 ① — 최초 마이그레이션이 vector 확장을 만들어야 이후 리비전의
        # vector 컬럼 생성이 UndefinedObject 로 죽지 않는다.
        engine = create_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                extversion = await conn.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
                revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        finally:
            await engine.dispose()

        assert extversion is not None, "vector 확장이 만들어지지 않았다"
        # hnsw.iterative_scan 이 0.8.0 이상을 요구한다 (`04 §7`).
        assert tuple(int(part) for part in extversion.split(".")) >= (0, 8, 0)
        assert revision is not None
    finally:
        await drop_database(url)


@pytest.mark.slow
async def test_alembic_check_reports_no_drift_from_models() -> None:
    """마이그레이션이 만든 스키마와 모델 메타데이터가 어긋나지 않는지 (`03 §5.4`).

    모델에만 있고 리비전에는 없는 인덱스·제약은 **테스트를 초록으로 통과한다** —
    `conftest.py` 는 `Base.metadata.create_all` 로 테이블을 만들지 alembic 을 타지 않기
    때문이다. 그 어긋남은 배포 DB 에서만 드러나므로 여기서 `alembic check` 로 잡는다.
    """
    url = _scratch_url(CHECK_DATABASE)

    await drop_database(url)
    await ensure_database_exists(url)
    try:
        assert (await _alembic(url, "upgrade", "head")).returncode == 0

        result = await _alembic(url, "check")
        assert result.returncode == 0, (
            f"모델과 마이그레이션이 어긋난다:\n{result.stdout}\n{result.stderr}"
        )
        assert "No new upgrade operations detected" in result.stdout
    finally:
        await drop_database(url)


@pytest.mark.slow
async def test_head_revision_downgrades_and_upgrades_back() -> None:
    """head 리비전의 `downgrade()` 가 **실제로** 되돌리는지 (`03 §5.4`).

    `upgrade()` 만 보고 넘어가면 drop 을 빠뜨린 `downgrade()` 가 초록으로 지나간다. 그 실패는
    롤백이 필요한 순간 — 배포가 이미 깨진 뒤 — 에야 드러나므로 여기서 왕복시켜 확인한다.
    되돌린 뒤 다시 올린 스키마가 원래와 같아야 하므로 **테이블 목록을 앞뒤로 비교**한다.
    """
    url = _scratch_url(ROUNDTRIP_DATABASE)

    await drop_database(url)
    await ensure_database_exists(url)
    try:
        assert (await _alembic(url, "upgrade", "head")).returncode == 0

        engine = create_engine(url, poolclass=NullPool)
        try:
            before = await _table_names(engine)
            assert {"lessons", "briefing_runs"} <= before

            down = await _alembic(url, "downgrade", "-1")
            assert down.returncode == 0, f"alembic downgrade -1 실패:\n{down.stderr}"

            rolled_back = await _table_names(engine)
            assert "lessons" not in rolled_back
            assert "briefing_runs" not in rolled_back

            up = await _alembic(url, "upgrade", "head")
            assert up.returncode == 0, f"재-upgrade 실패:\n{up.stderr}"
            assert await _table_names(engine) == before
        finally:
            await engine.dispose()
    finally:
        await drop_database(url)
