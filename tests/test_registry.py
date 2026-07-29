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
    r = await client.get("/api/v1/registry/users")
    assert r.status_code == 200
    for u in r.json()["items"]:
        assert "password_hash" not in u and "password" not in u


# ---- Pagination & search tests ----


async def test_list_regions_paginated(client):
    csrf = await login(client, "superadmin", "adminpass123")
    # Create several regions
    for name in ("Arusha", "Mwanza", "Dodoma", "Dar es Salaam"):
        await _mk(client, "regions", {"name": name}, csrf)

    # Default page
    r = await client.get("/api/v1/registry/regions")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data and data["page"] == 1
    assert "page_size" in data
    # The fixture seeds 2 regions + we added 4 = 6 total (at minimum)
    assert data["total"] >= 4

    # Search
    r2 = await client.get("/api/v1/registry/regions?search=Dar")
    assert r2.status_code == 200
    assert all("Dar" in item["name"] for item in r2.json()["items"])

    # Pagination
    r3 = await client.get("/api/v1/registry/regions?page=1&page_size=2")
    assert r3.status_code == 200
    assert len(r3.json()["items"]) <= 2


async def test_list_councils_filtered_by_region(client):
    csrf = await login(client, "superadmin", "adminpass123")
    reg = (await _mk(client, "regions", {"name": "FilterReg"}, csrf)).json()["id"]
    await _mk(client, "councils", {"name": "FilterCouncil1", "region_id": reg}, csrf)
    await _mk(client, "councils", {"name": "FilterCouncil2", "region_id": reg}, csrf)

    r = await client.get(f"/api/v1/registry/councils?region_id={reg}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all(c["region_id"] == reg for c in items)


async def test_list_schools_filtered_and_searched(client):
    csrf = await login(client, "superadmin", "adminpass123")
    reg = (await _mk(client, "regions", {"name": "SchReg"}, csrf)).json()["id"]
    council = (await _mk(client, "councils", {"name": "SchCouncil", "region_id": reg}, csrf)).json()["id"]
    await _mk(client, "schools", {
        "centre_number": "SC001", "name": "Alpha School",
        "region_id": reg, "council_id": council,
    }, csrf)
    await _mk(client, "schools", {
        "centre_number": "SC002", "name": "Beta School",
        "region_id": reg, "council_id": council,
    }, csrf)

    # Filter by region
    r = await client.get(f"/api/v1/registry/schools?region_id={reg}")
    assert r.status_code == 200
    assert r.json()["total"] >= 2

    # Search by centre_number
    r2 = await client.get("/api/v1/registry/schools?search=SC001")
    assert r2.status_code == 200
    assert any(s["centre_number"] == "SC001" for s in r2.json()["items"])

    # Search by name
    r3 = await client.get("/api/v1/registry/schools?search=Alpha")
    assert r3.status_code == 200
    assert any("Alpha" in s["name"] for s in r3.json()["items"])


# ---- Detail endpoint tests ----


async def test_region_detail(client):
    csrf = await login(client, "superadmin", "adminpass123")
    region = (await _mk(client, "regions", {"name": "Detail Region"}, csrf)).json()
    council = (await _mk(client, "councils", {"name": "Detail Council", "region_id": region["id"]}, csrf)).json()
    ward = (await _mk(client, "wards", {"name": "Detail Ward", "council_id": council["id"]}, csrf)).json()
    await _mk(client, "schools", {
        "centre_number": "DT001", "name": "Detail School",
        "region_id": region["id"], "council_id": council["id"], "ward_id": ward["id"],
    }, csrf)

    r = await client.get(f"/api/v1/registry/regions/{region['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Detail Region"
    assert data["council_count"] == 1
    assert data["ward_count"] == 1
    assert data["school_count"] == 1
    assert data["student_count"] == 0  # no exam students
    assert data["user_count"] >= 0


async def test_council_detail(client):
    csrf = await login(client, "superadmin", "adminpass123")
    region = (await _mk(client, "regions", {"name": "CDet Region"}, csrf)).json()
    council = (await _mk(client, "councils", {"name": "CDet Council", "region_id": region["id"]}, csrf)).json()
    ward = (await _mk(client, "wards", {"name": "CDet Ward", "council_id": council["id"]}, csrf)).json()
    await _mk(client, "schools", {
        "centre_number": "CD001", "name": "CDet School",
        "region_id": region["id"], "council_id": council["id"], "ward_id": ward["id"],
    }, csrf)

    r = await client.get(f"/api/v1/registry/councils/{council['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "CDet Council"
    assert data["region_name"] == "CDet Region"
    assert data["ward_count"] == 1
    assert data["school_count"] == 1


async def test_ward_detail(client):
    csrf = await login(client, "superadmin", "adminpass123")
    region = (await _mk(client, "regions", {"name": "WDet Region"}, csrf)).json()
    council = (await _mk(client, "councils", {"name": "WDet Council", "region_id": region["id"]}, csrf)).json()
    ward = (await _mk(client, "wards", {"name": "WDet Ward", "council_id": council["id"]}, csrf)).json()
    await _mk(client, "schools", {
        "centre_number": "WD001", "name": "WDet School",
        "council_id": council["id"], "ward_id": ward["id"],
    }, csrf)

    r = await client.get(f"/api/v1/registry/wards/{ward['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "WDet Ward"
    assert data["council_name"] == "WDet Council"
    assert data["region_name"] == "WDet Region"
    assert data["school_count"] == 1


async def test_school_detail(client):
    csrf = await login(client, "superadmin", "adminpass123")
    region = (await _mk(client, "regions", {"name": "SDet Region"}, csrf)).json()
    council = (await _mk(client, "councils", {"name": "SDet Council", "region_id": region["id"]}, csrf)).json()
    ward = (await _mk(client, "wards", {"name": "SDet Ward", "council_id": council["id"]}, csrf)).json()
    school = (await _mk(client, "schools", {
        "centre_number": "SD001", "name": "SDet School", "school_type": "GOVERNMENT",
        "region_id": region["id"], "council_id": council["id"], "ward_id": ward["id"],
        "can_download_template": True,
    }, csrf)).json()

    r = await client.get(f"/api/v1/registry/schools/{school['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "SDet School"
    assert data["centre_number"] == "SD001"
    assert data["school_type"] == "GOVERNMENT"
    assert data["region_name"] == "SDet Region"
    assert data["council_name"] == "SDet Council"
    assert data["ward_name"] == "SDet Ward"
    assert data["can_download_template"] is True
    assert data["student_count"] == 0
    assert data["exam_count"] == 0
    assert data["user_count"] == 0


async def test_detail_endpoints_return_404(client):
    await login(client, "superadmin", "adminpass123")
    for resource in ("regions", "councils", "wards", "schools"):
        response = await client.get(f"/api/v1/registry/{resource}/999999")
        assert response.status_code == 404
