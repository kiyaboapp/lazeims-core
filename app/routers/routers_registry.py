"""Registry CRUD endpoints (regions, councils, wards, schools, subjects, users).

Every mutating route is guarded by ``require_role`` — registry management is a
Super/System Admin capability (permissions matrix §3.2). Reads require an
authenticated user. School writes run through the shared geography validator.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
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
    UserIn,
    UserOut,
    WardIn,
    WardOut,
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

@router.get("/regions", response_model=list[RegionOut])
async def list_regions(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(Region).order_by(Region.name))).scalars().all()
    return [RegionOut.model_validate(r) for r in rows]


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

@router.get("/councils", response_model=list[CouncilOut])
async def list_councils(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(Council).order_by(Council.name))).scalars().all()
    return [CouncilOut.model_validate(r) for r in rows]


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

@router.get("/wards", response_model=list[WardOut])
async def list_wards(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(Ward).order_by(Ward.name))).scalars().all()
    return [WardOut.model_validate(r) for r in rows]


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

@router.get("/schools", response_model=list[SchoolOut])
async def list_schools(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(School).order_by(School.centre_number))).scalars().all()
    return [SchoolOut.model_validate(r) for r in rows]


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

@router.get("/subjects", response_model=list[SubjectOut])
async def list_subjects(db: AsyncSession = Depends(get_session), _: User = Depends(current_user)):
    rows = (await db.execute(select(Subject).order_by(Subject.code))).scalars().all()
    return [SubjectOut.model_validate(r) for r in rows]


@router.post("/subjects", response_model=SubjectOut, status_code=201, dependencies=[Depends(_ADMIN)])
async def create_subject(payload: SubjectIn, db: AsyncSession = Depends(get_session)):
    subject = Subject(**payload.model_dump())
    db.add(subject)
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

@router.get("/users", response_model=list[UserOut], dependencies=[Depends(_ADMIN)])
async def list_users(db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(User).options(selectinload(User.role)).order_by(User.username))).scalars().all()
    return [UserOut.from_user(u) for u in rows]


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
