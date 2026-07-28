"""Thin async client for the ExaMetrics (backend-sis) integration API.

Every call is authenticated with the per-exam ``X-API-Key`` and targets the
ExaMetrics ``exam_id`` recorded in the exam's :class:`ExamProcessingLink`. This
module knows nothing about Central's own persistence — callers pass the resolved
base URL / key / backend exam id.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from ..config import get_settings


class BackendSisError(RuntimeError):
    """Raised when the ExaMetrics API returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


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


def _unwrap(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        msg = body
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error")
            if isinstance(detail, dict):
                msg = detail.get("message") or detail
            elif detail:
                msg = detail
        raise BackendSisError(f"ExaMetrics error: {msg}", resp.status_code, body)
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.content


async def extract_registrations(api_key: str, filename: str, content: bytes, content_type: str) -> Any:
    """Free PDF/ZIP → rows extraction. Nothing is persisted in ExaMetrics."""
    async with _client(api_key) as client:
        resp = await client.post(
            "/integration/registrations/extract",
            files={"file": (filename, content, content_type or "application/octet-stream")},
        )
        return _unwrap(resp)


async def push_collection(api_key: str, backend_exam_id: str, payload: dict) -> Any:
    async with _client(api_key) as client:
        resp = await client.post(f"/integration/exams/{backend_exam_id}/collection", json=payload)
        return _unwrap(resp)


async def trigger_processing(api_key: str, backend_exam_id: str) -> Any:
    async with _client(api_key) as client:
        resp = await client.post(f"/integration/exams/{backend_exam_id}/process")
        return _unwrap(resp)


async def get_status(api_key: str, backend_exam_id: str) -> Any:
    async with _client(api_key) as client:
        resp = await client.get(f"/integration/exams/{backend_exam_id}/status")
        return _unwrap(resp)


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
