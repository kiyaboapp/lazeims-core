"""Finalize sweep for a scope ``(exam, school, exam_subject, paper)``.

Assembles per-student completeness from persisted data and evaluates it with the
shared ``evaluate_scope_completeness`` rule. On zero blockers, writes a
``FinalizedScope`` row (or bumps its revision on re-finalize after reopen).
"""

from __future__ import annotations
import uuid

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import (
    FillingMode,
    IncidentStatus,
    PaperType,
    WriterMode,
)
from lazeims_common.validation.scope import (
    StudentScopeState,
    evaluate_scope_completeness,
)

from ..models.assignments import FinalizedScope
from ..models.exam import Exam, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.marks import ExamIncident, ItemMark, TotalMark
from ..models.scoring import Question
from .attendance import resolve_effective_attendance


async def _students_in_scope(db: AsyncSession, exam_id: uuid.UUID, school_id: int, exam_subject_id: int):
    rows = (
        await db.execute(
            select(ExamStudent, ExamStudentSubject)
            .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
            .where(
                ExamStudent.exam_id == exam_id,
                ExamStudent.school_id == school_id,
                ExamStudentSubject.exam_subject_id == exam_subject_id,
            )
        )
    ).all()
    return rows


async def evaluate_scope(
    db: AsyncSession,
    *,
    exam: Exam,
    school_id: int,
    exam_subject: ExamSubject,
    paper_type: PaperType,
) -> dict:
    """Return a completeness result dict (does not write anything)."""
    mode = FillingMode((exam.settings or {}).get("filling_mode", "TOTAL_MARKS"))
    rows = await _students_in_scope(db, exam.id, school_id, exam_subject.id)

    # Pre-load question ids for item-mode completeness.
    required_qids: list[int] = []
    if mode == FillingMode.ITEM_LEVEL:
        required_qids = list(
            (
                await db.execute(
                    select(Question.id).where(
                        Question.exam_subject_id == exam_subject.id,
                        Question.paper_type == paper_type,
                    )
                )
            ).scalars().all()
        )

    # Open incidents for this scope (student-specific + scope-wide).
    incident_rows = (
        await db.execute(
            select(ExamIncident).where(
                ExamIncident.exam_id == exam.id,
                ExamIncident.school_id == school_id,
                ExamIncident.exam_subject_id == exam_subject.id,
                ExamIncident.paper_type == paper_type,
                ExamIncident.status.in_(list(IncidentStatus.unresolved())),
            )
        )
    ).scalars().all()
    open_ess_ids = {i.exam_student_subject_id for i in incident_rows if i.exam_student_subject_id}
    scope_wide_incident = any(i.exam_student_subject_id is None for i in incident_rows)

    states: list[StudentScopeState] = []
    for student, ess in rows:
        is_present = await resolve_effective_attendance(db, ess.id, paper_type)
        if mode == FillingMode.TOTAL_MARKS:
            tm = (
                await db.execute(
                    select(TotalMark.id).where(
                        TotalMark.exam_student_subject_id == ess.id,
                        TotalMark.paper_type == paper_type,
                    )
                )
            ).scalar_one_or_none()
            complete = tm is not None
        else:
            count = 0
            if required_qids:
                count = len(
                    (
                        await db.execute(
                            select(ItemMark.id).where(
                                ItemMark.exam_student_subject_id == ess.id,
                                ItemMark.question_id.in_(required_qids),
                            )
                        )
                    ).scalars().all()
                )
            complete = bool(required_qids) and count == len(required_qids)

        # Absent students are complete-by-definition (no marks expected).
        if not is_present:
            complete = True

        states.append(StudentScopeState(
            student_id=student.student_id,
            is_present=is_present,
            has_complete_marks=complete,
            has_open_incident=ess.id in open_ess_ids,
        ))

    result = evaluate_scope_completeness(states, has_unresolved_scope_incident=scope_wide_incident)
    out = result.as_dict()
    out["student_count"] = len(states)
    return out


async def finalize_scope(
    db: AsyncSession,
    *,
    exam: Exam,
    school_id: int,
    exam_subject: ExamSubject,
    paper_type: PaperType,
    writer_mode: WriterMode,
    finalized_by: int | None,
) -> tuple[bool, dict]:
    """Evaluate and, if complete, write/update the FinalizedScope row.

    Returns ``(finalized, result_dict)``.
    """
    result = await evaluate_scope(db, exam=exam, school_id=school_id,
                                  exam_subject=exam_subject, paper_type=paper_type)
    if not result["complete"]:
        return False, result

    existing = (
        await db.execute(
            select(FinalizedScope).where(
                FinalizedScope.exam_id == exam.id,
                FinalizedScope.school_id == school_id,
                FinalizedScope.exam_subject_id == exam_subject.id,
                FinalizedScope.paper_type == paper_type,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        db.add(FinalizedScope(
            exam_id=exam.id, school_id=school_id, exam_subject_id=exam_subject.id,
            paper_type=paper_type, writer_mode=writer_mode, revision=1,
            finalized_by=finalized_by, completion_snapshot=result,
        ))
    else:
        existing.revision += 1
        existing.finalized_by = finalized_by
        existing.completion_snapshot = result
        existing.writer_mode = writer_mode
    await db.flush()
    return True, result
