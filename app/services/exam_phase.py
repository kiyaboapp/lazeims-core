"""Exam phase state machine + readiness computation (Guide §3.3, delivery §4.1).

The collection lifecycle plus handoff to ExaMetrics (backend-sis) for processing:

    REGISTRATION  -> ENTRY_OPEN        (requires full readiness)
    ENTRY_OPEN    -> ENTRY_LOCKED      (lock entry)
    ENTRY_LOCKED  -> ENTRY_OPEN        (audited correction window)
    ENTRY_LOCKED  -> PROCESSING        (requires a configured processing link)
    PROCESSING    -> ENTRY_LOCKED      (revert if processing failed / needs edits)
    PROCESSING    -> RESULTS_PUBLISHED (requires ExaMetrics to report results ready)
    RESULTS_PUBLISHED -> ARCHIVED      (close the exam out)

Actual data push / processing / results retrieval happen in the integration
router; this state machine only guards the phase field and its preconditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import ExamPhase, PaperType, RejectionCode
from lazeims_common.errors import ValidationError

from ..models.exam import (
    Exam,
    ExamSchool,
    ExamStudent,
    ExamSubject,
)
from ..models.scoring import Question

# Allowed transitions.
ALLOWED_TRANSITIONS: dict[ExamPhase, set[ExamPhase]] = {
    ExamPhase.REGISTRATION: {ExamPhase.ENTRY_OPEN},
    ExamPhase.ENTRY_OPEN: {ExamPhase.ENTRY_LOCKED},
    ExamPhase.ENTRY_LOCKED: {ExamPhase.ENTRY_OPEN, ExamPhase.PROCESSING},
    ExamPhase.PROCESSING: {ExamPhase.ENTRY_LOCKED, ExamPhase.RESULTS_PUBLISHED},
    ExamPhase.RESULTS_PUBLISHED: {ExamPhase.ARCHIVED},
}

# Marker written to ExamProcessingLink.last_status once ExaMetrics reports the
# exam's results are processed and ready to publish.
RESULTS_READY_STATUS = "RESULTS_READY"


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

    # Handing off to ExaMetrics requires an APPROVED processing request for the
    # current closeout_revision and configuration_hash (C7 phase guard).
    if target == ExamPhase.PROCESSING:
        from ..models.processing import ExamProcessingLink, ExamProcessingRequest
        from ..models.collection import CollectionSnapshot

        link = (
            await db.execute(select(ExamProcessingLink).where(ExamProcessingLink.exam_id == exam.id))
        ).scalar_one_or_none()
        if link is None:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "Processing is not configured for this exam. Add the ExaMetrics exam id and API key first.",
                {"exam_id": str(exam.id)},
            )

        # Find the current sealed snapshot to get revision and hash.
        snap = (
            await db.execute(
                select(CollectionSnapshot)
                .where(
                    CollectionSnapshot.exam_id == exam.id,
                    CollectionSnapshot.status == "SEALED",
                )
                .order_by(CollectionSnapshot.closeout_revision.desc())
            )
        ).scalars().first()
        if snap is None:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "No sealed collection snapshot exists for this exam.",
                {"exam_id": str(exam.id)},
            )

        # Require an APPROVED request matching current revision AND hash.
        req = (
            await db.execute(
                select(ExamProcessingRequest).where(
                    ExamProcessingRequest.exam_id == exam.id,
                    ExamProcessingRequest.closeout_revision == snap.closeout_revision,
                    ExamProcessingRequest.configuration_hash == snap.configuration_hash,
                    ExamProcessingRequest.state == "APPROVED",
                )
            )
        ).scalar_one_or_none()
        if req is None:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "Processing requires an APPROVED request matching the current closeout revision and configuration hash.",
                {
                    "exam_id": str(exam.id),
                    "closeout_revision": snap.closeout_revision,
                    "configuration_hash": snap.configuration_hash,
                },
            )

    # Publishing requires ExaMetrics to have confirmed results are ready.
    if target == ExamPhase.RESULTS_PUBLISHED:
        from ..models.processing import ExamProcessingLink

        link = (
            await db.execute(select(ExamProcessingLink).where(ExamProcessingLink.exam_id == exam.id))
        ).scalar_one_or_none()
        if link is None or link.last_status != RESULTS_READY_STATUS:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "Results are not ready to publish yet. Wait until ExaMetrics reports processing complete.",
                {"exam_id": str(exam.id), "last_status": getattr(link, "last_status", None)},
            )
