from __future__ import annotations

import pytest

from tests.conftest import login


async def _mk(client, path, json, csrf):
    return await client.post(f"/api/v1/registry/{path}", json=json, headers={"X-CSRF-Token": csrf})


async def test_ward_unique_within_council_only(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = await _mk(client, "regions", {"name": "R1"}, csrf); region = r.json()["id"]
    c1 = (await _mk(client, "councils", {"name": "C1", "region_id": region}, csrf)).json()["id"]
    c2 = (await _mk(client, "councils", {"name": "C2", "region_id": region}, csrf)).json()["id"]
    # same ward name in two councils is allowed
    r1 = await _mk(client, "wards", {"name": "Mjini", "council_id": c1}, csrf)
    r2 = await _mk(client, "wards", {"name": "Mjini", "council_id": c2}, csrf)
    assert r1.status_code == 201 and r2.status_code == 201
    # duplicate within the same council -> conflict
    r3 = await _mk(client, "wards", {"name": "Mjini", "council_id": c1}, csrf)
    assert r3.status_code == 409


async def test_region_name_unique(client):
    csrf = await login(client, "superadmin", "adminpass123")
    assert (await _mk(client, "regions", {"name": "Dup"}, csrf)).status_code == 201
    assert (await _mk(client, "regions", {"name": "Dup"}, csrf)).status_code == 409


async def test_school_geography_consistency_enforced(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r_a = (await _mk(client, "regions", {"name": "RA"}, csrf)).json()["id"]
    r_b = (await _mk(client, "regions", {"name": "RB"}, csrf)).json()["id"]
    c_in_a = (await _mk(client, "councils", {"name": "CA", "region_id": r_a}, csrf)).json()["id"]

    # council belongs to RA but school claims region RB -> rejected
    bad = await _mk(client, "schools", {
        "centre_number": "S0001", "name": "Bad School",
        "region_id": r_b, "council_id": c_in_a,
    }, csrf)
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "CONFIGURATION_MISMATCH"

    # consistent -> ok
    good = await _mk(client, "schools", {
        "centre_number": "S0002", "name": "Good School",
        "region_id": r_a, "council_id": c_in_a,
    }, csrf)
    assert good.status_code == 201, good.text


async def test_ward_council_consistency(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = (await _mk(client, "regions", {"name": "RX"}, csrf)).json()["id"]
    c1 = (await _mk(client, "councils", {"name": "CX1", "region_id": r}, csrf)).json()["id"]
    c2 = (await _mk(client, "councils", {"name": "CX2", "region_id": r}, csrf)).json()["id"]
    w_in_c1 = (await _mk(client, "wards", {"name": "W1", "council_id": c1}, csrf)).json()["id"]
    # ward is in c1 but school says council c2 -> rejected
    bad = await _mk(client, "schools", {
        "centre_number": "S1", "name": "S", "council_id": c2, "ward_id": w_in_c1,
    }, csrf)
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "CONFIGURATION_MISMATCH"


async def test_school_all_geography_optional(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = await _mk(client, "schools", {"centre_number": "S9", "name": "Orphan"}, csrf)
    assert r.status_code == 201


async def test_create_user_and_login(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = await _mk(client, "users", {
        "first_name": "Ada", "surname": "Lovelace", "sex": "F",
        "username": "ada", "role": "SYSTEM_ADMIN", "password": "adapass123",
    }, csrf)
    assert r.status_code == 201, r.text
    # new user can log in on a fresh client
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        lr = await c2.post("/api/v1/auth/login", json={"username": "ada", "password": "adapass123"})
        assert lr.status_code == 200
        assert lr.json()["role"] == "SYSTEM_ADMIN"


async def test_password_hash_never_returned(client):
    csrf = await login(client, "superadmin", "adminpass123")
    r = await client.get("/api/v1/registry/users", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    for u in r.json():
        assert "password_hash" not in u and "password" not in u
