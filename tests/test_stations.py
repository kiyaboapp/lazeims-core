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
    return r.json()


async def _build_two_school_two_subject_exam(client, h):
    """Two schools, two subjects, students registered at each school for both."""
    s1 = (await _reg(client, "schools", {"centre_number": "SCH-1", "name": "School One"}, h))["id"]
    s2 = (await _reg(client, "schools", {"centre_number": "SCH-2", "name": "School Two"}, h))["id"]
    subj1 = (await _reg(client, "subjects", {"code": "011", "name": "History"}, h))["id"]
    subj2 = (await _reg(client, "subjects", {"code": "012", "name": "Geography"}, h))["id"]
    exam_id = (await _post(client, "exams", {"name": "E", "exam_code": f"EX-{uuid.uuid4().hex[:6]}", "level_id": 1}, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": s1}, h)
    await _post(client, f"exams/{exam_id}/schools", {"school_id": s2}, h)
    es1 = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subj1}, h))["id"]
    es2 = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subj2}, h))["id"]
    # students: school1 -> subj1; school2 -> subj2 (clear separation)
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S1-IN", "school_id": s1, "first_name": "In", "surname": "Scope",
                 "sex": "M", "subject_ids": [es1]}, h)
    await _post(client, f"exams/{exam_id}/students",
                {"student_id": "S2-OUT", "school_id": s2, "first_name": "Out", "surname": "Scope",
                 "sex": "F", "subject_ids": [es2]}, h)
    return {"exam_id": exam_id, "s1": s1, "s2": s2, "es1": es1, "es2": es2}


async def _create_station(client, h, exam_id, code=None):
    code = code or f"ST-{uuid.uuid4().hex[:6]}"
    r = await _post(client, f"exams/{exam_id}/stations", {"station_code": code, "name": "Centre"}, h)
    return r  # StationKeyOut


# ---------- GATE: scope-only package ----------

async def test_package_contains_zero_out_of_scope_records(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    station_id = st["station_id"]

    # narrow scope: only SCH-1 + subject 011 + THEORY1
    pkg = await _post(client, f"exams/{exam_id}/stations/{station_id}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    package_id = pkg["package_id"]

    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{station_id}/packages/{package_id}/download", headers=h
    )).json()
    seed = bundle["seed"]

    centre_numbers = {s["centre_number"] for s in seed["schools"]}
    subject_codes = {s["subject_code"] for s in seed["subjects"]}
    student_ids = {s["student_id"] for s in seed["students"]}
    reg_subjects = {r["subject_code"] for r in seed["registrations"]}

    # ONLY in-scope data present
    assert centre_numbers == {"SCH-1"}
    assert subject_codes == {"011"}
    assert student_ids == {"S1-IN"}                 # S2-OUT excluded
    assert reg_subjects == {"011"}                  # 012 excluded
    assert "SCH-2" not in centre_numbers
    assert "012" not in subject_codes
    assert "S2-OUT" not in student_ids


async def test_package_never_contains_processing_key(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download", headers=h
    )).json()
    assert bundle["seed"]["processing_api_key"] is None
    # no key-like field anywhere in the manifest
    import json as _json
    assert "PROCESSING_API_KEY" not in _json.dumps(bundle)


async def test_manifest_is_signed_and_verifiable(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download", headers=h
    )).json()
    manifest = bundle["manifest"]
    signature = manifest.pop("signature")
    from app.services.station_package import verify_manifest_signature
    assert verify_manifest_signature(manifest, signature)
    # tampering breaks verification
    manifest["station_code"] = "TAMPERED"
    assert not verify_manifest_signature(manifest, signature)


# ---------- machine key + credentials shown once ----------

async def test_station_sync_key_shown_once_only_hash_stored(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    st = await _create_station(client, h, ctx["exam_id"])
    assert st["sync_key"] and len(st["sync_key"]) > 20
    # the plaintext key is not retrievable via list
    rows = (await client.get(f"/api/v1/exams/{ctx['exam_id']}/stations", headers=h)).json()
    assert all("sync_key" not in r for r in rows)


async def test_de_credential_shown_once(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    # appoint a DE and issue a station credential
    users = (await client.get("/api/v1/registry/users", headers=h)).json()["items"]
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    ra = await _post(client, f"exams/{exam_id}/role-assignments",
                     {"user_id": owner_id, "role": "DATA_ENTERER"}, h)
    cred = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/credentials",
                       {"exam_role_assignment_id": ra["id"], "kind": "DE", "initials": "JK"}, h)
    assert cred["pin"] and len(cred["pin"]) == 6
    assert cred["initials"] == "JK"


async def test_de_credential_requires_initials(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    users = (await client.get("/api/v1/registry/users", headers=h)).json()["items"]
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    ra = await _post(client, f"exams/{exam_id}/role-assignments",
                     {"user_id": owner_id, "role": "DATA_ENTERER"}, h)
    r = await client.post(f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/credentials",
                          json={"exam_role_assignment_id": ra["id"], "kind": "DE"}, headers=h)
    assert r.status_code == 422


# ---------- revoke ----------

async def test_revoked_package_cannot_download(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    rv = await client.post(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/revoke", headers=h)
    assert rv.status_code == 200
    dl = await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download", headers=h)
    assert dl.status_code == 410


async def test_package_versions_increment(client):
    h = await _admin(client)
    ctx = await _build_two_school_two_subject_exam(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)
    p1 = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                     {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    p2 = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                     {"schools": ["SCH-1"], "subjects": ["011"], "papers": ["THEORY1"]}, h)
    assert p1["package_version"] == 1 and p2["package_version"] == 2
