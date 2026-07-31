"""Thin async client for the ExaMetrics (backend-sis) integration API.

Every call is authenticated with the per-exam ``X-API-Key`` and targets the
ExaMetrics ``exam_id`` recorded in the exam's :class:`ExamProcessingLink`. This
module knows nothing about Central's own persistence -- callers pass the resolved
base URL / key / backend exam id.

Error handling follows D9: upstream error codes from section 8's table
(401/403/402/409/413/422/429/503) are preserved so callers can act on them.
Only truly unknown failures fall back to a generic EXAMETRICS_ERROR.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Optional

import httpx

from lazeims_common.exametrics_digest import chunk_manifest, chunk_payload, collection_digest

from ..config import get_settings

# Maximum payload size in MB before switching to chunked upload.
_MAX_PAYLOAD_MB = 25
# Chunk size in rows (matching backend-sis's max_collection_chunk_size).
_CHUNK_MAX_ROWS = 5000


class BackendSisError(RuntimeError):
    """Raised when the ExaMetrics API returns an error or is unreachable."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any = None,
        code: str | None = None,
        details: list | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.code = code
        self.details = details


# Status codes whose meaning is defined by section 8 and should be preserved.
_PASSTHROUGH_CODES = {401, 402, 403, 409, 413, 422, 429, 503}


def _base_url() -> str:
    base = get_settings().backend_sis_base_url.strip().rstrip("/")
    if not base:
        raise BackendSisError("Processing is not configured (backend_sis_base_url is empty).")
    return base


def _client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_base_url(),
        headers={"X-API-Key": api_key},
        timeout=get_settings().backend_sis_timeout_seconds,
    )


def _provision_client() -> httpx.AsyncClient:
    """Client for key issuance, authenticated by the zone enrolment secret.

    Deliberately carries no ``X-API-Key``: this is how Central obtains one in the
    first place.
    """
    secret = get_settings().backend_sis_provision_secret.strip()
    if not secret:
        raise BackendSisError("Key provisioning is not configured (backend_sis_provision_secret is empty).")
    return httpx.AsyncClient(
        base_url=_base_url(),
        headers={"X-Provision-Secret": secret},
        timeout=get_settings().backend_sis_timeout_seconds,
    )


def _extract_error(resp: httpx.Response) -> BackendSisError:
    """Build a BackendSisError preserving the upstream code for known statuses (D9)."""
    try:
        body = resp.json()
    except Exception:
        body = resp.text

    # Extract structured fields from the response.
    code: str | None = None
    message: str = ""
    details: list | None = None

    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error")
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message") or str(detail)
            details = detail.get("details")
        elif isinstance(detail, str):
            message = detail
        else:
            message = str(body)
    else:
        message = str(body)

    if not code:
        code = "EXAMETRICS_ERROR"

    if resp.status_code in _PASSTHROUGH_CODES:
        # Preserve the upstream status and code so callers can act on them.
        return BackendSisError(
            message=f"ExaMetrics error: {message}",
            status_code=resp.status_code,
            payload=body,
            code=code,
            details=details,
        )
    else:
        # Unknown upstream status: wrap as 502.
        return BackendSisError(
            message=f"ExaMetrics error: {message}",
            status_code=resp.status_code,
            payload=body,
            code="EXAMETRICS_ERROR",
            details=details,
        )


