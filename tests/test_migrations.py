"""Migration smoke test: the Alembic migration applies cleanly on a pristine
schema and yields the expected tables. Self-contained: it resets the public
schema first so it never collides with metadata-created tables from other tests.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/lazeims_test"


async def _reset_schema() -> None:
    engine = create_async_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def test_alembic_upgrade_head_clean():
    asyncio.run(_reset_schema())
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": TEST_DB_URL,
        "SESSION_SECRET_KEY": "test-secret-key-0123456789",
        "STATION_PACKAGE_INTEGRITY_KEY": "test-integrity-key-0123456789",
        "APP_ENV": "development",
    })
    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stderr
    assert "Running upgrade" in (up.stderr + up.stdout)
