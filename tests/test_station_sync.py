from __future__ import annotations

import uuid

import pytest

from lazeims_common.reconcile import normalize_total_record, scope_digest

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
    return r.json()


async def _setup_station_exam(client, h):
    """Exam with a STATION-owned scope, open for entry; returns keys + package."""
    school = (await _reg(client, "schools", {"centre_number": "SCH-1", "name": "S1"}, h))["id"]
    subject = (await _reg(client, "subjects", {"code": "011", "name": "Hist"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_id": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": school}, h)
    es = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subject, "total_marks_theory1": 100}, h))["id"]
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S-1", "school_id": school, "first_name": "A", "surname": "B", "sex": "M", "subject_ids": [es]}, h)
    st = await _post(client, f"exams/{exam_id}/stations", {"station_code": "ST-1", "name": "Centre"}, h)
    station_key = st["sync_key"]
    # assign scope to STATION
    r = await client.put(f"/api/v1/exams/{exam_id}/writer-assignments", json={
        "school_id": school, "exam_subject_id": es, "paper_type": "THEORY1",
        "writer_mode": "STATION", "station_id": st["station_id"]}, headers=h)
    assert r.status_code == 201, r.text
    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    await _post(client, f"exams/{exam_id}/transitions", {"target_phase": "ENTRY_OPEN"}, h, expect=200)
    return {"exam_id": exam_id, "es": es, "station_key": station_key,
            "package_id": pkg["package_id"], "package_version": pkg["package_version"]}


def _nk(exam_id, student="S-1", subject="011", paper="THEORY1"):
    return {"exam_id": exam_id, "student_id": student, "subject_code": subject, "paper_type": paper}


def _att_event(ctx, eid, present=True):
    return {"event_id": eid, "entity_type": "ATTENDANCE_TRANSCRIBED", "operation": "UPSERT",
            "natural_key": _nk(ctx["exam_id"]), "value": {"is_present": present, "source": "INVIGILATOR_ISAL_TRANSCRIPTION"},
            "local_version": 1, "actor_assignment_id": "a", "occurred_at": "2026-07-27T08:00:00Z"}


def _marks_event(ctx, eid, total="67"):
    return {"event_id": eid, "entity_type": "STUDENT_PAPER_MARKS_REPLACED", "operation": "UPSERT",
            "natural_key": _nk(ctx["exam_id"]), "value": {"mode": "TOTAL_MARKS", "total": total},
            "local_version": 2, "actor_assignment_id": "a", "occurred_at": "2026-07-27T08:01:00Z"}


async def _sync(client, ctx, events):
    body = {"contract_version": "station-sync/v1", "station_code": "ST-1", "exam_id": ctx["exam_id"],
            "package_id": ctx["package_id"], "package_version": ctx["package_version"],
            "rules_version": "1.0", "events": events}
    return await client.post("/api/v1/station/sync/events", json=body,
                             headers={"X-Station-Key": ctx["station_key"]})


# ---------- accepted / duplicate / rejected ----------

async def test_sync_accepts_attendance_then_marks(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    r = await _sync(client, ctx, [_att_event(ctx, "evt_a"), _marks_event(ctx, "evt_m")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert {e["event_id"] for e in body["accepted"]} == {"evt_a", "evt_m"}
    assert body["rejected"] == []


async def test_duplicate_replay_returns_duplicate(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    await _sync(client, ctx, [_att_event(ctx, "evt_a")])
    r = await _sync(client, ctx, [_att_event(ctx, "evt_a")])  # replay
    body = r.json()
    assert body["duplicates"] == [{"event_id": "evt_a"}]
    assert body["accepted"] == []


async def test_marks_over_max_rejected(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    await _sync(client, ctx, [_att_event(ctx, "evt_a")])
    r = await _sync(client, ctx, [_marks_event(ctx, "evt_bad", total="150")])
    rej = r.json()["rejected"]
    assert rej and rej[0]["code"] == "MARK_OUT_OF_RANGE"


async def test_marks_before_attendance_rejected(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    r = await _sync(client, ctx, [_marks_event(ctx, "evt_m")])
    assert r.json()["rejected"][0]["code"] == "ATTENDANCE_REQUIRED_FIRST"


async def test_event_id_payload_conflict(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    await _sync(client, ctx, [_att_event(ctx, "evt_a")])
    r = await _sync(client, ctx, [_att_event(ctx, "evt_a", present=False)])  # same id, diff payload
    assert r.json()["rejected"][0]["code"] == "EVENT_ID_PAYLOAD_CONFLICT"


async def test_bad_station_key_401(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    ctx2 = dict(ctx); ctx2["station_key"] = "wrong-key"
    r = await _sync(client, ctx2, [_att_event(ctx, "evt_a")])
    assert r.status_code == 401


async def test_one_bad_event_does_not_abort_batch(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    r = await _sync(client, ctx, [_att_event(ctx, "evt_a"), _marks_event(ctx, "evt_bad", total="150")])
    body = r.json()
    assert {e["event_id"] for e in body["accepted"]} == {"evt_a"}  # attendance still committed
    assert body["rejected"][0]["event_id"] == "evt_bad"


# ---------- reconciliation ----------

async def test_reconcile_matched(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    await _sync(client, ctx, [_att_event(ctx, "evt_a"), _marks_event(ctx, "evt_m", total="67")])
    # station computes its local digest identically
    local = scope_digest([normalize_total_record("S-1", True, 67)])
    r = await client.post("/api/v1/station/sync/reconcile", json={
        "package_id": ctx["package_id"], "station_code": "ST-1",
        "scopes": [{"centre_number": "SCH-1", "subject_code": "011", "paper_type": "THEORY1", "local_digest": local}],
    }, headers={"X-Station-Key": ctx["station_key"]})
    assert r.status_code == 200
    assert r.json()["overall"] == "MATCHED"


async def test_reconcile_mismatched(client):
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    await _sync(client, ctx, [_att_event(ctx, "evt_a"), _marks_event(ctx, "evt_m", total="67")])
    wrong = scope_digest([normalize_total_record("S-1", True, 99)])
    r = await client.post("/api/v1/station/sync/reconcile", json={
        "package_id": ctx["package_id"], "station_code": "ST-1",
        "scopes": [{"centre_number": "SCH-1", "subject_code": "011", "paper_type": "THEORY1", "local_digest": wrong}],
    }, headers={"X-Station-Key": ctx["station_key"]})
    assert r.json()["overall"] == "MISMATCHED"


# ---------- portable round trip ----------

async def test_portable_import_round_trip(client):
    from lazeims_common.portable import DIRECTION_EVENTS, generate_key, open_envelope, seal
    h = await _admin(client); ctx = await _setup_station_exam(client, h)
    key = generate_key()
    events = [_att_event(ctx, "evt_pa"), _marks_event(ctx, "evt_pm", total="70")]
    token = seal({"package_id": ctx["package_id"], "events": events}, key=key,
                 sender="ST-1", recipient="CENTRAL", direction=DIRECTION_EVENTS, sequence=1)
    r = await client.post("/api/v1/station/sync/portable/import", json={
        "station_code": "ST-1", "key": key, "token": token,
    }, headers={"X-Station-Key": ctx["station_key"]})
    assert r.status_code == 200, r.text
    ack = open_envelope(r.json()["ack_token"], key=key, expected_recipient="ST-1")
    assert {e["event_id"] for e in ack.payload["accepted"]} == {"evt_pa", "evt_pm"}
