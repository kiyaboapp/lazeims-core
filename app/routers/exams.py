"""Exam setup + role model endpoints (Phase 2).

Authorization:
  * Creating exams / registry mgmt is global-admin (SUPER/SYSTEM).
  * Per-exam configuration, assignments, phase transitions require EXAM_ADMIN
    (or a global admin) via ``require_exam_admin`` — resolved server-side.
Marks-affecting configuration is blocked once the exam leaves REGISTRATION.
"""

from __future__ import annotations
import uuid

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import ExamPhase
from lazeims_common.errors import ValidationError

from ..db import get_session
from ..deps import current_user, require_role
from ..deps_exam import require_exam_admin
from ..enums import ExamRoleName, StandingRoleName
from ..models.assignments import (
    DataEntererScope,
    ExamRoleAssignment,
    ScopeRevision,
    ScopeWriteAssignment,
)
from ..models.exam import (
    Exam,
    ExamSchool,
    ExamStudent,
    ExamStudentSubject,
    ExamSubject,
)
from ..models.registry import User
from ..models.scoring import Question, QuestionGroup, QuestionTopic
from ..schemas_exam import (
    DataEntererScopeIn,
    ExamIn,
    ExamOut,
    ExamSchoolIn,
    ExamStudentIn,
    ExamStudentOut,
    ExamSubjectIn,
    ExamSubjectOut,
    PhaseTransitionIn,
    ReadinessOut,
    RoleAssignmentIn,
    RoleAssignmentOut,
    SubjectQuestionsIn,
    WriterAssignmentIn,
    WriterAssignmentOut,
)
from ..services.exam_config import seal_configuration_version, validate_subject_scoring
from ..services.exam_phase import (
    assert_transition_allowed,
    compute_entry_open_readiness,
)

router = APIRouter(prefix="/exams", tags=["exams"])

_GLOBAL_ADMIN = require_role(StandingRoleName.SUPER_ADMIN, StandingRoleName.SYSTEM_ADMIN)


def _vhttp(exc: ValidationError) -> HTTPException:
    return HTTPException(422, detail={"code": exc.code.value, "message": exc.message, "details": exc.details})


async def _get_exam(db: AsyncSession, exam_id: uuid.UUID) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    return exam


def _require_registration_phase(exam: Exam) -> None:
    """Marks-affecting config changes only allowed in REGISTRATION."""
    if exam.phase != ExamPhase.REGISTRATION:
        raise HTTPException(
            409,
            f"Configuration is immutable in phase {exam.phase.value}; only allowed in REGISTRATION.",
        )


# ---- exam CRUD ----

@router.get("", response_model=list[ExamOut])
async def list_exams(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(Exam).order_by(Exam.name))).scalars().all()
    return [ExamOut.model_validate(e) for e in rows]


@router.post("", response_model=ExamOut, status_code=201, dependencies=[Depends(_GLOBAL_ADMIN)])
async def create_exam(payload: ExamIn, db: AsyncSession = Depends(get_session)):
    exam = Exam(
        name=payload.name,
        level_id=payload.level_id,
        board_id=payload.board_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        phase=ExamPhase.REGISTRATION,
        settings=payload.settings.model_dump(),
    )
    db.add(exam)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "exam name already exists")

    # Auto-attach subjects based on exam level
    from ..models.registry import ExamLevel, Subject

    level = await db.get(ExamLevel, payload.level_id)
    if level:
        level_name = level.name.upper()
        if level_name in ("SFNA", "PSLE"):
            filter_col = Subject.is_primary
        elif level_name in ("FTNA", "CSEE"):
            filter_col = Subject.is_olevel
        elif level_name == "ACSEE":
            filter_col = Subject.is_alevel
        else:
            filter_col = None

        if filter_col is not None:
            subjects = (await db.execute(select(Subject).where(filter_col == True))).scalars().all()
            for subj in subjects:
                db.add(ExamSubject(
                    exam_id=exam.id,
                    subject_id=subj.id,
                    has_theory2=subj.has_theory2,
                    has_practical=subj.has_practical,
                    total_marks_theory1=100,
                    total_marks_theory2=100 if subj.has_theory2 else 0,
                    total_marks_practical=100 if subj.has_practical else 0,
                ))
            await db.flush()

    return ExamOut.model_validate(exam)


