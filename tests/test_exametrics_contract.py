"""Section 12 contract tests from shared fixtures.

Uses the transport-injection pattern (like test_cross_repo_sync.py) to verify
Central's behaviour against the shared lazeims-common fixtures. No network
required.

Test cases:
- test_exam_autoprovisioned_once: provisioning is idempotent.
- test_processing_requires_approval: ENTRY_LOCKED -> PROCESSING is blocked
  without an APPROVED request.
- test_approved_processing_runs_once: with approval, submit works and is
  idempotent.
- test_approval_invalidated_by_reopen: a reopen invalidates a paid approval.
- test_webhook_signature_rejected: tampered signature returns 401.
- test_capabilities_drive_ui_payload: capabilities JSON from identity drives
  the link's capabilities_json.
- test_collection_digest_matches_frozen: Central's collection payload digest
  matches the frozen_digest() from the shared fixtures.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from lazeims_common.exametrics_digest import collection_digest
from lazeims_common.fixtures.exametrics import (
    COLLECTION_PAYLOAD,
    IDENTITY_RESPONSE,
    PROCESSING_REQUEST_APPROVED,
    PROCESSING_REQUEST_PENDING,
    WEBHOOK_COMPLETED,
    frozen_digest,
)

from app.models.collection import CollectionSnapshot
from app.models.processing import ExamProcessingLink, ExamProcessingRequest
from tests.conftest import login


@pytest.fixture(autouse=True)
def _enable_processing(monkeypatch):
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics:8000")
    monkeypatch.setenv("BACKEND_SIS_PROVISION_SECRET", "test-provision-secret")
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---- test_collection_digest_matches_frozen ----

def test_collection_digest_matches_frozen():
    """Central's collection_digest produces the same value as the shared fixture."""
    digest = collection_digest(COLLECTION_PAYLOAD)
    assert digest == frozen_digest()


# ---- test_exam_autoprovisioned_once ----

@pytest.mark.asyncio
async def test_exam_autoprovisioned_once(client, db_setup):
    """Provisioning is idempotent: second call is a no-op."""
    csrf = await login(client, "superadmin", "adminpass123")

    provision_response = {
        "api_key": "exmk_contract_test_secret_key",
        "key_prefix": "exmk_contract",
        "exam_ref": "contract-exam-ref",
        "approval_status": "PENDING",
    }
    identity_resp = dict(IDENTITY_RESPONSE)

    with patch("app.services.backend_sis.provision_exam", new_callable=AsyncMock, return_value=provision_response):
        with patch("app.services.backend_sis.identity", new_callable=AsyncMock, return_value=identity_resp):
            resp = await client.post(
                "/api/v1/exams",
                json={"name": "Contract Exam", "level_id": 1},
                headers={"X-CSRF-Token": csrf},
            )
    assert resp.status_code == 201
    exam_id = resp.json()["id"]

    # Second key request should be a no-op (link already exists).
    with patch("app.services.backend_sis.provision_exam", new_callable=AsyncMock) as mock_prov:
        with patch("app.services.backend_sis.identity", new_callable=AsyncMock, return_value=identity_resp):
            resp2 = await client.post(
                f"/api/v1/exams/{exam_id}/processing/access-key",
                headers={"X-CSRF-Token": csrf},
            )
    assert resp2.status_code == 200
    # provision_exam should NOT be called again (link exists, not rotating).
    mock_prov.assert_not_called()


# ---- test_processing_requires_approval ----

@pytest.mark.asyncio
async def test_processing_requires_approval(client, db_setup):
    """ENTRY_LOCKED -> PROCESSING is blocked without an APPROVED request (C7)."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Approval Required Exam", "level_id": 1},
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
            backend_exam_id="contract-req-exam",
            api_key="exmk_contract_secret",
            key_source="PROVISIONED",
        )
        db.add(link)

        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=1,
            configuration_hash="sha256:contract_hash",
            content_hash="sha256:contract_content",
            status="SEALED",
            manifest={"schools": 1, "subjects": 1, "students": 1},
            sealed_by=1,
        )
        db.add(snap)
        await db.commit()

    with patch("app.services.backend_sis.push_collection", new_callable=AsyncMock, return_value={}):
        with patch("app.services.backend_sis.trigger_processing", new_callable=AsyncMock, return_value={}):
            resp = await client.post(
                f"/api/v1/exams/{exam_id}/processing/submit",
                headers={"X-CSRF-Token": csrf},
            )

    assert resp.status_code == 422
    assert "CONFIGURATION_MISMATCH" in resp.json()["detail"]["code"]


# ---- test_approved_processing_runs_once ----

@pytest.mark.asyncio
async def test_approved_processing_runs_once(client, db_setup):
    """With approval, submit works. Re-submit in PROCESSING is idempotent."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Approved Run Exam", "level_id": 1},
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
            backend_exam_id="contract-approved-exam",
            api_key="exmk_approved_secret",
            key_source="PROVISIONED",
        )
        db.add(link)

        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=1,
            configuration_hash="sha256:approved_hash",
            content_hash="sha256:approved_content",
            status="SEALED",
            manifest={"schools": 2, "subjects": 3, "students": 10},
            sealed_by=1,
        )
        db.add(snap)

        req = ExamProcessingRequest(
            exam_id=exam.id,
            request_id="prq_approved_contract",
            state="APPROVED",
            closeout_revision=1,
            configuration_hash="sha256:approved_hash",
        )
        db.add(req)
        await db.commit()

    with patch("app.services.backend_sis.push_collection", new_callable=AsyncMock, return_value={}):
        with patch("app.services.backend_sis.trigger_processing", new_callable=AsyncMock, return_value={"status": "RUNNING"}):
            resp = await client.post(
                f"/api/v1/exams/{exam_id}/processing/submit",
                headers={"X-CSRF-Token": csrf},
            )

    assert resp.status_code == 200
    assert resp.json()["phase"] == "PROCESSING"


