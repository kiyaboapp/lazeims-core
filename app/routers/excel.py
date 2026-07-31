"""Controlled Excel endpoints (Guide §2.7).

Workbook generation + import confirmation are authorized operators (Exam Admin /
global admin / Chief IT). Import runs the same validation as online/station and
requires an explicit confirm before anything is applied.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.errors import ValidationError

from ..db import get_session
from ..deps import current_user
from ..deps_exam import require_exam_admin
from ..models.exam import Exam
from ..models.excel import ExcelImportBatch, ExcelImportRow, ExcelWorkbook
from ..models.registry import School, User
from ..services import excel_import

router = APIRouter(tags=["excel"])

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _vhttp(exc: ValidationError) -> HTTPException:
    return HTTPException(422, detail={"code": exc.code.value, "message": exc.message, "details": exc.details})


class WorkbookIn(BaseModel):
    school_id: int
    subjects: list[str] = Field(min_length=1)


@router.post("/exams/{exam_id}/excel/workbooks")
async def create_workbook(
    exam_id: uuid.UUID, payload: WorkbookIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    school = await db.get(School, payload.school_id)
    if school is None:
        raise HTTPException(404, "School not found")
    try:
        wb, _bytes = await excel_import.generate_workbook(
            db, exam=exam, school=school, subject_codes=payload.subjects, generated_by=user.id)
    except ValidationError as exc:
        raise _vhttp(exc)
    return {"workbook_id": wb.workbook_id, "configuration_hash": wb.configuration_hash,
            "subject_scope": wb.subject_scope, "status": wb.status}


@router.get("/excel/workbooks/{workbook_id}/download")
async def download_workbook(
    workbook_id: str,
    db: AsyncSession = Depends(get_session), user: User = Depends(current_user),
):
    wb = await db.get(ExcelWorkbook, workbook_id)
    if wb is None:
        raise HTTPException(404, "workbook not found")
    try:
        content = await excel_import.render_workbook_bytes(db, wb)
    except ValidationError as exc:
        raise _vhttp(exc)
    headers = {"Content-Disposition": f'attachment; filename="{workbook_id}.xlsx"'}
    return Response(content=content, media_type=XLSX_MEDIA, headers=headers)


@router.post("/excel/workbooks/{workbook_id}/imports")
async def create_import(
    workbook_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session), user: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    channel: str = "WEB_UPLOAD",
):
    wb = await db.get(ExcelWorkbook, workbook_id)
    if wb is None:
        raise HTTPException(404, "workbook not found")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "workbook too large")
    key = idempotency_key or ("imp-" + uuid.uuid4().hex)
    try:
        batch = await excel_import.parse_and_stage(
            db, workbook=wb, file_bytes=content, channel=channel,
            idempotency_key=key, imported_by=user.id)
    except ValidationError as exc:
        raise _vhttp(exc)
    return {"import_id": batch.import_id, "row_count": batch.row_count,
            "accepted_count": batch.accepted_count, "rejected_count": batch.rejected_count,
            "status": batch.status}


async def _get_batch(db, import_id) -> ExcelImportBatch:
    batch = await db.get(ExcelImportBatch, import_id)
    if batch is None:
        raise HTTPException(404, "import not found")
    return batch


@router.get("/excel/imports/{import_id}/preview")
async def import_preview(import_id: str, db: AsyncSession = Depends(get_session), user: User = Depends(current_user)):
    batch = await _get_batch(db, import_id)
    rows = (await db.execute(
        select(ExcelImportRow).where(ExcelImportRow.import_id == import_id).order_by(ExcelImportRow.row_no))
    ).scalars().all()
    return {
        "import_id": import_id, "status": batch.status,
        "row_count": batch.row_count, "accepted_count": batch.accepted_count, "rejected_count": batch.rejected_count,
        "rows": [{"row_no": r.row_no, "student_id": r.student_id, "subject_code": r.subject_code,
                  "paper_type": r.paper_type, "status": r.status, "payload": r.payload} for r in rows],
    }


@router.get("/excel/imports/{import_id}/errors")
async def import_errors(import_id: str, db: AsyncSession = Depends(get_session), user: User = Depends(current_user)):
    await _get_batch(db, import_id)
    rows = (await db.execute(
        select(ExcelImportRow).where(ExcelImportRow.import_id == import_id, ExcelImportRow.status == "ERROR")
        .order_by(ExcelImportRow.row_no))
    ).scalars().all()
    return {"errors": [{"row_no": r.row_no, "student_id": r.student_id, "code": r.error_code,
                        "message": r.error_message} for r in rows]}


@router.post("/excel/imports/{import_id}/confirm")
async def confirm_import(import_id: str, db: AsyncSession = Depends(get_session), user: User = Depends(current_user)):
    batch = await _get_batch(db, import_id)

    # Phase gate: Excel import confirm is only allowed during ENTRY_OPEN.
    workbook = await db.get(ExcelWorkbook, batch.workbook_id)
    if workbook is not None:
        exam = await db.get(Exam, workbook.exam_id)
        if exam is not None:
            from lazeims_common.enums import ExamPhase
            if exam.phase != ExamPhase.ENTRY_OPEN:
                raise HTTPException(
                    409,
                    detail={
                        "code": "PHASE_NOT_OPEN",
                        "message": f"Excel import confirm is only allowed during ENTRY_OPEN (current: {exam.phase.value}).",
                    },
                )

    try:
        result = await excel_import.confirm_import(db, batch=batch, confirmed_by=user.id)
    except ValidationError as exc:
        raise _vhttp(exc)
    return result
