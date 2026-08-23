"""ExaMetrics (backend-sis) processing integration endpoints.

LAZEIMS collects; ExaMetrics processes. Central obtains each exam's ExaMetrics
key itself — on exam creation, or on demand through
``POST /exams/{exam_id}/processing/access-key`` — so an EXAM_ADMIN never handles
a key. They submit the collected data for processing, poll status, publish
results and download processed outputs, all proxied through Central so the
browser never holds the key. ``PUT /processing/link`` remains as a support-only
escape hatch for a hand-issued key.
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
from ..models.processing import ExamProcessingLink, ExamProcessingRequest
from ..models.registry import User, ExamLevel
from ..schemas_exam import ExamOut
from ..services import backend_sis
from ..services.backend_sis import BackendSisError
from ..services.exam_phase import RESULTS_READY_STATUS, assert_transition_allowed
from ..services.exametrics_provision import (
    KEY_SOURCE_PROVISIONED,
    PROVISIONING_DISABLED,
    ensure_access_key,
)
from ..services.processing_submit import build_collection_payload, build_school_payloads

router = APIRouter(prefix="/exams", tags=["processing"])


class ProcessingLinkIn(BaseModel):
    backend_exam_id: str | None = Field(None, max_length=36)
    api_key: str = Field(..., min_length=8, max_length=120)


class ProcessingLinkOut(BaseModel):
    """Everything the UI may know about a link. Deliberately no ``api_key``:
    ``key_prefix`` is the only key-derived value that may be displayed."""

    configured: bool
    backend_exam_id: str | None = None
    last_submitted_at: datetime | None = None
    last_status: str | None = None
    tenant_exam_name: str | None = None
    capabilities: dict | None = None
    capabilities_fetched_at: datetime | None = None
    key_source: str | None = None
    key_prefix: str | None = None
    key_issued_at: datetime | None = None
    approval_status: str | None = None
    approval_note: str | None = None


def _link_out(link: ExamProcessingLink) -> ProcessingLinkOut:
    return ProcessingLinkOut(
        configured=True,
        backend_exam_id=link.backend_exam_id,
        last_submitted_at=link.last_submitted_at,
        last_status=link.last_status,
        tenant_exam_name=link.tenant_exam_name,
        capabilities=link.capabilities_json,
        capabilities_fetched_at=link.capabilities_fetched_at,
        key_source=link.key_source,
        key_prefix=link.key_prefix,
        key_issued_at=link.key_issued_at,
        approval_status=link.approval_status,
        approval_note=link.approval_note,
    )


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
    """Map a BackendSisError to an HTTPException preserving upstream codes (D9).

    Known upstream statuses (401/402/403/409/413/422/429/503) are preserved so
    the caller and the frontend can act on them. Unknown statuses become 502.
    """
    # Use the upstream status if it is in the passthrough set, else 502.
    passthrough = {401, 402, 403, 409, 413, 422, 429, 503}
    status = exc.status_code if exc.status_code in passthrough else 502
    detail: dict[str, Any] = {
        "code": exc.code or "EXAMETRICS_ERROR",
        "message": str(exc),
    }
    if exc.details:
        detail["details"] = exc.details
    if exc.status_code and exc.status_code not in passthrough:
        detail["upstream_status"] = exc.status_code
    return HTTPException(status_code=status, detail=detail)


# ── Link configuration ──────────────────────────────────────────────────────
@router.get("/{exam_id}/processing/link", response_model=ProcessingLinkOut)
async def get_processing_link(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    link = await _get_link(db, exam_id)
    if link is None:
        return ProcessingLinkOut(configured=False)
    return _link_out(link)


@router.post("/{exam_id}/processing/access-key", response_model=ProcessingLinkOut)
async def request_access_key(
    exam_id: uuid.UUID,
    rotate: bool = False,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Ask ExaMetrics for this exam's access key.

    The normal path is automatic — an exam created after this shipped already has
    a key — so this exists for exams that predate it and for re-issuing one.
    Nothing is pasted: Central sends the exam details plus the requester's
    identity and stores the key it gets back. ``rotate=true`` replaces the key in
    place.

    Collection works as soon as the key arrives; processing and results wait for
    ExaMetrics to approve the paid scopes, which ``approval_status`` reports.
    """
    exam = await _get_exam(db, exam_id)
    link, failure = await ensure_access_key(db, exam, user, rotate=rotate)
    if failure == PROVISIONING_DISABLED:
        raise HTTPException(
            503,
            detail={
                "code": PROVISIONING_DISABLED,
                "message": (
                    "ExaMetrics integration is not configured on this deployment "
                    "(no ExaMetrics URL is set)."
                ),
            },
        )
    if link is None:
        raise _sis_http(BackendSisError(failure or "Key provisioning failed."))
    return _link_out(link)


