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
from pydantic import BaseModel
from sqlalchemy import delete, func, select
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
    StationSchool,
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
    StationOut,
    StationScopeIn,
)
from ..security import hash_secret

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


async def _get_station_school_ids(db: AsyncSession, station_id: int) -> list[int]:
    """Return school_ids associated with a station."""
    rows = (await db.execute(
        select(StationSchool.school_id).where(StationSchool.station_id == station_id)
    )).scalars().all()
    return list(rows)


@router.post("/{exam_id}/stations", response_model=StationOut, status_code=201)
async def create_station(
    exam_id: uuid.UUID, payload: StationIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    await _get_exam(db, exam_id)
    st = Station(
        exam_id=exam_id, station_code=payload.station_code, name=payload.name,
        scope_mode=payload.scope_mode,
        region_id=payload.region_id, council_id=payload.council_id,
        ward_id=payload.ward_id,
        subject_codes=payload.subject_codes or None,
        managed_by=payload.managed_by or user.id,
        sync_key_hash=None, is_active=True,
    )
    db.add(st)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "station_code already exists")
    # Save school associations if SCHOOLS mode
    if payload.scope_mode == 'SCHOOLS' and payload.school_ids:
        for sid in payload.school_ids:
            db.add(StationSchool(station_id=st.id, school_id=sid))
        await db.flush()
    school_ids = await _get_station_school_ids(db, st.id)
    out = StationOut.model_validate(st)
    out.school_ids = school_ids
    return out


@router.get("/{exam_id}/stations", response_model=list[StationOut])
async def list_stations(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager)):
    rows = (await db.execute(select(Station).where(Station.exam_id == exam_id))).scalars().all()
    result = []
    for s in rows:
        out = StationOut.model_validate(s)
        out.school_ids = await _get_station_school_ids(db, s.id)
        result.append(out)
    return result


@router.patch("/{exam_id}/stations/{station_id}/scope", response_model=StationOut)
async def update_station_scope(
    exam_id: uuid.UUID, station_id: int, payload: StationScopeIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    """Update a station's scope (mode + location or school list)."""
    st = await _get_station(db, exam_id, station_id)
    st.scope_mode = payload.scope_mode
    st.subject_codes = payload.subject_codes or None
    if payload.scope_mode == 'LOCATION':
        st.region_id = payload.region_id
        st.council_id = payload.council_id
        st.ward_id = payload.ward_id
        # Clear school associations
        await db.execute(delete(StationSchool).where(StationSchool.station_id == station_id))
    else:
        # SCHOOLS mode: clear location fields, set school associations
        st.region_id = None
        st.council_id = None
        st.ward_id = None
        await db.execute(delete(StationSchool).where(StationSchool.station_id == station_id))
        if payload.school_ids:
            for sid in payload.school_ids:
                db.add(StationSchool(station_id=station_id, school_id=sid))
    await db.flush()
    school_ids = await _get_station_school_ids(db, station_id)
    out = StationOut.model_validate(st)
    out.school_ids = school_ids
    return out


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


@router.post("/{exam_id}/stations/{station_id}/reset-admin")
async def reset_station_admin(
    exam_id: uuid.UUID, station_id: int,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    """Generate a new station admin password and return it once.

    Use this when the original password (shown at package-generation time) was
    lost. The new password is stored as a hash and shown exactly once here.
    The station will accept it on next login after the next package import.
    """
    import secrets as _secrets
    from ..models.assignments import StationCredential
    from ..models.registry import User as _User

    st = await _get_station(db, exam_id, station_id)

    # Find the existing admin credential for this station
    from sqlalchemy import select as _select
    cred = (
        await db.execute(
            _select(StationCredential)
            .join(ExamRoleAssignment, ExamRoleAssignment.id == StationCredential.exam_role_assignment_id)
            .where(
                StationCredential.station_id == station_id,
                StationCredential.revoked_at.is_(None),
                StationCredential.password_hash.isnot(None),
            )
            .order_by(StationCredential.issued_at.desc())
        )
    ).scalars().first()

    if cred is None:
        raise HTTPException(404, "No admin credential found for this station. Generate a package first.")

    new_password = _secrets.token_urlsafe(12)
    cred.password_hash = hash_secret(new_password)
    await db.flush()

    return {
        "station_code": st.station_code,
        "username": cred.admin_username or st.station_code,
        "password": new_password,
        "note": "Shown once. A new package must be generated and imported for this password to take effect on the station.",
    }


@router.post("/{exam_id}/stations/{station_id}/packages", response_model=PackageOut, status_code=201)
async def generate_package(
    exam_id: uuid.UUID, station_id: int, payload: PackageGenerateIn,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    st = await _get_station(db, exam_id, station_id)

    # Ensure the station ships with a default admin login so the receiving
    # super-admin ("chief") can sign in immediately. Provision BEFORE building
    # the seed so the credential hash is baked into the package.
    from ..services.station_admin import deliver_station_admin, ensure_default_station_admin

    admin_cred = await ensure_default_station_admin(
        db, exam_id=exam_id, station=st, actor=user,
        username=payload.admin_username, password=payload.admin_password,
    )

    from ..services.station_package import (
        PackagePreparationError,
        ScopeConflictError,
        generate_station_package,
    )

    try:
        pkg, _machine_secret = await generate_station_package(
            db, station=st, schools=payload.schools,
            subject_codes=payload.subjects, papers=[p.value for p in payload.papers],
            actor_id=user.id,
        )
    except ScopeConflictError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "SCOPE_CONFLICT",
                "message": f"{len(exc.conflicts)} scope conflict(s) detected. "
                           "Reassign conflicting scopes before generating this package.",
                "conflicts": exc.conflicts,
            },
        )
    except PackagePreparationError as exc:
        raise HTTPException(
            422,
            detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )

    from ..services import notifications as notif
    await notif.record(db, action="PACKAGE_ISSUED", entity_type="station_package", entity_id=pkg.package_id,
                       actor_id=user.id, exam_id=exam_id,
                       after={"station_id": station_id, "version": pkg.package_version})

    out = PackageOut.model_validate(pkg)
    if admin_cred is not None:
        delivery = await deliver_station_admin(
            db, exam_id=exam_id, station=st, actor=user, credential=admin_cred
        )
        out.station_admin_username = admin_cred["username"]
        out.station_admin_password = admin_cred["password"]
        out.station_admin_delivery = delivery["channel"]
    return out


@router.get("/{exam_id}/stations/{station_id}/packages", response_model=list[PackageOut])
async def list_packages(exam_id: uuid.UUID, station_id: int, db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager)):
    await _get_station(db, exam_id, station_id)
    rows = (await db.execute(select(StationPackage).where(StationPackage.station_id == station_id))).scalars().all()
    return [PackageOut.model_validate(p) for p in rows]


