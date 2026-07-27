from __future__ import annotations

import uuid

import pytest

from tests.conftest import login


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
    return r


def _key():
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _setup_total_exam(client, h, *, present_student=True):
    """Build a ready TOTAL_MARKS exam with one school/subject/student and open it."""
    region = (await _reg(client, "regions", {"name": f"R{uuid.uuid4().hex[:6]}"}, h))["id"]
    school = (await _reg(client, "schools", {"centre_number": f"S{uuid.uuid4().hex[:6]}", "name": "Sch"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": f"C{uuid.uuid4().hex[:4]}", "name": "Hist"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h)).json()["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects",
                      {"subject_id": subject, "total_marks_theory1": 100}, h)).json()["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-001", "school_id": school, "first_name": "A", "surname": "B",
                 "sex": "M", "subject_ids": [es]}, h)
    r = await client.put(f"/api/v1/exams/{exam_id}/writer-assignments",
                         json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1",
                               "writer_mode": "ONLINE"}, headers=h)
    assert r.status_code == 201, r.text
    rt = await client.post(f"/api/v1/exams/{exam_id}/transitions",
                           json={"target_phase": "ENTRY_OPEN"}, headers=h)
    assert rt.status_code == 200, rt.text
    return {"exam_id": exam_id, "school": school, "es": es}


async def _transcribe(client, h, exam_id, es, present, student_id="S-001"):
    return await client.put(f"/api/v1/exams/{exam_id}/attendance", json={
        "student_id": student_id, "exam_subject_id": es, "paper_type": "THEORY1",
        "is_present": present, "source": "INVIGILATOR_ISAL_TRANSCRIPTION",
    }, headers=h)


# ---------------- total-mode E2E ----------------

