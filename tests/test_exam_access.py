"""Navigation-support endpoints: per-exam capabilities and station credential candidates.

These endpoints exist so the frontend can build navigation from server-resolved
authority instead of guessing from a user's standing role. They are advisory for
the UI, so the tests assert the *authority resolution*, not UI behaviour.
"""

from __future__ import annotations

from tests.conftest import login


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _create_exam(client, h):
    r = await client.post(
        "/api/v1/exams",
        json={"name": "Access Exam", "exam_code": "ACC-2026", "level_id": 1},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _user_id(client, h, username):
    r = await client.get("/api/v1/registry/users?page_size=100", headers=h)
    assert r.status_code == 200, r.text
    for row in r.json()["items"]:
        if row["username"] == username:
            return row["id"]
    raise AssertionError(f"user {username} not found")


async def test_global_admin_gets_full_capabilities(client):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    r = await client.get(f"/api/v1/exams/{exam_id}/access")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["standing_role"] == "SUPER_ADMIN"
    caps = body["capabilities"]
    # A global admin may drive every stage of the lifecycle.
    assert caps["manage_exam"] is True
    assert caps["enter_data"] is True
    assert caps["manage_stations"] is True
    assert caps["oversee_attendance"] is True


async def test_capabilities_come_from_exam_assignment_not_standing_role(client):
    """A REO has no exam authority until it is assigned for THAT exam."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    owner_id = await _user_id(client, h, "examowner")

    # Before assignment: the standing role alone grants no exam management.
    await login(client, "examowner", "ownerpass123")
    before = (await client.get(f"/api/v1/exams/{exam_id}/access")).json()
    assert before["exam_roles"] == []
    assert before["capabilities"]["manage_exam"] is False
    assert before["capabilities"]["enter_data"] is False
    # Read-only lifecycle visibility is still allowed.
    assert before["capabilities"]["view_progress"] is True

    # Assign EXAM_ADMIN for this exam only.
    h = await _admin(client)
    r = await client.post(
        f"/api/v1/exams/{exam_id}/role-assignments",
        json={"user_id": owner_id, "role": "EXAM_ADMIN"},
        headers=h,
    )
    assert r.status_code == 201, r.text

    # After assignment: the same user now reports exam authority.
    await login(client, "examowner", "ownerpass123")
    after = (await client.get(f"/api/v1/exams/{exam_id}/access")).json()
    assert after["exam_roles"] == ["EXAM_ADMIN"]
    assert after["capabilities"]["manage_exam"] is True
    assert after["capabilities"]["enter_data"] is True
    assert after["capabilities"]["manage_stations"] is True


async def test_access_is_scoped_to_one_exam(client):
    """Authority on one exam must not leak into another exam."""
    h = await _admin(client)
    exam_a = await _create_exam(client, h)
    r = await client.post(
        "/api/v1/exams",
        json={"name": "Other Exam", "exam_code": "OTH-2026", "level_id": 1},
        headers=h,
    )
    assert r.status_code == 201, r.text
    exam_b = r.json()["id"]
    owner_id = await _user_id(client, h, "examowner")

    await client.post(
        f"/api/v1/exams/{exam_a}/role-assignments",
        json={"user_id": owner_id, "role": "EXAM_ADMIN"},
        headers=h,
    )

    await login(client, "examowner", "ownerpass123")
    a = (await client.get(f"/api/v1/exams/{exam_a}/access")).json()
    b = (await client.get(f"/api/v1/exams/{exam_b}/access")).json()

    assert a["capabilities"]["manage_exam"] is True
    assert b["capabilities"]["manage_exam"] is False


async def test_access_404_for_unknown_exam(client):
    await _admin(client)
    r = await client.get("/api/v1/exams/00000000-0000-0000-0000-000000000000/access")
    assert r.status_code == 404


async def test_credential_candidates_lists_station_eligible_assignments(client):
    """Station managers need the assignments they may issue a station login for."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    owner_id = await _user_id(client, h, "examowner")
    de_id = await _user_id(client, h, "reo_a")

    await client.post(
        f"/api/v1/exams/{exam_id}/role-assignments",
        json={"user_id": owner_id, "role": "EXAM_ADMIN"},
        headers=h,
    )
    await client.post(
        f"/api/v1/exams/{exam_id}/role-assignments",
        json={"user_id": de_id, "role": "DATA_ENTERER"},
        headers=h,
    )

    r = await client.get(f"/api/v1/exams/{exam_id}/stations/credential-candidates", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()

    by_role = {row["role"] for row in rows}
    assert by_role == {"EXAM_ADMIN", "DATA_ENTERER"}
    # Each row carries what the issuing UI needs to identify a person.
    for row in rows:
        assert row["assignment_id"] > 0
        assert row["name"].strip()
        assert row["username"]


async def test_credential_candidates_requires_station_manager(client):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    # A REO with no exam assignment is not a station manager.
    await login(client, "examowner", "ownerpass123")
    r = await client.get(f"/api/v1/exams/{exam_id}/stations/credential-candidates")
    assert r.status_code == 403


async def test_credential_candidate_can_receive_a_station_pin(client):
    """The candidate list feeds credential issuance without a separate lookup."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    de_id = await _user_id(client, h, "reo_a")
    await client.post(
        f"/api/v1/exams/{exam_id}/role-assignments",
        json={"user_id": de_id, "role": "DATA_ENTERER"},
        headers=h,
    )
    station = (
        await client.post(
            f"/api/v1/exams/{exam_id}/stations",
            json={"station_code": "STN-ACC-01", "name": "Access Venue"},
            headers=h,
        )
    ).json()

    candidates = (
        await client.get(f"/api/v1/exams/{exam_id}/stations/credential-candidates", headers=h)
    ).json()
    de = next(c for c in candidates if c["role"] == "DATA_ENTERER")

    issued = await client.post(
        f"/api/v1/exams/{exam_id}/stations/{station['station_id']}/credentials",
        json={"exam_role_assignment_id": de["assignment_id"], "kind": "DE", "initials": "AB"},
        headers=h,
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    # The PIN is returned exactly once for provisioning.
    assert body["kind"] == "DE"
    assert body["pin"] and len(body["pin"]) == 6
    assert body["initials"] == "AB"
