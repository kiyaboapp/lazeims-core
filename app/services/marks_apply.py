"""Apply one student-paper marks submission (total or item), validated by the
shared ``lazeims_common`` rules and persisted atomically.

The whole required set for a student-paper is applied together (replace
semantics) so a partial/invalid intermediate record can never exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import FillingMode, PaperType, RejectionCode
from lazeims_common.errors import ValidationError
from lazeims_common.validation.config import (
    PaperConfig,
    QuestionConfig,
    QuestionGroupConfig,
)
from lazeims_common.validation.marks import validate_marks_submission

from ..models.exam import ExamSubject
from ..models.marks import ItemMark, TotalMark
from ..models.scoring import Question, QuestionGroup
from .attendance import has_transcription, resolve_effective_attendance
from .scope_assignment import ResolvedStudentScope


def _paper_max(es: ExamSubject, paper: PaperType) -> Decimal:
    return Decimal(str({
        PaperType.THEORY1: es.total_marks_theory1,
        PaperType.THEORY2: es.total_marks_theory2,
        PaperType.PRACTICAL: es.total_marks_practical,
    }[paper]))


async def build_paper_config(db: AsyncSession, es: ExamSubject, paper: PaperType) -> PaperConfig:
    groups = (
        await db.execute(
            select(QuestionGroup).where(
                QuestionGroup.exam_subject_id == es.id, QuestionGroup.paper_type == paper
            )
        )
    ).scalars().all()
    questions = (
        await db.execute(
            select(Question).where(
                Question.exam_subject_id == es.id, Question.paper_type == paper
            )
        )
    ).scalars().all()
    gcode_by_id = {g.id: g.code for g in groups}
    return PaperConfig(
        paper_type=paper,
        paper_max=_paper_max(es, paper),
        questions=tuple(
            QuestionConfig(
                question_number=q.question_number,
                max_marks=Decimal(str(q.max_marks)),
                group_code=gcode_by_id.get(q.group_id) if q.group_id else None,
            )
            for q in questions
        ),
        groups=tuple(QuestionGroupConfig(code=g.code, pick_count=g.pick_count) for g in groups),
    )


async def apply_student_paper_marks(
    db: AsyncSession,
    *,
    scope: ResolvedStudentScope,
    paper_type: PaperType,
    mode: FillingMode,
    total_marks_obtained=None,
    items: dict[str, object] | None = None,
    actor_id: int | None = None,
) -> dict:
    """Validate and persist one student-paper submission.

    Returns a small result dict (used as the idempotency result snapshot).
    """
    ess_id = scope.exam_student_subject.id

    # Gate 1: attendance must be transcribed for this paper first.
    has_att = await has_transcription(db, ess_id, paper_type)
    is_present = await resolve_effective_attendance(db, ess_id, paper_type)

    paper = await build_paper_config(db, scope.exam_subject, paper_type)

    # Validate via the shared rules (raises ValidationError with a stable code).
    computed_total = validate_marks_submission(
        mode=mode,
        is_present=is_present,
        has_attendance_transcription=has_att,
        paper=paper,
        total_marks_obtained=total_marks_obtained,
        item_marks=items,
    )

    now = datetime.now(timezone.utc)

    # Persist with replace semantics for this student-paper.
    if mode == FillingMode.TOTAL_MARKS:
        await db.execute(
            delete(TotalMark).where(
                TotalMark.exam_student_subject_id == ess_id,
                TotalMark.paper_type == paper_type,
            )
        )
        if is_present:
            db.add(TotalMark(
                exam_student_subject_id=ess_id,
                paper_type=paper_type,
                total_marks_obtained=Decimal(str(total_marks_obtained)),
                entered_by=actor_id,
                entered_at=now,
            ))
    else:  # ITEM_LEVEL
        # Clear existing item marks for this paper's questions.
        qids = (
            await db.execute(
                select(Question.id).where(
                    Question.exam_subject_id == scope.exam_subject.id,
                    Question.paper_type == paper_type,
                )
            )
        ).scalars().all()
        if qids:
            await db.execute(
                delete(ItemMark).where(
                    ItemMark.exam_student_subject_id == ess_id,
                    ItemMark.question_id.in_(qids),
                )
            )
        if is_present:
            qrow_by_number = {
                q.question_number: q.id
                for q in (
                    await db.execute(
                        select(Question).where(
                            Question.exam_subject_id == scope.exam_subject.id,
                            Question.paper_type == paper_type,
                        )
                    )
                ).scalars().all()
            }
            for qnum, value in (items or {}).items():
                db.add(ItemMark(
                    exam_student_subject_id=ess_id,
                    question_id=qrow_by_number[qnum],
                    marks_obtained=Decimal(str(value)),
                    entered_by=actor_id,
                    entered_at=now,
                ))
    await db.flush()

    return {
        "student_id": scope.student.student_id,
        "exam_subject_id": scope.exam_subject.id,
        "paper_type": paper_type.value,
        "mode": mode.value,
        "is_present": is_present,
        "computed_total": str(computed_total) if computed_total is not None else None,
    }