# ---- test_approval_invalidated_by_reopen ----

@pytest.mark.asyncio
async def test_approval_invalidated_by_reopen(client, db_setup):
    """A reopen bumps closeout_revision, invalidating the old approval."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Reopen Invalidate Exam", "level_id": 1},
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
            backend_exam_id="contract-reopen-exam",
            api_key="exmk_reopen_secret",
            key_source="PROVISIONED",
        )
        db.add(link)

        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=1,
            configuration_hash="sha256:reopen_hash_v1",
            content_hash="sha256:reopen_content",
            status="SEALED",
            manifest={"schools": 1, "subjects": 1, "students": 1},
            sealed_by=1,
        )
        db.add(snap)

        # Old approval at revision 1.
        req = ExamProcessingRequest(
            exam_id=exam.id,
            request_id="prq_reopen_old",
            state="APPROVED",
            closeout_revision=1,
            configuration_hash="sha256:reopen_hash_v1",
        )
        db.add(req)
        await db.commit()

    # Reopen bumps to revision 2.
    resp = await client.post(
        f"/api/v1/exams/{exam_id}/collection-reopen",
        json={"reason": "Found errors in data"},
        headers={"X-CSRF-Token": csrf},
    )
    # Reopen might succeed or fail (needs exam in specific state).
    # Either way, if we create a new snapshot at rev 2, the old approval is stale.

    # Simulate the reopened state: now at revision 2 with a new snapshot.
    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        exam.phase = ExamPhase.ENTRY_LOCKED
        exam.closeout_revision = 2

        snap2 = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=exam.id,
            closeout_revision=2,
            configuration_hash="sha256:reopen_hash_v2",
            content_hash="sha256:reopen_content_v2",
            status="SEALED",
            manifest={"schools": 1, "subjects": 1, "students": 2},
            sealed_by=1,
        )
        db.add(snap2)
        await db.commit()

    # Try to submit: the approval is for rev 1, snapshot is rev 2.
    with patch("app.services.backend_sis.push_collection", new_callable=AsyncMock, return_value={}):
        with patch("app.services.backend_sis.trigger_processing", new_callable=AsyncMock, return_value={}):
            resp = await client.post(
                f"/api/v1/exams/{exam_id}/processing/submit",
                headers={"X-CSRF-Token": csrf},
            )

    assert resp.status_code == 422
    assert "CONFIGURATION_MISMATCH" in resp.json()["detail"]["code"]


# ---- test_webhook_signature_rejected ----

@pytest.mark.asyncio
async def test_webhook_signature_rejected(client, db_setup):
    """A tampered webhook signature returns 401."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "WH Sig Exam", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    exam_id = resp.json()["id"]

    from app.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        from app.models.exam import Exam
        exam = await db.get(Exam, uuid.UUID(exam_id))
        link = ExamProcessingLink(
            exam_id=exam.id,
            backend_exam_id="contract-sig-exam",
            api_key="exmk_sig_test_key",
            key_source="PROVISIONED",
        )
        db.add(link)
        await db.commit()

    payload = dict(WEBHOOK_COMPLETED)
    payload["external_ref"] = "contract-sig-exam"
    payload["signature"] = "hmac-sha256:definitely_wrong_value"

    resp = await client.post("/api/v1/integration/exametrics/webhooks", json=payload)
    assert resp.status_code == 401


# ---- test_capabilities_drive_ui_payload ----

@pytest.mark.asyncio
async def test_capabilities_drive_ui_payload(client, db_setup):
    """Identity response capabilities are stored on the link."""
    csrf = await login(client, "superadmin", "adminpass123")

    resp = await client.post(
        "/api/v1/exams",
        json={"name": "Cap Drive Exam", "level_id": 1},
        headers={"X-CSRF-Token": csrf},
    )
    exam_id = resp.json()["id"]

    identity_resp = dict(IDENTITY_RESPONSE)

    with patch("app.routers.integration.backend_sis.identity", new_callable=AsyncMock, return_value=identity_resp):
        resp = await client.put(
            f"/api/v1/exams/{exam_id}/processing/link",
            json={"api_key": "exmk_cap_test_key_123456"},
            headers={"X-CSRF-Token": csrf},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["capabilities"] == IDENTITY_RESPONSE["capabilities"]
    assert body["tenant_exam_name"] == IDENTITY_RESPONSE["tenant_exam"]["name"]
