"""Candidate registration ingestion endpoints.

Three doors, one funnel:

    Excel/CSV register  ─┐
    PDF / ZIP extract   ─┼─→  POST /registration/preview  →  POST /students/bulk
    hand-entered rows   ─┘

``preview`` parses and validates but persists nothing; ``bulk`` applies and
re-validates server-side. Registration mutates exam configuration, so both are
restricted to EXAM_ADMIN (or a global admin) and to the REGISTRATION phase.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import ExamPhase
from lazeims_common.errors import ValidationError

from ..db import get_session
from ..deps import current_user
from ..deps_exam import require_exam_admin
from ..models.exam import Exam
from ..models.registry import School, User
from ..services import backend_sis, registration_import
from ..services.backend_sis import BackendSisError

router = APIRouter(prefix="/exams", tags=["registration"])

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _vhttp(exc: ValidationError) -> HTTPException:
    return HTTPException(
        422,
        detail={"code": exc.code.value, "message": exc.message, "details": exc.details},
    )


async def _get_exam(db: AsyncSession, exam_id: uuid.UUID) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    return exam


def _require_registration_open(exam: Exam) -> None:
    """Registration stays open for corrections at every live phase.

    Rosters are corrected in the real world long after entry opens — a candidate
    was omitted, a name was wrong, a centre sent a late register. Locking
    registration to the REGISTRATION phase (as configuration is locked) would
    make those corrections impossible and push people into editing the database
    by hand.

    So the only closed state is ARCHIVED. Edits made outside REGISTRATION are
    recorded in the audit log by the caller, and the marks-affecting parts of the
    configuration (papers, maxima, filling mode) remain locked separately.
    """
    if exam.phase == ExamPhase.ARCHIVED:
        raise HTTPException(
            409, "This exam is archived and read-only. Reopen it before editing registrations."
        )


def _outside_registration(exam: Exam) -> bool:
    return exam.phase != ExamPhase.REGISTRATION


# ─────────────────────────────── schemas ────────────────────────────────


class RegisterRowIn(BaseModel):
    """One inbound candidate row.

    Identity may arrive either as a single ``full_name`` (Excel register) or
    pre-split (PDF extract). ``subject_codes`` are registry subject codes.
    """

    row_no: int | None = None
    student_id: str = ""
    full_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    surname: str | None = None
    sex: str | None = None
    subject_codes: list[str] = Field(default_factory=list)


class PreviewIn(BaseModel):
    school_id: int
    rows: list[RegisterRowIn] = Field(default_factory=list)


class BulkIn(BaseModel):
    school_id: int
    rows: list[RegisterRowIn] = Field(min_length=1)


# ──────────────────────────────── routes ────────────────────────────────


@router.get("/{exam_id}/registration/template")
async def download_register_template(
    exam_id: uuid.UUID,
    school_id: int | None = Query(None, description="Scope to one enrolled school. Omit for a blank register."),
    rows: int = Query(400, ge=1, le=registration_import.MAX_TEMPLATE_ROWS),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Download the candidate register `.xlsx`, pre-headed with this exam's
    subject codes (one column per subject — put X where the candidate sits it)."""
    exam = await _get_exam(db, exam_id)

    school: School | None = None
    if school_id is not None:
        try:
            school = await registration_import._require_enrolled_school(db, exam_id, school_id)
        except ValidationError as exc:
            raise _vhttp(exc)

    try:
        content = await registration_import.generate_register_template(
            db, exam=exam, school=school, rows=rows
        )
    except ValidationError as exc:
        raise _vhttp(exc)

    label = school.centre_number if school is not None else "BLANK"
    filename = f"REGISTER_{label}_{str(exam_id)[:8]}.xlsx"
    return Response(
        content=content,
        media_type=XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{exam_id}/registration/preview/upload")
async def preview_register_upload(
    exam_id: uuid.UUID,
    school_id: int = Query(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
) -> dict[str, Any]:
    """Parse + validate an uploaded `.xlsx`/`.xlsm`/`.csv` register.

    Persists nothing. The returned ``rows`` can be posted to
    ``/students/bulk`` once the operator has reviewed them.
    """
    exam = await _get_exam(db, exam_id)
    _require_registration_open(exam)
    content = await file.read()
    try:
        raw_rows = registration_import.parse_register_file(content, file.filename or "upload")
        return await registration_import.validate_rows(
            db, exam=exam, school_id=school_id, raw_rows=raw_rows
        )
    except ValidationError as exc:
        raise _vhttp(exc)


@router.post("/{exam_id}/registration/preview")
async def preview_register_rows(
    exam_id: uuid.UUID,
    payload: PreviewIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
) -> dict[str, Any]:
    """Validate rows supplied as JSON — used for the PDF/ZIP extract path and
    for hand-corrected rows coming back from the review table."""
    exam = await _get_exam(db, exam_id)
    _require_registration_open(exam)
    try:
        return await registration_import.validate_rows(
            db,
            exam=exam,
            school_id=payload.school_id,
            raw_rows=[r.model_dump() for r in payload.rows],
        )
    except ValidationError as exc:
        raise _vhttp(exc)


@router.post("/{exam_id}/students/bulk", status_code=201)
async def bulk_register_students(
    exam_id: uuid.UUID,
    payload: BulkIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
) -> dict[str, Any]:
    """Register many candidates at once.

    Idempotent: candidate numbers already present in the exam are skipped and
    counted, not rejected. Rows are re-validated here, so editing the preview
    payload cannot bypass validation.
    """
    exam = await _get_exam(db, exam_id)
    _require_registration_open(exam)
    try:
        return await registration_import.apply_rows(
            db,
            exam=exam,
            school_id=payload.school_id,
            rows=[r.model_dump() for r in payload.rows],
        )
    except ValidationError as exc:
        raise _vhttp(exc)


@router.post("/{exam_id}/registration/extract")
async def extract_registration_document(
    exam_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
) -> dict[str, Any]:
    """Parse a registration PDF, or a ZIP of PDFs, into per-centre candidate lists.

    Proxies ExaMetrics' parser, which is free and keyless — **no processing link
    or purchased API key is required**, so a zone can import registers long
    before it buys results processing.

    One PDF is one centre's register, so a ZIP returns many centres. For each
    document the response reports the centre, whether that centre exists in the
    registry and is enrolled in this exam, and the validated candidate rows.
    Nothing is written; post the rows to ``/registration/ingest`` to apply them.
    """
    exam = await _get_exam(db, exam_id)
    _require_registration_open(exam)

    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")

    try:
        payload = await backend_sis.extract_registrations(
            file.filename or "upload",
            content,
            file.content_type or "application/octet-stream",
        )
    except BackendSisError as exc:
        raise HTTPException(
            502,
            detail={
                "code": "EXAMETRICS_ERROR",
                "message": str(exc),
                "upstream_status": exc.status_code,
            },
        )

    groups = registration_import.rows_from_extract(payload)
    centres = await registration_import.resolve_centres(
        db, exam_id=exam_id, centre_numbers=[g["centre_number"] for g in groups if g["centre_number"]]
    )

    documents: list[dict[str, Any]] = []
    for group in groups:
        centre_key = (group["centre_number"] or "").upper()
        match = centres.get(centre_key)

        entry: dict[str, Any] = {
            "source": group["source"],
            "centre_number": group["centre_number"],
            "school_name_in_document": group["school_name"],
            "exam_level": group["exam_level"],
            "exam_year": group["exam_year"],
            "parse_error": group["parse_error"],
            "row_count": len(group["rows"]),
            "school_id": match["school_id"] if match else None,
            "registry_school_name": match["school_name"] if match else None,
            "enrolled": bool(match and match["enrolled"]),
            "preview": None,
        }

        # Validate against the exam only when we can place the centre.
        if match and match["enrolled"] and group["rows"]:
            entry["preview"] = await registration_import.validate_rows(
                db, exam=exam, school_id=match["school_id"], raw_rows=group["rows"]
            )
        elif match and not match["enrolled"]:
            entry["blocked_reason"] = (
                f"Centre {group['centre_number']} is in the registry but not enrolled in this "
                "exam. Enrol it, then re-run the import."
            )
        elif group["centre_number"] and not match:
            entry["blocked_reason"] = (
                f"Centre {group['centre_number']} is not in the school registry. Add the school "
                "first."
            )

        documents.append(entry)

    return {
        "file_count": payload.get("file_count", len(documents)),
        "total_rows": payload.get("total_rows", 0),
        "documents": documents,
        "phase_warning": (
            "This exam has moved past Registration. Changes are still allowed for "
            "corrections and will be recorded in the audit log."
            if _outside_registration(exam)
            else None
        ),
    }


class IngestGroupIn(BaseModel):
    """One centre's candidates, as reviewed by the operator."""

    school_id: int
    rows: list[RegisterRowIn] = Field(default_factory=list)


class IngestIn(BaseModel):
    groups: list[IngestGroupIn] = Field(min_length=1)


@router.post("/{exam_id}/registration/ingest", status_code=201)
async def ingest_registrations(
    exam_id: uuid.UUID,
    payload: IngestIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
) -> dict[str, Any]:
    """Register candidates for many centres in one call.

    This is the apply step for a ZIP of registers: each group is one centre.
    Groups are applied independently, so one bad centre does not roll back the
    rest, and every group is re-validated server-side.
    """
    exam = await _get_exam(db, exam_id)
    _require_registration_open(exam)

    results: list[dict[str, Any]] = []
    created = skipped = failed = 0

    for group in payload.groups:
        try:
            result = await registration_import.apply_rows(
                db,
                exam=exam,
                school_id=group.school_id,
                rows=[r.model_dump() for r in group.rows],
            )
            results.append({"school_id": group.school_id, **result})
            created += result["created"]
            skipped += result["skipped"]
            failed += result["failed"]
        except ValidationError as exc:
            results.append(
                {
                    "school_id": group.school_id,
                    "created": 0,
                    "skipped": 0,
                    "failed": len(group.rows),
                    "error": exc.message,
                }
            )
            failed += len(group.rows)

    if _outside_registration(exam):
        # Corrections after Registration are legitimate but must leave a trail.
        from ..services import notifications as notif

        await notif.record(
            db,
            action="REGISTRATION_CORRECTED",
            entity_type="exam",
            entity_id=str(exam_id),
            actor_id=user.id,
            exam_id=exam_id,
            after={
                "phase": exam.phase.value,
                "centres": len(payload.groups),
                "created": created,
                "skipped": skipped,
                "failed": failed,
            },
        )

    return {
        "centres": len(results),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


@router.get("/{exam_id}/registration/stats")
async def get_registration_stats(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
) -> dict[str, Any]:
    """Per-school candidate counts for the registration progress view."""
    await _get_exam(db, exam_id)
    return await registration_import.registration_stats(db, exam_id=exam_id)
