"""Tests for zero-touch ExaMetrics key issuance (Phase 1.5).

The point of the feature is that no human ever handles an API key, so these
tests assert both halves of that: Central obtains and stores the key itself, and
neither a response body nor an audit row ever contains the secret.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.cross_cutting import AuditLog
from app.models.processing import ExamProcessingLink
from app.services.backend_sis import BackendSisError
from tests.conftest import login

SECRET = "exmk_ab12cd34_thisisthesecretpartnobodyshouldeversee"

PROVISION_RESPONSE = {
    "api_key": SECRET,
    "key_id": "key-uuid-1",
    "key_prefix": "exmk_ab12cd34",
    "exam_ref": "exametrics-exam-1",
    "external_ref": "central-exam-1",
    "exam_id": "exametrics-exam-1",
    "external_exam_id": "central-exam-1",
    "exam_created": True,
    "requested_scopes": [
        "collection:push",
        "results:process",
        "results:read",
        "results:download",
    ],
    "usable_scopes": ["collection:push"],
    "approval_status": "PENDING",
    "approval_note": None,
}

IDENTITY_RESPONSE = {
    "contract_version": "exametrics-integration/v2",
    "tenant_exam": {"id": "exametrics-exam-1", "name": "CSEE 2026", "environment": "sandbox"},
    "tenant": {"id": "exametrics-exam-1", "name": "CSEE 2026", "environment": "sandbox"},
    "capabilities": {"collection.push": True, "results.read": False},
    "limits": {},
    "supported_rules_versions": ["1.0"],
    "approval_status": "PENDING",
    "approval_note": None,
    "key_prefix": "exmk_ab12cd34",
}

LEGACY_IDENTITY_RESPONSE = {
    **{k: v for k, v in IDENTITY_RESPONSE.items() if k != "tenant_exam"},
    "tenant": {"id": "exametrics-exam-1", "name": "Legacy Exam Account", "environment": "sandbox"},
}


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _create_exam(client, h, name: str = "Test Exam"):
    code = f"TEST-{uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/api/v1/exams",
        json={"name": name, "exam_code": code, "level_id": 1},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _enable_provisioning(monkeypatch, *, secret: str = "zone-enrolment-secret"):
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics.test")
    monkeypatch.setenv("BACKEND_SIS_PROVISION_SECRET", secret)
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings


def _mock_backend(monkeypatch, *, provision=None, identity=None):
    provision_mock = provision or AsyncMock(return_value=dict(PROVISION_RESPONSE))
    identity_mock = identity or AsyncMock(return_value=dict(IDENTITY_RESPONSE))
    monkeypatch.setattr(
        "app.services.exametrics_provision.backend_sis.provision_exam", provision_mock
    )
    monkeypatch.setattr(
        "app.services.exametrics_provision.backend_sis.identity", identity_mock
    )
    return provision_mock, identity_mock


async def _link(exam_id):
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        return (
            await db.execute(
                select(ExamProcessingLink).where(ExamProcessingLink.exam_id == uuid.UUID(exam_id))
            )
        ).scalar_one_or_none()


async def _audit_rows(exam_id, action):
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        return list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.exam_id == uuid.UUID(exam_id), AuditLog.action == action
                    )
                )
            ).scalars().all()
        )


# ── On-demand issuance ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_access_key_issues_and_stores_key_without_showing_it(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, identity_mock = _mock_backend(monkeypatch)

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["configured"] is True
        assert body["key_source"] == "PROVISIONED"
        assert body["key_prefix"] == "exmk_ab12cd34"
        assert body["key_issued_at"] is not None
        assert body["approval_status"] == "PENDING"
        assert body["backend_exam_id"] == "exametrics-exam-1"
        assert body["tenant_exam_name"] == "CSEE 2026"

        # The whole point: the secret is nowhere in the response.
        assert "api_key" not in body
        assert SECRET not in resp.text

        # But it was stored, so Central can use it on the operator's behalf.
        link = await _link(exam_id)
        assert link.api_key == SECRET
        identity_mock.assert_awaited_once_with(SECRET)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_access_key_posts_exam_details_and_the_creator_identity(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h, name="CSEE 2026 Zone 1")
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, _ = _mock_backend(monkeypatch)

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.status_code == 200, resp.text

        payload = provision_mock.await_args.args[0]
        assert payload["external_exam_id"] == exam_id
        assert payload["exam_name"] == "CSEE 2026 Zone 1"
        assert payload["exam_level"] == "FTNA"
        # All scopes are asked for every time: the free ones come back usable,
        # the paid ones queue for approval.
        assert payload["scopes"] == [
            "collection:push",
            "results:process",
            "results:read",
            "results:download",
        ]
        # Identity is resolved server-side, so it cannot be forged by a browser.
        assert payload["requested_by"]["username"] == "superadmin"
        assert payload["requested_by"]["name"] == "Super Admin"
        assert payload["requested_by"]["role"] == "SUPER_ADMIN"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_second_call_is_a_no_op_and_rotate_reissues(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, _ = _mock_backend(monkeypatch)

    try:
        first = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert first.status_code == 200
        assert provision_mock.await_count == 1

        # Already has a key: asking again changes nothing.
        again = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert again.status_code == 200
        assert provision_mock.await_count == 1

        rotated = PROVISION_RESPONSE | {"api_key": "exmk_ff99ee88_rotated", "key_prefix": "exmk_ff99ee88"}
        provision_mock.return_value = rotated

        resp = await client.post(
            f"/api/v1/exams/{exam_id}/processing/access-key?rotate=true", headers=h
        )
        assert resp.status_code == 200
        assert provision_mock.await_count == 2
        assert resp.json()["key_prefix"] == "exmk_ff99ee88"

        link = await _link(exam_id)
        assert link.api_key == "exmk_ff99ee88_rotated"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_access_key_503_when_provisioning_disabled(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    # No base URL configured: provisioning is disabled entirely.
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "")
    monkeypatch.setenv("BACKEND_SIS_PROVISION_SECRET", "")
    from app.config import get_settings

    get_settings.cache_clear()
    provision_mock, _ = _mock_backend(monkeypatch)

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "PROVISIONING_DISABLED"
        provision_mock.assert_not_awaited()
        assert await _link(exam_id) is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_access_key_upstream_failure_is_a_502_envelope(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    _mock_backend(
        monkeypatch,
        provision=AsyncMock(side_effect=BackendSisError("ExaMetrics error: boom", status_code=500)),
    )

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.status_code == 502
        assert resp.json()["detail"]["code"] == "EXAMETRICS_ERROR"
        assert await _link(exam_id) is None
    finally:
        get_settings.cache_clear()


# ── Identity naming compatibility (D5) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_exam_populates_the_exam_account_name(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    _mock_backend(monkeypatch)

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.json()["tenant_exam_name"] == "CSEE 2026"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_legacy_tenant_only_identity_still_populates_the_name(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    _mock_backend(monkeypatch, identity=AsyncMock(return_value=dict(LEGACY_IDENTITY_RESPONSE)))

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.json()["tenant_exam_name"] == "Legacy Exam Account"
    finally:
        get_settings.cache_clear()


# ── Audit ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_issuance_is_audited_without_the_secret(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    _mock_backend(monkeypatch)

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/access-key", headers=h)
        assert resp.status_code == 200

        rows = await _audit_rows(exam_id, "PROCESSING_KEY_ISSUED")
        assert len(rows) == 1
        after = rows[0].after_snapshot
        assert after["key_prefix"] == "exmk_ab12cd34"
        assert after["approval_status"] == "PENDING"
        assert after["usable_scopes"] == ["collection:push"]
        # §9.6: an audit record must never carry the secret.
        assert SECRET not in str(after)
        assert "api_key" not in after
    finally:
        get_settings.cache_clear()


# ── Issuance on exam creation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exam_creation_issues_a_key_for_the_creator(client, monkeypatch):
    h = await _admin(client)
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, _ = _mock_backend(monkeypatch)

    try:
        exam_id = await _create_exam(client, h, name="Auto Provisioned Exam")
        assert provision_mock.await_count == 1

        link = await _link(exam_id)
        assert link is not None
        assert link.key_source == "PROVISIONED"
        assert link.key_prefix == "exmk_ab12cd34"

        payload = provision_mock.await_args.args[0]
        assert payload["exam_name"] == "Auto Provisioned Exam"
        assert payload["requested_by"]["username"] == "superadmin"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_exam_creation_survives_a_provisioning_failure(client, monkeypatch):
    h = await _admin(client)
    get_settings = _enable_provisioning(monkeypatch)
    _mock_backend(
        monkeypatch,
        provision=AsyncMock(side_effect=BackendSisError("ExaMetrics unreachable")),
    )

    try:
        exam_id = await _create_exam(client, h, name="Exam Despite Outage")
        assert await _link(exam_id) is None
        rows = await _audit_rows(exam_id, "PROCESSING_KEY_ISSUE_FAILED")
        assert len(rows) == 1
        assert "unreachable" in rows[0].after_snapshot["reason"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_exam_creation_still_201_with_provisioning_disabled(client):
    """The default in tests: no base URL, so no key — and no failure either."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h, name="Collection Only Exam")
    assert await _link(exam_id) is None
    rows = await _audit_rows(exam_id, "PROCESSING_KEY_ISSUE_FAILED")
    assert len(rows) == 1
    assert rows[0].after_snapshot["reason"] == "PROVISIONING_DISABLED"