async def test_total_mode_e2e_finalize(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es, school = ctx["exam_id"], ctx["es"], ctx["school"]

    # attendance present
    assert (await _transcribe(client, h, exam_id, es, True)).status_code == 200
    # marks
    r = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                         json={"exam_subject_id": es, "paper_type": "THEORY1",
                               "mode": "TOTAL_MARKS", "total_marks_obtained": 67},
                         headers={**h, **_key()})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["computed_total"] == "67"
    # validation -> complete
    v = await client.get(f"/api/v1/exams/{exam_id}/scopes/validation",
                         params={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert v.json()["complete"] is True
    # finalize
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                          json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 200 and f.json()["finalized"] is True


async def test_attendance_required_before_marks(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    r = await client.put(f"/api/v1/exams/{ctx['exam_id']}/marks/students/S-001",
                         json={"exam_subject_id": ctx["es"], "paper_type": "THEORY1",
                               "mode": "TOTAL_MARKS", "total_marks_obtained": 50},
                         headers={**h, **_key()})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ATTENDANCE_REQUIRED_FIRST"


async def test_absent_student_with_marks_rejected(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es = ctx["exam_id"], ctx["es"]
    await _transcribe(client, h, exam_id, es, False)  # absent
    r = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                         json={"exam_subject_id": es, "paper_type": "THEORY1",
                               "mode": "TOTAL_MARKS", "total_marks_obtained": 10},
                         headers={**h, **_key()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ABSENT_STUDENT_HAS_MARKS"


async def test_total_over_max_rejected(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es = ctx["exam_id"], ctx["es"]
    await _transcribe(client, h, exam_id, es, True)
    r = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                         json={"exam_subject_id": es, "paper_type": "THEORY1",
                               "mode": "TOTAL_MARKS", "total_marks_obtained": 101},
                         headers={**h, **_key()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "MARK_OUT_OF_RANGE"


async def test_absent_no_marks_finalizes(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es, school = ctx["exam_id"], ctx["es"], ctx["school"]
    await _transcribe(client, h, exam_id, es, False)  # absent, no marks
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                          json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 200 and f.json()["finalized"] is True


# ---------------- idempotency ----------------

async def test_idempotency_replay_returns_original(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es = ctx["exam_id"], ctx["es"]
    await _transcribe(client, h, exam_id, es, True)
    key = _key()
    body = {"exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 55}
    r1 = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001", json=body, headers={**h, **key})
    r2 = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001", json=body, headers={**h, **key})
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert r2.json()["result"]["computed_total"] == "55"


async def test_idempotency_conflict_same_key_diff_payload(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es = ctx["exam_id"], ctx["es"]
    await _transcribe(client, h, exam_id, es, True)
    key = _key()
    await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                     json={"exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 40},
                     headers={**h, **key})
    r2 = await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                          json={"exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 41},
                          headers={**h, **key})
    assert r2.status_code == 422 and r2.json()["detail"]["code"] == "EVENT_ID_PAYLOAD_CONFLICT"


async def test_idempotency_key_required(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    r = await client.put(f"/api/v1/exams/{ctx['exam_id']}/marks/students/S-001",
                         json={"exam_subject_id": ctx["es"], "paper_type": "THEORY1",
                               "mode": "TOTAL_MARKS", "total_marks_obtained": 5}, headers=h)
    assert r.status_code == 400


# ---------------- incident block ----------------

async def test_incident_blocks_finalize_then_resolve(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es, school = ctx["exam_id"], ctx["es"], ctx["school"]
    # present but a missing script -> raise incident, no marks
    await _transcribe(client, h, exam_id, es, True)
    inc = await client.post(f"/api/v1/exams/{exam_id}/incidents", json={
        "exam_subject_id": es, "school_id": school, "paper_type": "THEORY1",
        "student_id": "S-001", "incident_type": "MISSING_SCRIPT",
    }, headers=h)
    assert inc.status_code == 201
    incident_id = inc.json()["id"]

    # finalize blocked (present student incomplete + open incident)
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                          json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 409
    blocking_codes = [b["code"] for b in f.json()["detail"]["result"]["blockers"]]
    assert "UNRESOLVED_INCIDENT" in blocking_codes

    # resolve incident + provide the mark -> finalize succeeds
    await client.patch(f"/api/v1/exams/{exam_id}/incidents/{incident_id}/status",
                       json={"status": "RESOLVED"}, headers=h)
    await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                     json={"exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 30},
                     headers={**h, **_key()})
    f2 = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                           json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f2.status_code == 200 and f2.json()["finalized"] is True


async def test_present_missing_marks_blocks_finalize(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es, school = ctx["exam_id"], ctx["es"], ctx["school"]
    await _transcribe(client, h, exam_id, es, True)  # present, no marks, no incident
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                          json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 409
    codes = [b["code"] for b in f.json()["detail"]["result"]["blockers"]]
    assert "BLANK_MARK_NOT_ALLOWED" in codes


# ---------------- reopen ----------------

async def test_reopen_after_finalize(client):
    h = await _admin(client)
    ctx = await _setup_total_exam(client, h)
    exam_id, es, school = ctx["exam_id"], ctx["es"], ctx["school"]
    await _transcribe(client, h, exam_id, es, True)
    await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-001",
                     json={"exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 70},
                     headers={**h, **_key()})
    await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                      json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    r = await client.post(f"/api/v1/exams/{exam_id}/scopes/reopen",
                          json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1",
                                "reason": "correction"}, headers=h)
    assert r.status_code == 200 and r.json()["reopened"] is True and r.json()["revision"] == 2


# ---------------- scope-write block ----------------

async def test_station_owned_scope_blocks_online_write(client):
    h = await _admin(client)
    region = (await _reg(client, "regions", {"name": f"R{uuid.uuid4().hex[:6]}"}, h))["id"]
    school = (await _reg(client, "schools", {"centre_number": f"S{uuid.uuid4().hex[:6]}", "name": "Sch"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": f"C{uuid.uuid4().hex[:4]}", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h)).json()["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h)).json()["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B",
                 "sex": "M", "subject_ids": [es]}, h)

    # create a Station row directly and assign the scope to STATION mode
    from app.db import get_sessionmaker
    from app.models.assignments import Station
    async with get_sessionmaker()() as db:
        st = Station(exam_id=exam_id, station_code=f"ST-{uuid.uuid4().hex[:6]}", name="Centre")
        db.add(st)
        await db.commit()
        station_id = st.id

    r = await client.put(f"/api/v1/exams/{exam_id}/writer-assignments",
                         json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1",
                               "writer_mode": "STATION", "station_id": station_id}, headers=h)
    assert r.status_code == 201
    await client.post(f"/api/v1/exams/{exam_id}/transitions", json={"target_phase": "ENTRY_OPEN"}, headers=h)

    # online attendance write to a STATION-owned scope -> WRITER_MODE_MISMATCH
    rr = await _transcribe(client, h, exam_id, es, True, student_id="S-1")
    assert rr.status_code == 422 and rr.json()["detail"]["code"] == "WRITER_MODE_MISMATCH"


# ---------------- item-mode E2E ----------------

async def _setup_item_exam(client, h):
    school = (await _reg(client, "schools", {"centre_number": f"S{uuid.uuid4().hex[:6]}", "name": "Sch"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": f"C{uuid.uuid4().hex[:4]}", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"IT-{uuid.uuid4().hex[:6]}",
                                             "level_id": 1, "settings": {"filling_mode": "ITEM_LEVEL"}}, h)).json()["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects",
                      {"subject_id": subject, "total_marks_theory1": 50}, h)).json()["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "IS-1", "school_id": school, "first_name": "A", "surname": "B",
                 "sex": "F", "subject_ids": [es]}, h)
    # 3 compulsory questions, 10 each -> max 30 <= 50
    await client.put(f"/api/v1/exams/{exam_id}/subjects/{es}/questions", json={
        "groups": [],
        "questions": [
            {"paper_type": "THEORY1", "question_number": "1", "max_marks": 10},
            {"paper_type": "THEORY1", "question_number": "2", "max_marks": 10},
            {"paper_type": "THEORY1", "question_number": "3", "max_marks": 10},
        ],
    }, headers=h)
    await client.post(f"/api/v1/exams/{exam_id}/configuration-versions", headers=h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments",
                     json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    rt = await client.post(f"/api/v1/exams/{exam_id}/transitions", json={"target_phase": "ENTRY_OPEN"}, headers=h)
    assert rt.status_code == 200, rt.text
    return {"exam_id": exam_id, "es": es, "school": school}


async def test_item_mode_e2e_finalize(client):
    h = await _admin(client)
    ctx = await _setup_item_exam(client, h)
    exam_id, es, school = ctx["exam_id"], ctx["es"], ctx["school"]
    await client.put(f"/api/v1/exams/{exam_id}/attendance", json={
        "student_id": "IS-1", "exam_subject_id": es, "paper_type": "THEORY1",
        "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"}, headers=h)
    r = await client.put(f"/api/v1/exams/{exam_id}/marks/students/IS-1", json={
        "exam_subject_id": es, "paper_type": "THEORY1", "mode": "ITEM_LEVEL",
        "items": [{"question_number": "1", "marks": 8}, {"question_number": "2", "marks": 6},
                  {"question_number": "3", "marks": 10}],
    }, headers={**h, **_key()})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["computed_total"] == "24"
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize",
                          json={"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 200 and f.json()["finalized"] is True


async def test_item_mode_missing_question_rejected(client):
    h = await _admin(client)
    ctx = await _setup_item_exam(client, h)
    exam_id, es = ctx["exam_id"], ctx["es"]
    await client.put(f"/api/v1/exams/{exam_id}/attendance", json={
        "student_id": "IS-1", "exam_subject_id": es, "paper_type": "THEORY1",
        "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"}, headers=h)
    r = await client.put(f"/api/v1/exams/{exam_id}/marks/students/IS-1", json={
        "exam_subject_id": es, "paper_type": "THEORY1", "mode": "ITEM_LEVEL",
        "items": [{"question_number": "1", "marks": 8}, {"question_number": "2", "marks": 6}],  # missing q3
    }, headers={**h, **_key()})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BLANK_MARK_NOT_ALLOWED"
