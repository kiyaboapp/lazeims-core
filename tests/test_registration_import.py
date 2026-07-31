"""Registration ingestion API regression coverage."""

from __future__ import annotations

from .conftest import login


async def _post(client, path: str, payload: dict, headers: dict, status: int = 201) -> dict:
    response = await client.post(f"/api/v1/{path}", json=payload, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


async def test_preview_then_bulk_registration_is_idempotent(client):
    """A reviewed register imports once and reports existing candidates on retry."""
    csrf = await login(client, "superadmin", "adminpass123")
    headers = {"X-CSRF-Token": csrf}

    school = await _post(
        client,
        "registry/schools",
        {"centre_number": "SCH-REG", "name": "Registration School"},
        headers,
    )
    subject = await _post(
        client,
        "registry/subjects",
        {"code": "011", "name": "History"},
        headers,
    )
    exam = await _post(
        client,
        "exams",
        {"name": "Registration Import Exam", "level_id": 1},
        headers,
    )
    await _post(client, f"exams/{exam['id']}/schools", {"school_id": school["id"]}, headers)
    await _post(
        client,
        f"exams/{exam['id']}/subjects",
        {"subject_id": subject["id"], "total_marks_theory1": 100},
        headers,
    )

    row = {
        "row_no": 2,
        "student_id": "CNO-001",
        "full_name": "Asha Mrema",
        "sex": "F",
        "subject_codes": ["011"],
    }
    preview = await _post(
        client,
        f"exams/{exam['id']}/registration/preview",
        {"school_id": school["id"], "rows": [row]},
        headers,
        status=200,
    )
    assert preview["ok_count"] == 1
    assert preview["error_count"] == 0
    assert preview["rows"][0]["exam_subject_ids"]

    imported = await _post(
        client,
        f"exams/{exam['id']}/students/bulk",
        {"school_id": school["id"], "rows": preview["rows"]},
        headers,
    )
    assert imported == {
        "school_id": school["id"],
        "centre_number": "SCH-REG",
        "created": 1,
        "skipped": 0,
        "failed": 0,
        "subject_registrations": 1,
        "errors": [],
    }

    retry = await _post(
        client,
        f"exams/{exam['id']}/students/bulk",
        {"school_id": school["id"], "rows": [row]},
        headers,
    )
    assert retry["created"] == 0
    assert retry["skipped"] == 1

    stats = await client.get(f"/api/v1/exams/{exam['id']}/registration/stats", headers=headers)
    assert stats.status_code == 200
    assert stats.json()["candidate_total"] == 1