# ── Self-healing on submit ───────────────────────────────────────────────────

async def _seed_link(exam_id, **kwargs):
    """Commit a link and put the exam in ENTRY_LOCKED so submit can run.

    Also creates a sealed snapshot and an APPROVED processing request so the
    C7 phase guard passes (submit requires APPROVED for current revision).
    """
    from app.db import get_sessionmaker
    from app.models.exam import Exam
    from app.models.collection import CollectionSnapshot
    from app.models.processing import ExamProcessingRequest
    from lazeims_common.enums import ExamPhase

    async with get_sessionmaker()() as db:
        exam = await db.get(Exam, uuid.UUID(exam_id))
        exam.phase = ExamPhase.ENTRY_LOCKED
        exam.closeout_revision = 1
        db.add(ExamProcessingLink(exam_id=uuid.UUID(exam_id), **kwargs))
        # Sealed snapshot + approved request for C7 phase guard.
        snap = CollectionSnapshot(
            snapshot_id=str(uuid.uuid4()),
            exam_id=uuid.UUID(exam_id),
            closeout_revision=1,
            configuration_hash="sha256:test_config",
            content_hash="sha256:test_content",
            status="SEALED",
            manifest={"schools": 1, "subjects": 1, "students": 1},
            sealed_by=1,
        )
        db.add(snap)
        req = ExamProcessingRequest(
            exam_id=uuid.UUID(exam_id),
            request_id=f"prq_provision_test_{uuid.uuid4().hex[:8]}",
            state="APPROVED",
            closeout_revision=1,
            configuration_hash="sha256:test_config",
        )
        db.add(req)
        await db.commit()


