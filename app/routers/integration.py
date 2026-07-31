"""ExaMetrics (backend-sis) processing integration endpoints.

LAZEIMS collects; ExaMetrics processes. An EXAM_ADMIN records the per-exam
ExaMetrics link (backend exam id + purchased API key), submits the collected
data for processing, polls status, publishes results, and downloads processed
outputs -- all proxied through Central so the browser never holds the API key.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import ExamPhase
from lazeims_common.errors import ValidationError

from ..config import get_settings
from ..db import get_session
from ..deps import current_user
from ..deps_exam import require_exam_admin
from ..models.exam import Exam
from ..models.processing import ExamProcessingLink
from ..models.registry import User
from ..schemas_exam import ExamOut
from ..services import backend_sis
from ..services.backend_sis import BackendSisError
from ..services.exam_phase import RESULTS_READY_STATUS, assert_transition_allowed
from ..services.processing_submit import build_collection_payload

router = APIRouter(prefix="/exams", tags=["processing"])


class ProcessingLinkIn(BaseModel):
    backend_exam_id: str | None = Field(None, max_length=36)
    api_key: str = Field(..., min_length=8, max_length=120)


class ProcessingLinkOut(BaseModel):
    configured: bool
    backend_exam_id: str | None = None
    last_submitted_at: datetime | None = None
    last_status: str | None = None
    tenant_name: str | None = None
    capabilities: dict | None = None
    capabilities_fetched_at: datetime | None = None


async def _get_exam(db: AsyncSession, exam_id: uuid.UUID) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    return exam


async def _get_link(db: AsyncSession, exam_id: uuid.UUID) -> ExamProcessingLink | None:
    return (
        await db.execute(select(ExamProcessingLink).where(ExamProcessingLink.exam_id == exam_id))
    ).scalar_one_or_none()


async def _require_link(db: AsyncSession, exam_id: uuid.UUID) -> ExamProcessingLink:
    link = await _get_link(db, exam_id)
    if link is None:
        raise HTTPException(
            409,
            detail={
                "code": "PROCESSING_NOT_CONFIGURED",
                "message": "Configure an API key for this exam before using processing.",
            },
        )
    if link.backend_exam_id is None:
        raise HTTPException(
            409,
            detail={
                "code": "EXAM_NOT_PROVISIONED",
                "message": "Exam provisioning has not completed yet.",
            },
        )
    if not get_settings().processing_enabled:
        raise HTTPException(
            503,
            detail={
                "code": "PROCESSING_DISABLED",
                "message": "This deployment has no ExaMetrics base URL configured.",
            },
        )
    return link


def _sis_http(exc: BackendSisError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"code": "EXAMETRICS_ERROR", "message": str(exc), "upstream_status": exc.status_code},
    )


# ── Link configuration ──────────────────────────────────────────────────────
@router.get("/{exam_id}/processing/link", response_model=ProcessingLinkOut)
async def get_processing_link(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    link = await _get_link(db, exam_id)
    if link is None:
        return ProcessingLinkOut(configured=False)
    return ProcessingLinkOut(
        configured=True,
        backend_exam_id=link.backend_exam_id,
        last_submitted_at=link.last_submitted_at,
        last_status=link.last_status,
        tenant_name=link.tenant_name,
        capabilities=link.capabilities_json,
        capabilities_fetched_at=link.capabilities_fetched_at,
    )


@router.put("/{exam_id}/processing/link", response_model=ProcessingLinkOut)
async def set_processing_link(
    exam_id: uuid.UUID,
    payload: ProcessingLinkIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    await _get_exam(db, exam_id)

    # Validate the API key against ExaMetrics before persisting.
    api_key = payload.api_key.strip()
    identity_data: dict[str, Any] | None = None
    if get_settings().processing_enabled:
        try:
            identity_data = await backend_sis.identity(api_key)
        except BackendSisError as exc:
            raise _sis_http(exc)

    link = await _get_link(db, exam_id)
    backend_exam_id = payload.backend_exam_id.strip() if payload.backend_exam_id else None

    if link is None:
        link = ExamProcessingLink(
            exam_id=exam_id, backend_exam_id=backend_exam_id,
            api_key=api_key, configured_by=user.id,
        )
        db.add(link)
    else:
        link.backend_exam_id = backend_exam_id
        link.api_key = api_key
        link.configured_by = user.id

    # Cache only the capabilities sub-map from the identity response.
    if identity_data is not None:
        link.capabilities_json = identity_data.get("capabilities")
        link.capabilities_fetched_at = datetime.now(timezone.utc)
        tenant = identity_data.get("tenant")
        if isinstance(tenant, dict):
            link.tenant_name = tenant.get("name")

    await db.flush()
    return ProcessingLinkOut(
        configured=True, backend_exam_id=link.backend_exam_id,
        last_submitted_at=link.last_submitted_at, last_status=link.last_status,
        tenant_name=link.tenant_name,
        capabilities=link.capabilities_json,
        capabilities_fetched_at=link.capabilities_fetched_at,
    )


# ── Capabilities ─────────────────────────────────────────────────────────────
@router.get("/{exam_id}/processing/capabilities")
async def get_capabilities(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    """Return cached capabilities for this exam's processing link."""
    link = await _get_link(db, exam_id)
    if link is None or link.capabilities_json is None:
        raise HTTPException(404, detail="No capabilities available for this exam.")
    return {
        "capabilities": link.capabilities_json,
        "tenant_name": link.tenant_name,
        "fetched_at": link.capabilities_fetched_at,
    }


