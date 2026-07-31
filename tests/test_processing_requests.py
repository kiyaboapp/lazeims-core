"""Tests for processing request endpoints (C6) and the phase guard (C7).

Verifies:
- POST /exams/{id}/processing/requests creates a request when a snapshot is sealed.
- GET /exams/{id}/processing/requests lists requests.
- POST /exams/{id}/processing/requests/{rid}/refresh updates state from remote.
- The phase guard (C7) blocks PROCESSING without an APPROVED request.
- The phase guard allows PROCESSING with a matching APPROVED request.
- Repeat creates are idempotent on (exam, revision, hash).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.collection import CollectionSnapshot
from app.models.processing import ExamProcessingLink, ExamProcessingRequest
from tests.conftest import login

import os


@pytest.fixture(autouse=True)
def _enable_processing(monkeypatch):
    """Enable processing for all tests in this module."""
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics:8000")
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def exam_with_snapshot(client, db_setup):
    """Create an exam, move it to ENTRY_LOCKED, seal a snapshot, and configure a link."""
    csrf = await login(client, "superadmin", "adminpass123")

    # Create exam.
    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Test Exam PR", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    exam_id = resp.json()["id"]

    # Manually configure the database state needed.
    from app.db import get_sessionmaker
    from app.models.exam import Exam
    from lazeims_common.enums import ExamPhase

    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        exam.phase = ExamPhase.ENTRY_LOCKED
        exam.closeout_revision = 1

        # Create a processing link.
        link = ExamProcessingLink(
            exam_id=exam.id,
            backend_exam_id="exametrics-exam-1",
            api_key="exmk_test_secret_key_value",
            key_source="PROVISIONED",
            key_prefix="exmk_test",
        )
        db.add(link)

        # Create a sealed snapshot.
        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=1,
            configuration_hash="sha256:abc123",
            content_hash="sha256:content_def",
            status="SEALED",
            manifest={"schools": 1, "subjects": 2, "students": 10},
            sealed_by=1,
        )
        db.add(snap)
        await db.commit()

    return exam_id, csrf


@pytest.mark.asyncio
async def test_create_processing_request(client, exam_with_snapshot):
    exam_id, csrf = exam_with_snapshot

    remote_response = {
        "request_id": "prq_test_001",
        "state": "PENDING_APPROVAL",
        "quote": {"amount": 4608000, "currency": "TZS", "unit": "per_student"},
    }

    with patch("app.services.backend_sis.request_processing", new_callable=AsyncMock, return_value=remote_response):
        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["request_id"] == "prq_test_001"
    assert data["state"] == "PENDING_APPROVAL"
    assert data["closeout_revision"] == 1
    assert data["configuration_hash"] == "sha256:abc123"


@pytest.mark.asyncio
async def test_create_processing_request_idempotent(client, exam_with_snapshot):
    exam_id, csrf = exam_with_snapshot

    remote_response = {
        "request_id": "prq_test_002",
        "state": "PENDING_APPROVAL",
        "quote": {"amount": 1000, "currency": "TZS"},
    }

    with patch("app.services.backend_sis.request_processing", new_callable=AsyncMock, return_value=remote_response) as mock_req:
        resp1 = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp1.status_code == 200

        # Second call should be idempotent (return existing, not call remote).
        resp2 = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp2.status_code == 200

    # Remote should only be called once.
    assert mock_req.call_count == 1
    assert resp2.json()["request_id"] == resp1.json()["request_id"]


@pytest.mark.asyncio
async def test_list_processing_requests(client, exam_with_snapshot):
    exam_id, csrf = exam_with_snapshot

    remote_response = {
        "request_id": "prq_list_001",
        "state": "PENDING_APPROVAL",
    }

    with patch("app.services.backend_sis.request_processing", new_callable=AsyncMock, return_value=remote_response):
        await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )

    resp = await client.get(f"/api/v1/exams/{exam_id}/processing/requests")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["request_id"] == "prq_list_001"


@pytest.mark.asyncio
async def test_refresh_processing_request(client, exam_with_snapshot):
    exam_id, csrf = exam_with_snapshot

    # Create a request first.
    remote_create = {"request_id": "prq_refresh_001", "state": "PENDING_APPROVAL"}
    with patch("app.services.backend_sis.request_processing", new_callable=AsyncMock, return_value=remote_create):
        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )
    assert resp.status_code == 200

    # Refresh with updated state.
    remote_poll = {
        "request_id": "prq_refresh_001",
        "state": "APPROVED",
        "decided_at": "2026-08-01T12:00:00+00:00",
        "decision_reason": "Payment confirmed",
    }
    with patch("app.services.backend_sis.get_processing_request", new_callable=AsyncMock, return_value=remote_poll):
        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests/prq_refresh_001/refresh",
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "APPROVED"
    assert data["decision_reason"] == "Payment confirmed"


@pytest.mark.asyncio
async def test_phase_guard_blocks_without_approval(client, exam_with_snapshot):
    """ENTRY_LOCKED -> PROCESSING is blocked without an APPROVED request (C7)."""
    exam_id, csrf = exam_with_snapshot

    # Try to submit for processing without any approved request.
    with patch("app.services.backend_sis.push_collection", new_callable=AsyncMock, return_value={}):
        with patch("app.services.backend_sis.trigger_processing", new_callable=AsyncMock, return_value={"status": "RUNNING"}):
            resp = await client.post(
                f"/api/v1/exams/{exam_id}/processing/submit",
                headers={"X-CSRF-Token": csrf},
            )

    # Should be rejected because no APPROVED request exists.
    assert resp.status_code == 422, resp.text
    assert "APPROVED" in resp.json()["detail"]["message"] or "CONFIGURATION_MISMATCH" in resp.json()["detail"]["code"]


@pytest.mark.asyncio
async def test_phase_guard_allows_with_approval(client, exam_with_snapshot):
    """ENTRY_LOCKED -> PROCESSING is allowed with a matching APPROVED request (C7)."""
    exam_id, csrf = exam_with_snapshot

    # Insert an APPROVED processing request directly.
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        req = ExamProcessingRequest(
            exam_id=uuid.UUID(exam_id),
            request_id="prq_approved_001",
            state="APPROVED",
            closeout_revision=1,
            configuration_hash="sha256:abc123",
        )
        db.add(req)
        await db.commit()

    # Now submit should work (phase guard passes).
    with patch("app.services.backend_sis.push_collection", new_callable=AsyncMock, return_value={}):
        with patch("app.services.backend_sis.trigger_processing", new_callable=AsyncMock, return_value={"status": "RUNNING"}):
            resp = await client.post(
                f"/api/v1/exams/{exam_id}/processing/submit",
                headers={"X-CSRF-Token": csrf},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["phase"] == "PROCESSING"


@pytest.mark.asyncio
async def test_phase_guard_rejects_stale_revision(client, db_setup):
    """An APPROVED request for a different revision is rejected (C7)."""
    csrf = await login(client, "superadmin", "adminpass123")

    # Create exam directly in ENTRY_LOCKED with revision 2.
    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Stale Rev Exam", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    exam_id = resp.json()["id"]

    from app.db import get_sessionmaker
    from app.models.exam import Exam
    from lazeims_common.enums import ExamPhase

    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        exam.phase = ExamPhase.ENTRY_LOCKED
        exam.closeout_revision = 2

        link = ExamProcessingLink(
            exam_id=exam.id,
            backend_exam_id="exametrics-stale-1",
            api_key="exmk_stale_secret_key",
            key_source="PROVISIONED",
        )
        db.add(link)

        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=2,
            configuration_hash="sha256:new_hash",
            content_hash="sha256:content2",
            status="SEALED",
            manifest={"schools": 1, "subjects": 1, "students": 5},
            sealed_by=1,
        )
        db.add(snap)

        # APPROVED request for revision 1, not current revision 2.
        req = ExamProcessingRequest(
            exam_id=exam.id,
            request_id="prq_stale_001",
            state="APPROVED",
            closeout_revision=1,
            configuration_hash="sha256:old_hash",
        )
        db.add(req)
        await db.commit()

    with patch("app.services.backend_sis.push_collection", new_callable=AsyncMock, return_value={}):
        with patch("app.services.backend_sis.trigger_processing", new_callable=AsyncMock, return_value={"status": "RUNNING"}):
            resp = await client.post(
                f"/api/v1/exams/{exam_id}/processing/submit",
                headers={"X-CSRF-Token": csrf},
            )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "CONFIGURATION_MISMATCH" in detail["code"]


@pytest.mark.asyncio
async def test_no_snapshot_blocks_processing_request(client, db_setup):
    """Creating a processing request without a sealed snapshot returns 409."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "No Snapshot Exam", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    exam_id = resp.json()["id"]

    from app.db import get_sessionmaker
    from app.models.exam import Exam

    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        link = ExamProcessingLink(
            exam_id=exam.id,
            backend_exam_id="exametrics-no-snap",
            api_key="exmk_no_snap_key",
            key_source="PROVISIONED",
        )
        db.add(link)
        await db.commit()

    resp = await client.post(
        f"/api/v1/exams/{exam_id}/processing/requests",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "NO_SEALED_SNAPSHOT"
