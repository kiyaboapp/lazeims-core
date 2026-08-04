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
from ..models.marks import ItemMark, MarksAudit, TotalMark
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
    station_occurred_at: str | None = None,
    station_id: int | None = None,
    sync_event_id: str | None = None,
    actor_assignment_id: int | None = None,
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

    # Determine the authoritative entered_at: station timestamp wins (Blueprint §8)
    if station_occurred_at:
        try:
            entered_at = datetime.fromisoformat(station_occurred_at)
        except (ValueError, TypeError):
            entered_at = datetime.now(timezone.utc)
    else:
        entered_at = datetime.now(timezone.utc)

    # --- Capture before-state for audit trail ---
    before_total = None
    before_items = None

    if mode == FillingMode.TOTAL_MARKS:
        existing_tm = (
            await db.execute(
                select(TotalMark).where(
                    TotalMark.exam_student_subject_id == ess_id,
                    TotalMark.paper_type == paper_type,
                )
            )
        ).scalar_one_or_none()
        if existing_tm:
            before_total = float(existing_tm.total_marks_obtained)
    else:  # ITEM_LEVEL
        qids = (
            await db.execute(
                select(Question.id, Question.question_number).where(
                    Question.exam_subject_id == scope.exam_subject.id,
                    Question.paper_type == paper_type,
                )
            )
        ).all()
        qnum_by_id = {qid: qnum for qid, qnum in qids}
        existing_items = (
            await db.execute(
                select(ItemMark).where(
                    ItemMark.exam_student_subject_id == ess_id,
                    ItemMark.question_id.in_(list(qnum_by_id.keys())),
                )
            )
        ).scalars().all()
        if existing_items:
            before_items = {qnum_by_id[im.question_id]: float(im.marks_obtained) for im in existing_items}

    # --- Persist domain rows with upsert semantics ---
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if mode == FillingMode.TOTAL_MARKS:
        if is_present and total_marks_obtained is not None:
            stmt = pg_insert(TotalMark).values(
                exam_student_subject_id=ess_id,
                paper_type=paper_type,
                total_marks_obtained=Decimal(str(total_marks_obtained)),
                entered_by=actor_id,
                entered_at=entered_at,
                station_occurred_at=entered_at if station_occurred_at else None,
            ).on_conflict_do_update(
                constraint="uq_total_marks_ess_paper",
                set_=dict(
                    total_marks_obtained=Decimal(str(total_marks_obtained)),
                    entered_by=actor_id,
                    entered_at=entered_at,
                    station_occurred_at=entered_at if station_occurred_at else None,
                )
            )
            await db.execute(stmt)
        else:
            # Absent or null mark — clear any existing record
            await db.execute(
                delete(TotalMark).where(
                    TotalMark.exam_student_subject_id == ess_id,
                    TotalMark.paper_type == paper_type,
                )
            )
    else:  # ITEM_LEVEL
        # Clear existing item marks for this paper's questions.
        qids_list = (
            await db.execute(
                select(Question.id).where(
                    Question.exam_subject_id == scope.exam_subject.id,
                    Question.paper_type == paper_type,
                )
            )
        ).scalars().all()
        if qids_list:
            await db.execute(
                delete(ItemMark).where(
                    ItemMark.exam_student_subject_id == ess_id,
                    ItemMark.question_id.in_(qids_list),
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
                    entered_at=entered_at,
                    station_occurred_at=entered_at if station_occurred_at else None,
                ))
    await db.flush()

    # --- Write audit trail (Blueprint §9.6) ---
    after_total = None
    after_items = None
    operation = "CLEAR"

    if mode == FillingMode.TOTAL_MARKS:
        if is_present and total_marks_obtained is not None:
            after_total = float(total_marks_obtained)
            operation = "SET"
    else:
        if is_present and items:
            after_items = {k: float(v) for k, v in items.items()}
            operation = "ITEM_SET"
        else:
            operation = "ITEM_CLEAR"

    db.add(MarksAudit(
        exam_id=scope.exam.id,
        exam_student_subject_id=ess_id,
        paper_type=paper_type.value,
        operation=operation,
        mode=mode.value,
        before_total=Decimal(str(before_total)) if before_total is not None else None,
        before_items=before_items,
        after_total=Decimal(str(after_total)) if after_total is not None else None,
        after_items=after_items,
        station_id=station_id,
        station_occurred_at=entered_at,
        sync_event_id=sync_event_id,
        actor_assignment_id=actor_assignment_id,
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
