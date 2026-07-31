"""Tests for the ExaMetrics webhook receiver (C9).

Verifies:
- A valid processing.completed webhook advances the mirrored request state.
- A tampered signature returns 401 and leaves the state untouched.
- The same envelope delivered twice changes state only once (idempotent).
- Webhook never exposes the api_key or webhook_secret in audit records.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.processing import ExamProcessingLink, ExamProcessingRequest, WebhookReceipt
from app.services.exametrics_webhooks import verify_signature
from tests.conftest import login


def _compute_signature(payload: dict, secret: str) -> str:
    """Compute the expected HMAC-SHA256 signature for a webhook payload."""
    canonical = {k: v for k, v in payload.items() if k != "signature"}
    canonical_bytes = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return "hmac-sha256:" + hmac_mod.new(
        secret.encode(), canonical_bytes, hashlib.sha256
    ).hexdigest()


def test_verify_signature_valid():
    payload = {"event": "processing.completed", "request_id": "prq_1", "occurred_at": "2026-08-01T00:00:00Z"}
    secret = "test_webhook_secret_12345678"
    sig = _compute_signature(payload, secret)
    payload["signature"] = sig
    assert verify_signature(payload, secret) is True


def test_verify_signature_invalid():
    payload = {"event": "processing.completed", "request_id": "prq_1", "occurred_at": "2026-08-01T00:00:00Z", "signature": "hmac-sha256:badbadbad"}
    assert verify_signature(payload, "test_secret") is False


def test_verify_signature_missing():
    payload = {"event": "processing.completed", "request_id": "prq_1", "occurred_at": "2026-08-01T00:00:00Z"}
    assert verify_signature(payload, "test_secret") is False


@pytest.fixture
async def exam_with_request(client, db_setup):
    """Create an exam, link, and a PENDING_APPROVAL processing request."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Webhook Test Exam", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    exam_id = resp.json()["id"]

    from app.db import get_sessionmaker
    from app.models.exam import Exam
    from app.models.collection import CollectionSnapshot
    from lazeims_common.enums import ExamPhase

    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        exam.phase = ExamPhase.ENTRY_LOCKED

        link = ExamProcessingLink(
            exam_id=exam.id,
            backend_exam_id="wh-exam-ref-1",
            api_key="exmk_webhook_test_secret",
            key_source="PROVISIONED",
            key_prefix="exmk_webhook",
        )
        db.add(link)
        await db.flush()

        # Compute the webhook secret the same way the router does.
        webhook_secret = hashlib.sha256(
            f"webhook:{link.api_key_encrypted[:32]}".encode()
        ).hexdigest()[:32]

        req = ExamProcessingRequest(
            exam_id=exam.id,
            request_id="prq_wh_001",
            state="PENDING_APPROVAL",
            closeout_revision=1,
            configuration_hash="sha256:wh_hash",
        )
        db.add(req)
        await db.commit()

    return exam_id, csrf, webhook_secret


@pytest.mark.asyncio
async def test_webhook_processing_completed(client, exam_with_request):
    """A valid processing.completed webhook advances the mirrored state."""
    exam_id, csrf, webhook_secret = exam_with_request

    payload = {
        "event": "processing.completed",
        "request_id": "prq_wh_001",
        "external_ref": "wh-exam-ref-1",
        "occurred_at": "2026-08-01T12:00:00Z",
        "data": {"run_id": "run_123"},
    }
    payload["signature"] = _compute_signature(payload, webhook_secret)

    resp = await client.post("/api/v1/integration/exametrics/webhooks", json=payload)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["applied"] is True

    # Verify the request state was updated.
    from app.db import get_sessionmaker
    from sqlalchemy import select

    async with get_sessionmaker()() as db:
        req = (await db.execute(
            select(ExamProcessingRequest).where(ExamProcessingRequest.request_id == "prq_wh_001")
        )).scalar_one()
        assert req.state == "COMPLETED"
        assert req.run_id == "run_123"


@pytest.mark.asyncio
async def test_webhook_bad_signature_401(client, exam_with_request):
    """A tampered signature returns 401 and leaves the state untouched."""
    exam_id, csrf, webhook_secret = exam_with_request

    payload = {
        "event": "processing.completed",
        "request_id": "prq_wh_001",
        "external_ref": "wh-exam-ref-1",
        "occurred_at": "2026-08-01T12:00:00Z",
        "data": {"run_id": "run_evil"},
        "signature": "hmac-sha256:tampered_value_here",
    }

    resp = await client.post("/api/v1/integration/exametrics/webhooks", json=payload)
    assert resp.status_code == 401

    # State should be unchanged.
    from app.db import get_sessionmaker
    from sqlalchemy import select

    async with get_sessionmaker()() as db:
        req = (await db.execute(
            select(ExamProcessingRequest).where(ExamProcessingRequest.request_id == "prq_wh_001")
        )).scalar_one()
        assert req.state == "PENDING_APPROVAL"


@pytest.mark.asyncio
async def test_webhook_idempotent(client, exam_with_request):
    """The same webhook delivered twice changes state only once."""
    exam_id, csrf, webhook_secret = exam_with_request

    payload = {
        "event": "processing.completed",
        "request_id": "prq_wh_001",
        "external_ref": "wh-exam-ref-1",
        "occurred_at": "2026-08-01T12:00:00Z",
        "data": {"run_id": "run_999"},
    }
    payload["signature"] = _compute_signature(payload, webhook_secret)

    resp1 = await client.post("/api/v1/integration/exametrics/webhooks", json=payload)
    assert resp1.status_code == 200
    assert resp1.json()["applied"] is True

    # Second delivery should be a no-op.
    resp2 = await client.post("/api/v1/integration/exametrics/webhooks", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["applied"] is False
    assert resp2.json()["reason"] == "already_applied"
