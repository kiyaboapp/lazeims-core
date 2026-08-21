from __future__ import annotations

import json
import uuid

import pytest

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


async def _ready_exam(client, h):
    """A minimal ONLINE exam with one finalized scope -> closeout-ready."""
    school = (await _reg(client, "schools", {"centre_number": f"SC{uuid.uuid4().hex[:5]}", "name": "S"}, h))
    school_id, centre = school["id"], school["centre_number"]
    subject = (await _reg(client, "subjects", {"code": "011", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school_id}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school_id, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school_id, "exam_subject_id": es, "paper_type": "THEORY1", "writer_mode": "ONLINE"}, headers=h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)
    await client.put(f"/api/v1/exams/{exam_id}/attendance", json={
        "student_id": "S-1", "exam_subject_id": es, "paper_type": "THEORY1",
        "is_present": True, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"}, headers=h)
    await client.put(f"/api/v1/exams/{exam_id}/marks/students/S-1", json={
        "exam_subject_id": es, "paper_type": "THEORY1", "mode": "TOTAL_MARKS", "total_marks_obtained": 67},
        headers={**h, **_key()})
    f = await client.post(f"/api/v1/exams/{exam_id}/scopes/finalize", json={
        "school_id": school_id, "exam_subject_id": es, "paper_type": "THEORY1"}, headers=h)
    assert f.status_code == 200 and f.json()["finalized"]
    # Transition to ENTRY_LOCKED so closeout can seal.
    await client.post(f"/api/v1/exams/{exam_id}/transitions", json={"target_phase": "ENTRY_LOCKED"}, headers=h)
    return {"exam_id": exam_id, "es": es, "school_id": school_id}


# ---------- blocked with evidence ----------

async def test_incomplete_closeout_blocked_with_evidence(client):
    h = await _admin(client)
    # exam with a registered student but nothing finalized
    school = (await _reg(client, "schools", {"centre_number": f"SC{uuid.uuid4().hex[:5]}", "name": "S"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": "011", "name": "H"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject}, h))["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)

    r = await client.get(f"/api/v1/exams/{exam_id}/collection-readiness", headers=h)
    body = r.json()
    assert body["ready"] is False
    codes = {b["code"] for b in body["blockers"]}
    assert "SCOPE_NOT_FINALIZED" in codes

    # Blockers must carry itemised, human-identifiable evidence so the UI can list
    # each failing record and link to it, not just show a count.
    scope_blocker = next(b for b in body["blockers"] if b["code"] == "SCOPE_NOT_FINALIZED")
    assert scope_blocker["message"]
    items = scope_blocker["evidence"]["items"]
    assert len(items) == scope_blocker["evidence"]["unfinalized_count"]
    assert items[0]["school_id"] == school
    assert items[0]["centre_number"]
    assert items[0]["school_name"] == "S"
    assert items[0]["subject_code"] == "011"
    assert items[0]["subject_name"] == "H"
    assert items[0]["paper"]

    # sealing is blocked
    s = await client.post(f"/api/v1/exams/{exam_id}/collection-snapshots", headers=h)
    assert s.status_code == 409
    assert s.json()["detail"]["code"] == "CLOSEOUT_BLOCKED"
    # Phase gate blocks because exam is not in ENTRY_LOCKED.
    blockers = s.json()["detail"]["blockers"]
    assert any(b["code"] in ("SCOPE_NOT_FINALIZED", "PHASE_NOT_ENTRY_LOCKED") for b in blockers)


async def test_readiness_run_is_recorded(client):
    h = await _admin(client)
    ctx = await _ready_exam(client, h)
    r = await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-readiness/runs", headers=h)
    assert r.status_code == 200 and r.json()["ready"] is True and "run_id" in r.json()


# ---------- reproducible seal ----------

async def test_seal_is_reproducible_and_idempotent(client):
    h = await _admin(client)
    ctx = await _ready_exam(client, h)
    s1 = (await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    s2 = (await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    assert s1["snapshot_id"] == s2["snapshot_id"]
    assert s1["content_hash"] == s2["content_hash"]
    # only one snapshot exists
    lst = (await client.get(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    assert len(lst) == 1


# ---------- reopen preserves old + new revision ----------

async def test_reopen_preserves_old_and_creates_new_revision(client):
    h = await _admin(client)
    ctx = await _ready_exam(client, h)
    s1 = (await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    assert s1["closeout_revision"] == 1

    # reopen -> revision 2
    rp = await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-reopen",
                           json={"reason": "correction"}, headers=h)
    assert rp.status_code == 200 and rp.json()["closeout_revision"] == 2

    # seal again at revision 2 -> a NEW snapshot; the old one is preserved
    s2 = (await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    assert s2["closeout_revision"] == 2
    assert s2["snapshot_id"] != s1["snapshot_id"]

    lst = (await client.get(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    revisions = sorted(x["closeout_revision"] for x in lst)
    assert revisions == [1, 2]  # old preserved + new


async def test_reopen_requires_reason(client):
    h = await _admin(client)
    ctx = await _ready_exam(client, h)
    await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)
    r = await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-reopen", json={"reason": ""}, headers=h)
    assert r.status_code == 422


# ---------- export contains no processing logic ----------

async def test_export_is_service_neutral(client):
    h = await _admin(client)
    ctx = await _ready_exam(client, h)
    snap = (await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    dl = await client.get(f"/api/v1/collection-snapshots/{snap['snapshot_id']}/download", headers=h)
    assert dl.status_code == 200
    content = dl.json()["content"]
    blob = json.dumps(content).lower()
    # collected data + integrity only — no processing/analysis terms
    for banned in ["rank", "division", "gpa", "analysis", "cira", "grade", "position"]:
        assert banned not in blob, f"export leaked processing term: {banned}"
    # it does carry the collected records + integrity hash
    assert content["scopes"][0]["records"][0]["student_id"] == "S-1"
    assert content["manifest"]["scope_counts"][0]["scope_hash"].startswith("sha256:")


async def test_snapshot_get_returns_manifest(client):
    h = await _admin(client)
    ctx = await _ready_exam(client, h)
    snap = (await client.post(f"/api/v1/exams/{ctx['exam_id']}/collection-snapshots", headers=h)).json()
    g = await client.get(f"/api/v1/collection-snapshots/{snap['snapshot_id']}", headers=h)
    assert g.status_code == 200
    assert g.json()["manifest"]["exam_id"] == ctx["exam_id"]
    assert g.json()["manifest"]["total_marks"] == 1
