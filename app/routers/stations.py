"""Central station-management endpoints (Phase 4).

Authorization: EXAM_ADMIN (exam-scoped) or Chief IT (standing REGION_ITS /
COUNCIL_ITS); global admins bypass. Credentials and the machine sync key are
shown exactly ONCE — only hashes are stored. No processing key is ever emitted.
"""

from __future__ import annotations
import uuid

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import current_user
from ..deps_exam import get_exam_roles
from ..enums import ExamRoleName, StandingRoleName
from ..models.assignments import (
    ExamRoleAssignment,
    FinalizedScope,
    Station,
    StationCredential,
)
from ..models.exam import Exam
from ..models.registry import User
from ..models.station import StationPackage
from ..schemas_station import (
    CredentialIn,
    CredentialOut,
    PackageGenerateIn,
    PackageOut,
    StationIn,
    StationKeyOut,
    StationOut,
)
from ..security import hash_secret
from ..services.station_package import generate_station_package

router = APIRouter(prefix="/exams", tags=["stations"])

_CHIEF_IT_STANDING = {StandingRoleName.REGION_ITS.value, StandingRoleName.COUNCIL_ITS.value}
_GLOBAL_ADMIN = {StandingRoleName.SUPER_ADMIN.value, StandingRoleName.SYSTEM_ADMIN.value}


async def require_station_manager(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> User:
    """Allow global admins, exam EXAM_ADMINs, or Chief IT standing roles."""
    if user.role.name.value in _GLOBAL_ADMIN or user.role.name.value in _CHIEF_IT_STANDING:
        return user
    roles = await get_exam_roles(db, exam_id, user)
    if ExamRoleName.EXAM_ADMIN.value in roles:
        return user
    raise HTTPException(403, "Not permitted to manage stations for this exam")


async def _get_exam(db: AsyncSession, exam_id: uuid.UUID) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    return exam


async def _get_station(db: AsyncSession, exam_id: uuid.UUID, station_id: int) -> Station:
    st = await db.get(Station, station_id)
    if st is None or st.exam_id != exam_id:
        raise HTTPException(404, "Station not found for this exam")
    return st


@router.post("/{exam_id}/stations", response_model=StationKeyOut, status_code=201)
async def create_station(
    exam_id: uuid.UUID, payload: StationIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    await _get_exam(db, exam_id)
    sync_key = secrets.token_urlsafe(32)  # shown once
    st = Station(
        exam_id=exam_id, station_code=payload.station_code, name=payload.name,
        region_id=payload.region_id, council_id=payload.council_id,
        managed_by=payload.managed_by or user.id,
        sync_key_hash=hash_secret(sync_key), is_active=True,
    )
    db.add(st)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "station_code already exists")
    return StationKeyOut(station_id=st.id, station_code=st.station_code, sync_key=sync_key)


@router.get("/{exam_id}/stations", response_model=list[StationOut])
async def list_stations(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager)):
    rows = (await db.execute(select(Station).where(Station.exam_id == exam_id))).scalars().all()
    return [StationOut.model_validate(s) for s in rows]


@router.get("/{exam_id}/stations/credential-candidates")
async def credential_candidates(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_station_manager),
):
    """List exam assignments eligible for a station login.

    Station managers need this narrow view to issue credentials; the full exam
    team endpoint remains restricted to exam administrators.
    """
    await _get_exam(db, exam_id)
    rows = (
        await db.execute(
            select(ExamRoleAssignment, User)
            .join(User, User.id == ExamRoleAssignment.user_id)
            .where(
                ExamRoleAssignment.exam_id == exam_id,
                ExamRoleAssignment.role.in_(
                    [ExamRoleName.DATA_ENTERER, ExamRoleName.EXAM_ADMIN]
                ),
            )
            .order_by(ExamRoleAssignment.role, User.surname, User.first_name)
        )
    ).all()
    return [
        {
            "assignment_id": assignment.id,
            "user_id": assignment.user_id,
            "role": assignment.role.value,
            "name": f"{candidate.first_name} {candidate.surname}".strip(),
            "username": candidate.username,
        }
        for assignment, candidate in rows
    ]


