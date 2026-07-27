"""Cross-repo integration: the REAL station produces outbox events from local
entry, the REAL Central intake service consumes them, and both sides' scope
digests match (reconcile MATCHED). Proves the two repos interoperate on the
shared contract, not just against mocks.
"""

from __future__ import annotations

import uuid

import pytest
from argon2 import PasswordHasher

from lazeims_common.enums import FillingMode, PaperType
from lazeims_common.hashing import sha256_prefixed

# station repo (installed into this venv)
from station import entry as SE
from station import sync as SS
from station.db import connect as st_connect
from station.migrations import apply_migrations as st_migrate, import_package as st_import

from app.services import station_sync
from app.db import get_sessionmaker
from tests.conftest import login

_ph = PasswordHasher()
T1 = PaperType.THEORY1


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _reg(client, path, json, h):
    r = await client.post(f"/api/v1/registry/{path}", json=json, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _post(client, path, json, h, expect=201):
    r = await client.post(f"/api/v1/{path}", json=json, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def _station_bundle(exam_code):
    seed = {
        "schools": [{"centre_number": "SCH-1", "name": "S1"}],
        "subjects": [{"subject_code": "011", "name": "H", "papers": ["THEORY1"],
                      "total_marks": {"THEORY1": 100, "THEORY2": 0, "PRACTICAL": 0}, "groups": [], "questions": []}],
        "students": [{"student_id": "S-1", "centre_number": "SCH-1", "first_name": "A", "middle_name": None, "surname": "B", "sex": "M"}],
        "registrations": [{"student_id": "S-1", "subject_code": "011"}],
        "credentials": [], "processing_api_key": None,
    }
    return {"manifest": {"contract_version": "station-package/v1", "package_id": "PKGID", "package_version": 1,
                         "rules_version": "1.0", "software_min_version": "1.0.0", "station_code": "ST-1",
                         "exam_code": exam_code, "configuration_hash": sha256_prefixed(seed),
                         "issued_at": "2026-07-27T08:00:00Z",
                         "scope": {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, "signature": "x"},
            "seed": seed}


@pytest.mark.asyncio
async def test_station_events_apply_on_central_and_reconcile(client, tmp_path):
    # --- Central setup: STATION-owned scope, open ---
    h = await _admin(client)
    school = (await _reg(client, "schools", {"centre_number": "SCH-1", "name": "S1"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": "011", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    exam_code = (await client.get(f"/api/v1/exams/{exam_id}")).json()["exam_code"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    st = await _post(client, f"exams/{exam_id}/stations", {"station_code": "ST-1", "name": "Centre"}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1",
        "writer_mode": "STATION", "station_id": st["station_id"]}, headers=h)
    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)

    # --- Station side: real local entry produces outbox events ---
    bundle = _station_bundle(exam_code)
    bundle["manifest"]["package_id"] = pkg["package_id"]
    bundle["manifest"]["configuration_hash"] = sha256_prefixed(bundle["seed"])
    sconn = st_connect(tmp_path / "s.sqlite3")
    st_migrate(sconn)
    st_import(sconn, bundle)
    SE.transcribe_attendance(sconn, student_id="S-1", subject_code="011", paper_type=T1,
                             is_present=True, source="INVIGILATOR_ISAL_TRANSCRIPTION", actor_assignment_id=1)
    SE.apply_student_paper_marks(sconn, student_id="S-1", subject_code="011", paper_type=T1,
                                 mode=FillingMode.TOTAL_MARKS, total_marks_obtained=67, actor_assignment_id=1)
    events = SS.select_pending(sconn)
    assert len(events) == 2

    # --- Central intake consumes the station's real events ---
    async with get_sessionmaker()() as db:
        result = await station_sync.process_events(
            db, exam_id=exam_id, station_id=st["station_id"], package_id=pkg["package_id"], events=events)
        await db.commit()
    assert len(result["accepted"]) == 2 and result["rejected"] == []

    # --- reconcile: station digest == central digest ---
    from station import finalize as SF
    SF.finalize(sconn, centre_number="SCH-1", subject_code="011", paper_type=T1, finalized_by=1)
    station_recs = SS.compute_reconciliation(sconn)
    assert len(station_recs) == 1
    local_digest = station_recs[0]["local_digest"]

    async with get_sessionmaker()() as db:
        central_digest, counts = await station_sync.compute_central_scope_digest(
            db, exam_id=exam_id, centre_number="SCH-1", subject_code="011", paper_type=T1)
    assert local_digest == central_digest  # MATCHED
    assert counts["present"] == 1 and counts["with_marks"] == 1