def _unwrap(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise _extract_error(resp)
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.content


def _idempotency_key() -> str:
    """Generate a unique idempotency key per section 7.1."""
    return str(uuid.uuid4())


# ---- Provisioning ----

async def provision_exam(payload: dict) -> dict:
    """Register an exam with ExaMetrics and receive its API key.

    Server-to-server: the response carries the secret exactly once, so it is
    stored on the exam's :class:`ExamProcessingLink` and never returned to a
    browser or written to an audit record.
    """
    async with _provision_client() as client:
        resp = await client.post("/integration/provision", json=payload)
        return _unwrap(resp) or {}


# ---- Identity / capabilities ----

async def identity(api_key: str) -> dict:
    """Validate a key and fetch the exam account/capabilities via GET /integration/me."""
    async with _client(api_key) as client:
        resp = await client.get("/integration/me")
        return _unwrap(resp)


# ---- Exam management ----

async def upsert_exam(api_key: str, payload: dict) -> dict:
    """PUT /integration/exams - create or update exam configuration."""
    async with _client(api_key) as client:
        resp = await client.put(
            "/integration/exams",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def get_exam(api_key: str, exam_ref: str) -> dict:
    """GET /integration/exams/{ref} - retrieve exam details."""
    async with _client(api_key) as client:
        resp = await client.get(f"/integration/exams/{exam_ref}")
        return _unwrap(resp) or {}


# ---- Entitlements ----

async def get_entitlements(api_key: str, exam_ref: str) -> dict:
    """GET /integration/exams/{ref}/entitlements - check what this key can do."""
    async with _client(api_key) as client:
        resp = await client.get(f"/integration/exams/{exam_ref}/entitlements")
        return _unwrap(resp) or {}


# ---- Processing requests ----

async def request_processing(api_key: str, exam_ref: str, payload: dict) -> dict:
    """POST /integration/exams/{ref}/processing-requests - request processing approval."""
    async with _client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{exam_ref}/processing-requests",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def get_processing_request(api_key: str, exam_ref: str, request_id: str) -> dict:
    """GET /integration/exams/{ref}/processing-requests/{id} - poll request state."""
    async with _client(api_key) as client:
        resp = await client.get(
            f"/integration/exams/{exam_ref}/processing-requests/{request_id}"
        )
        return _unwrap(resp) or {}


# ---- Collection ----

async def push_collection(api_key: str, backend_exam_id: str, payload: dict) -> Any:
    """POST single-shot collection push."""
    async with _client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{backend_exam_id}/collection",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp)


async def start_collection_session(api_key: str, exam_ref: str, payload: dict) -> dict:
    """POST /integration/exams/{ref}/collection-sessions - start chunked upload."""
    async with _client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{exam_ref}/collection-sessions",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def put_chunk(api_key: str, session_id: str, chunk_number: int, chunk_data: dict) -> dict:
    """PUT /integration/collection-sessions/{id}/chunks/{n} - upload a chunk."""
    async with _client(api_key) as client:
        resp = await client.put(
            f"/integration/collection-sessions/{session_id}/chunks/{chunk_number}",
            json=chunk_data,
        )
        return _unwrap(resp) or {}


async def complete_collection_session(api_key: str, session_id: str, payload: dict) -> dict:
    """POST /integration/collection-sessions/{id}/complete - finalize chunked upload."""
    async with _client(api_key) as client:
        resp = await client.post(
            f"/integration/collection-sessions/{session_id}/complete",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def push_collection_auto(api_key: str, exam_ref: str, payload: dict) -> Any:
    """Push a collection payload, automatically switching to chunked upload
    when the payload exceeds max_payload_mb or when a 413 is received.

    This is the preferred entry point for collection submission.
    """
    payload_bytes = json.dumps(payload).encode()
    payload_size_mb = len(payload_bytes) / (1024 * 1024)

    if payload_size_mb <= _MAX_PAYLOAD_MB:
        # Try single-shot first.
        try:
            return await push_collection(api_key, exam_ref, payload)
        except BackendSisError as exc:
            if exc.status_code != 413:
                raise
            # Fall through to chunked path on 413.

    # Chunked upload path.
    digest = collection_digest(payload)
    chunks = chunk_payload(payload, _CHUNK_MAX_ROWS)
    manifest = chunk_manifest(chunks)

    session_payload = {
        "total_chunks": len(chunks),
        "digest": digest,
        "manifest": manifest,
    }
    session = await start_collection_session(api_key, exam_ref, session_payload)
    session_id = session.get("session_id") or session.get("id", "")

    for i, chunk_data in enumerate(chunks, start=1):
        await put_chunk(api_key, session_id, i, chunk_data)

    complete_payload = {"digest": digest}
    return await complete_collection_session(api_key, session_id, complete_payload)


# ---- Processing trigger ----

async def trigger_processing(api_key: str, backend_exam_id: str) -> Any:
    """POST /integration/exams/{ref}/process - trigger processing run."""
    async with _client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{backend_exam_id}/process",
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp)


# ---- Status ----

async def get_status(api_key: str, backend_exam_id: str) -> Any:
    async with _client(api_key) as client:
        resp = await client.get(f"/integration/exams/{backend_exam_id}/status")
        return _unwrap(resp)


# ---- Results ----

async def get_results_stats(api_key: str, backend_exam_id: str) -> Any:
    async with _client(api_key) as client:
        resp = await client.get(f"/integration/exams/{backend_exam_id}/results/stats")
        return _unwrap(resp)


async def download_school_pdf(
    api_key: str, backend_exam_id: str, centre_number: str, include_marks: bool = True
) -> tuple[bytes, str]:
    async with _client(api_key) as client:
        resp = await client.get(
            f"/integration/exams/{backend_exam_id}/results/school/{centre_number}.pdf",
            params={"include_marks": include_marks},
        )
        if resp.status_code >= 400:
            _unwrap(resp)
        return resp.content, resp.headers.get("content-type", "application/pdf")


async def download_rawdata(
    api_key: str, backend_exam_id: str, **params: Optional[str]
) -> tuple[bytes, str]:
    clean = {k: v for k, v in params.items() if v is not None}
    async with _client(api_key) as client:
        resp = await client.get(
            f"/integration/exams/{backend_exam_id}/results/rawdata.xlsx", params=clean
        )
        if resp.status_code >= 400:
            _unwrap(resp)
        return resp.content, resp.headers.get(
            "content-type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ---- Free extraction ----

async def extract_registrations(api_key: str, filename: str, content: bytes, content_type: str) -> Any:
    """Free PDF/ZIP -> rows extraction. Nothing is persisted in ExaMetrics."""
    async with _client(api_key) as client:
        resp = await client.post(
            "/integration/registrations/extract",
            files={"file": (filename, content, content_type or "application/octet-stream")},
        )
        return _unwrap(resp)
