"""Test fixtures for the Central API.

Runs against the ``lazeims_test`` database. The schema is created from the ORM
metadata (fast, isolated); migrations themselves are smoke-tested separately.
"""

from __future__ import annotations

import os

# Point the app at the test DB BEFORE any app import triggers settings caching.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:ma0zYn9RzAZbhBOE2Bs235KSwPdTeF4D@127.0.0.1:5432/lazeims_test"
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("STATION_PACKAGE_INTEGRITY_KEY", "test-integrity-key-0123456789")
os.environ.setdefault("APP_ENV", "development")
# Never let tests hit the real ExaMetrics server. Tests that need provisioning
# must explicitly set this via monkeypatch + mock the backend_sis calls.
os.environ["BACKEND_SIS_BASE_URL"] = ""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from lazeims_common.enums import Sex

from app.db import Base, get_engine, get_sessionmaker
from app.enums import StandingRoleName
from app.main import app
from app.models.registry import Region, Role, User
from app.security import hash_secret
from app.seed import seed_roles


@pytest_asyncio.fixture(scope="function")
async def db_setup():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # Seed roles + representative users.
    async with get_sessionmaker()() as db:
        await seed_roles(db)
        # a region for scope tests
        region_a = Region(name="Region A")
        region_b = Region(name="Region B")
        db.add_all([region_a, region_b])
        await db.flush()

        super_role = (await db.execute(
            _select_role(StandingRoleName.SUPER_ADMIN))).scalar_one()
        reo_role = (await db.execute(
            _select_role(StandingRoleName.REO))).scalar_one()

        db.add(User(
            first_name="Super", surname="Admin", sex=Sex.M, username="superadmin",
            role_id=super_role.id, password_hash=hash_secret("adminpass123"),
        ))
        db.add(User(
            first_name="Regional", surname="Officer", sex=Sex.F, username="reo_a",
            role_id=reo_role.id, region_id=region_a.id,
            password_hash=hash_secret("reopass123"),
        ))
        # A plain SYSTEM-independent user to be appointed EXAM_ADMIN in tests.
        db.add(User(
            first_name="Exam", surname="Owner", sex=Sex.M, username="examowner",
            role_id=reo_role.id, region_id=region_a.id,
            password_hash=hash_secret("ownerpass123"),
        ))
        # Seed one exam level (id=1) so exam creation has a valid level_id.
        from app.models.registry import ExamLevel
        db.add(ExamLevel(name="FTNA"))
        await db.commit()
    yield
    await engine.dispose()


def _select_role(name):
    from sqlalchemy import select
    return select(Role).where(Role.name == name)


@pytest_asyncio.fixture
async def client(db_setup):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: AsyncClient, username: str, password: str) -> str:
    """Log in and return the CSRF token. Cookies persist on the client."""
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]