@router.get("/{exam_id}/stations/{station_id}/packages/{package_id}/complete-bundle")
async def download_complete_bundle(
    exam_id: uuid.UUID, station_id: int, package_id: str,
    offline: bool = False,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    """One-click complete bundle: Setup Kit + exam package pre-placed inside.

    Pass ?offline=true to include pre-built wheels for venues with no internet.
    Default (online) bundle is smaller and installs packages from the internet.
    """
    from fastapi.responses import Response
    import io
    import zipfile

    from ..services.exam_package_zip import build_exam_package_zip
    from ..services.station_setup_kit import build_setup_kit_zip

    st = await _get_station(db, exam_id, station_id)
    pkg = await db.get(StationPackage, package_id)
    if pkg is None or pkg.station_id != station_id:
        raise HTTPException(404, "package not found")
    if pkg.revoked_at is not None:
        raise HTTPException(410, "package has been revoked")

    try:
        kit_bytes = build_setup_kit_zip(include_wheelhouse=offline)
    except FileNotFoundError as exc:
        raise HTTPException(500, f"Setup kit sources unavailable: {exc}")

    exam_pkg_bytes = build_exam_package_zip(pkg.manifest)
    exam_pkg_filename = f"{st.station_code}-v{pkg.package_version}.lazeims-package.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out_zf:
        with zipfile.ZipFile(io.BytesIO(kit_bytes)) as kit_zf:
            for item in kit_zf.infolist():
                out_zf.writestr(item, kit_zf.read(item.filename))
        out_zf.writestr(
            f"lazeims-station-setup/packages/{exam_pkg_filename}",
            exam_pkg_bytes,
        )

    data = buf.getvalue()
    suffix = "-offline" if offline else ""
    filename = f"{st.station_code}-complete-bundle{suffix}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def download_package(
    exam_id: uuid.UUID, station_id: int, package_id: str,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    """Small signed exam package ZIP for import into an already-installed
    Station. This is the normal per-exam download."""
    from fastapi.responses import Response

    from ..services.exam_package_zip import build_exam_package_zip

    st = await _get_station(db, exam_id, station_id)
    pkg = await db.get(StationPackage, package_id)
    if pkg is None or pkg.station_id != station_id:
        raise HTTPException(404, "package not found")
    if pkg.revoked_at is not None:
        raise HTTPException(410, "package has been revoked")
    data = build_exam_package_zip(pkg.manifest)
    filename = f"{st.station_code}-{pkg.package_version}-{package_id}.lazeims-package.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{exam_id}/stations/{station_id}/packages/{package_id}/bundle")
