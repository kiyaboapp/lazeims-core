"""Exam phase state machine + readiness computation (Guide §3.3, delivery §4.1).

This delivery implements the collection lifecycle through ``ENTRY_LOCKED`` only.
``PROCESSING``/``RESULTS_PUBLISHED``/``ARCHIVED`` are reserved enum values with
no command endpoints here.

Each transition is an explicit command with server-side preconditions:
    REGISTRATION  -> ENTRY_OPEN     (requires full readiness)
    ENTRY_OPEN    -> ENTRY_LOCKED   (lock entry)
    ENTRY_LOCKED  -> ENTRY_OPEN     (audited correction window)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import ExamPhase, PaperType, RejectionCode
from lazeims_common.errors import ValidationError

from ..models.assignments import ScopeWriteAssignment
from ..models.exam import (
    Exam,
    ExamSchool,
    ExamStudent,
    ExamStudentSubject,
    ExamSubject,
)
from ..models.scoring import Question

# Allowed transitions in this delivery.
ALLOWED_TRANSITIONS: dict[ExamPhase, set[ExamPhase]] = {
    ExamPhase.REGISTRATION: {ExamPhase.ENTRY_OPEN},
    ExamPhase.ENTRY_OPEN: {ExamPhase.ENTRY_LOCKED},
    ExamPhase.ENTRY_LOCKED: {ExamPhase.ENTRY_OPEN},
}


@dataclass
class ReadinessItem:
    key: str
    ok: bool
    detail: str


@dataclass
class ReadinessResult:
    ready: bool
    checks: list[ReadinessItem] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checks": [{"key": c.key, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "blocking": [c.key for c in self.checks if not c.ok],
        }


def applicable_papers(subject: ExamSubject) -> list[PaperType]:
    papers = [PaperType.THEORY1]
    if subject.has_theory2:
        papers.append(PaperType.THEORY2)
    if subject.has_practical:
        papers.append(PaperType.PRACTICAL)
    return papers


async def compute_entry_open_readiness(db: AsyncSession, exam: Exam) -> ReadinessResult:
    """Compute whether an exam may advance REGISTRATION -> ENTRY_OPEN."""
    checks: list[ReadinessItem] = []

    school_count = await db.scalar(
        select(func.count()).select_from(ExamSchool).where(ExamSchool.exam_id == exam.id)
    )
    checks.append(ReadinessItem("has_schools", bool(school_count), f"{school_count} school(s) attached"))

    subjects = (
        await db.execute(select(ExamSubject).where(ExamSubject.exam_id == exam.id))
    ).scalars().all()
    checks.append(ReadinessItem("has_subjects", bool(subjects), f"{len(subjects)} subject(s) offered"))

    student_count = await db.scalar(
        select(func.count()).select_from(ExamStudent).where(ExamStudent.exam_id == exam.id)
    )
    checks.append(ReadinessItem("has_students", bool(student_count), f"{student_count} student(s) registered"))

    # In-scope (school, exam_subject, paper) combos: derived from actual
    # registrations so we never demand assignments for subjects a school
    # doesn't offer.
    reg_rows = (
        await db.execute(
            select(distinct(ExamStudent.school_id), ExamStudentSubject.exam_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam.id)
        )
    ).all()

    subject_by_id = {s.id: s for s in subjects}
    expected: set[tuple[int, int, str]] = set()
    for school_id, exam_subject_id in reg_rows:
        subj = subject_by_id.get(exam_subject_id)
        if subj is None:
            continue
        for paper in applicable_papers(subj):
            expected.add((school_id, exam_subject_id, paper.value))

    existing_rows = (
        await db.execute(
            select(
                ScopeWriteAssignment.school_id,
                ScopeWriteAssignment.exam_subject_id,
                ScopeWriteAssignment.paper_type,
            ).where(ScopeWriteAssignment.exam_id == exam.id)
        )
    ).all()
    existing = {(r[0], r[1], r[2].value if hasattr(r[2], "value") else r[2]) for r in existing_rows}
    missing = expected - existing
    checks.append(ReadinessItem(
        "writer_assignments_complete",
        len(missing) == 0,
        "all in-scope combinations assigned a writer" if not missing
        else f"{len(missing)} scope(s) missing a writer assignment",
    ))

    # Item-level exams need scoring configuration + a sealed version.
    filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
    if filling_mode == "ITEM_LEVEL":
        subjects_missing_questions = []
        for subj in subjects:
            for paper in applicable_papers(subj):
                qn = await db.scalar(
                    select(func.count()).select_from(Question).where(
                        Question.exam_subject_id == subj.id,
                        Question.paper_type == paper,
                    )
                )
                if not qn:
                    subjects_missing_questions.append(f"subject#{subj.id}/{paper.value}")
        checks.append(ReadinessItem(
            "item_config_complete",
            len(subjects_missing_questions) == 0,
            "all papers have questions configured" if not subjects_missing_questions
            else f"missing questions: {', '.join(subjects_missing_questions)}",
        ))
        checks.append(ReadinessItem(
            "configuration_sealed",
            exam.current_configuration_version is not None,
            "configuration version sealed" if exam.current_configuration_version
            else "no sealed configuration version",
        ))

    ready = all(c.ok for c in checks)
    return ReadinessResult(ready=ready, checks=checks)


async def assert_transition_allowed(db: AsyncSession, exam: Exam, target: ExamPhase) -> None:
    """Validate a requested phase transition, including preconditions.

    Raises :class:`ValidationError` if the transition is not permitted or its
    preconditions are unmet.
    """
    allowed = ALLOWED_TRANSITIONS.get(exam.phase, set())
    if target not in allowed:
        raise ValidationError(
            RejectionCode.CONFIGURATION_MISMATCH,
            f"Transition {exam.phase.value} -> {target.value} is not allowed.",
            {"from": exam.phase.value, "to": target.value,
             "allowed": sorted(p.value for p in allowed)},
        )

    if target == ExamPhase.ENTRY_OPEN and exam.phase == ExamPhase.REGISTRATION:
        readiness = await compute_entry_open_readiness(db, exam)
        if not readiness.ready:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "Exam is not ready to open entry.",
                readiness.as_dict(),
            )
