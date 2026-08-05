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

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import ExamPhase
from lazeims_common.errors import ValidationError

from ..db import get_session
from ..config import get_settings
from ..deps import current_user, require_role
from ..deps_exam import get_exam_roles, require_exam_admin
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
from ..models.registry import User, ExamLevel
from ..models.scoring import Question, QuestionGroup, QuestionTopic
from ..schemas_exam import (
    BulkExamSchoolIn,
    DataEntererScopeIn,
    ExamIn,
    ExamOut,
    ExamPatch,
    ExamSchoolIn,
    ExamStudentIn,
    ExamStudentOut,
    ExamSubjectIn,
    ExamSubjectOut,
    ExamSubjectPatch,
    PhaseTransitionIn,
    ReadinessOut,
    RoleAssignmentIn,
    RoleAssignmentOut,
    SubjectQuestionsIn,
    WriterAssignmentIn,
    WriterAssignmentOut,
)
from ..schemas_registry import PaginatedResponse
from ..services import notifications
from ..services.backend_sis import BackendSisError
from ..services.collection_progress import collection_progress
from ..services.exam_config import seal_configuration_version, validate_subject_scoring
from ..services.exametrics_provision import build_subjects_payload, ensure_access_key
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


@router.post("", response_model=ExamOut, status_code=201)
async def create_exam(
    payload: ExamIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(_GLOBAL_ADMIN),
):
    """Create an exam and register it with ExaMetrics.

    The creator is resolved from the session here, not asked for, and travels to
    ExaMetrics as the key requester — so the exam comes into existence with a
    working access key and nobody is ever handed one to paste. Provisioning
    failures are recorded and swallowed: a collection-only deployment, or an
    ExaMetrics outage, must not stop an exam being created.
    """
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
            extra_filter = None
        elif level_name == "CSEE":
            # CSEE: academic O-level subjects only (codes 011–099).
            # Vocational/trade subjects (200+, 800-series, L0x, A0x etc.) are
            # O-level too but belong to FTNA vocational tracks, not CSEE.
            filter_col = Subject.is_olevel
            extra_filter = Subject.code.regexp_match(r'^0[0-9]{2}$')
        elif level_name == "FTNA":
            # FTNA: all O-level subjects including vocational trades
            filter_col = Subject.is_olevel
            extra_filter = None
        elif level_name == "ACSEE":
            filter_col = Subject.is_alevel
            extra_filter = None
        else:
            filter_col = None
            extra_filter = None

        if filter_col is not None:
            stmt = select(Subject).where(filter_col == True)
            if extra_filter is not None:
                stmt = stmt.where(extra_filter)
            subjects = (await db.execute(stmt)).scalars().all()
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

    # Ask ExaMetrics for this exam's access key. Best-effort by design: the exam
    # is still created if it fails, and the operator can retry from the exam's
    # processing tab.
    try:
        _, failure = await ensure_access_key(db, exam, user)
    except Exception as exc:  # noqa: BLE001 - provisioning must never fail creation
        failure = str(exc)
    if failure:
        await notifications.record(
            db,
            action="PROCESSING_KEY_ISSUE_FAILED",
            entity_type="exam",
            entity_id=exam.id,
            exam_id=exam.id,
            actor_id=user.id,
            after={"reason": failure},
        )

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


# ---- current-user exam access ----

