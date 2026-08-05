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
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx

from lazeims_common.exametrics_digest import chunk_payload, collection_digest

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
    """Client for key issuance.

    No longer requires backend_sis_provision_secret: requests are authenticated
    by an Ed25519 signature of the payload (X-Provision-Signature header). If the
    legacy provision secret is configured, it is still sent as X-Provision-Secret
    for backward compatibility with older backend-sis deployments.
    """
    headers: dict[str, str] = {}
    secret = get_settings().backend_sis_provision_secret.strip()
    if secret:
        headers["X-Provision-Secret"] = secret
    return httpx.AsyncClient(
        base_url=_base_url(),
        headers=headers,
        timeout=get_settings().backend_sis_timeout_seconds,
    )


def _is_html(text: str) -> bool:
    """Detect HTML responses (e.g. Cloudflare error pages)."""
    stripped = text.strip()[:200].lower()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")


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

    if isinstance(body, str) and _is_html(body):
        # Cloudflare or similar proxy returned an HTML error page — don't
        # surface raw HTML to callers or the frontend.
        message = "The ExaMetrics server is not reachable. Please try again later."
        code = "EXAMETRICS_UNREACHABLE"
    elif isinstance(body, dict):
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
        message = str(body) if body else "Unexpected response from ExaMetrics."

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
            code=code if code != "EXAMETRICS_ERROR" else "EXAMETRICS_ERROR",
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


def _wrap_transport_error(exc: httpx.HTTPError) -> BackendSisError:
    """Convert httpx transport/network errors into a clean BackendSisError."""
    if isinstance(exc, httpx.TimeoutException):
        return BackendSisError(
            message="The ExaMetrics server did not respond in time. Please try again later.",
            status_code=None,
            code="EXAMETRICS_UNREACHABLE",
        )
    # ConnectError, ReadError, etc.
    return BackendSisError(
        message="The ExaMetrics server is not reachable. Please try again later.",
        status_code=None,
        code="EXAMETRICS_UNREACHABLE",
    )


def _idempotency_key() -> str:
    """Generate a unique idempotency key per section 7.1."""
    return str(uuid.uuid4())


@asynccontextmanager
async def _safe_client(api_key: str) -> AsyncIterator[httpx.AsyncClient]:
    """Wrap _client with transport error handling."""
    try:
        async with _client(api_key) as client:
            yield client
    except BackendSisError:
        raise
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(exc) from exc


@asynccontextmanager
async def _safe_provision_client() -> AsyncIterator[httpx.AsyncClient]:
    """Wrap _provision_client with transport error handling."""
    try:
        async with _provision_client() as client:
            yield client
    except BackendSisError:
        raise
    except httpx.HTTPError as exc:
        raise _wrap_transport_error(exc) from exc


# ---- Provisioning ----

async def provision_exam(payload: dict) -> dict:
    """Register an exam with ExaMetrics and receive its API key.

    Server-to-server: the response carries the secret exactly once, so it is
    stored on the exam's :class:`ExamProcessingLink` and never returned to a
    browser or written to an audit record.

    Authentication is via Ed25519 signature of the payload (X-Provision-Signature
    header). The legacy X-Provision-Secret is also sent if configured.
    """
    from lazeims_common.signing import sign_provision_request

    signature = sign_provision_request(payload)
    async with _safe_provision_client() as client:
        resp = await client.post(
            "/integration/provision",
            json=payload,
            headers={"X-Provision-Signature": signature},
        )
        return _unwrap(resp) or {}


# ---- Identity / capabilities ----

async def identity(api_key: str) -> dict:
    """Validate a key and fetch the exam account/capabilities via GET /integration/me."""
    async with _safe_client(api_key) as client:
        resp = await client.get("/integration/me")
        return _unwrap(resp)


# ---- Exam management ----

