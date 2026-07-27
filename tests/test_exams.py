from __future__ import annotations

import pytest

from tests.conftest import login

CSRF = {"headers": {}}


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _mk(client, path, json, h):
    return await client.post(f"/api/v1/{path}", json=json, headers=h)


async def _mk_registry(client, path, json, h):
    return await client.post(f"/api/v1/registry/{path}", json=json, headers=h)


async def _create_exam(client, h, code="FTNA-2026", settings=None):
    body = {"name": "Exam", "exam_code": code, "level_id": 1}
    if settings:
        body["settings"] = settings
    r = await _mk(client, "exams", body, h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------- phase / readiness ----------

async def test_readiness_blocks_empty_exam(client):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    r = await client.get(f"/api/v1/exams/{exam_id}/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert "has_schools" in body["blocking"]


async def test_transition_to_entry_open_blocked_when_not_ready(client):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    r = await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CONFIGURATION_MISMATCH"


async def test_full_happy_path_to_entry_open(client):
    h = await _admin(client)
    # registry: region/council/school/subject
    region = (await _mk_registry(client, "regions", {"name": "R"}, h)).json()["id"]
    council = (await _mk_registry(client, "councils", {"name": "C", "region_id": region}, h)).json()["id"]
    school = (await _mk_registry(client, "schools", {"centre_number": "S1", "name": "Sch",
                                                     "region_id": region, "council_id": council}, h)).json()["id"]
    subject = (await _mk_registry(client, "subjects", {"code": "011", "name": "History"}, h)).json()["id"]

    exam_id = await _create_exam(client, h)
    await _mk(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _mk(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h)).json()["id"]
    # register a student for that subject
    r_stu = await _mk(client, f"exams/{exam_id}/students", {
        "student_id": "S1-001", "school_id": school, "first_name": "A", "surname": "B",
        "sex": "M", "subject_ids": [es],
    }, h)
    assert r_stu.status_code == 201, r_stu.text

    # readiness now blocks only on writer assignment
    body = (await client.get(f"/api/v1/exams/{exam_id}/readiness")).json()
    assert body["ready"] is False
    assert body["blocking"] == ["writer_assignments_complete"]

    # assign writer for the in-scope (school, subject, THEORY1)
    r_wa = await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE",
    }, headers=h)
    assert r_wa.status_code == 201, r_wa.text

    body2 = (await client.get(f"/api/v1/exams/{exam_id}/readiness")).json()
    assert body2["ready"] is True, body2

    # now transition succeeds
    r_t = await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h)
    assert r_t.status_code == 200
    assert r_t.json()["phase"] == "ENTRY_OPEN"


async def test_config_immutable_after_registration(client):
    h = await _admin(client)
    # build minimal ready exam then open
    region = (await _mk_registry(client, "regions", {"name": "R"}, h)).json()["id"]
    school = (await _mk_registry(client, "schools", {"centre_number": "S1", "name": "Sch"}, h)).json()["id"]
    subject = (await _mk_registry(client, "subjects", {"code": "011", "name": "H"}, h)).json()["id"]
    exam_id = await _create_exam(client, h)
    await _mk(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _mk(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h)).json()["id"]
    await _mk(client, f"exams/{exam_id}/students", {"student_id": "X1", "school_id": school,
              "first_name": "A", "surname": "B", "sex": "F", "subject_ids": [es]}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h)

    # attaching another subject now must be blocked (409)
    subject2 = (await _mk_registry(client, "subjects", {"code": "012", "name": "Geo"}, h)).json()["id"]
    r = await _mk(client, f"exams/{exam_id}/subjects", {"subject_id": subject2}, h)
    assert r.status_code == 409


async def test_reopen_requires_reason(client):
    h = await _admin(client)
    # get an exam to ENTRY_LOCKED
    school = (await _mk_registry(client, "schools", {"centre_number": "S1", "name": "S"}, h)).json()["id"]
    subject = (await _mk_registry(client, "subjects", {"code": "011", "name": "H"}, h)).json()["id"]
    exam_id = await _create_exam(client, h)
    await _mk(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _mk(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h)).json()["id"]
    await _mk(client, f"exams/{exam_id}/students", {"student_id": "X1", "school_id": school,
              "first_name": "A", "surname": "B", "sex": "F", "subject_ids": [es]}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h)
    await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_LOCKED"}, h)
    # reopen without reason -> 422
    r = await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h)
    assert r.status_code == 422
    # with reason -> ok
    r2 = await _mk(client, f"exams/{exam_id}/transitions",
                   {"target_phase": "ENTRY_OPEN", "reason": "correction window"}, h)
    assert r2.status_code == 200