@router.get("/{exam_id}", response_model=ExamOut)
async def get_exam(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    return ExamOut.model_validate(await _get_exam(db, exam_id))


# ---- phase transitions ----

@router.get("/{exam_id}/readiness", response_model=ReadinessOut)
async def exam_readiness(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    exam = await _get_exam(db, exam_id)
    result = await compute_entry_open_readiness(db, exam)
    return ReadinessOut(**result.as_dict())


@router.post("/{exam_id}/transitions", response_model=ExamOut)
async def transition_phase(
    exam_id: uuid.UUID,
    payload: PhaseTransitionIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    try:
        await assert_transition_allowed(db, exam, payload.target_phase)
    except ValidationError as exc:
        raise _vhttp(exc)

    # Reopen requires a reason.
    if exam.phase == ExamPhase.ENTRY_LOCKED and payload.target_phase == ExamPhase.ENTRY_OPEN:
        if not payload.reason or not payload.reason.strip():
            raise HTTPException(422, "A reason is required to reopen entry.")

    exam.phase = payload.target_phase
    await db.flush()
    return ExamOut.model_validate(exam)


# ---- schools ----

@router.get("/{exam_id}/schools")
async def list_exam_schools(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(ExamSchool).where(ExamSchool.exam_id == exam_id))).scalars().all()
    return [{"id": r.id, "school_id": r.school_id} for r in rows]


@router.post("/{exam_id}/schools", status_code=201)
async def attach_school(
    exam_id: uuid.UUID, payload: ExamSchoolIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)
    row = ExamSchool(exam_id=exam_id, school_id=payload.school_id)
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "school already attached")
    return {"id": row.id, "school_id": row.school_id}


# ---- subjects ----

@router.get("/{exam_id}/subjects", response_model=list[ExamSubjectOut])
async def list_exam_subjects(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(ExamSubject).where(ExamSubject.exam_id == exam_id))).scalars().all()
    return [ExamSubjectOut.model_validate(r) for r in rows]


@router.post("/{exam_id}/subjects", response_model=ExamSubjectOut, status_code=201)
async def attach_subject(
    exam_id: uuid.UUID, payload: ExamSubjectIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)
    row = ExamSubject(exam_id=exam_id, **payload.model_dump())
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "subject already offered")
    return ExamSubjectOut.model_validate(row)


# ---- students ----

@router.get("/{exam_id}/students", response_model=list[ExamStudentOut])
async def list_students(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(ExamStudent).where(ExamStudent.exam_id == exam_id).limit(500))).scalars().all()
    return [ExamStudentOut.model_validate(r) for r in rows]


@router.post("/{exam_id}/students", response_model=ExamStudentOut, status_code=201)
async def register_student(
    exam_id: uuid.UUID, payload: ExamStudentIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)
    student = ExamStudent(
        student_id=payload.student_id, exam_id=exam_id, school_id=payload.school_id,
        first_name=payload.first_name, middle_name=payload.middle_name,
        surname=payload.surname, sex=payload.sex,
    )
    db.add(student)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "student_id already registered in this exam")
    for exam_subject_id in payload.subject_ids:
        db.add(ExamStudentSubject(exam_student_id=student.id, exam_subject_id=exam_subject_id))
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(422, "invalid or duplicate subject registration")
    return ExamStudentOut.model_validate(student)


# ---- questions config (item mode) ----