@router.post("/{exam_id}/stations/{station_id}/credentials", response_model=CredentialOut, status_code=201)
async def issue_credential(
    exam_id: uuid.UUID, station_id: int, payload: CredentialIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    st = await _get_station(db, exam_id, station_id)
    assignment = await db.get(ExamRoleAssignment, payload.exam_role_assignment_id)
    if assignment is None or assignment.exam_id != exam_id:
        raise HTTPException(404, "role assignment not found for this exam")

    now = datetime.now(timezone.utc)
    if payload.kind == "DE":
        if assignment.role != ExamRoleName.DATA_ENTERER:
            raise HTTPException(422, "DE credential requires a DATA_ENTERER assignment")
        if not payload.initials:
            raise HTTPException(422, "initials are required for a DE credential")
        pin = f"{secrets.randbelow(1_000_000):06d}"  # shown once
        cred = StationCredential(
            exam_role_assignment_id=assignment.id, station_id=st.id,
            pin_hash=hash_secret(pin), initials=payload.initials, issued_at=now,
        )
        db.add(cred)
        await db.flush()
        return CredentialOut(credential_id=cred.id, kind="DE", pin=pin, initials=payload.initials)
    else:  # ADMIN
        if assignment.role != ExamRoleName.EXAM_ADMIN:
            raise HTTPException(422, "ADMIN credential requires an EXAM_ADMIN assignment")
        password = secrets.token_urlsafe(12)  # shown once
        cred = StationCredential(
            exam_role_assignment_id=assignment.id, station_id=st.id,
            password_hash=hash_secret(password), issued_at=now,
        )
        db.add(cred)
        await db.flush()
        return CredentialOut(credential_id=cred.id, kind="ADMIN", password=password)


@router.post("/{exam_id}/stations/{station_id}/packages", response_model=PackageOut, status_code=201)
async def generate_package(
    exam_id: uuid.UUID, station_id: int, payload: PackageGenerateIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    st = await _get_station(db, exam_id, station_id)
    pkg = await generate_station_package(
        db, station=st, schools=payload.schools,
        subject_codes=payload.subjects, papers=[p.value for p in payload.papers],
    )
    from ..services import notifications as notif
    await notif.record(db, action="PACKAGE_ISSUED", entity_type="station_package", entity_id=pkg.package_id,
                       actor_id=user.id, exam_id=exam_id,
                       after={"station_id": station_id, "version": pkg.package_version})
    return PackageOut.model_validate(pkg)


@router.get("/{exam_id}/stations/{station_id}/packages", response_model=list[PackageOut])
async def list_packages(exam_id: uuid.UUID, station_id: int, db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager)):
    await _get_station(db, exam_id, station_id)
    rows = (await db.execute(select(StationPackage).where(StationPackage.station_id == station_id))).scalars().all()
    return [PackageOut.model_validate(p) for p in rows]


@router.get("/{exam_id}/stations/{station_id}/packages/{package_id}/download")
async def download_package(
    exam_id: uuid.UUID, station_id: int, package_id: str,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    await _get_station(db, exam_id, station_id)
    pkg = await db.get(StationPackage, package_id)
    if pkg is None or pkg.station_id != station_id:
        raise HTTPException(404, "package not found")
    if pkg.revoked_at is not None:
        raise HTTPException(410, "package has been revoked")
    # The bundle content: signed manifest + scope-only seed.
    return pkg.manifest


@router.post("/{exam_id}/stations/{station_id}/packages/{package_id}/revoke")
async def revoke_package(
    exam_id: uuid.UUID, station_id: int, package_id: str,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    await _get_station(db, exam_id, station_id)
    pkg = await db.get(StationPackage, package_id)
    if pkg is None or pkg.station_id != station_id:
        raise HTTPException(404, "package not found")
    if pkg.revoked_at is None:
        pkg.revoked_at = datetime.now(timezone.utc)
        await db.flush()
    return {"package_id": package_id, "revoked": True}


@router.get("/{exam_id}/stations/{station_id}/progress")
async def station_progress(
    exam_id: uuid.UUID, station_id: int,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    await _get_station(db, exam_id, station_id)
    packages = await db.scalar(
        select(func.count()).select_from(StationPackage).where(StationPackage.station_id == station_id)
    )
    finalized = await db.scalar(
        select(func.count()).select_from(FinalizedScope).where(
            FinalizedScope.exam_id == exam_id,
        )
    )
    return {"station_id": station_id, "packages": packages, "exam_finalized_scopes": finalized}