async def test_invalid_transition_rejected(client):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    # REGISTRATION -> ENTRY_LOCKED not allowed
    r = await _mk(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_LOCKED"}, h)
    assert r.status_code == 422


# ---------- item config validation ----------

async def _item_exam_with_subject(client, h):
    school = (await _mk_registry(client, "schools", {"centre_number": "S1", "name": "S"}, h)).json()["id"]
    subject = (await _mk_registry(client, "subjects", {"code": "011", "name": "H"}, h)).json()["id"]
    topic = None
    exam_id = await _create_exam(client, h, code="ITEM-1", settings={"filling_mode": "ITEM_LEVEL"})
    await _mk(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _mk(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h)).json()["id"]
    return exam_id, es, subject, school


async def test_item_questions_bad_topic_weight_rejected(client):
    h = await _admin(client)
    exam_id, es, subject, school = await _item_exam_with_subject(client, h)
    # seed a topic via registry? topics have no endpoint; create through subject? skip: use weights that sum!=1
    r = await client.put(f"/api/v1/exams/{exam_id}/subjects/{es}/questions", json={
        "groups": [],
        "questions": [
            {"paper_type": "THEORY1", "question_number": "1", "max_marks": 10,
             "topics": [{"topic_id": 1, "weight": 0.5}]},  # only 0.5 -> invalid sum
        ],
    }, headers=h)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "TOPIC_WEIGHT_SUM_INVALID"


async def test_item_questions_bad_group_pick_count_rejected(client):
    h = await _admin(client)
    exam_id, es, subject, school = await _item_exam_with_subject(client, h)
    r = await client.put(f"/api/v1/exams/{exam_id}/subjects/{es}/questions", json={
        "groups": [{"paper_type": "THEORY1", "code": "B", "name": "Sec B", "pick_count": 3}],
        "questions": [
            {"paper_type": "THEORY1", "question_number": "1", "max_marks": 10, "group_code": "B"},
        ],  # only 1 member but pick 3 -> invalid
    }, headers=h)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INCOMPLETE_QUESTION_SET"


async def test_item_questions_valid_and_seal(client):
    h = await _admin(client)
    exam_id, es, subject, school = await _item_exam_with_subject(client, h)
    r = await client.put(f"/api/v1/exams/{exam_id}/subjects/{es}/questions", json={
        "groups": [{"paper_type": "THEORY1", "code": "B", "name": "Sec B", "pick_count": 1}],
        "questions": [
            {"paper_type": "THEORY1", "question_number": "1", "max_marks": 10},
            {"paper_type": "THEORY1", "question_number": "2", "max_marks": 20, "group_code": "B"},
            {"paper_type": "THEORY1", "question_number": "3", "max_marks": 20, "group_code": "B"},
        ],
    }, headers=h)
    assert r.status_code == 200, r.text
    # seal config version -> hash
    rs = await client.post(f"/api/v1/exams/{exam_id}/configuration-versions", headers=h)
    assert rs.status_code == 200
    assert rs.json()["version"] == 1
    assert rs.json()["configuration_hash"].startswith("sha256:")


# ---------- scope-write uniqueness ----------

async def test_writer_assignment_upsert_is_idempotent_per_scope(client):
    h = await _admin(client)
    school = (await _mk_registry(client, "schools", {"centre_number": "S1", "name": "S"}, h)).json()["id"]
    subject = (await _mk_registry(client, "subjects", {"code": "011", "name": "H"}, h)).json()["id"]
    exam_id = await _create_exam(client, h)
    es = (await _mk(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h)).json()["id"]
    payload = {"school_id": school, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}
    r1 = await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json=payload, headers=h)
    r2 = await client.put(f"/api/v1/exams/{exam_id}/writer-assignments",
                          json={**payload, "writer_mode": "STATION", "station_id": None}, headers=h)
    assert r1.status_code == 201
    # station mode without station_id -> 422
    assert r2.status_code == 422
    # only one row exists for the scope
    lst = (await client.get(f"/api/v1/exams/{exam_id}/writer-assignments", headers=h)).json()
    assert len([w for w in lst if w["exam_subject_id"] == es and w["paper_type"] == "THEORY1"]) == 1


# ---------- role / geography leak suite ----------

async def test_exam_scoped_endpoint_denies_unassigned_user(client):
    # examowner has no exam role -> 403 on exam-admin-only endpoint
    h_admin = await _admin(client)
    exam_id = await _create_exam(client, h_admin)

    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        r = await c2.post("/api/v1/auth/login", json={"username": "examowner", "password": "ownerpass123"})
        csrf = r.json()["csrf_token"]
        # list role assignments requires exam admin
        resp = await c2.get(f"/api/v1/exams/{exam_id}/role-assignments")
        assert resp.status_code == 403


async def test_exam_admin_assignment_grants_access(client):
    h_admin = await _admin(client)
    exam_id = await _create_exam(client, h_admin)
    # find examowner user id
    users = (await client.get("/api/v1/registry/users", headers=h_admin)).json()
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    # appoint examowner as EXAM_ADMIN
    r = await _mk(client, f"exams/{exam_id}/role-assignments",
                  {"user_id": owner_id, "role": "EXAM_ADMIN"}, h_admin)
    assert r.status_code == 201

    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        lr = await c2.post("/api/v1/auth/login", json={"username": "examowner", "password": "ownerpass123"})
        csrf = lr.json()["csrf_token"]
        # now examowner can access exam-admin endpoint
        resp = await c2.get(f"/api/v1/exams/{exam_id}/role-assignments")
        assert resp.status_code == 200


async def test_client_supplied_role_not_trusted(client):
    # A DATA_ENTERER-assigned user cannot reach exam-admin config even if they
    # try; role is resolved from assignment, not request.
    h_admin = await _admin(client)
    exam_id = await _create_exam(client, h_admin)
    users = (await client.get("/api/v1/registry/users", headers=h_admin)).json()
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    await _mk(client, f"exams/{exam_id}/role-assignments",
              {"user_id": owner_id, "role": "DATA_ENTERER"}, h_admin)

    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c2:
        lr = await c2.post("/api/v1/auth/login", json={"username": "examowner", "password": "ownerpass123"})
        csrf = lr.json()["csrf_token"]
        # attempt to attach a subject (exam-admin only) -> 403
        subject = (await _mk_registry(client, "subjects", {"code": "011", "name": "H"}, h_admin)).json()["id"]
        resp = await c2.post(f"/api/v1/exams/{exam_id}/subjects", json={"subject_id": subject},
                             headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 403
