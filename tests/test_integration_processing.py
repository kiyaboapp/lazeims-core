"""Tests for processing link + capabilities cache (Phase 1).

Verifies:
- set_processing_link accepts key-only (no backend_exam_id)
- identity() validation is called on save, failures return 502
- GET /processing/capabilities returns cached data
- Backward compat: providing backend_exam_id still works
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.backend_sis import BackendSisError
from tests.conftest import login


FAKE_IDENTITY_RESPONSE = {
    "contract_version": "exametrics-integration/v2",
    "tenant": {"id": "tenant-abc", "name": "Test Board", "environment": "sandbox"},
    "capabilities": {
        "exam.provision": True,
        "collection.push": True,
        "registrations.extract": True,
        "processing.request": False,
        "results.read": True,
    },
    "limits": {"max_students_per_exam": 50000},
    "supported_rules_versions": ["1.0", "1.1"],
}


async def _admin(client):
    csrf = await login(client, "superadmin", "adminpass123")
    return {"X-CSRF-Token": csrf}


async def _create_exam(client, h):
    code = f"TEST-{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/v1/exams", json={"name": "Test Exam", "exam_code": code, "level_id": 1}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_link_accepts_key_only(client, monkeypatch):
    """set_processing_link with only api_key (no backend_exam_id) succeeds
    after verifying the key via identity()."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    mock_identity = AsyncMock(return_value=FAKE_IDENTITY_RESPONSE)
    monkeypatch.setattr("app.routers.integration.backend_sis.identity", mock_identity)
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics.test")
    # Clear settings cache so the new env var is picked up.
    from app.config import get_settings
    get_settings.cache_clear()

    try:
        resp = await client.put(
            f"/api/v1/exams/{exam_id}/processing/link",
            json={"api_key": "valid-test-key-12345678"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        assert body["backend_exam_id"] is None
        # The fake identity response still uses the legacy "tenant" key, which
        # Central must keep reading for one release (D5).
        assert body["tenant_exam_name"] == "Test Board"
        assert body["key_source"] == "MANUAL"
        assert body["capabilities"] == FAKE_IDENTITY_RESPONSE["capabilities"]
        assert body["capabilities_fetched_at"] is not None
        mock_identity.assert_called_once_with("valid-test-key-12345678")
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_link_accepts_key_with_backend_exam_id(client, monkeypatch):
    """Backward compat: providing both backend_exam_id and api_key works."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    mock_identity = AsyncMock(return_value=FAKE_IDENTITY_RESPONSE)
    monkeypatch.setattr("app.routers.integration.backend_sis.identity", mock_identity)
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics.test")
    from app.config import get_settings
    get_settings.cache_clear()

    try:
        resp = await client.put(
            f"/api/v1/exams/{exam_id}/processing/link",
            json={"backend_exam_id": "abc-123-def-456", "api_key": "valid-test-key-12345678"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        assert body["backend_exam_id"] == "abc-123-def-456"
        assert body["tenant_exam_name"] == "Test Board"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_invalid_key_rejected_on_save(client, monkeypatch):
    """identity() raising BackendSisError causes 502 on the save endpoint."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    mock_identity = AsyncMock(side_effect=BackendSisError("Invalid API key", status_code=401, code="INVALID_KEY"))
    monkeypatch.setattr("app.routers.integration.backend_sis.identity", mock_identity)
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics.test")
    from app.config import get_settings
    get_settings.cache_clear()

    try:
        resp = await client.put(
            f"/api/v1/exams/{exam_id}/processing/link",
            json={"api_key": "bad-key-12345678"},
            headers=h,
        )
        # D9: 401 is a passthrough code, so it is preserved (not 502).
        assert resp.status_code == 401
        body = resp.json()
        assert body["detail"]["code"] == "INVALID_KEY"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_capabilities_endpoint(client, monkeypatch):
    """GET /processing/capabilities returns cached capabilities."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    # First, set up a link with capabilities.
    mock_identity = AsyncMock(return_value=FAKE_IDENTITY_RESPONSE)
    monkeypatch.setattr("app.routers.integration.backend_sis.identity", mock_identity)
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "http://fake-exametrics.test")
    from app.config import get_settings
    get_settings.cache_clear()

    try:
        resp = await client.put(
            f"/api/v1/exams/{exam_id}/processing/link",
            json={"api_key": "valid-test-key-12345678"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text

        # Now fetch capabilities.
        resp = await client.get(f"/api/v1/exams/{exam_id}/processing/capabilities", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["capabilities"] == FAKE_IDENTITY_RESPONSE["capabilities"]
        assert body["tenant_exam_name"] == "Test Board"
        assert body["fetched_at"] is not None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_capabilities_endpoint_404_no_link(client):
    """GET /processing/capabilities returns 404 when no link exists."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    resp = await client.get(f"/api/v1/exams/{exam_id}/processing/capabilities", headers=h)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_link_without_processing_enabled(client, monkeypatch):
    """When processing is not enabled (no base URL), identity is not called
    but the link is still saved."""
    h = await _admin(client)
    exam_id = await _create_exam(client, h)

    mock_identity = AsyncMock(return_value=FAKE_IDENTITY_RESPONSE)
    monkeypatch.setattr("app.routers.integration.backend_sis.identity", mock_identity)
    # Ensure BACKEND_SIS_BASE_URL is empty (processing disabled).
    monkeypatch.setenv("BACKEND_SIS_BASE_URL", "")
    from app.config import get_settings
    get_settings.cache_clear()

    try:
        resp = await client.put(
            f"/api/v1/exams/{exam_id}/processing/link",
            json={"api_key": "valid-test-key-12345678"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        # Identity was NOT called because processing is disabled.
        mock_identity.assert_not_called()
        # No capabilities cached.
        assert body["capabilities"] is None
        assert body["tenant_exam_name"] is None
    finally:
        get_settings.cache_clear()