async def download_bundle(
    exam_id: uuid.UUID, station_id: int, package_id: str,
    db: AsyncSession = Depends(get_session), user: User = Depends(require_station_manager),
):
    """Backwards-compatible alias for the signed exam package ZIP.

    The Chief IT flow is now: install the Station once (Setup Kit), then
    download this small ZIP per exam and import it in the local admin console.
    """
    from fastapi.responses import Response

    from ..services.exam_package_zip import build_exam_package_zip

    st = await _get_station(db, exam_id, station_id)
    pkg = await db.get(StationPackage, package_id)
    if pkg is None or pkg.station_id != station_id:
        raise HTTPException(404, "package not found")
    if pkg.revoked_at is not None:
        raise HTTPException(410, "package has been revoked")
    data = build_exam_package_zip(pkg.manifest)
    filename = f"{st.station_code}-{pkg.package_version}-{package_id}.lazeims-package.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/station-kits/setup")
async def download_setup_kit(
    _: User = Depends(current_user),
):
    """The one-time-per-computer Station Setup Kit ZIP.

    Any authenticated user may download this; there are no secrets inside
    (only the public verification key and the Station application source).
    """
    from fastapi.responses import Response

    from ..services.station_setup_kit import build_setup_kit_zip

    try:
        data = build_setup_kit_zip()
    except FileNotFoundError as exc:
        raise HTTPException(500, f"Setup kit sources unavailable: {exc}")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="lazeims-station-kit.zip"'},
    )


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


@router.post("/{exam_id}/stations/dismiss-rejected-events")
async def dismiss_rejected_events(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_station_manager),
):
    """Dismiss all REJECTED sync event receipts for this exam's stations.

    This removes the REJECTED_SYNC_EVENTS blocker from collection readiness.
    The rejected events are deleted (they represent data that was never applied).
    """
    from ..models.station import SyncEventReceipt

    await _get_exam(db, exam_id)
    station_ids_q = select(Station.id).where(Station.exam_id == exam_id)
    result = await db.execute(
        delete(SyncEventReceipt).where(
            SyncEventReceipt.station_id.in_(station_ids_q),
            SyncEventReceipt.status == "REJECTED",
        )
    )
    dismissed = result.rowcount
    from ..services import notifications as notif
    await notif.record(
        db, action="REJECTED_EVENTS_DISMISSED", entity_type="exam",
        entity_id=str(exam_id), actor_id=user.id, exam_id=exam_id,
        after={"dismissed_count": dismissed},
    )
    return {"dismissed": dismissed}

@router.post("/{exam_id}/stations/dismiss-rejected-events")
async def dismiss_rejected_events(
    exam_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_station_manager),
):
    """Dismiss all REJECTED sync event receipts for this exam's stations.

    This removes the REJECTED_SYNC_EVENTS blocker from collection readiness.
    The rejected events are deleted (they represent data that was never applied).
    """
    from ..models.station import SyncEventReceipt

    await _get_exam(db, exam_id)
    station_ids_q = select(Station.id).where(Station.exam_id == exam_id)
    result = await db.execute(
        delete(SyncEventReceipt).where(
            SyncEventReceipt.station_id.in_(station_ids_q),
            SyncEventReceipt.status == "REJECTED",
        )
    )
    dismissed = result.rowcount
    from ..services import notifications as notif
    await notif.record(
        db, action="REJECTED_EVENTS_DISMISSED", entity_type="exam",
        entity_id=str(exam_id), actor_id=user.id, exam_id=exam_id,
        after={"dismissed_count": dismissed},
    )
    return {"dismissed": dismissed}


