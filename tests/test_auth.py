from __future__ import annotations

import pytest

from tests.conftest import login


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_readiness_db_ok(client):
    r = await client.get("/readiness")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


async def test_login_success_returns_csrf(client):
    r = await client.post("/api/v1/auth/login", json={"username": "superadmin", "password": "adminpass123"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "SUPER_ADMIN"
    assert body["csrf_token"]


async def test_login_bad_password_401(client):
    r = await client.post("/api/v1/auth/login", json={"username": "superadmin", "password": "wrong"})
    assert r.status_code == 401


async def test_login_unknown_user_401(client):
    r = await client.post("/api/v1/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


async def test_me_requires_auth_401(client):
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


async def test_me_after_login(client):
    await login(client, "superadmin", "adminpass123")
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "superadmin"


async def test_protected_registry_write_requires_auth_401(client):
    r = await client.post("/api/v1/registry/regions", json={"name": "X"})
    assert r.status_code == 401


async def test_non_admin_cannot_create_region_403(client):
    csrf = await login(client, "reo_a", "reopass123")
    r = await client.post(
        "/api/v1/registry/regions",
        json={"name": "New Region"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403


async def test_admin_can_create_region(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = await client.post(
        "/api/v1/registry/regions",
        json={"name": "New Region"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "New Region"


async def test_csrf_required_on_mutation(client):
    await login(client, "superadmin", "adminpass123")
    # No X-CSRF-Token header -> 403
    r = await client.post("/api/v1/registry/regions", json={"name": "NoCSRF"})
    assert r.status_code == 403


async def test_logout_revokes_session(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    # session cookie cleared / revoked -> me is 401
    r2 = await client.get("/api/v1/auth/me")
    assert r2.status_code == 401


async def test_reads_require_authentication(client):
    # listing regions requires a logged-in user
    r = await client.get("/api/v1/registry/regions")
    assert r.status_code == 401
    await login(client, "reo_a", "reopass123")
    r2 = await client.get("/api/v1/registry/regions")
    assert r2.status_code == 200