@router.put("/{exam_id}/subjects/{exam_subject_id}/questions")
async def set_subject_questions(
    exam_id: uuid.UUID, exam_subject_id: int, payload: SubjectQuestionsIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)
    es = await db.get(ExamSubject, exam_subject_id)
    if es is None or es.exam_id != exam_id:
        raise HTTPException(404, "exam subject not found for this exam")

    # 1) Validate the payload STRUCTURALLY (topic weights, group pick_count,
    #    duplicate questions, unknown group refs) BEFORE touching the DB, using
    #    the shared lazeims_common rules — no partial writes on invalid input.
    from decimal import Decimal
    from lazeims_common.enums import PaperType as _PT
    from lazeims_common.validation.config import (
        PaperConfig, QuestionConfig, QuestionGroupConfig, TopicWeight,
    )
    from lazeims_common.validation.scoring import validate_paper_config

    paper_max = {
        _PT.THEORY1: es.total_marks_theory1,
        _PT.THEORY2: es.total_marks_theory2,
        _PT.PRACTICAL: es.total_marks_practical,
    }
    groups_by_paper: dict = {}
    for g in payload.groups:
        groups_by_paper.setdefault(g.paper_type, []).append(
            QuestionGroupConfig(code=g.code, pick_count=g.pick_count)
        )
    questions_by_paper: dict = {}
    for q in payload.questions:
        questions_by_paper.setdefault(q.paper_type, []).append(
            QuestionConfig(
                question_number=q.question_number,
                max_marks=Decimal(str(q.max_marks)),
                group_code=q.group_code,
                topics=tuple(TopicWeight(str(t.topic_id), Decimal(str(t.weight))) for t in q.topics),
            )
        )
    try:
        for paper_type in set(questions_by_paper) | set(groups_by_paper):
            validate_paper_config(PaperConfig(
                paper_type=paper_type,
                paper_max=Decimal(str(paper_max.get(paper_type, 0))),
                questions=tuple(questions_by_paper.get(paper_type, [])),
                groups=tuple(groups_by_paper.get(paper_type, [])),
            ))
    except ValidationError as exc:
        raise _vhttp(exc)

    # 2) Verify referenced topics exist (FK safety with a clean 422).
    topic_ids = {t.topic_id for q in payload.questions for t in q.topics}
    if topic_ids:
        from ..models.registry import Topic
        found = set((await db.execute(select(Topic.id).where(Topic.id.in_(topic_ids)))).scalars().all())
        missing = topic_ids - found
        if missing:
            raise HTTPException(422, f"unknown topic id(s): {sorted(missing)}")

    # 3) Replace existing config for this subject.
    q_ids = (await db.execute(select(Question.id).where(Question.exam_subject_id == exam_subject_id))).scalars().all()
    if q_ids:
        await db.execute(delete(QuestionTopic).where(QuestionTopic.question_id.in_(q_ids)))
    await db.execute(delete(Question).where(Question.exam_subject_id == exam_subject_id))
    await db.execute(delete(QuestionGroup).where(QuestionGroup.exam_subject_id == exam_subject_id))
    await db.flush()

    group_id_by_code: dict[tuple, int] = {}
    for g in payload.groups:
        row = QuestionGroup(
            exam_subject_id=exam_subject_id, paper_type=g.paper_type, code=g.code,
            name=g.name, instruction=g.instruction, pick_count=g.pick_count,
        )
        db.add(row)
        await db.flush()
        group_id_by_code[(g.paper_type, g.code)] = row.id

    for q in payload.questions:
        gid = None
        if q.group_code is not None:
            gid = group_id_by_code.get((q.paper_type, q.group_code))
            if gid is None:
                raise HTTPException(422, f"question {q.question_number} references unknown group {q.group_code}")
        qrow = Question(
            exam_subject_id=exam_subject_id, paper_type=q.paper_type,
            question_number=q.question_number, group_id=gid, max_marks=q.max_marks,
        )
        db.add(qrow)
        await db.flush()
        for t in q.topics:
            db.add(QuestionTopic(question_id=qrow.id, topic_id=t.topic_id, weight=t.weight))
    await db.flush()
    return {"status": "ok", "exam_subject_id": exam_subject_id,
            "groups": len(payload.groups), "questions": len(payload.questions)}


@router.post("/{exam_id}/configuration-versions")
async def seal_configuration(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)
    try:
        version = await seal_configuration_version(db, exam, sealed_by=user.id)
    except ValidationError as exc:
        raise _vhttp(exc)
    return {"version": version.version, "configuration_hash": version.configuration_hash}


