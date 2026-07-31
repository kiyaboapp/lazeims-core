"""Tests for C12: audit/notify across the processing spend path.

Verifies:
- Processing request creation emits an audit row.
- Webhook-driven state change emits an audit row.
- No audit after_snapshot contains the api_key or webhook_secret.
- The audit action values are correct for each step.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.collection import CollectionSnapshot
from app.models.cross_cutting import AuditLog
from app.models.processing import ExamProcessingLink, ExamProcessingRequest
from tests.conftest import login


@pytest.fixture(autouse=True)
def _enable_processing(monkeypatch):
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics:8000")
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _compute_signature(payload: dict, secret: str) -> str:
    canonical = {k: v for k, v in payload.items() if k != "signature"}
    canonical_bytes = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return "hmac-sha256:" + hmac_mod.new(
        secret.encode(), canonical_bytes, hashlib.sha256
    ).hexdigest()


@pytest.fixture
async def exam_for_audit(client, db_setup):
    """Create exam in ENTRY_LOCKED with link and snapshot."""
    csrf = await login(client, "superadmin", "adminpass123")
    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Audit Test Exam", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    exam_id = resp.json()["id"]

    from app.db import get_sessionmaker
    from app.models.exam import Exam
    from lazeims_common.enums import ExamPhase

    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        exam.phase = ExamPhase.ENTRY_LOCKED
        exam.closeout_revision = 1

        link = ExamProcessingLink(
            exam_id=exam.id,
            backend_exam_id="audit-exam-ref",
            api_key="exmk_audit_test_secret_key_never_log_this",
            key_source="PROVISIONED",
            key_prefix="exmk_audit",
        )
        db.add(link)
        await db.flush()

        webhook_secret = hashlib.sha256(
            f"webhook:{link.api_key_encrypted[:32]}".encode()
        ).hexdigest()[:32]

        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=1,
            configuration_hash="sha256:audit_hash",
            content_hash="sha256:audit_content",
            status="SEALED",
            manifest={"schools": 1, "subjects": 1, "students": 5},
            sealed_by=1,
        )
        db.add(snap)
        await db.commit()

    return exam_id, csrf, webhook_secret


@pytest.mark.asyncio
async def test_processing_request_creates_audit(client, exam_for_audit):
    """Creating a processing request emits a PROCESSING_REQUESTED audit row."""
    exam_id, csrf, _ = exam_for_audit

    remote_response = {"request_id": "prq_audit_001", "state": "PENDING_APPROVAL"}
    with patch("app.services.backend_sis.request_processing", new_callable=AsyncMock, return_value=remote_response):
        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )
    assert resp.status_code == 200

    from app.db import get_sessionmaker
    async with get_sessionmaker()() as db:
        logs = (await db.execute(
            select(AuditLog).where(
                AuditLog.exam_id == uuid.UUID(exam_id),
                AuditLog.action == "PROCESSING_REQUESTED",
            )
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].entity_id == "prq_audit_001"
        # The after snapshot must NOT contain the api_key.
        after = logs[0].after_snapshot or {}
        assert "exmk_audit_test_secret_key_never_log_this" not in json.dumps(after)


@pytest.mark.asyncio
async def test_webhook_creates_audit(client, exam_for_audit):
    """A webhook state change emits a PROCESSING_WEBHOOK_APPLIED audit row."""
    exam_id, csrf, webhook_secret = exam_for_audit

    # First create a processing request.
    from app.db import get_sessionmaker
    async with get_sessionmaker()() as db:
        req = ExamProcessingRequest(
            exam_id=uuid.UUID(exam_id),
            request_id="prq_audit_wh",
            state="PENDING_APPROVAL",
            closeout_revision=1,
            configuration_hash="sha256:audit_hash",
        )
        db.add(req)
        await db.commit()

    # Send a webhook.
    payload = {
        "event": "processing.completed",
        "request_id": "prq_audit_wh",
        "external_ref": "audit-exam-ref",
        "occurred_at": "2026-08-01T14:00:00Z",
        "data": {"run_id": "run_audit"},
    }
    payload["signature"] = _compute_signature(payload, webhook_secret)

    resp = await client.post("/api/v1/integration/exametrics/webhooks", json=payload)
    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    async with get_sessionmaker()() as db:
        logs = (await db.execute(
            select(AuditLog).where(
                AuditLog.action == "PROCESSING_WEBHOOK_APPLIED",
                AuditLog.entity_id == "prq_audit_wh",
            )
        )).scalars().all()
        assert len(logs) == 1
        after = logs[0].after_snapshot or {}
        # Must not contain the key or webhook secret.
        after_str = json.dumps(after)
        assert "exmk_audit_test_secret_key_never_log_this" not in after_str
        assert webhook_secret not in after_str


@pytest.mark.asyncio
async def test_refresh_state_change_creates_audit(client, exam_for_audit):
    """Refreshing a request that changes state emits PROCESSING_STATE_CHANGED."""
    exam_id, csrf, _ = exam_for_audit

    # Create request.
    remote_create = {"request_id": "prq_audit_refresh", "state": "PENDING_APPROVAL"}
    with patch("app.services.backend_sis.request_processing", new_callable=AsyncMock, return_value=remote_create):
        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests",
            headers={"X-CSRF-Token": csrf},
        )
    assert resp.status_code == 200

    # Refresh with state change.
    remote_poll = {"request_id": "prq_audit_refresh", "state": "APPROVED"}
    with patch("app.services.backend_sis.get_processing_request", new_callable=AsyncMock, return_value=remote_poll):
        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/requests/prq_audit_refresh/refresh",
            headers={"X-CSRF-Token": csrf},
        )
    assert resp.status_code == 200

    from app.db import get_sessionmaker
    async with get_sessionmaker()() as db:
        logs = (await db.execute(
            select(AuditLog).where(
                AuditLog.action == "PROCESSING_STATE_CHANGED",
                AuditLog.entity_id == "prq_audit_refresh",
            )
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].before_snapshot == {"state": "PENDING_APPROVAL"}
        assert logs[0].after_snapshot["state"] == "APPROVED"
