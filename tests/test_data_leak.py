from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.conftest import login


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _post(client, path, json_, h, expect=201):
    r = await client.post(f"/api/v1/{path}", json=json_, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


# ---------- machine (station) auth cannot reach human routes ----------

async def test_station_key_cannot_access_human_routes(client):
    # A station machine credential header must NOT authenticate a human route.
    r = await client.get("/api/v1/registry/regions", headers={"X-Station-Key": "anything"})
    assert r.status_code == 401


async def test_human_session_cannot_be_used_as_station(client):
    # Logged-in human hitting the station sync route without a station key -> 401.
    await login(client, "superadmin", "adminpass123")
    r = await client.get("/api/v1/station/sync/status", params={"station_code": "X", "package_id": "Y"})
    assert r.status_code == 401


# ---------- cross-exam isolation ----------

async def test_exam_admin_of_one_exam_cannot_admin_another(client):
    h = await _admin(client)
    exam_a = (await _post(client, "exams", {"name": "A", "exam_code": f"A-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    exam_b = (await _post(client, "exams", {"name": "B", "exam_code": f"B-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    users = (await client.get("/api/v1/registry/users", headers=h)).json()
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    # examowner is EXAM_ADMIN on A only
    await _post(client, f"exams/{exam_a}/role-assignments", {"user_id": owner_id, "role": "EXAM_ADMIN"}, h)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        await c2.post("/api/v1/auth/login", json={"username": "examowner", "password": "ownerpass123"})
        # can admin A
        ra = await c2.get(f"/api/v1/exams/{exam_a}/role-assignments")
        assert ra.status_code == 200
        # cannot admin B
        rb = await c2.get(f"/api/v1/exams/{exam_b}/role-assignments")
        assert rb.status_code == 403


# ---------- snapshot download access control ----------

async def _ready_exam_with_snapshot(client, h):
    school = (await client.post("/api/v1/registry/schools", json={"centre_number": f"SC{uuid.uuid4().hex[:5]}", "name": "S"}, headers=h)).json()["id"]
    subject = (await client.post("/api/v1/registry/subjects", json={"code": "011", "name": "H"}, headers=h)).json()["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students", {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)
    await client.put(f"/api/v1/exams/{exam_id}/attendance", json={"student_id": "S-1", "exam_subject_id": es, "paper_type": "THEORY1", "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"}, headers=h)
    await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-1", json={"exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 60}, headers={**h, "Idempotency-Key": uuid.uuid4().hex})
    await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize", json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    snap = await _post(client, f"exams/{exam_id}/collection-snapshots", {}, h, expect=200) if False else (await client.post(f"/api/v1/exams/{exam_id}/collection-snapshots", headers=h)).json()
    return exam_id, snap["snapshot_id"]


async def test_snapshot_download_denied_to_non_admin(client):
    h = await _admin(client)
    _exam_id, snapshot_id = await _ready_exam_with_snapshot(client, h)
    # reo_a (no exam role, not global admin) cannot download the snapshot
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        await c2.post("/api/v1/auth/login", json={"username": "reo_a", "password": "reopass123"})
        r = await c2.get(f"/api/v1/collection-snapshots/{snapshot_id}/download")
        assert r.status_code == 403


async def test_snapshot_download_allowed_to_global_admin(client):
    h = await _admin(client)
    _exam_id, snapshot_id = await _ready_exam_with_snapshot(client, h)
    r = await client.get(f"/api/v1/collection-snapshots/{snapshot_id}/download", headers=h)
    assert r.status_code == 200
    assert "scopes" in r.json()["content"]
