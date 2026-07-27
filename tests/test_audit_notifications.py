from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db import get_sessionmaker
from app.models.cross_cutting import AuditLog
from tests.conftest import login


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _reg(client, path, json_, h):
    r = await client.post(f"/api/v1/registry/{path}", json=json_, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _post(client, path, json_, h, expect=201):
    r = await client.post(f"/api/v1/{path}", json=json_, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def _key():
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _audit_count(exam_id, action):
    async with get_sessionmaker()() as db:
        return await db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.exam_id == exam_id, AuditLog.action == action))


async def _full_flow(client, h):
    school = (await _reg(client, "schools", {"centre_number": f"SC{uuid.uuid4().hex[:5]}", "name": "S"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": "011", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    # role assignment (examowner)
    users = (await client.get("/api/v1/registry/users", headers=h)).json()
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    await _post(client, f"exams/{exam_id}/role-assignments", {"user_id": owner_id, "role": "DATA_ENTERER"}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)
    await client.put(f"/api/v1/exams/{exam_id}/attendance", json={
        "student_id": "S-1", "exam_subject_id": es, "paper_type": "THEORY1", "is_present": True,
        "source": "INVIGILATOR_ISAL_TRANSCRIPTION"}, headers=h)
    await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-1", json={
        "exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 67},
        headers={**h, **_key()})
    await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    await client.post(f"/api/v1/exams/{exam_id}/collection-snapshots", headers=h)
    return {"exam_id": exam_id, "owner_id": owner_id}


# ---------- audit coverage for key mutations ----------

@pytest.mark.parametrize("action", ["ROLE_ASSIGNED", "MARKS_SAVED", "SCOPE_FINALIZED", "COLLECTION_SEALED"])
async def test_key_mutations_emit_audit(client, action):
    h = await _admin(client)
    ctx = await _full_flow(client, h)
    assert await _audit_count(ctx["exam_id"], action) >= 1


async def test_incident_emits_audit_and_notification(client):
    h = await _admin(client)
    ctx = await _full_flow(client, h)
    exam_id = ctx["exam_id"]
    # raise an incident
    subj = (await client.get(f"/api/v1/exams/{exam_id}/subjects", headers=h)).json()[0]
    schools = (await client.get(f"/api/v1/exams/{exam_id}/schools", headers=h)).json()
    await client.post(f"/api/v1/exams/{exam_id}/incidents", json={
        "exam_subject_id": subj["id"], "school_id": schools[0]["school_id"], "paper_type": "THEORY1",
        "student_id": "S-1", "incident_type": "MISSING_SCRIPT"}, headers=h)
    assert await _audit_count(exam_id, "INCIDENT_RAISED") >= 1
    # superadmin is SUPER_ADMIN -> in the incident audience -> sees the broadcast
    notifs = (await client.get("/api/v1/notifications", headers=h)).json()
    assert any(n["type"] == "INCIDENT_RAISED" for n in notifs)


async def test_assignment_notifies_the_assigned_user(client):
    h = await _admin(client)
    ctx = await _full_flow(client, h)
    # examowner (assigned DATA_ENTERER) should have an ASSIGNMENT_RECEIVED notification
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        await c2.post("/api/v1/auth/login", json={"username": "examowner", "password": "ownerpass123"})
        notifs = (await c2.get("/api/v1/notifications")).json()
        assert any(n["type"] == "ASSIGNMENT_RECEIVED" for n in notifs)


async def test_mark_notification_read(client):
    h = await _admin(client)
    ctx = await _full_flow(client, h)
    exam_id = ctx["exam_id"]
    subj = (await client.get(f"/api/v1/exams/{exam_id}/subjects", headers=h)).json()[0]
    schools = (await client.get(f"/api/v1/exams/{exam_id}/schools", headers=h)).json()
    await client.post(f"/api/v1/exams/{exam_id}/incidents", json={
        "exam_subject_id": subj["id"], "school_id": schools[0]["school_id"], "paper_type": "THEORY1",
        "incident_type": "OTHER"}, headers=h)
    notifs = (await client.get("/api/v1/notifications", headers=h)).json()
    nid = notifs[0]["id"]
    r = await client.post(f"/api/v1/notifications/{nid}/read", headers=h)
    assert r.status_code == 200 and r.json()["read"] is True


# ---------- append-only ----------

async def test_audit_log_is_append_only_no_write_routes(client):
    from app.main import app
    paths = app.openapi()["paths"]
    audit_paths = {p: list(m.keys()) for p, m in paths.items() if "audit-log" in p}
    # only GET exists; never PUT/POST/PATCH/DELETE on audit-logs
    for p, methods in audit_paths.items():
        assert set(m.lower() for m in methods) <= {"get"}, f"{p} has write methods {methods}"


async def test_audit_log_admin_only(client):
    # a non-admin (reo_a) cannot read the audit log
    csrf = await login(client, "reo_a", "reopass123")
    r = await client.get("/api/v1/audit-logs", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403


async def test_audit_log_visible_to_admin(client):
    h = await _admin(client)
    ctx = await _full_flow(client, h)
    r = await client.get("/api/v1/audit-logs", params={"exam_id": ctx["exam_id"]}, headers=h)
    assert r.status_code == 200
    actions = {a["action"] for a in r.json()}
    assert {"ROLE_ASSIGNED", "MARKS_SAVED", "SCOPE_FINALIZED", "COLLECTION_SEALED"} <= actions


# ---------- audience filtering ----------

async def test_broadcast_not_leaked_to_wrong_role(client):
    # reo_a (REO) is not in the incident audience -> should not see INCIDENT_RAISED broadcast
    h = await _admin(client)
    ctx = await _full_flow(client, h)
    exam_id = ctx["exam_id"]
    subj = (await client.get(f"/api/v1/exams/{exam_id}/subjects", headers=h)).json()[0]
    schools = (await client.get(f"/api/v1/exams/{exam_id}/schools", headers=h)).json()
    await client.post(f"/api/v1/exams/{exam_id}/incidents", json={
        "exam_subject_id": subj["id"], "school_id": schools[0]["school_id"], "paper_type": "THEORY1",
        "incident_type": "OTHER"}, headers=h)
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        await c2.post("/api/v1/auth/login", json={"username": "reo_a", "password": "reopass123"})
        notifs = (await c2.get("/api/v1/notifications")).json()
        assert not any(n["type"] == "INCIDENT_RAISED" for n in notifs)


async def test_sync_rejected_emits_notification_and_audit(client):
    # reuse the station-sync setup to produce a rejected event
    h = await _admin(client)
    school = (await _reg(client, "schools", {"centre_number": "SCH-1", "name": "S1"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": "011", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    exam_code = (await client.get(f"/api/v1/exams/{exam_id}")).json()["exam_code"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    st = await _post(client, f"exams/{exam_id}/stations", {"station_code": "ST-1", "name": "C"}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "STATION",
        "station_id": st["station_id"]}, headers=h)
    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)
    # marks before attendance -> rejected
    ev = {"event_id": "evt_r", "entity_type": "STUDENT_PAPER_MARKS_REPLACED", "operation": "UPSERT",
          "natural_key": {"exam_code": exam_code, "student_id": "S-1", "subject_code": "011", "paper_type": "THEORY1"},
          "value": {"mode": "TOTAL_MARKS", "total": "50"}, "local_version": 1,
          "actor_assignment_id": "a", "occurred_at": "2026-07-27T08:00:00Z"}
    body = {"contract_version": "station-sync/v1", "station_code": "ST-1", "exam_code": exam_code,
            "package_id": pkg["package_id"], "package_version": pkg["package_version"], "rules_version": "1.0",
            "events": [ev]}
    r = await client.post("/api/v1/station/sync/events", json=body, headers={"X-Station-Key": st["sync_key"]})
    assert r.json()["rejected"][0]["code"] == "ATTENDANCE_REQUIRED_FIRST"
    assert await _audit_count(exam_id, "SYNC_REJECTED") >= 1
    # exam admin sees the REJECTED_EVENTS notification
    notifs = (await client.get("/api/v1/notifications", headers=h)).json()
    assert any(n["type"] == "REJECTED_EVENTS" for n in notifs)
