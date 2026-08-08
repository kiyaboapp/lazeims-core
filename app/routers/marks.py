"""Online attendance / incidents / marks / finalize endpoints (Phase 3).

Every write:
  1. requires the exam to be in ENTRY_OPEN;
  2. rejects writes to an already-finalized scope;
  3. validates content through the shared lazeims_common rules;
  4. (marks) is idempotent via the ``Idempotency-Key`` header.

ScopeWriteAssignment is NOT checked here — the online channel is open to any
assigned data enterer. Station and Excel writes enforce their own channel checks
at write time in station_sync.py / excel_import.py.
"""

from __future__ import annotations
import uuid

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import (
    ExamPhase,
    FillingMode,
    IncidentStatus,
    PaperType,
    RejectionCode,
    WriterMode,
)
from lazeims_common.errors import ValidationError
from lazeims_common.hashing import sha256_prefixed

from ..db import get_session
from ..deps import current_user, require_role
from ..deps_exam import get_exam_roles, require_exam_role
from ..enums import ExamRoleName, StandingRoleName
from ..models.assignments import FinalizedScope, ScopeRevision
from ..models.exam import Exam, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.marks import Attendance, ExamIncident
from ..models.registry import User
from ..schemas_marks import (
    AttendanceIn,
    BulkAttendanceIn,
    CalBaselineIn,
    IncidentIn,
    IncidentOut,
    IncidentStatusIn,
    MarksBatchIn,
    ReopenIn,
    ScopeRef,
    StudentMarksIn,
)
from ..services import idempotency
from ..services.attendance import upsert_attendance
from ..services.finalize import evaluate_scope, finalize_scope
from ..services.marks_apply import apply_student_paper_marks
from ..services.scope_assignment import (
    is_scope_finalized,
    resolve_student_scope,
)

router = APIRouter(prefix="/exams", tags=["marks"])

# marks/attendance actors: assigned Data Enterer or Exam Admin (global admin bypasses)
_ENTRY_ACTOR = require_exam_role(ExamRoleName.DATA_ENTERER, ExamRoleName.EXAM_ADMIN)
_EXAM_ADMIN = require_exam_role(ExamRoleName.EXAM_ADMIN)
_CHIEF_IT = require_role(
    StandingRoleName.REGION_ITS, StandingRoleName.COUNCIL_ITS,
    StandingRoleName.SUPER_ADMIN, StandingRoleName.SYSTEM_ADMIN,
)


