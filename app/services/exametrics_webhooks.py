"""ExaMetrics webhook processing (C9).

Handles inbound webhooks from ExaMetrics, verifying HMAC signatures and
applying state changes idempotently. A webhook redelivery is a no-op thanks
to the (request_id, event, occurred_at) uniqueness constraint on
``WebhookReceipt``.

NEVER log the webhook_secret or the api_key in audit records.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.processing import ExamProcessingLink, ExamProcessingRequest, WebhookReceipt
from ..services import notifications
from ..services.exam_phase import RESULTS_READY_STATUS


def verify_signature(payload: dict, secret: str) -> bool:
    """Verify the HMAC-SHA256 signature on a webhook envelope.

    The signature is computed over the canonical JSON of the envelope with the
    ``signature`` field removed. Uses hmac.compare_digest for timing safety.
    """
    signature = payload.get("signature", "")
    if not signature:
        return False

    # Build canonical JSON without the signature field.
    canonical = {k: v for k, v in payload.items() if k != "signature"}
    canonical_bytes = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # Compute expected HMAC.
    expected = "hmac-sha256:" + hmac.new(
        secret.encode(), canonical_bytes, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


async def apply_webhook(
    db: AsyncSession,
    payload: dict,
    link: ExamProcessingLink,
) -> dict:
    """Apply a verified webhook idempotently.

    Returns a dict with the outcome: {"applied": True/False, "reason": ...}
    """
    request_id = payload.get("request_id", "")
    event = payload.get("event", "")
    occurred_at = payload.get("occurred_at", "")

    if not request_id or not event or not occurred_at:
        return {"applied": False, "reason": "missing_fields"}

    # Idempotency check: has this exact webhook already been applied?
    existing = (
        await db.execute(
            select(WebhookReceipt).where(
                WebhookReceipt.request_id == request_id,
                WebhookReceipt.event == event,
                WebhookReceipt.occurred_at == occurred_at,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"applied": False, "reason": "already_applied"}

    # Record the receipt.
    receipt = WebhookReceipt(
        request_id=request_id,
        event=event,
        occurred_at=occurred_at,
        external_ref=payload.get("external_ref"),
        payload_json=payload,
        applied_at=datetime.now(timezone.utc),
    )
    db.add(receipt)

    # Apply the state change to the mirrored processing request.
    req = (
        await db.execute(
            select(ExamProcessingRequest).where(
                ExamProcessingRequest.request_id == request_id,
            )
        )
    ).scalar_one_or_none()

    old_state = None
    new_state = None

    if req is not None:
        old_state = req.state
        data = payload.get("data", {})

        if event == "processing.approval_changed":
            new_state = data.get("state", req.state)
            req.state = new_state
            if data.get("decided_at"):
                req.decided_at = datetime.fromisoformat(data["decided_at"]) if isinstance(data["decided_at"], str) else None
            if data.get("decision_reason"):
                req.decision_reason = data["decision_reason"]

        elif event == "processing.completed":
            req.state = "COMPLETED"
            new_state = "COMPLETED"
            if data.get("run_id"):
                req.run_id = data["run_id"]
            # Mark the link as results-ready so publish can be gated without polling.
            link.last_status = RESULTS_READY_STATUS

        elif event == "processing.failed":
            req.state = "FAILED"
            new_state = "FAILED"
            if data.get("run_id"):
                req.run_id = data["run_id"]
            if data.get("reason"):
                req.decision_reason = data["reason"]

        elif event == "results.ready":
            link.last_status = RESULTS_READY_STATUS
            new_state = req.state  # state unchanged

        req.last_polled_at = datetime.now(timezone.utc)

    try:
        await db.flush()
    except IntegrityError:
        # Race condition: another request applied the same webhook. That is fine.
        await db.rollback()
        return {"applied": False, "reason": "duplicate"}

    # Audit the webhook application (never include the key or secret).
    if req is not None and old_state != new_state and new_state is not None:
        await notifications.record(
            db,
            action="PROCESSING_WEBHOOK_APPLIED",
            entity_type="exam_processing_request",
            entity_id=request_id,
            exam_id=req.exam_id,
            after={"event": event, "old_state": old_state, "new_state": new_state},
        )

    return {"applied": True, "event": event, "request_id": request_id}
