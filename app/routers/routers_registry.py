"""Registry CRUD endpoints (regions, councils, wards, schools, subjects, users).

Every mutating route is guarded by ``require_role`` — registry management is a
Super/System Admin capability (permissions matrix §3.2). Reads require an
authenticated user. School writes run through the shared geography validator.

List endpoints support optional pagination (page/page_size) and search params.
Detail endpoints return enriched schemas with inline counts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from lazeims_common.errors import ValidationError

from ..db import get_session
from ..deps import current_user, require_role
from ..enums import StandingRoleName
from ..models.registry import (
    Board,
    Council,
    ExamLevel,
    Region,
    Role,
    School,
    Subject,
    User,
    Ward,
)
from ..models.exam import ExamSchool, ExamStudent
from ..schemas import (
    BoardIn,
    BoardOut,
    CouncilIn,
    CouncilOut,
    ExamLevelIn,
    ExamLevelOut,
    RegionIn,
    RegionOut,
    SchoolIn,
    SchoolOut,
    SubjectIn,
    SubjectOut,
    SubjectPatch,
    UserIn,
    UserOut,
    WardIn,
    WardOut,
)
from ..schemas_registry import (
    CouncilDetail,
    PaginatedResponse,
    RegionDetail,
    SchoolDetail,
    WardDetail,
)
from ..security import hash_secret
from ..services.geography import validate_school_geography

router = APIRouter(prefix="/registry", tags=["registry"])

_ADMIN = require_role(StandingRoleName.SUPER_ADMIN, StandingRoleName.SYSTEM_ADMIN)


def _validation_http(exc: ValidationError) -> HTTPException:
    return HTTPException(
        422,
        detail={"code": exc.code.value, "message": exc.message, "details": exc.details},
    )


# ---- regions ----


@router.get("/regions", response_model=PaginatedResponse[RegionOut])
async def list_regions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=120),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    stmt = select(Region)
    if search:
        stmt = stmt.where(Region.name.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Region.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[RegionOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/regions/{region_id}", response_model=RegionDetail)
async def get_region(
    region_id: int,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    region = await db.get(Region, region_id)
    if region is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Region not found")

    council_count = (
        await db.execute(
            select(func.count()).where(Council.region_id == region_id)
        )
    ).scalar_one()

    # Wards via councils in this region
    ward_count = (
        await db.execute(
            select(func.count()).select_from(Ward).join(
                Council, Ward.council_id == Council.id
            ).where(Council.region_id == region_id)
        )
    ).scalar_one()

    school_count = (
        await db.execute(
            select(func.count()).where(School.region_id == region_id)
        )
    ).scalar_one()

    student_count = (
        await db.execute(
            select(func.count()).select_from(ExamStudent).where(
                ExamStudent.school_id.in_(
                    select(School.id).where(School.region_id == region_id)
                )
            )
        )
    ).scalar_one()

    user_count = (
        await db.execute(
            select(func.count()).where(User.region_id == region_id)
        )
    ).scalar_one()

    return RegionDetail(
        id=region.id,
        name=region.name,
        council_count=council_count,
        ward_count=ward_count,
        school_count=school_count,
        student_count=student_count,
        user_count=user_count,
    )


@router.post("/regions", response_model=RegionOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_region(payload: RegionIn, db: AsyncSession = Depends(get_session)):
    region = Region(name=payload.name)
    db.add(region)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Region name already exists")
    return RegionOut.model_validate(region)


# ---- councils ----


@router.get("/councils", response_model=PaginatedResponse[CouncilOut])
async def list_councils(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=120),
    region_id: int | None = Query(None),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    stmt = select(Council)
    if region_id is not None:
        stmt = stmt.where(Council.region_id == region_id)
    if search:
        stmt = stmt.where(Council.name.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Council.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[CouncilOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/councils/{council_id}", response_model=CouncilDetail)
async def get_council(
    council_id: int,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    council = await db.get(Council, council_id)
    if council is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Council not found")

    region = await db.get(Region, council.region_id)

    ward_count = (
        await db.execute(
            select(func.count()).where(Ward.council_id == council_id)
        )
    ).scalar_one()

    school_count = (
        await db.execute(
            select(func.count()).where(School.council_id == council_id)
        )
    ).scalar_one()

    student_count = (
        await db.execute(
            select(func.count()).select_from(ExamStudent).where(
                ExamStudent.school_id.in_(
                    select(School.id).where(School.council_id == council_id)
                )
            )
        )
    ).scalar_one()

    user_count = (
        await db.execute(
            select(func.count()).where(User.council_id == council_id)
        )
    ).scalar_one()

    return CouncilDetail(
        id=council.id,
        name=council.name,
        region_id=council.region_id,
        region_name=region.name if region else "",
        ward_count=ward_count,
        school_count=school_count,
        student_count=student_count,
        user_count=user_count,
    )


@router.post("/councils", response_model=CouncilOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_council(payload: CouncilIn, db: AsyncSession = Depends(get_session)):
    if await db.get(Region, payload.region_id) is None:
        raise HTTPException(422, "region_id does not exist")
    council = Council(name=payload.name, region_id=payload.region_id)
    db.add(council)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Council name already exists")
    return CouncilOut.model_validate(council)


# ---- wards ----


@router.get("/wards", response_model=PaginatedResponse[WardOut])
async def list_wards(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=120),
    council_id: int | None = Query(None),
    region_id: int | None = Query(None),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    stmt = select(Ward)
    if council_id is not None:
        stmt = stmt.where(Ward.council_id == council_id)
    elif region_id is not None:
        # Wards belonging to councils within this region
        stmt = stmt.join(Council, Ward.council_id == Council.id).where(
            Council.region_id == region_id
        )
    if search:
        stmt = stmt.where(Ward.name.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Ward.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[WardOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/wards/{ward_id}", response_model=WardDetail)
async def get_ward(
    ward_id: int,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    ward = await db.get(Ward, ward_id)
    if ward is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ward not found")

    council = await db.get(Council, ward.council_id)
    region = await db.get(Region, council.region_id) if council else None

    school_count = (
        await db.execute(
            select(func.count()).where(School.ward_id == ward_id)
        )
    ).scalar_one()

    return WardDetail(
        id=ward.id,
        name=ward.name,
        council_id=ward.council_id,
        council_name=council.name if council else "",
        region_id=council.region_id if council else 0,
        region_name=region.name if region else "",
        school_count=school_count,
    )


@router.post("/wards", response_model=WardOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_ward(payload: WardIn, db: AsyncSession = Depends(get_session)):
    if await db.get(Council, payload.council_id) is None:
        raise HTTPException(422, "council_id does not exist")
    ward = Ward(name=payload.name, council_id=payload.council_id)
    db.add(ward)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ward name already exists within this council")
    return WardOut.model_validate(ward)


# ---- schools ----


@router.get("/schools", response_model=PaginatedResponse[SchoolOut])
async def list_schools(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=200),
    region_id: int | None = Query(None),
    council_id: int | None = Query(None),
    ward_id: int | None = Query(None),
    school_type: str | None = Query(None),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    stmt = select(School)
    if region_id is not None:
        stmt = stmt.where(School.region_id == region_id)
    if council_id is not None:
        stmt = stmt.where(School.council_id == council_id)
    if ward_id is not None:
        stmt = stmt.where(School.ward_id == ward_id)
    if school_type:
        stmt = stmt.where(School.school_type == school_type)
    if search:
        stmt = stmt.where(
            School.name.ilike(f"%{search}%") | School.centre_number.ilike(f"%{search}%")
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(School.centre_number)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[SchoolOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/schools/{school_id}", response_model=SchoolDetail)
async def get_school(
    school_id: int,
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    school = await db.get(School, school_id)
    if school is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")

    # Resolve geography names
    region = await db.get(Region, school.region_id) if school.region_id else None
    council = await db.get(Council, school.council_id) if school.council_id else None
    ward = await db.get(Ward, school.ward_id) if school.ward_id else None

    student_count = (
        await db.execute(
            select(func.count()).where(ExamStudent.school_id == school_id)
        )
    ).scalar_one()

    exam_count = (
        await db.execute(
            select(func.count()).where(ExamSchool.school_id == school_id)
        )
    ).scalar_one()

    user_count = (
        await db.execute(
            select(func.count()).where(User.school_id == school_id)
        )
    ).scalar_one()

    return SchoolDetail(
        id=school.id,
        centre_number=school.centre_number,
        name=school.name,
        school_type=school.school_type,
        region_id=school.region_id,
        region_name=region.name if region else None,
        council_id=school.council_id,
        council_name=council.name if council else None,
        ward_id=school.ward_id,
        ward_name=ward.name if ward else None,
        can_download_template=school.can_download_template,
        student_count=student_count,
        exam_count=exam_count,
        user_count=user_count,
    )


@router.post("/schools", response_model=SchoolOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_school(payload: SchoolIn, db: AsyncSession = Depends(get_session)):
    try:
        await validate_school_geography(
            db,
            region_id=payload.region_id,
            council_id=payload.council_id,
            ward_id=payload.ward_id,
        )
    except ValidationError as exc:
        raise _validation_http(exc)

    school = School(
        centre_number=payload.centre_number,
        name=payload.name,
        school_type=payload.school_type,
        region_id=payload.region_id,
        council_id=payload.council_id,
        ward_id=payload.ward_id,
        can_download_template=payload.can_download_template,
    )
    db.add(school)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "centre_number already exists")
    return SchoolOut.model_validate(school)


# ---- subjects ----

@router.get("/subjects", response_model=PaginatedResponse[SubjectOut])
async def list_subjects(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=160),
    db: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
):
    stmt = select(Subject)
    if search:
        stmt = stmt.where(
            Subject.name.ilike(f"%{search}%") | Subject.code.ilike(f"%{search}%")
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Subject.code)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[SubjectOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/subjects", response_model=SubjectOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_subject(payload: SubjectIn, db: AsyncSession = Depends(get_session)):
    subject = Subject(**payload.model_dump())
    db.add(subject)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Subject code already exists")
    return SubjectOut.model_validate(subject)


@router.patch("/subjects/{subject_id}", response_model=SubjectOut, dependencies=[Depends(_ADMIN)])
async def patch_subject(subject_id: int, payload: SubjectPatch, db: AsyncSession = Depends(get_session)):
    subject = (await db.execute(select(Subject).where(Subject.id == subject_id))).scalar_one_or_none()
    if not subject:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subject not found")
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return SubjectOut.model_validate(subject)
    for key, value in updates.items():
        setattr(subject, key, value)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Subject code already exists")
    return SubjectOut.model_validate(subject)


# ---- exam levels ----

@router.get("/exam-levels", response_model=list[ExamLevelOut])
async def list_exam_levels(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(ExamLevel).order_by(ExamLevel.name))).scalars().all()
    return [ExamLevelOut.model_validate(r) for r in rows]


@router.post("/exam-levels", response_model=ExamLevelOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_exam_level(payload: ExamLevelIn, db: AsyncSession = Depends(get_session)):
    row = ExamLevel(name=payload.name)
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Exam level already exists")
    return ExamLevelOut.model_validate(row)


# ---- boards ----

@router.get("/boards", response_model=list[BoardOut])
async def list_boards(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(Board).order_by(Board.name))).scalars().all()
    return [BoardOut.model_validate(r) for r in rows]


@router.post("/boards", response_model=BoardOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_board(payload: BoardIn, db: AsyncSession = Depends(get_session)):
    row = Board(name=payload.name, region_id=payload.region_id, council_id=payload.council_id)
    db.add(row)
    await db.flush()
    return BoardOut.model_validate(row)


# ---- users ----

@router.get("/users", response_model=PaginatedResponse[UserOut], dependencies=[Depends(_ADMIN)])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=160),
    region_id: int | None = Query(None),
    council_id: int | None = Query(None),
    school_id: int | None = Query(None),
    db: AsyncSession = Depends(get_session),
):
    stmt = select(User).options(selectinload(User.role))
    if region_id is not None:
        stmt = stmt.where(User.region_id == region_id)
    if council_id is not None:
        stmt = stmt.where(User.council_id == council_id)
    if school_id is not None:
        stmt = stmt.where(User.school_id == school_id)
    if search:
        stmt = stmt.where(
            User.username.ilike(f"%{search}%")
            | User.first_name.ilike(f"%{search}%")
            | User.surname.ilike(f"%{search}%")
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(User.username)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[UserOut.from_user(u) for u in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/users", response_model=UserOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_user(payload: UserIn, db: AsyncSession = Depends(get_session)):
    role = (await db.execute(select(Role).where(Role.name == payload.role))).scalar_one_or_none()
    if role is None:
        raise HTTPException(422, f"Standing role {payload.role.value} is not seeded")
    user = User(
        first_name=payload.first_name,
        middle_name=payload.middle_name,
        surname=payload.surname,
        sex=payload.sex,
        username=payload.username,
        email=payload.email,
        role_id=role.id,
        region_id=payload.region_id,
        council_id=payload.council_id,
        school_id=payload.school_id,
        password_hash=hash_secret(payload.password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists")
    await db.refresh(user, attribute_names=["role"])
    return UserOut.from_user(user)
