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
    ess_ids = [ess.id for _, ess in rows]

    # Batch: attendance for all students
    from ..models.marks import Attendance as _Att
    att_rows = (await db.execute(
        select(_Att.exam_student_subject_id, _Att.paper_type, _Att.is_present)
        .where(
            _Att.exam_student_subject_id.in_(ess_ids),
            _Att.paper_type.in_([paper_type, PaperType.ALL]),
        )
    )).all() if ess_ids else []
    # Resolve attendance: specific paper overrides ALL
    att_specific: dict[int, bool] = {}
    att_all: dict[int, bool] = {}
    for ess_id, pt, is_present in att_rows:
        pt_val = pt.value if hasattr(pt, 'value') else pt
        if pt_val == paper_type.value:
            att_specific[ess_id] = is_present
        elif pt_val == 'ALL':
            att_all[ess_id] = is_present
    att_map = {ess_id: att_specific.get(ess_id, att_all.get(ess_id, True)) for ess_id in ess_ids}

    # Batch: marks completeness
    if mode == FillingMode.TOTAL_MARKS:
        tm_set = set(
            (await db.execute(
                select(TotalMark.exam_student_subject_id).where(
                    TotalMark.exam_student_subject_id.in_(ess_ids),
                    TotalMark.paper_type == paper_type,
                )
            )).scalars().all()
        ) if ess_ids else set()
    else:
        # Item mode: count how many required questions each student answered
        im_counts: dict[int, int] = {}
        if required_qids and ess_ids:
            from sqlalchemy import func as _fn
            im_rows = (await db.execute(
                select(ItemMark.exam_student_subject_id, _fn.count(ItemMark.id))
                .where(
                    ItemMark.exam_student_subject_id.in_(ess_ids),
                    ItemMark.question_id.in_(required_qids),
                )
                .group_by(ItemMark.exam_student_subject_id)
            )).all()
            im_counts = {r[0]: r[1] for r in im_rows}

    for student, ess in rows:
        is_present = att_map.get(ess.id, True)

        if mode == FillingMode.TOTAL_MARKS:
            complete = ess.id in tm_set
        else:
            count = im_counts.get(ess.id, 0)
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
