"""Tests for the canonical station package: Ed25519 signing, machine
credentials, supersession, Central URL, DE scopes, admin username, scope
conflict detection, and applicable-paper scoping.
"""

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


async def _build_exam_with_schools_and_subjects(client, h):
    s1 = (await _reg(client, "schools", {"centre_number": "SCH-A1", "name": "School Alpha"}, h))["id"]
    s2 = (await _reg(client, "schools", {"centre_number": "SCH-B2", "name": "School Beta"}, h))["id"]
    subj1 = (await _reg(client, "subjects", {"code": "031", "name": "Physics"}, h))["id"]
    subj2 = (await _reg(client, "subjects", {"code": "032", "name": "Chemistry"}, h))["id"]
    exam_id = (await _post(client, "exams", {
        "name": "Package Test Exam", "exam_code": f"PKG-{uuid.uuid4().hex[:6]}", "level_id": 1
    }, h))["id"]
    await _post(client, f"exams/{exam_id}/schools", {"school_id": s1}, h)
    await _post(client, f"exams/{exam_id}/schools", {"school_id": s2}, h)
    es1 = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subj1}, h))["id"]
    es2 = (await _post(client, f"exams/{exam_id}/subjects", {"subject_id": subj2}, h))["id"]
    await _post(client, f"exams/{exam_id}/students", {
        "student_id": "ST-A-01", "school_id": s1, "first_name": "Alpha",
        "surname": "Student", "sex": "M", "subject_ids": [es1]
    }, h)
    await _post(client, f"exams/{exam_id}/students", {
        "student_id": "ST-B-01", "school_id": s2, "first_name": "Beta",
        "surname": "Learner", "sex": "F", "subject_ids": [es2]
    }, h)
    return {"exam_id": exam_id, "s1": s1, "s2": s2, "es1": es1, "es2": es2}


async def _create_station(client, h, exam_id, code=None):
    code = code or f"ST-{uuid.uuid4().hex[:6]}"
    return await _post(client, f"exams/{exam_id}/stations", {"station_code": code, "name": "Centre"}, h)


# ── Ed25519 signature ────────────────────────────────────────────────────────

async def test_package_has_ed25519_signature(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()

    assert bundle["contract_version"] == "station-package/v1"
    assert bundle["signature"].startswith("ed25519:")

    from lazeims_common.signing import verify_package_signature
    manifest = bundle["manifest"]
    assert verify_package_signature(manifest, bundle["signature"])


async def test_package_signature_tamper_detection(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()

    from lazeims_common.signing import verify_package_signature
    manifest = bundle["manifest"]
    sig = bundle["signature"]
    manifest["station_code"] = "TAMPERED"
    assert not verify_package_signature(manifest, sig)


# ── Machine credential ───────────────────────────────────────────────────────

async def test_package_contains_machine_credential(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()

    mc = bundle["machine_credential"]
    assert mc["credential_id"].startswith("mc_")
    assert len(mc["secret"]) > 20
    assert mc["package_id"] == pkg["package_id"]
    assert mc["station_code"] == st["station_code"]


async def test_machine_credential_hash_stored_centrally(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()
    mc = bundle["machine_credential"]

    from app.db import get_sessionmaker
    from app.models.station import StationMachineCredential
    from app.security import verify_secret
    from sqlalchemy import select

    async with get_sessionmaker()() as db:
        stored = (await db.execute(
            select(StationMachineCredential).where(
                StationMachineCredential.credential_id == mc["credential_id"]
            )
        )).scalar_one()
        assert stored.secret_hash.startswith("$argon2id$")
        assert stored.algorithm == "argon2id"
        assert stored.status == "ACTIVE"
        assert verify_secret(stored.secret_hash, mc["secret"])


# ── Supersession ─────────────────────────────────────────────────────────────

async def test_package_supersedes_previous(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    p1 = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                     {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    p2 = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                     {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)

    b2 = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{p2['package_id']}/download",
        headers=h,
    )).json()
    manifest = b2["manifest"]
    assert manifest["supersedes_package_id"] == p1["package_id"]
    assert manifest["package_version"] == 2


# ── Central URL ──────────────────────────────────────────────────────────────

async def test_package_contains_central_url_field(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()
    assert "central_base_url" in bundle["manifest"]


# ── Scope conflict detection ─────────────────────────────────────────────────

async def test_scope_conflict_between_stations(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]

    st1 = await _create_station(client, h, exam_id, code=f"ST-A-{uuid.uuid4().hex[:4]}")
    await _post(client, f"exams/{exam_id}/stations/{st1['station_id']}/packages",
                {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)

    st2 = await _create_station(client, h, exam_id, code=f"ST-B-{uuid.uuid4().hex[:4]}")
    r = await client.post(
        f"/api/v1/exams/{exam_id}/stations/{st2['station_id']}/packages",
        json={"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]},
        headers=h,
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "SCOPE_CONFLICT"
    assert len(body["detail"]["conflicts"]) > 0


async def test_same_station_can_regenerate_no_conflict(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    p2 = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                     {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    assert p2["package_version"] == 2


# ── DE scopes and admin username ─────────────────────────────────────────────

async def test_package_includes_de_scope_entries(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    users = (await client.get("/api/v1/registry/users", headers=h)).json()["items"]
    owner_id = next(u["id"] for u in users if u["username"] == "examowner")
    ra = await _post(client, f"exams/{exam_id}/role-assignments",
                     {"user_id": owner_id, "role": "DATA_ENTERER"}, h)
    await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/credentials",
                {"exam_role_assignment_id": ra["id"], "kind": "DE", "initials": "EO"}, h)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()
    manifest = bundle["manifest"]
    assert len(manifest["data_enterers"]) >= 1
    de = manifest["data_enterers"][0]
    assert de["initials"] == "EO"
    assert de["pin_hash"].startswith("$argon2id$")


async def test_package_admin_username_is_dedicated(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()
    admin = bundle["manifest"].get("station_admin")
    assert admin is not None
    assert len(admin["username"]) >= 1
    assert admin["password_hash"].startswith("$argon2id$")


# ── Scope-only guarantee ─────────────────────────────────────────────────────

async def test_package_narrow_scope_has_no_out_of_scope_records(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()
    seed = bundle["seed"]

    centre_numbers = {s["centre_number"] for s in seed["schools"]}
    subject_codes = {s["subject_code"] for s in seed["subjects"]}
    student_ids = {s["student_id"] for s in seed["students"]}

    assert centre_numbers == {"SCH-A1"}
    assert subject_codes == {"031"}
    assert student_ids == {"ST-A-01"}
    assert "SCH-B2" not in centre_numbers
    assert "032" not in subject_codes


# ── Signing metadata ─────────────────────────────────────────────────────────

async def test_manifest_signing_metadata_is_present(client):
    h = await _admin(client)
    ctx = await _build_exam_with_schools_and_subjects(client, h)
    exam_id = ctx["exam_id"]
    st = await _create_station(client, h, exam_id)

    pkg = await _post(client, f"exams/{exam_id}/stations/{st['station_id']}/packages",
                      {"schools": ["SCH-A1"], "subjects": ["031"], "papers": ["THEORY1"]}, h)
    bundle = (await client.get(
        f"/api/v1/exams/{exam_id}/stations/{st['station_id']}/packages/{pkg['package_id']}/download",
        headers=h,
    )).json()
    manifest = bundle["manifest"]
    assert manifest["signing"]["algorithm"] == "ed25519"
    assert manifest["machine_credential"]["algorithm"] == "argon2id"