@router.get("/{exam_id}/access")
async def get_my_exam_access(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Return server-resolved capabilities for navigation and action discovery.

    This is advisory UI metadata only; every domain endpoint still enforces its
    own authorization. Exam roles always come from ExamRoleAssignment.
    """
    await _get_exam(db, exam_id)
    exam_roles = await get_exam_roles(db, exam_id, user)
    standing_role = user.role.name.value
    is_global_admin = standing_role in {
        StandingRoleName.SUPER_ADMIN.value,
        StandingRoleName.SYSTEM_ADMIN.value,
    }
    is_chief_it = standing_role in {
        StandingRoleName.REGION_ITS.value,
        StandingRoleName.COUNCIL_ITS.value,
    }
    is_exam_admin = ExamRoleName.EXAM_ADMIN.value in exam_roles
    is_data_enterer = ExamRoleName.DATA_ENTERER.value in exam_roles

    return {
        "standing_role": standing_role,
        "exam_roles": sorted(exam_roles),
        "capabilities": {
            "manage_exam": is_global_admin or is_exam_admin,
            "enter_data": is_global_admin or is_exam_admin or is_data_enterer,
            "oversee_attendance": is_global_admin or is_chief_it,
            "manage_stations": is_global_admin or is_exam_admin or is_chief_it,
            "view_incidents": (
                is_global_admin or is_exam_admin or is_data_enterer or is_chief_it
            ),
            "view_progress": True,
            "view_results": True,
        },
    }


# ---- entry-scopes (for data entry navigation) ----

class EntryScopeOut(BaseModel):
    region_id: int | None
    region_name: str | None
    council_id: int | None
    council_name: str | None
    school_id: int
    school_name: str
    centre_number: str


@router.get("/{exam_id}/entry-scopes")
async def get_entry_scopes(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Return schools with their region/council geography for the data entry
    navigation. This is used for the cascading Region -> Council -> School
    selector in the data entry flow.

    Returns all enrolled schools enriched with their geographical hierarchy.
    The client can filter further based on the user's assignment scopes
    (DataEntererScope) if needed.

    Returns a flat list for simplicity; the client builds the hierarchy.
    """
    from ..models.registry import Region, Council, School

    rows = (
        await db.execute(
            select(
                Region.id.label("region_id"),
                Region.name.label("region_name"),
                Council.id.label("council_id"),
                Council.name.label("council_name"),
                School.id.label("school_id"),
                School.name.label("school_name"),
                School.centre_number,
            )
            .select_from(ExamSchool)
            .join(School, School.id == ExamSchool.school_id)
            .outerjoin(Region, Region.id == School.region_id)
            .outerjoin(Council, Council.id == School.council_id)
            .where(ExamSchool.exam_id == exam_id)
            .order_by(Region.name, Council.name, School.centre_number)
        )
    ).all()

    scopes = []
    for r in rows:
        scopes.append(
            EntryScopeOut(
                region_id=r.region_id,
                region_name=r.region_name,
                council_id=r.council_id,
                council_name=r.council_name,
                school_id=r.school_id,
                school_name=r.school_name,
                centre_number=r.centre_number,
            )
        )
    return scopes


# ---- filling progress ----

@router.get("/{exam_id}/filling-progress")
async def get_filling_progress(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    """Per-scope collection progress.

    Kept at this path for backward compatibility, but it now returns the real
    progress payload instead of an empty list: expected vs entered counts,
    attendance transcription, finalization state and the specific blockers per
    (school, subject, paper) scope.
    """
    exam = await _get_exam(db, exam_id)
    return await collection_progress(db, exam=exam)


@router.patch("/{exam_id}", response_model=ExamOut)
async def update_exam(
    exam_id: uuid.UUID,
    payload: ExamPatch,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Update exam metadata and collection settings.

    Settings govern how marks are validated, so they are only mutable during
    REGISTRATION — the same rule the rest of the configuration follows. `phase`
    is deliberately not patchable: it moves only through
    ``POST /exams/{id}/transitions``, which enforces its preconditions.
    """
    exam = await _get_exam(db, exam_id)
    updates = payload.model_dump(exclude_unset=True)

    settings_update = updates.pop("settings", None)
    if settings_update is not None:
        _require_registration_phase(exam)
        # Merge rather than replace, so a partial patch cannot silently drop
        # settings the client did not send.
        exam.settings = {**(exam.settings or {}), **settings_update}

    if updates:
        # Renaming or re-dating an exam is safe at any phase; changing its level
        # is not, because subjects and candidates are already bound to it.
        if "level_id" in updates and updates["level_id"] != exam.level_id:
            _require_registration_phase(exam)
        for key, value in updates.items():
            setattr(exam, key, value)

    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "exam name already exists")
    return ExamOut.model_validate(exam)


# ---- schools ----

@router.get("/{exam_id}/schools")
async def list_exam_schools(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    """Enrolled schools for an exam, enriched with registry identity.

    Returns the school name/centre inline so the client never has to bulk-load
    the whole school registry just to render this list.
    """
    from ..models.registry import School as _School, Council as _Council, Region as _Region

    rows = (
        await db.execute(
            select(
                ExamSchool.id,
                ExamSchool.school_id,
                _School.name,
                _School.centre_number,
                _School.school_type,
                _School.region_id,
                _Region.name.label("region_name"),
                _School.council_id,
                _Council.name.label("council_name"),
            )
            .join(_School, _School.id == ExamSchool.school_id)
            .outerjoin(_Region, _Region.id == _School.region_id)
            .outerjoin(_Council, _Council.id == _School.council_id)
            .where(ExamSchool.exam_id == exam_id)
            .order_by(_School.centre_number)
        )
    ).all()
    return [
        {
            "id": r.id,
            "school_id": r.school_id,
            "school_name": r.name,
            "centre_number": r.centre_number,
            "school_type": r.school_type.value if r.school_type else None,
            "region_id": r.region_id,
            "region_name": r.region_name,
            "council_id": r.council_id,
            "council_name": r.council_name,
        }
        for r in rows
    ]


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


@router.post("/{exam_id}/schools/bulk", status_code=201)
async def bulk_enroll_schools(
    exam_id: uuid.UUID,
    payload: BulkExamSchoolIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_exam_admin()),
):
    """Enroll many schools at once.

    Either pass explicit ``school_ids``, or pass a geography filter
    (``region_id`` / ``council_id`` / ``ward_id``) to enroll every school in
    that area. Schools are always constrained to the level that sits this
    exam, and already-enrolled schools are skipped (idempotent).
    """
    from ..models.registry import ExamLevel as _ExamLevel, School as _School

    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)

    if not payload.school_ids and not any(
        (payload.region_id, payload.council_id, payload.ward_id)
    ):
        raise HTTPException(422, "Provide school_ids or a region/council/ward filter.")

    stmt = select(_School.id)

    if payload.school_ids:
        stmt = stmt.where(_School.id.in_(payload.school_ids))
    else:
        if payload.ward_id is not None:
            stmt = stmt.where(_School.ward_id == payload.ward_id)
        elif payload.council_id is not None:
            stmt = stmt.where(_School.council_id == payload.council_id)
        elif payload.region_id is not None:
            stmt = stmt.where(_School.region_id == payload.region_id)

    # Constrain to the school level that sits this exam.
    level = await db.get(_ExamLevel, exam.level_id)
    level_name = (level.name or "").upper() if level else ""
    if level_name in ("SFNA", "PSLE"):
        stmt = stmt.where(_School.is_primary == True)
    elif level_name in ("FTNA", "CSEE"):
        stmt = stmt.where(_School.is_olevel == True)
    elif level_name == "ACSEE":
        stmt = stmt.where(_School.is_alevel == True)

    if payload.school_type:
        stmt = stmt.where(_School.school_type == payload.school_type)

    candidate_ids = set((await db.execute(stmt)).scalars().all())
    if not candidate_ids:
        return {"enrolled": 0, "skipped": 0, "total_matched": 0}

    already = set(
        (
            await db.execute(
                select(ExamSchool.school_id).where(
                    ExamSchool.exam_id == exam_id,
                    ExamSchool.school_id.in_(candidate_ids),
                )
            )
        ).scalars().all()
    )

    to_add = candidate_ids - already
    for school_id in to_add:
        db.add(ExamSchool(exam_id=exam_id, school_id=school_id))
    await db.flush()

    return {
        "enrolled": len(to_add),
        "skipped": len(already),
        "total_matched": len(candidate_ids),
    }


@router.get("/{exam_id}/schools/stats")
async def exam_schools_stats(
    exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), _: User = Depends(current_user)
):
    """Enrolment totals for the exam, broken down by region and school type.

    Lets the enrolled-schools page show coverage without paging the whole list.
    """
    from ..models.registry import Region as _Region, School as _School

    total = await db.scalar(
        select(func.count()).select_from(ExamSchool).where(ExamSchool.exam_id == exam_id)
    )
    with_candidates = await db.scalar(
        select(func.count(func.distinct(ExamStudent.school_id))).where(
            ExamStudent.exam_id == exam_id
        )
    )
    by_region = (
        await db.execute(
            select(_Region.name, func.count(ExamSchool.id))
            .select_from(ExamSchool)
            .join(_School, _School.id == ExamSchool.school_id)
            .outerjoin(_Region, _Region.id == _School.region_id)
            .where(ExamSchool.exam_id == exam_id)
            .group_by(_Region.name)
            .order_by(_Region.name)
        )
    ).all()
    by_type = (
        await db.execute(
            select(_School.school_type, func.count(ExamSchool.id))
            .select_from(ExamSchool)
            .join(_School, _School.id == ExamSchool.school_id)
            .where(ExamSchool.exam_id == exam_id)
            .group_by(_School.school_type)
        )
    ).all()

    return {
        "total": total or 0,
        "with_candidates": with_candidates or 0,
        "without_candidates": (total or 0) - (with_candidates or 0),
        "by_region": [{"region": r or "Unassigned", "count": c} for r, c in by_region],
        "by_school_type": [
            {"school_type": (t.value if hasattr(t, "value") else str(t)), "count": c}
            for t, c in by_type
        ],
    }


@router.delete("/{exam_id}/schools/{school_id}", status_code=204)
async def detach_school(
    exam_id: uuid.UUID, school_id: int,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    """Un-enrol a school. Refused while it still has registered candidates, so
    marks can never be orphaned."""
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)

    row = (
        await db.execute(
            select(ExamSchool).where(
                ExamSchool.exam_id == exam_id, ExamSchool.school_id == school_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "school is not enrolled in this exam")

    candidates = await db.scalar(
        select(func.count()).select_from(ExamStudent).where(
            ExamStudent.exam_id == exam_id, ExamStudent.school_id == school_id
        )
    )
    if candidates:
        raise HTTPException(
            409,
            f"Cannot un-enrol: {candidates} candidate(s) are registered at this school. "
            "Remove them first.",
        )

    await db.delete(row)
    await db.flush()
    return Response(status_code=204)


# ---- subjects ----

@router.get("/{exam_id}/subjects", response_model=list[ExamSubjectOut])
async def list_exam_subjects(
    exam_id: uuid.UUID,
    school_id: int | None = Query(None, description="When set, return only subjects that have registered students at this school."),
    school_ids: str | None = Query(None, description="Comma-separated school IDs. When set, return only subjects with students at any of these schools."),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    """Subjects offered in this exam, enriched with registry code/name and the
    number of candidates registered for each.

    Pass ``school_id`` to get only subjects that have at least one registered
    student at that school — this is what the data entry scope selector needs
    so enterers never see subjects with no candidates to enter marks for.

    Pass ``school_ids`` (comma-separated) to filter by multiple schools at once
    (used by the package generation dialog).
    """
    from ..models.registry import Subject as _Subject

    # Resolve school filter
    resolved_school_ids: list[int] | None = None
    if school_ids:
        resolved_school_ids = [int(s.strip()) for s in school_ids.split(",") if s.strip().isdigit()]
    elif school_id is not None:
        resolved_school_ids = [school_id]

    school_filter = (
        ExamStudent.school_id.in_(resolved_school_ids)
        if resolved_school_ids
        else ExamStudent.exam_id == exam_id
    )

    counts = dict(
        (
            await db.execute(
                select(ExamStudentSubject.exam_subject_id, func.count())
                .join(ExamSubject, ExamSubject.id == ExamStudentSubject.exam_subject_id)
                .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
                .where(ExamSubject.exam_id == exam_id)
                .where(school_filter)
                .group_by(ExamStudentSubject.exam_subject_id)
            )
        ).all()
    )

    rows_query = (
        select(ExamSubject, _Subject.code, _Subject.name)
        .join(_Subject, _Subject.id == ExamSubject.subject_id)
        .where(ExamSubject.exam_id == exam_id)
    )

    # When filtering by school(s), only return subjects that have students there
    if resolved_school_ids is not None:
        rows_query = rows_query.where(ExamSubject.id.in_(counts.keys()))

    rows = (
        await db.execute(rows_query.order_by(_Subject.code))
    ).all()

    out: list[ExamSubjectOut] = []
    for exam_subject, code, name in rows:
        item = ExamSubjectOut.model_validate(exam_subject)
        item.subject_code = code
        item.subject_name = name
        item.candidate_count = counts.get(exam_subject.id, 0)
        out.append(item)
    return out


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


@router.post("/{exam_id}/subjects/seed-defaults")
async def seed_default_subjects(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    """Re-attach every registry subject that belongs to this exam's level.

    Same mapping used at exam creation (SFNA/PSLE → primary, FTNA/CSEE → O-level,
    ACSEE → A-level). Idempotent: subjects already offered are left untouched,
    so their paper configuration is never overwritten.
    """
    from ..models.registry import ExamLevel as _ExamLevel, Subject as _Subject

    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)

    level = await db.get(_ExamLevel, exam.level_id)
    level_name = (level.name or "").upper() if level else ""
    flag = {
        "SFNA": _Subject.is_primary, "PSLE": _Subject.is_primary,
        "FTNA": _Subject.is_olevel, "CSEE": _Subject.is_olevel,
        "ACSEE": _Subject.is_alevel,
    }.get(level_name)
    if flag is None:
        raise HTTPException(422, f"No default subject set is defined for level {level_name!r}.")

    existing = set(
        (
            await db.execute(
                select(ExamSubject.subject_id).where(ExamSubject.exam_id == exam_id)
            )
        ).scalars().all()
    )
    candidates = (await db.execute(select(_Subject).where(flag.is_(True)))).scalars().all()

    added = 0
    for subject in candidates:
        if subject.id in existing:
            continue
        db.add(
            ExamSubject(
                exam_id=exam_id,
                subject_id=subject.id,
                has_theory2=subject.has_theory2,
                has_practical=subject.has_practical,
                total_marks_theory1=100,
                total_marks_theory2=100 if subject.has_theory2 else 0,
                total_marks_practical=100 if subject.has_practical else 0,
            )
        )
        added += 1
    await db.flush()

    # Sync the exam definition (subjects + max marks) to ExaMetrics after
    # seeding. Best-effort: same error handling as patch_exam_subject.
    try:
        level = await db.get(_ExamLevel, exam.level_id)
        level_name = (level.name or "").upper() if level else ""
        subjects = await build_subjects_payload(db, exam)
        filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
        from ..models.processing import ExamProcessingLink
        link = (
            await db.execute(
                select(ExamProcessingLink).where(ExamProcessingLink.exam_id == exam.id)
            )
        ).scalar_one_or_none()
        if link and link.api_key:
            await backend_sis.upsert_exam_definition(
                link.api_key,
                external_ref=str(exam.id),
                name=exam.name,
                level=level_name,
                zone_name=get_settings().zone_name or None,
                filling_mode=filling_mode,
                subjects=subjects,
            )
    except Exception:  # noqa: BLE001 — best effort, don't fail the POST
        pass

    return {"level": level_name, "added": added, "skipped": len(existing), "matched": len(candidates)}


@router.patch("/{exam_id}/subjects/{exam_subject_id}", response_model=ExamSubjectOut)
async def patch_exam_subject(
    exam_id: uuid.UUID, exam_subject_id: int, payload: ExamSubjectPatch,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    """Update one subject's paper configuration (which papers exist and their
    maximum marks). Locked outside REGISTRATION — these values define how marks
    are validated."""
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)

    row = (
        await db.execute(
            select(ExamSubject).where(
                ExamSubject.id == exam_subject_id, ExamSubject.exam_id == exam_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "subject is not offered in this exam")

    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        if value is not None:
            setattr(row, key, value)

    # Keep maxima coherent with which papers are enabled.
    if not row.has_theory2:
        row.total_marks_theory2 = 0
    elif row.total_marks_theory2 == 0:
        row.total_marks_theory2 = 100
    if not row.has_practical:
        row.total_marks_practical = 0
    elif row.total_marks_practical == 0:
        row.total_marks_practical = 100
    if row.total_marks_theory1 <= 0:
        raise HTTPException(422, "Theory 1 maximum marks must be greater than zero.")

    await db.flush()

    # Sync the exam definition (subjects + max marks) to ExaMetrics after any
    # subject configuration change. Best-effort: ExaMetrics may reject if
    # processing has started; we log but don't fail the PATCH request.
    try:
        level = await db.get(ExamLevel, exam.level_id)
        level_name = (level.name if level else "").upper()
        subjects = await build_subjects_payload(db, exam)
        filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
        # Need to get the link's api_key - pull the processing link
        from ..models.processing import ExamProcessingLink
        link = (
            await db.execute(
                select(ExamProcessingLink).where(ExamProcessingLink.exam_id == exam.id)
            )
        ).scalar_one_or_none()
        if link and link.api_key:
            await backend_sis.upsert_exam_definition(
                link.api_key,
                external_ref=str(exam.id),
                name=exam.name,
                level=level_name,
                zone_name=get_settings().zone_name or None,
                filling_mode=filling_mode,
                subjects=subjects,
            )
    except Exception:  # noqa: BLE001 — best effort, don't fail the PATCH
        pass

    return ExamSubjectOut.model_validate(row)


@router.delete("/{exam_id}/subjects/{exam_subject_id}", status_code=204)
async def detach_subject(
    exam_id: uuid.UUID, exam_subject_id: int,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    """Stop offering a subject. Refused once candidates are registered for it."""
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)

    row = (
        await db.execute(
            select(ExamSubject).where(
                ExamSubject.id == exam_subject_id, ExamSubject.exam_id == exam_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "subject is not offered in this exam")

    registered = await db.scalar(
        select(func.count()).select_from(ExamStudentSubject).where(
            ExamStudentSubject.exam_subject_id == exam_subject_id
        )
    )
    if registered:
        raise HTTPException(
            409,
            f"Cannot remove: {registered} candidate(s) are registered for this subject.",
        )

    await db.delete(row)
    await db.flush()
    return Response(status_code=204)


# ---- students ----

@router.get("/{exam_id}/students", response_model=PaginatedResponse[ExamStudentOut])
async def list_students(
    exam_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    school_id: int | None = Query(None, description="Restrict to one enrolled school."),
    search: str = Query("", max_length=160, description="Match candidate number or name."),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    """Paginated candidate roster, enriched with centre/school and subject count.

    Replaces the previous bare ``.limit(500)``, which silently truncated any
    exam larger than 500 candidates.
    """
    from ..models.registry import School as _School

    stmt = (
        select(ExamStudent, _School.centre_number, _School.name)
        .join(_School, _School.id == ExamStudent.school_id)
        .where(ExamStudent.exam_id == exam_id)
    )
    if school_id is not None:
        stmt = stmt.where(ExamStudent.school_id == school_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            ExamStudent.student_id.ilike(like)
            | ExamStudent.first_name.ilike(like)
            | ExamStudent.middle_name.ilike(like)
            | ExamStudent.surname.ilike(like)
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(_School.centre_number, ExamStudent.student_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    student_ids = [s.id for s, _c, _n in rows]
    counts: dict[int, int] = {}
    if student_ids:
        counts = dict(
            (
                await db.execute(
                    select(ExamStudentSubject.exam_student_id, func.count())
                    .where(ExamStudentSubject.exam_student_id.in_(student_ids))
                    .group_by(ExamStudentSubject.exam_student_id)
                )
            ).all()
        )

    items: list[ExamStudentOut] = []
    for student, centre_number, school_name in rows:
        item = ExamStudentOut.model_validate(student)
        item.centre_number = centre_number
        item.school_name = school_name
        item.subject_count = counts.get(student.id, 0)
        items.append(item)

    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


@router.delete("/{exam_id}/students/{student_row_id}", status_code=204)
async def remove_student(
    exam_id: uuid.UUID, student_row_id: int,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin()),
):
    """De-register a candidate and their subject registrations."""
    exam = await _get_exam(db, exam_id)
    _require_registration_phase(exam)

    student = (
        await db.execute(
            select(ExamStudent).where(
                ExamStudent.id == student_row_id, ExamStudent.exam_id == exam_id
            )
        )
    ).scalar_one_or_none()
    if student is None:
        raise HTTPException(404, "candidate not found in this exam")

    await db.execute(
        delete(ExamStudentSubject).where(ExamStudentSubject.exam_student_id == student.id)
    )
    await db.delete(student)
    await db.flush()
    return Response(status_code=204)


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