# ── Submit for processing ────────────────────────────────────────────────────
@router.post("/{exam_id}/processing/submit", response_model=ExamOut)
async def submit_for_processing(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Push the collected data to ExaMetrics, trigger processing, and move the
    exam into PROCESSING. Allowed from ENTRY_LOCKED (first run) or PROCESSING
    (re-submit after edits)."""
    exam = await _get_exam(db, exam_id)
    link = await _require_link(db, exam_id)

    if exam.phase not in (ExamPhase.ENTRY_LOCKED, ExamPhase.PROCESSING):
        raise HTTPException(
            409,
            f"Submit is only allowed from ENTRY_LOCKED or PROCESSING (current: {exam.phase.value}).",
        )

    payload = await build_collection_payload(db, exam)
    try:
        await backend_sis.push_collection(link.api_key, link.backend_exam_id, payload)
        proc = await backend_sis.trigger_processing(link.api_key, link.backend_exam_id)
    except BackendSisError as exc:
        raise _sis_http(exc)

    if exam.phase == ExamPhase.ENTRY_LOCKED:
        try:
            await assert_transition_allowed(db, exam, ExamPhase.PROCESSING)
        except ValidationError as exc:
            raise HTTPException(422, detail={"code": exc.code.value, "message": exc.message, "details": exc.details})
        exam.phase = ExamPhase.PROCESSING

    link.last_submitted_at = datetime.now(timezone.utc)
    link.last_status = str((proc or {}).get("status", "SUBMITTED")) if isinstance(proc, dict) else "SUBMITTED"
    await db.flush()
    return ExamOut.model_validate(exam)


# ── Status polling ───────────────────────────────────────────────────────────
@router.get("/{exam_id}/processing/status")
async def processing_status(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    link = await _require_link(db, exam_id)
    try:
        status_payload = await backend_sis.get_status(link.api_key, link.backend_exam_id)
    except BackendSisError as exc:
        raise _sis_http(exc)

    # Cache a "ready to publish" marker locally so publish can be gated offline.
    if isinstance(status_payload, dict) and status_payload.get("results_processed"):
        if link.last_status != RESULTS_READY_STATUS:
            link.last_status = RESULTS_READY_STATUS
            await db.flush()

    return {"local_status": link.last_status, "backend": status_payload}


# ── Publish ──────────────────────────────────────────────────────────────────
@router.post("/{exam_id}/processing/publish", response_model=ExamOut)
async def publish_results(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    link = await _require_link(db, exam_id)

    # Refresh readiness from ExaMetrics before publishing.
    try:
        status_payload = await backend_sis.get_status(link.api_key, link.backend_exam_id)
    except BackendSisError as exc:
        raise _sis_http(exc)
    if isinstance(status_payload, dict) and status_payload.get("results_processed"):
        link.last_status = RESULTS_READY_STATUS
        await db.flush()

    try:
        await assert_transition_allowed(db, exam, ExamPhase.RESULTS_PUBLISHED)
    except ValidationError as exc:
        raise HTTPException(422, detail={"code": exc.code.value, "message": exc.message, "details": exc.details})
    exam.phase = ExamPhase.RESULTS_PUBLISHED
    await db.flush()
    return ExamOut.model_validate(exam)


# ── Results (read + downloads) ───────────────────────────────────────────────
@router.get("/{exam_id}/results/stats")
async def results_stats(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    link = await _require_link(db, exam_id)
    try:
        return await backend_sis.get_results_stats(link.api_key, link.backend_exam_id)
    except BackendSisError as exc:
        raise _sis_http(exc)


@router.get("/{exam_id}/results/school/{centre_number}.pdf")
async def download_school_pdf(
    exam_id: uuid.UUID,
    centre_number: str,
    include_marks: bool = True,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    link = await _require_link(db, exam_id)
    try:
        content, media_type = await backend_sis.download_school_pdf(
            link.api_key, link.backend_exam_id, centre_number, include_marks
        )
    except BackendSisError as exc:
        raise _sis_http(exc)
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="results_{centre_number}.pdf"'},
    )


@router.get("/{exam_id}/results/rawdata.xlsx")
async def download_rawdata(
    exam_id: uuid.UUID,
    centre_number: str | None = None,
    region_name: str | None = None,
    council_name: str | None = None,
    ward_name: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    link = await _require_link(db, exam_id)
    try:
        content, media_type = await backend_sis.download_rawdata(
            link.api_key, link.backend_exam_id,
            centre_number=centre_number, region_name=region_name,
            council_name=council_name, ward_name=ward_name,
        )
    except BackendSisError as exc:
        raise _sis_http(exc)
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": 'attachment; filename="rawdata.xlsx"'},
    )


# ── Free registration extraction (PDF/ZIP → rows) ────────────────────────────
@router.post("/{exam_id}/registrations/extract")
async def extract_registrations(
    exam_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Extract student registrations from an uploaded PDF/ZIP via ExaMetrics.
    Nothing is stored in ExaMetrics; the parsed rows are returned so Central can
    register the students itself."""
    link = await _require_link(db, exam_id)
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        return await backend_sis.extract_registrations(
            link.api_key, file.filename or "upload", content, file.content_type or "application/octet-stream"
        )
    except BackendSisError as exc:
        raise _sis_http(exc)
