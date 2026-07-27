"""Mandatory E2E scenario coverage (delivery §16.2) that isn't already covered
elsewhere. Most §16.2 scenarios live in test_marks / test_excel / test_closeout /
test_station_sync / station tests; this adds the CAL-baseline override scenario.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import login


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _post(client, path, json_, h, expect=201):
    r = await client.post(f"/api/v1/{path}", json=json_, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


async def test_cal_baseline_overridden_by_specific_isal(client):
    h = await _admin(client)
    school = (await client.post("/api/v1/registry/schools", json={"centre_number": f"SC{uuid.uuid4().hex[:5]}", "name": "S"}, headers=h)).json()["id"]
    subject = (await client.post("/api/v1/registry/subjects", json={"code": "011", "name": "H"}, headers=h)).json()["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students", {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)

    # Chief IT transcribes the Supervisor CAL as an ALL baseline: present.
    r = await client.put(f"/api/v1/exams/{exam_id}/schools/{school}/cal-baseline",
                         json={"exam_subject_id": es, "entries": [{"student_id": "S-1", "is_present": True}]}, headers=h)
    assert r.status_code == 200

    # Roster reflects the ALL baseline -> present.
    q = f"school_id={school}&exam_subject_id={es}&paper_type=THEORY1"
    roster1 = (await client.get(f"/api/v1/exams/{exam_id}/scopes/roster?{q}", headers=h)).json()
    assert roster1["students"][0]["attendance"] is True

    # Data Enterer transcribes the specific ISAL for THEORY1: absent (a script never arrived).
    await client.put(f"/api/v1/exams/{exam_id}/attendance", json={
        "student_id": "S-1", "exam_subject_id": es, "paper_type": "THEORY1",
        "is_present": False, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"}, headers=h)

    # Specific ISAL wins over the ALL baseline -> now absent.
    roster2 = (await client.get(f"/api/v1/exams/{exam_id}/scopes/roster?{q}", headers=h)).json()
    assert roster2["students"][0]["attendance"] is False

    # An absent student with a mark is rejected.
    m = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-1", json={
        "exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 40},
        headers={**h, "Idempotency-Key": uuid.uuid4().hex})
    assert m.status_code == 422 and m.json()["detail"]["code"] == "ABSENT_STUDENT_HAS_MARKS"

    # Absent with no marks -> scope finalizes.
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize", json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 200 and f.json()["finalized"] is True
