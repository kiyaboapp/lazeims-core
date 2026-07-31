"""Tests for the ExaMetrics (backend-sis) client layer (C1 completion + D9).

Tests use httpx.MockTransport so no network is needed. They verify:
- New methods send correct paths/headers (upsert_exam, get_entitlements, etc.)
- All mutating calls include an Idempotency-Key header.
- push_collection_auto sends single-shot for small payloads.
- push_collection_auto falls back to chunked on 413 or oversized payload.
- The D9 error mapper preserves upstream status for known codes.
- Unknown upstream statuses become generic EXAMETRICS_ERROR.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.services import backend_sis
from app.services.backend_sis import BackendSisError


# ---- Helpers ----

def _mock_transport(handler):
    """Create a mock transport from a request handler function."""
    return httpx.MockTransport(handler)


def _ok_response(data: dict, status: int = 200):
    return httpx.Response(status, json=data)


def _error_response(status: int, code: str, message: str, details: list | None = None):
    body = {"detail": {"code": code, "message": message}}
    if details:
        body["detail"]["details"] = details
    return httpx.Response(status, json=body)


# ---- Test: Mutating calls send Idempotency-Key ----

@pytest.mark.asyncio
async def test_upsert_exam_sends_idempotency_key():
    captured_headers = {}

    def handler(request: httpx.Request):
        captured_headers.update(dict(request.headers))
        return _ok_response({"exam_ref": "ex-1", "state": "CREATED"})

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        result = await backend_sis.upsert_exam("key123", {"external_ref": "abc"})

    assert "idempotency-key" in captured_headers
    assert result["exam_ref"] == "ex-1"


@pytest.mark.asyncio
async def test_request_processing_sends_idempotency_key():
    captured_headers = {}

    def handler(request: httpx.Request):
        captured_headers.update(dict(request.headers))
        return _ok_response({"request_id": "prq_1", "state": "PENDING_APPROVAL"})

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        result = await backend_sis.request_processing("key123", "ex-1", {"closeout_revision": 1})

    assert "idempotency-key" in captured_headers
    assert result["request_id"] == "prq_1"


# ---- Test: push_collection_auto single-shot for small payloads ----

@pytest.mark.asyncio
async def test_push_collection_auto_single_shot():
    """A small payload goes single-shot."""
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        return _ok_response({"status": "accepted", "digest": "sha256:abc"})

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        payload = {"schools": [], "subjects": [], "students": [], "marks": []}
        result = await backend_sis.push_collection_auto("key123", "ex-1", payload)

    assert len(calls) == 1
    assert "/collection" in calls[0]


# ---- Test: push_collection_auto falls back to chunked on 413 ----

@pytest.mark.asyncio
async def test_push_collection_auto_fallback_on_413():
    """A 413 on single-shot triggers chunked upload."""
    calls = []

    def handler(request: httpx.Request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/collection") and request.method == "POST" and "collection-sessions" not in path:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "too big")
        if "collection-sessions" in path and request.method == "POST" and "complete" not in path:
            return _ok_response({"session_id": "sess-1"})
        if "chunks" in path:
            return _ok_response({"chunk": "ok"})
        if "complete" in path:
            return _ok_response({"status": "applied"})
        return _ok_response({})

    transport = _mock_transport(handler)

    def _make_client(api_key):
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    with patch.object(backend_sis, "_client", side_effect=_make_client):
        payload = {"schools": [{"centre_number": "S0001"}], "subjects": [], "students": [], "marks": []}
        result = await backend_sis.push_collection_auto("key123", "ex-1", payload)

    # Should have: 1 failed collection + 1 session start + N chunks + 1 complete
    assert any("collection-sessions" in c for c in calls)
    assert any("complete" in c for c in calls)


# ---- Test: D9 error mapper preserves known upstream codes ----

@pytest.mark.asyncio
async def test_error_mapper_preserves_403():
    """A 403 SCOPE_NOT_GRANTED surfaces as 403, not 502."""

    def handler(request: httpx.Request):
        return _error_response(403, "SCOPE_NOT_GRANTED", "results:read not approved")

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        with pytest.raises(BackendSisError) as exc_info:
            await backend_sis.get_entitlements("key123", "ex-1")

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "SCOPE_NOT_GRANTED"


@pytest.mark.asyncio
async def test_error_mapper_preserves_429():
    """A 429 surfaces with the upstream code."""

    def handler(request: httpx.Request):
        return _error_response(429, "RATE_LIMITED", "too many requests")

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        with pytest.raises(BackendSisError) as exc_info:
            await backend_sis.identity("key123")

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_error_mapper_unknown_status_becomes_exametrics_error():
    """An unknown upstream status (e.g. 500) becomes EXAMETRICS_ERROR."""

    def handler(request: httpx.Request):
        return httpx.Response(500, json={"detail": "internal error"})

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        with pytest.raises(BackendSisError) as exc_info:
            await backend_sis.get_exam("key123", "ex-1")

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "EXAMETRICS_ERROR"


# ---- Test: get_entitlements, get_processing_request ----

@pytest.mark.asyncio
async def test_get_entitlements():
    def handler(request: httpx.Request):
        assert "/entitlements" in request.url.path
        return _ok_response({"processing": {"state": "APPROVED"}, "results": {"state": "LOCKED"}})

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        result = await backend_sis.get_entitlements("key123", "ex-1")

    assert result["processing"]["state"] == "APPROVED"


@pytest.mark.asyncio
async def test_get_processing_request():
    def handler(request: httpx.Request):
        assert "processing-requests/prq_1" in request.url.path
        return _ok_response({"request_id": "prq_1", "state": "APPROVED"})

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        result = await backend_sis.get_processing_request("key123", "ex-1", "prq_1")

    assert result["state"] == "APPROVED"


# ---- Test: D9 surfaces details array ----

@pytest.mark.asyncio
async def test_error_mapper_passes_details():
    """Row-addressable details from a 422 are preserved."""
    details = [{"student_id": "S001", "subject_code": "MATH", "code": "MARK_EXCEEDS_MAX", "message": "80 > 75"}]

    def handler(request: httpx.Request):
        return _error_response(422, "VALIDATION_FAILED", "1 row error", details)

    transport = _mock_transport(handler)
    with patch.object(backend_sis, "_client", return_value=httpx.AsyncClient(transport=transport, base_url="http://test")):
        with pytest.raises(BackendSisError) as exc_info:
            await backend_sis.push_collection("key123", "ex-1", {})

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "VALIDATION_FAILED"
    assert exc_info.value.details == details
