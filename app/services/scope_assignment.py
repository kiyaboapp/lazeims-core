"""Scope resolution + writer-channel enforcement.

Before ANY write to a scope, we confirm the write is coming through the channel
that owns that ``(exam, school, exam_subject, paper)`` scope
(``ScopeWriteAssignment``). Online endpoints assert ``WriterMode.ONLINE``; a
mismatch is a hard rejection (``WRITER_MODE_MISMATCH``) BEFORE any content
validation.
"""

from __future__ import annotations
import uuid

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import PaperType, RejectionCode, WriterMode
from lazeims_common.errors import ValidationError

from ..models.assignments import FinalizedScope, ScopeWriteAssignment
from ..models.exam import Exam, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.registry import Subject


@dataclass
class ResolvedStudentScope:
    exam: Exam
    student: ExamStudent
    exam_subject: ExamSubject
    subject: Subject
    exam_student_subject: ExamStudentSubject


async def resolve_student_scope(
    db: AsyncSession,
    *,
    exam_id: uuid.UUID,
    student_id: str,
    exam_subject_id: int,
) -> ResolvedStudentScope:
    """Resolve natural coordinates to concrete rows, or raise NOT_REGISTERED."""
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED, "Exam not found.", {"exam_id": exam_id})

    student = (
        await db.execute(
            select(ExamStudent).where(
                ExamStudent.exam_id == exam_id, ExamStudent.student_id == student_id
            )
        )
    ).scalar_one_or_none()
    if student is None:
        raise ValidationError(
            RejectionCode.NOT_REGISTERED,
            f"Student {student_id} is not registered in this exam.",
            {"student_id": student_id},
        )

    exam_subject = await db.get(ExamSubject, exam_subject_id)
    if exam_subject is None or exam_subject.exam_id != exam_id:
        raise ValidationError(
            RejectionCode.NOT_REGISTERED,
            "Subject is not offered in this exam.",
            {"exam_subject_id": exam_subject_id},
        )

    ess = (
        await db.execute(
            select(ExamStudentSubject).where(
                ExamStudentSubject.exam_student_id == student.id,
                ExamStudentSubject.exam_subject_id == exam_subject_id,
            )
        )
    ).scalar_one_or_none()
    if ess is None:
        raise ValidationError(
            RejectionCode.NOT_REGISTERED,
            f"Student {student_id} is not registered for this subject.",
            {"student_id": student_id, "exam_subject_id": exam_subject_id},
        )

    subject = await db.get(Subject, exam_subject.subject_id)
    return ResolvedStudentScope(exam, student, exam_subject, subject, ess)


async def enforce_writer_mode(
    db: AsyncSession,
    *,
    exam_id: uuid.UUID,
    school_id: int,
    exam_subject_id: int,
    paper_type: PaperType,
    expected_mode: WriterMode,
    station_id: int | None = None,
) -> ScopeWriteAssignment:
    """Ensure the scope is owned by ``expected_mode``. Raises WRITER_MODE_MISMATCH.

    A scope with no assignment at all is also a mismatch — a writer must be
    explicitly assigned before any write is accepted. When ``expected_mode`` is
    STATION and ``station_id`` is given, the assignment's station_id must match
    (a package may only write its own assigned station's scopes).
    """
    assignment = (
        await db.execute(
            select(ScopeWriteAssignment).where(
                ScopeWriteAssignment.exam_id == exam_id,
                ScopeWriteAssignment.school_id == school_id,
                ScopeWriteAssignment.exam_subject_id == exam_subject_id,
                ScopeWriteAssignment.paper_type == paper_type,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise ValidationError(
            RejectionCode.WRITER_MODE_MISMATCH,
            "No writer channel is assigned for this scope.",
            {"school_id": school_id, "exam_subject_id": exam_subject_id, "paper_type": paper_type.value},
        )
    if assignment.writer_mode != expected_mode:
        raise ValidationError(
            RejectionCode.WRITER_MODE_MISMATCH,
            f"This scope is owned by {assignment.writer_mode.value}, not {expected_mode.value}.",
            {"owned_by": assignment.writer_mode.value, "attempted": expected_mode.value},
        )
    if expected_mode == WriterMode.STATION and station_id is not None:
        if assignment.station_id != station_id:
            raise ValidationError(
                RejectionCode.OUTSIDE_STATION_SCOPE,
                "This scope is assigned to a different station.",
                {"assigned_station_id": assignment.station_id, "station_id": station_id},
            )
    return assignment


async def is_scope_finalized(
    db: AsyncSession,
    *,
    exam_id: uuid.UUID,
    school_id: int,
    exam_subject_id: int,
    paper_type: PaperType,
) -> bool:
    row = (
        await db.execute(
            select(FinalizedScope.id).where(
                FinalizedScope.exam_id == exam_id,
                FinalizedScope.school_id == school_id,
                FinalizedScope.exam_subject_id == exam_subject_id,
                FinalizedScope.paper_type == paper_type,
            )
        )
    ).scalar_one_or_none()
    return row is not None