@router.put("/{exam_id}/processing/link", response_model=ProcessingLinkOut)
async def set_processing_link(
    exam_id: uuid.UUID,
    payload: ProcessingLinkIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Record a hand-issued key. Support escape hatch only.

    The normal path is ``POST /processing/access-key``, which asks ExaMetrics for
    a key so nobody has to hold one. This stays for the cases where ExaMetrics
    staff issued a key out of band; the link is marked ``MANUAL`` so automatic
    re-provisioning leaves it alone.
    """
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

    # A hand-entered key is marked MANUAL so re-provisioning never clobbers it.
    link.key_source = "MANUAL"
    link.key_prefix = None

    # Cache only the capabilities sub-map from the identity response.
    if identity_data is not None:
        link.capabilities_json = identity_data.get("capabilities")
        link.capabilities_fetched_at = datetime.now(timezone.utc)
        tenant_exam = identity_data.get("tenant_exam") or identity_data.get("tenant")
        if isinstance(tenant_exam, dict):
            link.tenant_exam_name = tenant_exam.get("name")
        link.approval_status = identity_data.get("approval_status")
        link.approval_note = identity_data.get("approval_note")
        link.key_prefix = identity_data.get("key_prefix")

    await db.flush()
    return _link_out(link)


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
        "tenant_exam_name": link.tenant_exam_name,
        "fetched_at": link.capabilities_fetched_at,
    }


# ── Entitlements ─────────────────────────────────────────────────────────────
@router.get("/{exam_id}/processing/entitlements")
async def get_entitlements(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    """Fetch entitlements from ExaMetrics for this exam."""
    link = await _get_link(db, exam_id)
    if link is None or not link.api_key_encrypted:
        return {
            "exam_ref": str(exam_id),
            "closeout_revision": None,
            "processing": {"state": "NONE", "reason": "NOT_CONFIGURED"},
            "results": {"state": "LOCKED", "reason": "NOT_CONFIGURED"},
        }
    try:
        from ..services import backend_sis
        data = await backend_sis.get_entitlements(link.api_key, link.backend_exam_id)
        return data
    except Exception:
        return {
            "exam_ref": str(exam_id),
            "closeout_revision": None,
            "processing": {"state": "NONE", "reason": "UNAVAILABLE"},
            "results": {"state": "LOCKED", "reason": "UNAVAILABLE"},
        }


# ── Submit for processing ────────────────────────────────────────────────────
@router.post("/{exam_id}/processing/push")
async def push_collection_data(
    exam_id: uuid.UUID,
    force: bool = False,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Push the current collected data to ExaMetrics without triggering processing.

    Allowed at any time once the exam has a configured link — use this to sync
    data daily or whenever convenient. No phase restriction: the backend accepts
    data at any point and ExaMetrics stores the latest snapshot.

    When force=False (default), only changed rows since the last push are sent.
    When force=True, the diff cache is cleared and everything is re-pushed.

    The heavy work (loading data, computing diffs, uploading) is done in a
    background Celery task. This endpoint returns immediately with a task_id.
    """
    exam = await _get_exam(db, exam_id)

    existing = await _get_link(db, exam_id)
    if existing is None or (
        existing.backend_exam_id is None and existing.key_source == KEY_SOURCE_PROVISIONED
    ):
        await ensure_access_key(db, exam, user, rotate=existing is not None)

    link = await _require_link(db, exam_id)

    # Count schools that have marks entered (only these are worth pushing)
    from sqlalchemy import func, distinct
    from ..models.exam import ExamStudent, ExamStudentSubject
    from ..models.marks import TotalMark
    school_count = (
        await db.execute(
            select(func.count(distinct(ExamStudent.school_id)))
            .select_from(TotalMark)
            .join(ExamStudentSubject, ExamStudentSubject.id == TotalMark.exam_student_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).scalar() or 0

    if school_count == 0:
        return {"status": "no_data", "task_id": None, "total_schools": 0}

    # Kick off the Celery task — all heavy lifting happens there
    from ..tasks.push_collection import push_collection_chunked

    task = push_collection_chunked.delay(
        exam_id=str(exam_id),
        api_key=link.api_key,
        backend_exam_id=link.backend_exam_id,
        force=force,
    )

    link.last_submitted_at = datetime.now(timezone.utc)
    link.last_status = "PUSHING"
    link.active_task_id = task.id
    await db.flush()

    return {"task_id": task.id, "total_schools": school_count}


@router.get("/{exam_id}/processing/push-preview")
async def push_preview(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    """Preview what would be pushed: summary counts without loading full data.

    Returns total enrolled schools, how many have been pushed before (cached),
    and how many are new. Lightweight — no full payload building.
    """
    from sqlalchemy import func, distinct, union
    from ..models.exam import ExamSchool, ExamStudent, ExamStudentSubject
    from ..models.push_cache import CollectionPushCache
    from ..models.registry import School

    await _get_exam(db, exam_id)
    link = await _get_link(db, exam_id)
    if link is None or not link.api_key_encrypted:
        raise HTTPException(409, detail="Processing link not configured.")

    # Count schools that have any marks data (TotalMark, ItemMark, or Attendance)
    # This matches what build_collection_payload actually sends.
    from ..models.marks import TotalMark, ItemMark, Attendance

    schools_with_total = (
        select(distinct(ExamStudent.school_id))
        .select_from(TotalMark)
        .join(ExamStudentSubject, ExamStudentSubject.id == TotalMark.exam_student_subject_id)
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    schools_with_item = (
        select(distinct(ExamStudent.school_id))
        .select_from(ItemMark)
        .join(ExamStudentSubject, ExamStudentSubject.id == ItemMark.exam_student_subject_id)
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    schools_with_attendance = (
        select(distinct(ExamStudent.school_id))
        .select_from(Attendance)
        .join(ExamStudentSubject, ExamStudentSubject.id == Attendance.exam_student_subject_id)
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    schools_with_data_subq = union(schools_with_total, schools_with_item, schools_with_attendance).subquery()

    total_schools = (await db.execute(
        select(func.count()).select_from(schools_with_data_subq)
    )).scalar() or 0

    # Count students in those schools (push sends all students in a school with data)
    total_students = (await db.execute(
        select(func.count())
        .select_from(ExamStudent)
        .where(ExamStudent.exam_id == exam_id, ExamStudent.school_id.in_(select(schools_with_data_subq)))
    )).scalar() or 0

    # Count marks rows that would be sent (student-subject pairs with any data)
    # A marks row is sent when it has TotalMark, ItemMark, or Attendance data.
    ess_with_total = (
        select(distinct(TotalMark.exam_student_subject_id))
        .join(ExamStudentSubject, ExamStudentSubject.id == TotalMark.exam_student_subject_id)
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    ess_with_item = (
        select(distinct(ItemMark.exam_student_subject_id))
        .join(ExamStudentSubject, ExamStudentSubject.id == ItemMark.exam_student_subject_id)
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    ess_with_att = (
        select(distinct(Attendance.exam_student_subject_id))
        .join(ExamStudentSubject, ExamStudentSubject.id == Attendance.exam_student_subject_id)
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    ess_with_data_subq = union(ess_with_total, ess_with_item, ess_with_att).subquery()
    total_marks = (await db.execute(
        select(func.count()).select_from(ess_with_data_subq)
    )).scalar() or 0

    # Count cached entries (already pushed)
    cached_schools = (await db.execute(
        select(func.count(distinct(CollectionPushCache.centre_number)))
        .where(CollectionPushCache.exam_id == exam_id, CollectionPushCache.entity_type == "school")
    )).scalar() or 0

    cached_students = (await db.execute(
        select(func.count())
        .select_from(CollectionPushCache)
        .where(CollectionPushCache.exam_id == exam_id, CollectionPushCache.entity_type == "student")
    )).scalar() or 0

    cached_marks = (await db.execute(
        select(func.count())
        .select_from(CollectionPushCache)
        .where(CollectionPushCache.exam_id == exam_id, CollectionPushCache.entity_type == "marks")
    )).scalar() or 0

    # Last push time
    last_push = (await db.execute(
        select(func.max(CollectionPushCache.pushed_at))
        .where(CollectionPushCache.exam_id == exam_id)
    )).scalar()

    return {
        "total_schools": total_schools,
        "total_students": total_students,
        "total_marks": total_marks,
        "cached_schools": cached_schools,
        "cached_students": cached_students,
        "cached_marks": cached_marks,
        "new_schools": total_schools - cached_schools,
        "new_students": total_students - cached_students,
        "new_or_changed_marks": total_marks - cached_marks,
        "last_pushed_at": last_push.isoformat() if last_push else None,
        "has_changes": (total_schools > cached_schools) or (total_students > cached_students) or (total_marks > cached_marks),
    }


@router.get("/{exam_id}/processing/push-progress")
async def push_progress(
    exam_id: uuid.UUID,
    task_id: str | None = None,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    from celery.result import AsyncResult
    from ..celery_app import celery_app

    # If no task_id provided, look up the persisted one from the link.
    if not task_id:
        link = await _get_link(db, exam_id)
        if link and link.active_task_id:
            task_id = link.active_task_id
    if not task_id:
        return {"state": "IDLE", "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": [], "error": None}

    result = AsyncResult(task_id, app=celery_app)
    if result.state == "PENDING":
        return {"state": "PENDING", "task_id": task_id, "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": [], "error": None}
    elif result.state == "PROGRESS":
        return {"state": "PROGRESS", "task_id": task_id, **result.info}
    elif result.state == "SUCCESS":
        return {"state": "SUCCESS", "task_id": task_id, **result.result}
    elif result.state == "FAILURE":
        return {"state": "FAILURE", "task_id": task_id, "error": str(result.result), "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": []}
    elif result.state == "REVOKED":
        return {"state": "CANCELLED", "task_id": task_id, "error": "Push was cancelled.", "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": []}
    else:
        return {"state": result.state, "task_id": task_id, "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": [], "error": None}


@router.post("/{exam_id}/processing/push-cancel")
async def cancel_push(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(require_exam_admin()),
):
    """Cancel a running push task."""
    from celery.result import AsyncResult
    from ..celery_app import celery_app
    from sqlalchemy import update as sa_update

    link = await _get_link(db, exam_id)
    task_id = link.active_task_id if link else None
    if not task_id:
        raise HTTPException(404, detail="No active push task found.")

    result = AsyncResult(task_id, app=celery_app)
    if result.state in ("SUCCESS", "FAILURE", "REVOKED"):
        # Clear DB state so UI doesn't keep polling
        await db.execute(
            sa_update(ExamProcessingLink)
            .where(ExamProcessingLink.exam_id == exam_id)
            .values(last_status="IDLE", active_task_id=None)
        )
        await db.commit()
        return {"status": "already_finished", "state": result.state, "task_id": task_id}

    celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")

    # Clear DB state so UI stops polling on next page load
    await db.execute(
        sa_update(ExamProcessingLink)
        .where(ExamProcessingLink.exam_id == exam_id)
        .values(last_status="IDLE", active_task_id=None)
    )
    await db.commit()

    return {"status": "cancelled", "task_id": task_id}


@router.post("/{exam_id}/processing/submit", response_model=ExamOut)
async def submit_for_processing(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Push the collected data to ExaMetrics AND trigger processing. This
    transitions the exam into PROCESSING and requires ENTRY_LOCKED (closeout).

    For pushing data without triggering processing (e.g. daily syncs), use
    POST /{exam_id}/processing/push instead.
    """
    exam = await _get_exam(db, exam_id)

    if exam.phase not in (ExamPhase.ENTRY_LOCKED, ExamPhase.PROCESSING):
        raise HTTPException(
            409,
            detail={
                "code": "PHASE_REQUIRED",
                "message": "Triggering processing requires the exam to be sealed (ENTRY_LOCKED). "
                           "To push data without processing, use the 'Push data' action instead.",
            },
        )

    existing = await _get_link(db, exam_id)
    if existing is None or (
        existing.backend_exam_id is None and existing.key_source == KEY_SOURCE_PROVISIONED
    ):
        await ensure_access_key(db, exam, user, rotate=existing is not None)

    link = await _require_link(db, exam_id)

    # Sync the exam definition before pushing collection. Best-effort.
    from ..services.exametrics_provision import build_subjects_payload
    try:
        level = await db.get(ExamLevel, exam.level_id)
        level_name = (level.name if level else "").upper()
        subjects = await build_subjects_payload(db, exam)
        filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
        await backend_sis.upsert_exam_definition(
            link.api_key,
            external_ref=str(exam.id),
            name=exam.name,
            level=level_name,
            zone_name=get_settings().zone_name or None,
            filling_mode=filling_mode,
            subjects=subjects,
        )
    except BackendSisError:
        pass
    except Exception:  # noqa: BLE001
        pass

    payload = await build_collection_payload(db, exam)
    try:
        # Try triggering processing directly — data was likely already pushed.
        proc = await backend_sis.trigger_processing(link.api_key, link.backend_exam_id)
    except BackendSisError as exc:
        # If ExaMetrics requires a fresh push before processing, push then trigger.
        if exc.code and "COLLECTION" in exc.code.upper():
            try:
                await backend_sis.push_collection(link.api_key, link.backend_exam_id, payload)
                proc = await backend_sis.trigger_processing(link.api_key, link.backend_exam_id)
            except BackendSisError as exc2:
                raise _sis_http(exc2)
        else:
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
    register the students itself.

    Unlike processing endpoints, extraction does not require backend_exam_id
    because the call is free and stateless (no exam path on the ExaMetrics side).
    """
    # Extraction is stateless on the ExaMetrics side — it parses rows and returns
    # them without persisting anything. It only needs a valid api_key, NOT a
    # backend_exam_id, so _get_link() is correct here (not _require_link()).
    link = await _get_link(db, exam_id)
    if link is None:
        raise HTTPException(
            409,
            detail={
                "code": "PROCESSING_NOT_CONFIGURED",
                "message": "Configure an API key for this exam before using extraction.",
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
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        return await backend_sis.extract_registrations(
            link.api_key, file.filename or "upload", content, file.content_type or "application/octet-stream"
        )
    except BackendSisError as exc:
        raise _sis_http(exc)


# ── Processing Requests (C6) ─────────────────────────────────────────────────

class ProcessingRequestOut(BaseModel):
    id: int
    request_id: str
    state: str
    closeout_revision: int
    configuration_hash: str
    quote_json: dict | None = None
    run_id: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    last_polled_at: datetime | None = None


@router.post("/{exam_id}/processing/requests")
async def create_processing_request(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Request processing approval from ExaMetrics for this exam's current
    sealed snapshot (closeout_revision + configuration_hash).

    Idempotent on (exam, revision, hash) -- a repeat returns the existing
    request rather than creating a second one.
    """
    from ..models.collection import CollectionSnapshot

    exam = await _get_exam(db, exam_id)
    link = await _require_link(db, exam_id)

    # Get the current sealed snapshot.
    snap = (
        await db.execute(
            select(CollectionSnapshot)
            .where(
                CollectionSnapshot.exam_id == exam_id,
                CollectionSnapshot.status == "SEALED",
            )
            .order_by(CollectionSnapshot.closeout_revision.desc())
        )
    ).scalars().first()
    if snap is None:
        raise HTTPException(
            409,
            detail={
                "code": "NO_SEALED_SNAPSHOT",
                "message": "Seal a collection snapshot before requesting processing.",
            },
        )

    revision = snap.closeout_revision
    config_hash = snap.configuration_hash

    # Check for existing request at this revision+hash (idempotent).
    existing = (
        await db.execute(
            select(ExamProcessingRequest).where(
                ExamProcessingRequest.exam_id == exam_id,
                ExamProcessingRequest.closeout_revision == revision,
                ExamProcessingRequest.configuration_hash == config_hash,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ProcessingRequestOut(
            id=existing.id, request_id=existing.request_id, state=existing.state,
            closeout_revision=existing.closeout_revision,
            configuration_hash=existing.configuration_hash,
            quote_json=existing.quote_json, run_id=existing.run_id,
            decided_at=existing.decided_at, decision_reason=existing.decision_reason,
            last_polled_at=existing.last_polled_at,
        )

    # Call ExaMetrics to create the request.
    try:
        remote = await backend_sis.request_processing(
            link.api_key, link.backend_exam_id,
            {"closeout_revision": revision, "configuration_hash": config_hash, "external_ref": str(exam_id)},
        )
    except BackendSisError as exc:
        raise _sis_http(exc)

    req = ExamProcessingRequest(
        exam_id=exam_id,
        request_id=remote.get("request_id", str(uuid.uuid4())),
        state=remote.get("state", "PENDING_APPROVAL"),
        closeout_revision=revision,
        configuration_hash=config_hash,
        quote_json=remote.get("quote"),
        requested_by=user.id,
        last_polled_at=datetime.now(timezone.utc),
    )
    db.add(req)
    await db.flush()

    # Audit the request creation.
    from ..services import notifications as notif
    await notif.record(
        db, action="PROCESSING_REQUESTED", entity_type="exam_processing_request",
        entity_id=req.request_id, actor_id=user.id, exam_id=exam_id,
        after={"request_id": req.request_id, "state": req.state,
               "closeout_revision": revision, "configuration_hash": config_hash},
    )

    return ProcessingRequestOut(
        id=req.id, request_id=req.request_id, state=req.state,
        closeout_revision=req.closeout_revision,
        configuration_hash=req.configuration_hash,
        quote_json=req.quote_json, run_id=req.run_id,
        decided_at=req.decided_at, decision_reason=req.decision_reason,
        last_polled_at=req.last_polled_at,
    )


@router.get("/{exam_id}/processing/requests")
async def list_processing_requests(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    """List all processing requests for an exam."""
    rows = (
        await db.execute(
            select(ExamProcessingRequest)
            .where(ExamProcessingRequest.exam_id == exam_id)
            .order_by(ExamProcessingRequest.id.desc())
        )
    ).scalars().all()
    return [
        ProcessingRequestOut(
            id=r.id, request_id=r.request_id, state=r.state,
            closeout_revision=r.closeout_revision,
            configuration_hash=r.configuration_hash,
            quote_json=r.quote_json, run_id=r.run_id,
            decided_at=r.decided_at, decision_reason=r.decision_reason,
            last_polled_at=r.last_polled_at,
        )
        for r in rows
    ]


@router.post("/{exam_id}/processing/requests/{request_id}/refresh")
async def refresh_processing_request(
    exam_id: uuid.UUID,
    request_id: str,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Poll ExaMetrics for the latest state of a processing request."""
    link = await _require_link(db, exam_id)
    req = (
        await db.execute(
            select(ExamProcessingRequest).where(
                ExamProcessingRequest.exam_id == exam_id,
                ExamProcessingRequest.request_id == request_id,
            )
        )
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(404, "Processing request not found")

    try:
        remote = await backend_sis.get_processing_request(
            link.api_key, link.backend_exam_id, request_id
        )
    except BackendSisError as exc:
        raise _sis_http(exc)

    old_state = req.state
    new_state = remote.get("state", req.state)
    req.state = new_state
    req.last_polled_at = datetime.now(timezone.utc)
    if remote.get("run_id"):
        req.run_id = remote["run_id"]
    if remote.get("decided_at"):
        req.decided_at = datetime.fromisoformat(remote["decided_at"]) if isinstance(remote["decided_at"], str) else remote["decided_at"]
    if remote.get("decision_reason"):
        req.decision_reason = remote["decision_reason"]
    if remote.get("quote"):
        req.quote_json = remote["quote"]

    await db.flush()

    # Audit state change.
    if old_state != new_state:
        from ..services import notifications as notif
        await notif.record(
            db, action="PROCESSING_STATE_CHANGED", entity_type="exam_processing_request",
            entity_id=req.request_id, actor_id=user.id, exam_id=exam_id,
            before={"state": old_state},
            after={"state": new_state, "request_id": req.request_id},
        )

    return ProcessingRequestOut(
        id=req.id, request_id=req.request_id, state=req.state,
        closeout_revision=req.closeout_revision,
        configuration_hash=req.configuration_hash,
        quote_json=req.quote_json, run_id=req.run_id,
        decided_at=req.decided_at, decision_reason=req.decision_reason,
        last_polled_at=req.last_polled_at,
    )
