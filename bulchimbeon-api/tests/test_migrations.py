"""마이그레이션 검증 — 빈 DB 에서 `alembic upgrade head` 가 통과하는지 (`03 §5.3`).

느리므로 `@pytest.mark.slow` 로 분리한다. CI 는 이 테스트만 따로 돌린다.
"""

import asyncio
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.config import PROJECT_ROOT, settings
from app.database import create_engine
from tests.conftest import drop_database, ensure_database_exists

# 테스트 DB 를 재사용하면 이미 확장·테이블이 있어 "빈 DB에서 통과" 를 확인할 수 없다.
SCRATCH_DATABASE = "bulchimbeon_migrations_test"


def _scratch_url() -> str:
    return (
        make_url(settings.test_database_url)
        .set(database=SCRATCH_DATABASE)
        .render_as_string(hide_password=False)
    )


@pytest.mark.slow
async def test_alembic_upgrade_head_on_empty_database() -> None:
    url = _scratch_url()

    await drop_database(url)
    await ensure_database_exists(url)
    try:
        # env.py 가 asyncio.run() 을 부르므로 인프로세스로 못 돌린다 — 서브프로세스로 띄운다.
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "-x", f"database_url={url}", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
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