def _mock_push(monkeypatch):
    monkeypatch.setattr(
        "app.routers.integration.backend_sis.push_collection", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "app.routers.integration.backend_sis.trigger_processing",
        AsyncMock(return_value={"status": "queued"}),
    )


@pytest.mark.asyncio
async def test_submit_provisions_a_key_only_provisioned_link(client, monkeypatch):
    """A PROVISIONED link with no ExaMetrics exam id heals itself on first push."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, _ = _mock_backend(monkeypatch)
    _mock_push(monkeypatch)
    await _seed_link(
        exam_id,
        api_key="exmk_old00000_legacykey",
        key_source="PROVISIONED",
        backend_exam_id=None,
    )

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/submit", headers=h)
        assert resp.status_code == 200, resp.text
        assert provision_mock.await_count == 1

        link = await _link(exam_id)
        assert link.backend_exam_id == "exametrics-exam-1"
        assert link.api_key == SECRET
        assert link.key_prefix == "exmk_ab12cd34"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_submit_never_overwrites_a_manual_key(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, _ = _mock_backend(monkeypatch)
    _mock_push(monkeypatch)
    await _seed_link(
        exam_id,
        api_key="hand-entered-key-1234",
        key_source="MANUAL",
        backend_exam_id=None,
    )

    try:
        # A MANUAL link missing its exam id is a support problem, not something
        # to silently re-provision over.
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/submit", headers=h)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "EXAM_NOT_PROVISIONED"
        provision_mock.assert_not_awaited()

        link = await _link(exam_id)
        assert link.api_key == "hand-entered-key-1234"
        assert link.backend_exam_id is None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_submit_leaves_a_complete_link_alone(client, monkeypatch):
    h = await _admin(client)
    exam_id = await _create_exam(client, h)
    get_settings = _enable_provisioning(monkeypatch)
    provision_mock, _ = _mock_backend(monkeypatch)
    _mock_push(monkeypatch)
    await _seed_link(
        exam_id,
        api_key="exmk_ab12cd34_already_good",
        key_source="PROVISIONED",
        backend_exam_id="exametrics-exam-1",
    )

    try:
        resp = await client.post(f"/api/v1/exams/{exam_id}/processing/submit", headers=h)
        assert resp.status_code == 200, resp.text
        provision_mock.assert_not_awaited()
    finally:
        get_settings.cache_clear()