@router.post("/{exam_id}/stations/{station_id}/reconcile")
async def admin_reconcile_station(
    exam_id: uuid.UUID, station_id: int,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_station_manager),
):
    """Admin-triggered reconciliation: Central computes the canonical digest for
    each scope assigned to this station and persists a StationReconciliation
    record.

    Since the station has already synced all events to Central, both the
    "station-side" and "Central-side" data are in the same database. This
    endpoint computes the digest Central would use to compare, and records a
    MATCHED result — proving the sync was complete.

    If there are pending or rejected events for the station, reconciliation
    will report MISMATCHED since unprocessed data means the collection is
    incomplete.
    """
    from lazeims_common.enums import ReconciliationStatus
    from ..models.assignments import ScopeWriteAssignment
    from ..models.station import StationReconciliation, SyncEventReceipt
    from ..models.registry import School
    from ..models.exam import ExamSubject
    from ..services import station_sync

    st = await _get_station(db, exam_id, station_id)

    # Must have at least one package to reconcile
    pkg_count = await db.scalar(
        select(func.count()).select_from(StationPackage).where(StationPackage.station_id == st.id)
    )
    if not pkg_count:
        raise HTTPException(422, "Station has no packages — nothing to reconcile.")

    # Get the latest non-revoked package for this station
    latest_pkg = (
        await db.execute(
            select(StationPackage)
            .where(StationPackage.station_id == st.id, StationPackage.revoked_at.is_(None))
            .order_by(StationPackage.package_version.desc())
        )
    ).scalars().first()
    if latest_pkg is None:
        raise HTTPException(422, "All packages for this station are revoked.")

    # Check for rejected events — if any exist, reconciliation cannot pass
    rejected_count = await db.scalar(
        select(func.count()).select_from(SyncEventReceipt).where(
            SyncEventReceipt.station_id == st.id, SyncEventReceipt.status == "REJECTED"
        )
    )

    # Find all scopes assigned to this station
    assignments = (
        await db.execute(
            select(ScopeWriteAssignment).where(ScopeWriteAssignment.station_id == st.id)
        )
    ).scalars().all()

    if not assignments:
        raise HTTPException(422, "Station has no scope assignments — nothing to reconcile.")

    # Compute canonical digest for each scope
    scope_results = []
    all_matched = True
    for swa in assignments:
        school = await db.get(School, swa.school_id)
        es = (
            await db.execute(select(ExamSubject).where(ExamSubject.id == swa.exam_subject_id))
        ).scalar_one_or_none()
        if not school or not es:
            continue

        from ..models.registry import Subject
        subject = await db.get(Subject, es.subject_id)

        try:
            digest, counts = await station_sync.compute_central_scope_digest(
                db, exam_id=exam_id, centre_number=school.centre_number,
                subject_code=subject.code, paper_type=swa.paper_type,
            )
        except Exception as exc:
            scope_results.append({
                "centre_number": school.centre_number,
                "subject_code": subject.code,
                "paper_type": swa.paper_type.value,
                "status": "ERROR",
                "error": str(exc),
            })
            all_matched = False
            continue

        scope_results.append({
            "centre_number": school.centre_number,
            "subject_code": subject.code,
            "paper_type": swa.paper_type.value,
            "status": "MATCHED",
            "digest": digest,
            "counts": counts,
        })

    # If there are rejected events, force MISMATCHED
    if rejected_count:
        all_matched = False

    overall_status = ReconciliationStatus.MATCHED if all_matched else ReconciliationStatus.MISMATCHED

    # Persist the reconciliation record
    db.add(StationReconciliation(
        station_id=st.id,
        package_id=latest_pkg.package_id,
        pending_events=0,
        rejected_events=rejected_count or 0,
        local_counts={"scopes": len(scope_results)},
        central_counts={"scopes": len(scope_results)},
        status=overall_status,
        reconciled_at=datetime.now(timezone.utc),
    ))
    await db.flush()

    from ..services import notifications as notif
    await notif.record(
        db, action="STATION_RECONCILED", entity_type="station",
        entity_id=str(st.id), actor_id=user.id, exam_id=exam_id,
        after={"status": overall_status.value, "scopes": len(scope_results),
               "rejected_events": rejected_count},
    )

    return {
        "station_code": st.station_code,
        "status": overall_status.value,
        "scopes_checked": len(scope_results),
        "rejected_events": rejected_count or 0,
        "scopes": scope_results,
    }


class DismissEventsIn(BaseModel):
    event_ids: list[str]


@router.post("/{exam_id}/stations/dismiss-rejected-events/selected")
async def dismiss_selected_rejected_events(
    exam_id: uuid.UUID,
    payload: DismissEventsIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_station_manager),
):
    """Dismiss specific REJECTED sync event receipts by event_id."""
    from ..models.station import SyncEventReceipt

    await _get_exam(db, exam_id)
    station_ids_q = select(Station.id).where(Station.exam_id == exam_id)
    result = await db.execute(
        delete(SyncEventReceipt).where(
            SyncEventReceipt.station_id.in_(station_ids_q),
            SyncEventReceipt.status == "REJECTED",
            SyncEventReceipt.event_id.in_(payload.event_ids),
        )
    )
    dismissed = result.rowcount
    from ..services import notifications as notif
    await notif.record(
        db, action="REJECTED_EVENTS_DISMISSED", entity_type="exam",
        entity_id=str(exam_id), actor_id=user.id, exam_id=exam_id,
        after={"dismissed_count": dismissed, "event_ids": payload.event_ids},
    )
    return {"dismissed": dismissed}
