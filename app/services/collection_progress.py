"""Per-scope collection progress.

A "scope" is one (school, exam_subject, paper) triple — derived from actual
candidate enrollments. Only scopes that have at least one enrolled candidate
are tracked. ScopeWriteAssignment is irrelevant here: the online channel is
open to any assigned data enterer; station/excel channels enforce their own
write assignment at write time.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import FillingMode, IncidentStatus, PaperType

from ..models.assignments import FinalizedScope
from ..models.exam import Exam, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.marks import Attendance, ExamIncident, ItemMark, TotalMark
from ..models.registry import School, Subject
from .exam_phase import applicable_papers


def _paper_value(paper: Any) -> str:
    return paper.value if hasattr(paper, "value") else str(paper)


async def collection_progress(db: AsyncSession, *, exam: Exam) -> dict[str, Any]:
    """Return per-scope progress plus roll-ups for an exam."""
    exam_id: uuid.UUID = exam.id
    item_level = (exam.settings or {}).get("filling_mode") == FillingMode.ITEM_LEVEL.value

    # ── scopes: every (school, exam_subject) pair that has enrolled candidates ─
    # This is the ground truth — no writer assignment required.
    enrollment_rows = (
        await db.execute(
            select(
                distinct(ExamStudent.school_id),
                ExamStudentSubject.exam_subject_id,
            )
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).all()

    if not enrollment_rows:
        return {
            "scope_count": 0,
            "expected_total": 0,
            "entered_total": 0,
            "attendance_total": 0,
            "finalized_count": 0,
            "blocked_count": 0,
            "by_writer_mode": {},
            "scopes": [],
        }

    # Expand to (school, exam_subject, paper) — papers come from the subject config.
    all_subjects = {
        s.id: s
        for s in (
            await db.execute(select(ExamSubject).where(ExamSubject.exam_id == exam_id))
        ).scalars().all()
    }

    scope_keys: list[tuple[int, int, str]] = []
    for school_id, es_id in enrollment_rows:
        subj = all_subjects.get(es_id)
        if subj is None:
            continue
        for paper in applicable_papers(subj):
            scope_keys.append((school_id, es_id, paper.value))

    if not scope_keys:
        return {
            "scope_count": 0, "expected_total": 0, "entered_total": 0,
            "attendance_total": 0, "finalized_count": 0, "blocked_count": 0,
            "by_writer_mode": {}, "scopes": [],
        }

    school_ids   = {k[0] for k in scope_keys}
    subject_ids  = {k[1] for k in scope_keys}

    # ── labels ────────────────────────────────────────────────────────────────
    school_labels = {
        r[0]: (r[1], r[2])
        for r in (
            await db.execute(
                select(School.id, School.centre_number, School.name)
                .where(School.id.in_(school_ids))
            )
        ).all()
    }

    subject_labels = {
        r[0]: (r[1], r[2])
        for r in (
            await db.execute(
                select(ExamSubject.id, Subject.code, Subject.name)
                .join(Subject, Subject.id == ExamSubject.subject_id)
                .where(ExamSubject.exam_id == exam_id)
            )
        ).all()
    }

    # ── writer mode per scope (historical — no longer enforced) ────────────────
    # Kept in response for backward compat but always empty.
    writer_mode_map: dict[tuple[int, int, str], str] = {}

    # ── expected candidates per (school, subject) ─────────────────────────────
    expected = {
        (r[0], r[1]): r[2]
        for r in (
            await db.execute(
                select(
                    ExamStudent.school_id,
                    ExamStudentSubject.exam_subject_id,
                    func.count(ExamStudentSubject.id),
                )
                .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
                .where(ExamStudent.exam_id == exam_id)
                .group_by(ExamStudent.school_id, ExamStudentSubject.exam_subject_id)
            )
        ).all()
    }

    # ── attendance transcribed per (school, subject, paper) ───────────────────
    attendance: dict[tuple[int, int, str], tuple[int, int]] = {}
    for school_id, subject_id, paper, transcribed, present in (
        await db.execute(
            select(
                ExamStudent.school_id,
                ExamStudentSubject.exam_subject_id,
                Attendance.paper_type,
                func.count(Attendance.id),
                func.sum(case((Attendance.is_present.is_(True), 1), else_=0)),
            )
            .join(ExamStudentSubject, ExamStudentSubject.id == Attendance.exam_student_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam_id)
            .group_by(ExamStudent.school_id, ExamStudentSubject.exam_subject_id, Attendance.paper_type)
        )
    ).all():
        attendance[(school_id, subject_id, _paper_value(paper))] = (
            transcribed or 0, int(present or 0)
        )

    # ── marks entered per (school, subject, paper) ────────────────────────────
    entered: dict[tuple[int, int, str], int] = {}
    if item_level:
        rows = (
            await db.execute(
                select(
                    ExamStudent.school_id,
                    ExamStudentSubject.exam_subject_id,
                    func.count(distinct(ItemMark.exam_student_subject_id)),
                )
                .join(ExamStudentSubject, ExamStudentSubject.id == ItemMark.exam_student_subject_id)
                .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
                .where(ExamStudent.exam_id == exam_id)
                .group_by(ExamStudent.school_id, ExamStudentSubject.exam_subject_id)
            )
        ).all()
        for school_id, subject_id, count in rows:
            for paper in PaperType.real_papers():
                entered[(school_id, subject_id, paper.value)] = count or 0
    else:
        for school_id, subject_id, paper, count in (
            await db.execute(
                select(
                    ExamStudent.school_id,
                    ExamStudentSubject.exam_subject_id,
                    TotalMark.paper_type,
                    func.count(TotalMark.id),
                )
                .join(ExamStudentSubject, ExamStudentSubject.id == TotalMark.exam_student_subject_id)
                .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
                .where(ExamStudent.exam_id == exam_id)
                .group_by(ExamStudent.school_id, ExamStudentSubject.exam_subject_id, TotalMark.paper_type)
            )
        ).all():
            entered[(school_id, subject_id, _paper_value(paper))] = count or 0

    # ── finalized scopes ──────────────────────────────────────────────────────
    finalized = {
        (r[0], r[1], _paper_value(r[2])): r[3]
        for r in (
            await db.execute(
                select(
                    FinalizedScope.school_id,
                    FinalizedScope.exam_subject_id,
                    FinalizedScope.paper_type,
                    FinalizedScope.revision,
                ).where(FinalizedScope.exam_id == exam_id)
            )
        ).all()
    }

    # ── unresolved incidents ──────────────────────────────────────────────────
    incidents: dict[tuple[int, int, str], int] = {}
    for school_id, subject_id, paper, count in (
        await db.execute(
            select(
                ExamIncident.school_id,
                ExamIncident.exam_subject_id,
                ExamIncident.paper_type,
                func.count(ExamIncident.id),
            )
            .where(
                ExamIncident.exam_id == exam_id,
                ExamIncident.status.in_(list(IncidentStatus.unresolved())),
            )
            .group_by(ExamIncident.school_id, ExamIncident.exam_subject_id, ExamIncident.paper_type)
        )
    ).all():
        if school_id is None or subject_id is None:
            continue
        incidents[(school_id, subject_id, _paper_value(paper))] = count or 0

    # ── compose ───────────────────────────────────────────────────────────────
    scopes: list[dict[str, Any]] = []
    by_mode: dict[str, dict[str, int]] = {}

    for school_id, es_id, paper in scope_keys:
        key = (school_id, es_id, paper)
        mode = writer_mode_map.get(key, "ONLINE")

        expected_count = expected.get((school_id, es_id), 0)
        transcribed, present = attendance.get(key, (0, 0))
        entered_count = entered.get(key, 0)
        open_incidents = incidents.get(key, 0)
        revision = finalized.get(key)

        centre, school_name = school_labels.get(school_id, (None, None))
        code, subject_name = subject_labels.get(es_id, (None, None))

        markable = present if transcribed else expected_count
        complete = markable > 0 and entered_count >= markable

        bucket = by_mode.setdefault(mode, {"scopes": 0, "expected": 0, "entered": 0, "finalized": 0})
        bucket["scopes"] += 1
        bucket["expected"] += expected_count
        bucket["entered"] += entered_count
        if revision is not None:
            bucket["finalized"] += 1

        scopes.append({
            "school_id": school_id,
            "centre_number": centre,
            "school_name": school_name,
            "exam_subject_id": es_id,
            "subject_code": code,
            "subject_name": subject_name,
            "paper_type": paper,
            "writer_mode": mode,
            "expected": expected_count,
            "attendance_transcribed": transcribed,
            "present": present,
            "entered": entered_count,
            "markable": markable,
            "complete": complete,
            "finalized": revision is not None,
            "revision": revision,
            "open_incidents": open_incidents,
            "blocked_by": _blockers(expected_count, transcribed, entered_count, markable, open_incidents),
        })

    scopes.sort(key=lambda s: (s["centre_number"] or "", s["subject_code"] or "", s["paper_type"]))

    return {
        "scope_count": len(scopes),
        "expected_total": sum(s["expected"] for s in scopes),
        "entered_total": sum(s["entered"] for s in scopes),
        "attendance_total": sum(s["attendance_transcribed"] for s in scopes),
        "finalized_count": sum(1 for s in scopes if s["finalized"]),
        "blocked_count": sum(1 for s in scopes if s["blocked_by"] and not s["finalized"]),
        "by_writer_mode": by_mode,
        "scopes": scopes,
    }


def _blockers(
    expected: int, transcribed: int, entered: int, markable: int, open_incidents: int
) -> list[str]:
    blockers: list[str] = []
    if transcribed == 0:
        blockers.append("attendance not transcribed")
    elif transcribed < expected:
        blockers.append(f"attendance missing for {expected - transcribed} candidate(s)")
    if markable > 0 and entered < markable:
        blockers.append(f"marks missing for {markable - entered} candidate(s)")
    if open_incidents:
        blockers.append(f"{open_incidents} unresolved incident(s)")
    return blockers
