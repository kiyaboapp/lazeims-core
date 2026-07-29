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
from ..services import registration_import

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


def _require_registration_phase(exam: Exam) -> None:
    if exam.phase != ExamPhase.REGISTRATION:
        raise HTTPException(
            409,
            f"Candidates can only be registered in REGISTRATION; exam is {exam.phase.value}.",
        )


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
    _require_registration_phase(exam)
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
    _require_registration_phase(exam)
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
    _require_registration_phase(exam)
    try:
        return await registration_import.apply_rows(
            db,
            exam=exam,
            school_id=payload.school_id,
            rows=[r.model_dump() for r in payload.rows],
        )
    except ValidationError as exc:
        raise _vhttp(exc)


@router.get("/{exam_id}/registration/stats")
async def get_registration_stats(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
) -> dict[str, Any]:
    """Per-school candidate counts for the registration progress view."""
    await _get_exam(db, exam_id)
    return await registration_import.registration_stats(db, exam_id=exam_id)