# ---- role assignments ----

@router.get("/{exam_id}/role-assignments", response_model=list[RoleAssignmentOut])
async def list_role_assignments(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    rows = (await db.execute(select(ExamRoleAssignment).where(ExamRoleAssignment.exam_id == exam_id))).scalars().all()
    return [RoleAssignmentOut.model_validate(r) for r in rows]


@router.post("/{exam_id}/role-assignments", response_model=RoleAssignmentOut, status_code=201)
async def create_role_assignment(
    exam_id: uuid.UUID, payload: RoleAssignmentIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    await _get_exam(db, exam_id)
    if await db.get(User, payload.user_id) is None:
        raise HTTPException(422, "user_id does not exist")
    row = ExamRoleAssignment(
        exam_id=exam_id, user_id=payload.user_id, role=payload.role,
        region_id=payload.region_id, council_id=payload.council_id, appointed_by=user.id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "assignment already exists")
    from ..services import notifications as notif
    await notif.record(db, action="ROLE_ASSIGNED", entity_type="exam_role_assignment",
                       entity_id=row.id, actor_id=user.id, exam_id=exam_id,
                       after={"user_id": payload.user_id, "role": payload.role.value})
    await notif.notify(db, type="ASSIGNMENT_RECEIVED", severity="INFO", user_id=payload.user_id,
                       exam_id=exam_id, message=f"You were assigned {payload.role.value} for this exam.")
    return RoleAssignmentOut.model_validate(row)


@router.post("/{exam_id}/data-enterer-scopes", status_code=201)
async def add_data_enterer_scope(
    exam_id: uuid.UUID, payload: DataEntererScopeIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    assignment = await db.get(ExamRoleAssignment, payload.exam_role_assignment_id)
    if assignment is None or assignment.exam_id != exam_id:
        raise HTTPException(404, "role assignment not found for this exam")
    if assignment.role != ExamRoleName.DATA_ENTERER:
        raise HTTPException(422, "scope can only narrow a DATA_ENTERER assignment")
    row = DataEntererScope(
        exam_role_assignment_id=payload.exam_role_assignment_id,
        subject_id=payload.subject_id, school_id=payload.school_id,
    )
    db.add(row)
    await db.flush()
    return {"id": row.id}


# ---- writer assignments ----

@router.get("/{exam_id}/writer-assignments", response_model=list[WriterAssignmentOut])
async def list_writer_assignments(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    rows = (await db.execute(select(ScopeWriteAssignment).where(ScopeWriteAssignment.exam_id == exam_id))).scalars().all()
    return [WriterAssignmentOut.model_validate(r) for r in rows]


@router.put("/{exam_id}/writer-assignments", response_model=WriterAssignmentOut, status_code=201)
async def set_writer_assignment(
    exam_id: uuid.UUID, payload: WriterAssignmentIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    from lazeims_common.enums import WriterMode
    await _get_exam(db, exam_id)
    if payload.writer_mode == WriterMode.STATION and payload.station_id is None:
        raise HTTPException(422, "station_id is required when writer_mode is STATION")
    # Upsert on the unique scope key.
    existing = (
        await db.execute(
            select(ScopeWriteAssignment).where(
                ScopeWriteAssignment.exam_id == exam_id,
                ScopeWriteAssignment.school_id == payload.school_id,
                ScopeWriteAssignment.exam_subject_id == payload.exam_subject_id,
                ScopeWriteAssignment.paper_type == payload.paper_type,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = ScopeWriteAssignment(
            exam_id=exam_id, school_id=payload.school_id, exam_subject_id=payload.exam_subject_id,
            paper_type=payload.paper_type, writer_mode=payload.writer_mode,
            station_id=payload.station_id, assigned_by=user.id,
        )
        db.add(existing)
    else:
        existing.writer_mode = payload.writer_mode
        existing.station_id = payload.station_id
        existing.assigned_by = user.id
    await db.flush()
    return WriterAssignmentOut.model_validate(existing)