async def _incident_viewer(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> User:
    standing_role = user.role.name.value
    if standing_role in {
        StandingRoleName.REGION_ITS.value,
        StandingRoleName.COUNCIL_ITS.value,
        StandingRoleName.SUPER_ADMIN.value,
        StandingRoleName.SYSTEM_ADMIN.value,
    }:
        return user
    roles = await get_exam_roles(db, exam_id, user)
    if roles & {ExamRoleName.DATA_ENTERER.value, ExamRoleName.EXAM_ADMIN.value}:
        return user
    raise HTTPException(403, "Not permitted to view incidents for this exam")


def _vhttp(exc: ValidationError) -> HTTPException:
    return HTTPException(422, detail={"code": exc.code.value, "message": exc.message, "details": exc.details})


async def _get_exam(db: AsyncSession, exam_id: uuid.UUID) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    return exam


def _require_entry_open(exam: Exam) -> None:
    """Block marks/attendance only when the exam is past the collection phases.

    REGISTRATION and ENTRY_OPEN are both valid for data entry. The phase model
    is advisory for workflow navigation; the backend must not gate data entry
    so strictly that operators cannot enter marks at all.
    Blocked phases: ENTRY_LOCKED, PROCESSING, RESULTS_PUBLISHED.
    """
    blocked = {ExamPhase.ENTRY_LOCKED, ExamPhase.PROCESSING, ExamPhase.RESULTS_PUBLISHED}
    if exam.phase in blocked:
        raise HTTPException(409, f"Entry is locked (phase {exam.phase.value}).")


# ---------------- attendance ----------------

@router.put("/{exam_id}/attendance")
async def transcribe_attendance(
    exam_id: uuid.UUID, payload: AttendanceIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    exam = await _get_exam(db, exam_id)
    _require_entry_open(exam)
    if payload.paper_type == PaperType.ALL:
        raise HTTPException(422, "Use the cal-baseline endpoint for the ALL baseline.")
    try:
        scope = await resolve_student_scope(
            db, exam_id=exam_id, student_id=payload.student_id, exam_subject_id=payload.exam_subject_id
        )
    except ValidationError as exc:
        raise _vhttp(exc)
    if await is_scope_finalized(db, exam_id=exam_id, school_id=scope.student.school_id,
                                exam_subject_id=payload.exam_subject_id, paper_type=payload.paper_type):
        raise _vhttp(ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized."))
    # Prevent marking absent if marks already exist for this student-subject
    if not payload.is_present:
        from ..models.marks import TotalMark
        has_marks = (await db.execute(
            select(func.count()).where(
                TotalMark.exam_student_subject_id == scope.exam_student_subject.id,
                TotalMark.paper_type == payload.paper_type,
            )
        )).scalar_one()
        if has_marks > 0:
            raise HTTPException(422, "Cannot mark absent — marks already entered for this paper.")
    row = await upsert_attendance(
        db, exam_student_subject_id=scope.exam_student_subject.id, paper_type=payload.paper_type,
        is_present=payload.is_present, source=payload.source, actor_id=user.id,
    )
    return {"id": row.id, "is_present": row.is_present, "paper_type": row.paper_type.value}


@router.put("/{exam_id}/attendance/bulk")
async def transcribe_attendance_bulk(
    exam_id: uuid.UUID, payload: BulkAttendanceIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    """Bulk-upsert attendance for all students in a scope (one DB round-trip).

    Used by the data entry screen to confirm attendance for an entire
    school-subject-paper in a single request instead of N individual PUTs.
    """
    exam = await _get_exam(db, exam_id)
    _require_entry_open(exam)
    if payload.paper_type == PaperType.ALL:
        raise HTTPException(422, "Use the cal-baseline endpoint for the ALL baseline.")

    written = 0
    for entry in payload.entries:
        try:
            scope = await resolve_student_scope(
                db, exam_id=exam_id, student_id=entry.student_id,
                exam_subject_id=payload.exam_subject_id,
            )
        except ValidationError as exc:
            raise _vhttp(exc)
        if await is_scope_finalized(
            db, exam_id=exam_id, school_id=scope.student.school_id,
            exam_subject_id=payload.exam_subject_id, paper_type=payload.paper_type,
        ):
            raise _vhttp(ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized."))
        # Prevent marking absent if marks already exist (same as single PUT + station)
        if not entry.is_present:
            from ..models.marks import TotalMark
            has_marks = (await db.execute(
                select(func.count()).where(
                    TotalMark.exam_student_subject_id == scope.exam_student_subject.id,
                    TotalMark.paper_type == payload.paper_type,
                )
            )).scalar_one()
            if has_marks > 0:
                raise HTTPException(422, f"Cannot mark {entry.student_id} absent — marks already entered.")
        await upsert_attendance(
            db, exam_student_subject_id=scope.exam_student_subject.id,
            paper_type=payload.paper_type, is_present=entry.is_present,
            source=payload.source, actor_id=user.id,
        )
        written += 1
    return {"written": written}


@router.put("/{exam_id}/schools/{school_id}/cal-baseline")
async def set_cal_baseline(
    exam_id: uuid.UUID, school_id: int, payload: CalBaselineIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_CHIEF_IT),
):
    """Chief IT optional subject-wide baseline (Attendance paper_type=ALL)."""
    from lazeims_common.enums import AttendanceSource
    from ..models.marks import TotalMark
    exam = await _get_exam(db, exam_id)
    written = 0
    errors: list[dict] = []
    for entry in payload.entries:
        try:
            scope = await resolve_student_scope(
                db, exam_id=exam_id, student_id=entry.student_id, exam_subject_id=payload.exam_subject_id
            )
        except ValidationError as exc:
            raise _vhttp(exc)
        if scope.student.school_id != school_id:
            continue
        # Prevent marking absent if marks already exist for this student-subject
        if not entry.is_present:
            has_marks = (await db.execute(
                select(func.count()).where(TotalMark.exam_student_subject_id == scope.exam_student_subject.id)
            )).scalar_one()
            if has_marks > 0:
                errors.append({
                    "student_id": entry.student_id,
                    "reason": "Cannot mark absent — marks already entered for this student-subject.",
                })
                continue
        await upsert_attendance(
            db, exam_student_subject_id=scope.exam_student_subject.id, paper_type=PaperType.ALL,
            is_present=entry.is_present, source=AttendanceSource.SUPERVISOR_CAL_TRANSCRIPTION,
            actor_id=user.id,
        )
        written += 1
    if errors:
        raise HTTPException(422, detail={"baseline_rows_written": written, "errors": errors})
    return {"baseline_rows_written": written}


# ---------------- bulk school attendance (CAL grid) ----------------

@router.get("/{exam_id}/schools/{school_id}/attendance-grid")
async def school_attendance_grid(
    exam_id: uuid.UUID, school_id: int,
    db: AsyncSession = Depends(get_session), _: User = Depends(current_user),
):
    """Bulk equivalent of calling scope_roster for every subject-paper at a school.

    Returns per-column data: for each (exam_subject_id, paper_type) that has
    registered students at this school, returns the student list with resolved
    attendance — exactly what the CAL grid needs, in one round trip.

    Response: { columns: [ { exam_subject_id, paper_type, students: [{student_id, full_name, attendance: bool|null}] } ] }
    """
    from ..models.exam import ExamStudent, ExamStudentSubject, ExamSubject
    from ..models.marks import Attendance as _Att
    from ..services.exam_phase import applicable_papers
    from lazeims_common.enums import PaperType

    # 1) All students at this school
    student_rows = (await db.execute(
        select(ExamStudent)
        .where(ExamStudent.exam_id == exam_id, ExamStudent.school_id == school_id)
        .order_by(ExamStudent.student_id)
    )).scalars().all()

    if not student_rows:
        return {"columns": []}

    student_by_id = {s.id: s for s in student_rows}
    student_ids = list(student_by_id.keys())

    # 2) All registrations: which students have which subjects
    ess_rows = (await db.execute(
        select(ExamStudentSubject.id, ExamStudentSubject.exam_student_id, ExamStudentSubject.exam_subject_id)
        .where(ExamStudentSubject.exam_student_id.in_(student_ids))
    )).all()

    ess_ids = [r[0] for r in ess_rows]
    ess_map = {r[0]: (r[1], r[2]) for r in ess_rows}

    # Group: exam_subject_id -> list of (ess_id, student_pk)
    by_subject: dict[int, list[tuple[int, int]]] = {}
    for ess_id, student_pk, es_id in ess_rows:
        by_subject.setdefault(es_id, []).append((ess_id, student_pk))

    # 3) All attendance records
    att_rows = (await db.execute(
        select(_Att.exam_student_subject_id, _Att.paper_type, _Att.is_present)
        .where(_Att.exam_student_subject_id.in_(ess_ids))
    )).all() if ess_ids else []

    # Index: ess_id -> {paper_type_value: is_present}
    att_index: dict[int, dict[str, bool]] = {}
    for ess_id, pt, is_present in att_rows:
        pt_val = pt.value if hasattr(pt, 'value') else pt
        att_index.setdefault(ess_id, {})[pt_val] = is_present

    # 4) Get subject configs to know applicable papers
    subject_ids_in_scope = list(by_subject.keys())
    exam_subjects = {
        es.id: es for es in (await db.execute(
            select(ExamSubject).where(ExamSubject.id.in_(subject_ids_in_scope))
        )).scalars().all()
    } if subject_ids_in_scope else {}

    # 5) Build response per column
    columns = []
    for es_id, registrations in by_subject.items():
        es = exam_subjects.get(es_id)
        if not es:
            continue
        for paper in applicable_papers(es):
            paper_val = paper.value
            students_out = []
            for ess_id, student_pk in registrations:
                student = student_by_id[student_pk]
                att_for_ess = att_index.get(ess_id, {})
                # Resolve: specific paper overrides ALL
                if paper_val in att_for_ess:
                    resolved = att_for_ess[paper_val]
                elif "ALL" in att_for_ess:
                    resolved = att_for_ess["ALL"]
                else:
                    resolved = None  # no record yet
                students_out.append({
                    "student_id": student.student_id,
                    "full_name": student.full_name,
                    "attendance": resolved,
                })
            # Sort by student_id
            students_out.sort(key=lambda x: x["student_id"])
            columns.append({
                "exam_subject_id": es_id,
                "paper_type": paper_val,
                "students": students_out,
            })

    return {"columns": columns}


# ---------------- incidents ----------------

@router.post("/{exam_id}/incidents", response_model=IncidentOut, status_code=201)
async def create_incident(
    exam_id: uuid.UUID, payload: IncidentIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    await _get_exam(db, exam_id)
    ess_id = None
    if payload.student_id is not None:
        try:
            scope = await resolve_student_scope(
                db, exam_id=exam_id, student_id=payload.student_id, exam_subject_id=payload.exam_subject_id
            )
        except ValidationError as exc:
            raise _vhttp(exc)
        ess_id = scope.exam_student_subject.id
    row = ExamIncident(
        exam_id=exam_id, exam_student_subject_id=ess_id, school_id=payload.school_id,
        exam_subject_id=payload.exam_subject_id, paper_type=payload.paper_type,
        incident_type=payload.incident_type,
        documented_in_supervisor_cal=payload.documented_in_supervisor_cal,
        explanation=payload.explanation, status=IncidentStatus.OPEN,
        raised_by=user.id, raised_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    from ..services import notifications as notif
    await notif.record(db, action="INCIDENT_RAISED", entity_type="exam_incident", entity_id=row.id,
                       actor_id=user.id, exam_id=exam_id,
                       after={"incident_type": payload.incident_type.value, "student_id": payload.student_id})
    await notif.notify(db, type="INCIDENT_RAISED", severity="WARNING", exam_id=exam_id,
                       audience_roles=["REGION_ITS", "COUNCIL_ITS", "SUPER_ADMIN", "SYSTEM_ADMIN"],
                       message=f"Incident raised ({payload.incident_type.value}) for subject scope.")
    return IncidentOut.model_validate(row)


@router.get("/{exam_id}/incidents", response_model=list[IncidentOut])
async def list_incidents(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(_incident_viewer)):
    rows = (await db.execute(select(ExamIncident).where(ExamIncident.exam_id == exam_id))).scalars().all()
    return [IncidentOut.model_validate(r) for r in rows]


@router.patch("/{exam_id}/incidents/{incident_id}/status", response_model=IncidentOut)
async def update_incident_status(
    exam_id: uuid.UUID, incident_id: int, payload: IncidentStatusIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_CHIEF_IT),
):
    row = await db.get(ExamIncident, incident_id)
    if row is None or row.exam_id != exam_id:
        raise HTTPException(404, "incident not found")
    row.status = payload.status
    if payload.status == IncidentStatus.RESOLVED:
        row.resolved_by = user.id
        row.resolved_at = datetime.now(timezone.utc)
    await db.flush()
    return IncidentOut.model_validate(row)


# ---------------- marks ----------------

async def _apply_one(db, exam_id, user, student_id, sm) -> dict:
    scope = await resolve_student_scope(
        db, exam_id=exam_id, student_id=student_id, exam_subject_id=sm.exam_subject_id
    )
    if await is_scope_finalized(db, exam_id=exam_id, school_id=scope.student.school_id,
                                exam_subject_id=sm.exam_subject_id, paper_type=sm.paper_type):
        raise ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized.")
    return await apply_student_paper_marks(
        db, scope=scope, paper_type=sm.paper_type, mode=sm.mode,
        total_marks_obtained=sm.total_marks_obtained,
        items={k: v for k, v in sm.item_map().items()} if sm.mode == FillingMode.ITEM_LEVEL else None,
        actor_id=user.id,
    )


@router.put("/{exam_id}/marks/students/{student_id}")
async def put_student_marks(
    exam_id: uuid.UUID, student_id: str, payload: StudentMarksIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    exam = await _get_exam(db, exam_id)
    _require_entry_open(exam)
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")
    body = payload.model_dump(mode="json")
    body["student_id"] = student_id
    try:
        replay = await idempotency.check_replay(db, idempotency_key, body)
        if replay is not None:
            return {"replayed": True, "result": replay}
        result = await _apply_one(db, exam_id, user, student_id, payload)
        await idempotency.record_receipt(
            db, idempotency_key=idempotency_key, exam_id=exam_id, actor_id=user.id,
            payload=body, result=result,
        )
        from ..services import notifications as notif
        await notif.record(db, action="MARKS_SAVED", entity_type="student_paper_marks",
                           entity_id=f"{student_id}/{payload.exam_subject_id}/{payload.paper_type.value}",
                           actor_id=user.id, exam_id=exam_id, after=result)
    except ValidationError as exc:
        raise _vhttp(exc)
    return {"replayed": False, "result": result}


@router.post("/{exam_id}/marks/batches")
async def post_marks_batch(
    exam_id: uuid.UUID, payload: MarksBatchIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    exam = await _get_exam(db, exam_id)
    _require_entry_open(exam)
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")
    body = payload.model_dump(mode="json")
    try:
        replay = await idempotency.check_replay(db, idempotency_key, body)
        if replay is not None:
            return {"replayed": True, "result": replay}
        results = []
        for sm in payload.submissions:
            results.append(await _apply_one(db, exam_id, user, sm.student_id, sm))
        result = {"applied": len(results), "items": results}
        await idempotency.record_receipt(
            db, idempotency_key=idempotency_key, exam_id=exam_id, actor_id=user.id,
            payload=body, result=result,
        )
    except ValidationError as exc:
        raise _vhttp(exc)
    return {"replayed": False, "result": result}


# ---------------- validation / finalize / reopen ----------------

async def _resolve_scope_subject(db, exam_id, exam_subject_id) -> ExamSubject:
    es = await db.get(ExamSubject, exam_subject_id)
    if es is None or es.exam_id != exam_id:
        raise HTTPException(404, "exam subject not found for this exam")
    return es


@router.get("/{exam_id}/scopes/roster")
async def scope_roster(
    exam_id: uuid.UUID, school_id: int, exam_subject_id: int, paper_type: PaperType,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    """Students in a scope with their current attendance + marks status (online grid).

    For ITEM_LEVEL exams, also returns:
      - ``questions``: ordered list of {number, max_marks} for the paper
      - per-student ``item_marks``: dict of question_number -> marks_obtained

    Optimised: all data loaded in bulk (no per-student queries).
    """
    from sqlalchemy import select as _select
    from ..models.exam import ExamStudent, ExamStudentSubject
    from ..models.marks import Attendance as _Att, ItemMark, TotalMark
    from ..models.scoring import Question
    from lazeims_common.enums import PaperType as _PT
    from lazeims_common.validation.attendance import (
        AttendanceRow as _AR, effective_attendance,
    )

    exam = await _get_exam(db, exam_id)
    es = await _resolve_scope_subject(db, exam_id, exam_subject_id)
    mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")

    # For ITEM_LEVEL: load question config (cached — doesn't change during entry)
    questions_out: list[dict] = []
    qid_to_number: dict[int, str] = {}
    qids: list[int] = []
    if mode == "ITEM_LEVEL":
        from ..services import exam_cache
        cache_key = f"questions:{es.id}:{paper_type.value}"
        cached_q = exam_cache.get(exam_id, cache_key)
        if cached_q is not None:
            questions_out, qid_to_number, qids = cached_q
        else:
            q_rows = (await db.execute(
                _select(Question)
                .where(Question.exam_subject_id == es.id, Question.paper_type == paper_type)
                .order_by(Question.question_number)
            )).scalars().all()
            questions_out = [{"number": q.question_number, "max_marks": float(q.max_marks)} for q in q_rows]
            qid_to_number = {q.id: q.question_number for q in q_rows}
            qids = list(qid_to_number.keys())
            exam_cache.put(exam_id, cache_key, (questions_out, qid_to_number, qids))

    # 1) Load all students in this scope (single query)
    rows = (await db.execute(
        _select(ExamStudent, ExamStudentSubject)
        .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
        .where(ExamStudent.exam_id == exam_id, ExamStudent.school_id == school_id,
               ExamStudentSubject.exam_subject_id == es.id)
        .order_by(ExamStudent.student_id)
    )).all()

    if not rows:
        result: dict = {"mode": mode, "students": []}
        if mode == "ITEM_LEVEL":
            result["questions"] = questions_out
        return result

    ess_ids = [ess.id for _, ess in rows]

    # 2) Bulk-load attendance for all students in this scope (single query)
    att_rows = (await db.execute(
        _select(_Att.exam_student_subject_id, _Att.paper_type, _Att.is_present)
        .where(_Att.exam_student_subject_id.in_(ess_ids))
    )).all()
    # Group by ess_id
    att_by_ess: dict[int, list[_AR]] = {}
    for ess_id, pt, is_present in att_rows:
        att_by_ess.setdefault(ess_id, []).append(_AR(paper_type=pt, is_present=is_present))

    # 3) Bulk-load marks (single query)
    if mode == "ITEM_LEVEL":
        # All item marks for these students + these questions
        im_all: dict[int, dict[str, float]] = {}
        if qids:
            im_rows = (await db.execute(
                _select(ItemMark.exam_student_subject_id, ItemMark.question_id, ItemMark.marks_obtained)
                .where(
                    ItemMark.exam_student_subject_id.in_(ess_ids),
                    ItemMark.question_id.in_(qids),
                )
            )).all()
            for ess_id, qid, marks in im_rows:
                im_all.setdefault(ess_id, {})[qid_to_number[qid]] = float(marks)
    else:
        # All total marks for these students + this paper
        tm_map: dict[int, float] = {}
        tm_rows = (await db.execute(
            _select(TotalMark.exam_student_subject_id, TotalMark.total_marks_obtained)
            .where(
                TotalMark.exam_student_subject_id.in_(ess_ids),
                TotalMark.paper_type == paper_type,
            )
        )).all()
        for ess_id, total in tm_rows:
            tm_map[ess_id] = float(total)

    # 4) Assemble response (no DB queries in this loop)
    out = []
    for student, ess in rows:
        att_list = att_by_ess.get(ess.id, [])
        present = effective_attendance(att_list, paper_type)

        if mode == "ITEM_LEVEL":
            item_marks = im_all.get(ess.id, {})
            has_marks = len(item_marks) == len(qids) and len(qids) > 0
            out.append({
                "student_id": student.student_id,
                "full_name": student.full_name,
                "attendance": present, "has_marks": has_marks, "total": None,
                "item_marks": item_marks,
            })
        else:
            total = tm_map.get(ess.id)
            out.append({
                "student_id": student.student_id,
                "full_name": student.full_name,
                "attendance": present, "has_marks": total is not None,
                "total": total,
            })

    result = {"mode": mode, "students": out}
    if mode == "ITEM_LEVEL":
        result["questions"] = questions_out
    return result


@router.get("/{exam_id}/scopes/report")
async def scope_report(
    exam_id: uuid.UUID, school_id: int, exam_subject_id: int, paper_type: PaperType,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    """Generate marks report data for a scope (school + subject + paper)."""
    from ..models.registry import School as _School, Subject as _Subject
    from ..models.marks import TotalMark, ItemMark
    from ..models.scoring import Question

    exam = await _get_exam(db, exam_id)
    es = await db.get(ExamSubject, exam_subject_id)
    if es is None or es.exam_id != exam_id:
        raise HTTPException(404, "Exam subject not found")

    school = (await db.execute(select(_School).where(_School.id == school_id))).scalar_one_or_none()
    if school is None:
        raise HTTPException(404, "School not found")

    subject = (await db.execute(select(_Subject).where(_Subject.id == es.subject_id))).scalar_one_or_none()

    # Total possible marks for this paper
    total_possible = es.total_marks_theory1
    if paper_type == PaperType.THEORY2:
        total_possible = es.total_marks_theory2
    elif paper_type == PaperType.PRACTICAL:
        total_possible = es.total_marks_practical

    # Get questions if item-level
    questions = []
    q_rows = (await db.execute(
        select(Question)
        .where(Question.exam_subject_id == exam_subject_id, Question.paper_type == paper_type)
        .order_by(Question.question_number)
    )).scalars().all()
    if q_rows:
        questions = [{"number": str(q.question_number), "max_marks": float(q.max_marks)} for q in q_rows]

    # Get students for this scope
    students_q = (await db.execute(
        select(ExamStudent, ExamStudentSubject)
        .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
        .where(
            ExamStudent.exam_id == exam_id,
            ExamStudent.school_id == school_id,
            ExamStudentSubject.exam_subject_id == exam_subject_id,
        )
        .order_by(ExamStudent.student_id)
    )).all()

    if not students_q:
        out_students = []
    else:
        ess_ids = [ess.id for _, ess in students_q]

        # Batch: attendance for all students in this scope
        att_rows = (await db.execute(
            select(Attendance.exam_student_subject_id, Attendance.paper_type, Attendance.is_present)
            .where(
                Attendance.exam_student_subject_id.in_(ess_ids),
                Attendance.paper_type.in_([paper_type, PaperType.ALL]),
            )
        )).all()
        # Resolve: specific paper_type overrides ALL; if only ALL exists, use it
        att_map: dict[int, bool] = {}
        att_all_map: dict[int, bool] = {}
        for ess_id, pt, is_present in att_rows:
            pt_val = pt.value if hasattr(pt, 'value') else pt
            if pt_val == paper_type.value:
                att_map[ess_id] = is_present
            elif pt_val == 'ALL':
                att_all_map[ess_id] = is_present
        # Merge: specific overrides ALL
        for ess_id in ess_ids:
            if ess_id not in att_map:
                att_map[ess_id] = att_all_map.get(ess_id, True)  # default present

        # Batch: total marks
        tm_rows = (await db.execute(
            select(TotalMark.exam_student_subject_id, TotalMark.total_marks_obtained)
            .where(
                TotalMark.exam_student_subject_id.in_(ess_ids),
                TotalMark.paper_type == paper_type,
            )
        )).all()
        tm_map = {r[0]: float(r[1]) if r[1] is not None else None for r in tm_rows}

        # Batch: item marks (if questions exist)
        im_by_ess: dict[int, dict[str, float | None]] = {}
        if questions:
            q_id_to_num = {q.id: str(q.question_number) for q in q_rows}
            qids = list(q_id_to_num.keys())
            im_rows = (await db.execute(
                select(ItemMark.exam_student_subject_id, ItemMark.question_id, ItemMark.marks_obtained)
                .where(
                    ItemMark.exam_student_subject_id.in_(ess_ids),
                    ItemMark.question_id.in_(qids),
                )
            )).all()
            for ess_id, qid, marks in im_rows:
                qnum = q_id_to_num.get(qid, str(qid))
                im_by_ess.setdefault(ess_id, {})[qnum] = float(marks) if marks is not None else None

        out_students = []
        for student, ess in students_q:
            out_students.append({
                "student_id": student.student_id,
                "full_name": student.full_name,
                "attendance": att_map.get(ess.id, True),
                "total_marks": tm_map.get(ess.id),
                "item_marks": im_by_ess.get(ess.id) if questions else None,
            })

    # Get enterer info (who finalized this scope)
    finalized_row = (await db.execute(
        select(FinalizedScope).where(
            FinalizedScope.exam_id == exam_id,
            FinalizedScope.school_id == school_id,
            FinalizedScope.exam_subject_id == exam_subject_id,
            FinalizedScope.paper_type == paper_type,
        )
    )).scalar_one_or_none()

    enterer_initials = None
    finalized_at = None
    if finalized_row:
        finalized_at = finalized_row.created_at.isoformat() if finalized_row.created_at else None

    return {
        "exam_name": exam.name,
        "school_name": school.name,
        "centre_number": school.centre_number,
        "subject_code": subject.code if subject else str(exam_subject_id),
        "subject_name": subject.name if subject else "",
        "paper_type": paper_type.value,
        "total_possible": total_possible,
        "enterer_initials": enterer_initials,
        "finalized_at": finalized_at,
        "questions": questions if questions else None,
        "students": out_students,
    }


@router.get("/{exam_id}/scopes/validation")
async def scope_validation(
    exam_id: uuid.UUID, school_id: int, exam_subject_id: int, paper_type: PaperType,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    exam = await _get_exam(db, exam_id)
    es = await _resolve_scope_subject(db, exam_id, exam_subject_id)
    return await evaluate_scope(db, exam=exam, school_id=school_id, exam_subject=es, paper_type=paper_type)


@router.post("/{exam_id}/scopes/finalize")
async def finalize(
    exam_id: uuid.UUID, payload: ScopeRef,
    db: AsyncSession = Depends(get_session), user: User = Depends(_ENTRY_ACTOR),
):
    exam = await _get_exam(db, exam_id)
    _require_entry_open(exam)
    es = await _resolve_scope_subject(db, exam_id, payload.exam_subject_id)
    finalized, result = await finalize_scope(
        db, exam=exam, school_id=payload.school_id, exam_subject=es,
        paper_type=payload.paper_type, writer_mode=WriterMode.ONLINE, finalized_by=user.id,
    )
    if not finalized:
        raise HTTPException(409, detail={"code": "SCOPE_NOT_COMPLETE", "result": result})
    from ..services import notifications as notif
    await notif.record(db, action="SCOPE_FINALIZED", entity_type="finalized_scope",
                       entity_id=f"{payload.school_id}/{payload.exam_subject_id}/{payload.paper_type.value}",
                       actor_id=user.id, exam_id=exam_id, after=result)
    return {"finalized": True, "result": result}


@router.post("/{exam_id}/scopes/reopen")
async def reopen(
    exam_id: uuid.UUID, payload: ReopenIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(_EXAM_ADMIN),
):
    exam = await _get_exam(db, exam_id)
    es = await _resolve_scope_subject(db, exam_id, payload.exam_subject_id)
    existing = (
        await db.execute(
            select(FinalizedScope).where(
                FinalizedScope.exam_id == exam_id,
                FinalizedScope.school_id == payload.school_id,
                FinalizedScope.exam_subject_id == payload.exam_subject_id,
                FinalizedScope.paper_type == payload.paper_type,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(404, "scope is not finalized")
    new_revision = existing.revision + 1
    await db.execute(delete(FinalizedScope).where(FinalizedScope.id == existing.id))
    db.add(ScopeRevision(
        exam_id=exam_id, school_id=payload.school_id, exam_subject_id=payload.exam_subject_id,
        paper_type=payload.paper_type, revision=new_revision, reason=payload.reason, actor_id=user.id,
    ))
    await db.flush()
    return {"reopened": True, "revision": new_revision}