async def upsert_exam(api_key: str, payload: dict) -> dict:
    """PUT /integration/exams - create or update exam configuration."""
    async with _safe_client(api_key) as client:
        resp = await client.put(
            "/integration/exams",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def upsert_exam_definition(
    api_key: str,
    *,
    external_ref: str,
    name: str,
    level: str,
    zone_name: str | None,
    filling_mode: str,
    subjects: list[dict],
) -> dict:
    """Push the exam definition (name, level, subjects with max marks) to
    ExaMetrics via PUT /integration/exams.

    ExaMetrics uses ``theory_max`` / ``theory_2_max`` / ``practical_max`` per
    subject to validate marks and compute grades/GPA. This call must be made:
      - after a key is issued (provisioning or rotation);
      - before a collection is submitted for processing;
      - whenever subjects are patched or re-seeded.

    Idempotent: an identical body returns ``state: "UNCHANGED"`` with no DB
    write on the ExaMetrics side.
    """
    payload = {
        "external_ref": external_ref,
        "name": name,
        "level": level,
        "zone_name": zone_name or None,
        "filling_mode": filling_mode,  # TOTAL_MARKS | ITEM_LEVEL
        "subjects": subjects,
    }
    async with _safe_client(api_key) as client:
        resp = await client.put(
            "/integration/exams",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def get_exam(api_key: str, exam_ref: str) -> dict:
    """GET /integration/exams/{ref} - retrieve exam details."""
    async with _safe_client(api_key) as client:
        resp = await client.get(f"/integration/exams/{exam_ref}")
        return _unwrap(resp) or {}


# ---- Entitlements ----

async def get_entitlements(api_key: str, exam_ref: str) -> dict:
    """GET /integration/exams/{ref}/entitlements - check what this key can do."""
    async with _safe_client(api_key) as client:
        resp = await client.get(f"/integration/exams/{exam_ref}/entitlements")
        return _unwrap(resp) or {}


# ---- Processing requests ----

async def request_processing(api_key: str, exam_ref: str, payload: dict) -> dict:
    """POST /integration/exams/{ref}/processing-requests - request processing approval."""
    async with _safe_client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{exam_ref}/processing-requests",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def get_processing_request(api_key: str, exam_ref: str, request_id: str) -> dict:
    """GET /integration/exams/{ref}/processing-requests/{id} - poll request state."""
    async with _safe_client(api_key) as client:
        resp = await client.get(
            f"/integration/exams/{exam_ref}/processing-requests/{request_id}"
        )
        return _unwrap(resp) or {}


# ---- Collection ----

async def push_collection(api_key: str, backend_exam_id: str, payload: dict) -> Any:
    """POST single-shot collection push."""
    async with _safe_client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{backend_exam_id}/collection",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp)


async def start_collection_session(api_key: str, exam_ref: str, payload: dict) -> dict:
    """POST /integration/exams/{ref}/collection-sessions - start chunked upload."""
    async with _safe_client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{exam_ref}/collection-sessions",
            json=payload,
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp) or {}


async def put_chunk(api_key: str, session_id: str, chunk_number: int, chunk_data: dict) -> dict:
    """PUT /integration/collection-sessions/{id}/chunks/{n} - upload a chunk."""
    async with _safe_client(api_key) as client:
        resp = await client.put(
            f"/integration/collection-sessions/{session_id}/chunks/{chunk_number}",
            json=chunk_data,
        )
        return _unwrap(resp) or {}


async def complete_collection_session(api_key: str, session_id: str, payload: dict) -> dict:
    """POST /integration/collection-sessions/{id}/complete - finalize chunked upload."""
    async with _safe_client(api_key) as client:
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
        "chunk_count": len(chunks),
        "expected_digest": digest,
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
    async with _safe_client(api_key) as client:
        resp = await client.post(
            f"/integration/exams/{backend_exam_id}/process",
            headers={"Idempotency-Key": _idempotency_key()},
        )
        return _unwrap(resp)


# ---- Status ----

async def get_status(api_key: str, backend_exam_id: str) -> Any:
    async with _safe_client(api_key) as client:
        resp = await client.get(f"/integration/exams/{backend_exam_id}/status")
        return _unwrap(resp)


# ---- Results ----

async def get_results_stats(api_key: str, backend_exam_id: str) -> Any:
    async with _safe_client(api_key) as client:
        resp = await client.get(f"/integration/exams/{backend_exam_id}/results/stats")
        return _unwrap(resp)


async def download_school_pdf(
    api_key: str, backend_exam_id: str, centre_number: str, include_marks: bool = True
) -> tuple[bytes, str]:
    async with _safe_client(api_key) as client:
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
    async with _safe_client(api_key) as client:
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
    async with _safe_client(api_key) as client:
        resp = await client.post(
            "/integration/registrations/extract",
            files={"file": (filename, content, content_type or "application/octet-stream")},
        )
        return _unwrap(resp)
