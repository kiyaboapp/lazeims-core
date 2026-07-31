"""Scripted station field rehearsal (delivery §16.3, headless-friendly). Proves
the full offline path over the REAL HTTP contract in one flow:

  1. Central: register station, generate a scope-only package, open entry.
  2. Station: fresh SQLite, import the downloaded package, transcribe attendance,
     enter marks, finalize locally (offline).
  3. Station -> Central: POST the outbox events to /station/sync/events (HTTP).
  4. Reconcile: station digest == Central digest via /station/sync/reconcile -> MATCHED.

Hardware steps (launcher double-click, multi-browser LAN) are covered by the
launcher smoke + station entry/restart tests; this ties it together end-to-end.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from lazeims_common.enums import FillingMode, PaperType

from station import entry as SE
from station import finalize as SF
from station import sync as SS
from station.db import connect as st_connect
from station.migrations import apply_migrations as st_migrate, import_package as st_import

T1 = PaperType.THEORY1


@pytest.mark.asyncio
async def test_station_field_rehearsal(client, tmp_path):
    # `client` fixture provides a seeded test DB + ASGI transport.
    h = {"X-CSRF-Token": (await client.post("/api/v1/auth/login", json={"username": "superadmin", "password": "adminpass123"})).json()["csrf_token"]}

    async def post(path, body, expect=201):
        r = await client.post(f"/api/v1{path}", json=body, headers=h)
        assert r.status_code == expect, r.text
        return r.json()

    # 1. Central setup: STATION-owned scope, open entry, generate package
    school = (await client.post("/api/v1/registry/schools", json={"centre_number": "SCH-FR", "name": "Rehearsal Centre"}, headers=h)).json()["id"]
    subject = (await client.post("/api/v1/registry/subjects", json={"code": "011", "name": "History"}, headers=h)).json()["id"]
    exam_id = (await post("/exams", {"name": "Field Rehearsal", "exam_id": f"FR-{uuid.uuid4().hex[:6]}", "level_id": 1}))["id"]
    await post(f"/exams/{exam_id}/schools", {"school_id": school})
    es = (await post(f"/exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}))["id"]
    await post(f"/exams/{exam_id}/students", {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]})
    st = await post(f"/exams/{exam_id}/stations", {"station_code": "ST-FR-1", "name": "Rehearsal Centre"})
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "STATION", "station_id": st["station_id"]}, headers=h)
    pkg = await post(f"/exams/{exam_id}/stations/{st['station_id']}/packages", {"schools": ["SCH-FR"], "subjects": ["011"], "papers": ["THEORY1"]})
    await post(f"/exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, expect=200)
    bundle = (await client.get(f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download", headers=h)).json()
    sync_key = st["sync_key"]

    # 2. Station: fresh offline DB, import, enter data, finalize (all offline)
    conn = st_connect(tmp_path / "station.sqlite3")
    st_migrate(conn)
    imp = st_import(conn, bundle)
    assert imp["station_code"] == "ST-FR-1" and imp["students"] == 1
    SE.transcribe_attendance(conn, student_id="S-1", subject_code="011", paper_type=T1, is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=1)
    SE.apply_student_paper_marks(conn, student_id="S-1", subject_code="011", paper_type=T1, mode=FillingMode.TOTAL_MARKS, total_marks_obtained=72, actor_assignment_id=1)
    ok, _ = SF.finalize(conn, centre_number="SCH-FR", subject_code="011", paper_type=T1, finalized_by=1)
    assert ok
    events = SS.select_pending(conn)
    assert len(events) == 3  # attendance, marks, finalize

    # 3. Station -> Central sync over HTTP
    body = {"contract_version": "station-sync/v1", "station_code": "ST-FR-1", "exam_id": exam_id,
            "package_id": pkg["package_id"], "package_version": pkg["package_version"], "rules_version": "1.0", "events": events}
    resp = await client.post("/api/v1/station/sync/events", json=body, headers={"X-Station-Key": sync_key})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["accepted"]) == 3 and resp.json()["rejected"] == []

    # 4. Reconcile -> MATCHED
    recs = SS.compute_reconciliation(conn)
    assert len(recs) == 1
    rr = await client.post("/api/v1/station/sync/reconcile", json={
        "package_id": pkg["package_id"], "station_code": "ST-FR-1",
        "scopes": [{"centre_number": recs[0]["centre_number"], "subject_code": recs[0]["subject_code"],
                    "paper_type": recs[0]["paper_type"], "local_digest": recs[0]["local_digest"]}],
    }, headers={"X-Station-Key": sync_key})
    assert rr.status_code == 200 and rr.json()["overall"] == "MATCHED", rr.text
    conn.close()
