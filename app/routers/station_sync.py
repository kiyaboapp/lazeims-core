"""Central station machine-sync router (versioned, machine-authenticated).

Routes (Guide §7.1):
    POST /api/v1/station/sync/events
    GET  /api/v1/station/sync/status
    GET  /api/v1/station/sync/package-status
    POST /api/v1/station/sync/reconcile
    POST /api/v1/station/sync/portable/import
    POST /api/v1/station/sync/portable/ack-export

Machine auth: the request carries ``station_code``; the ``X-Station-Key`` header
is verified against ``Station.sync_key_hash``. This scheme is entirely separate
from the human session scheme and can never reach human routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import PaperType, ReconciliationStatus
from lazeims_common.reconcile import reconcile as reconcile_digests
from lazeims_common.schemas.station_sync import SyncRequest

from ..db import get_session
from ..models.assignments import Station
from ..models.station import StationPackage, StationReconciliation, SyncEventReceipt
from ..security import verify_secret
from ..services import station_sync

router = APIRouter(prefix="/station/sync", tags=["station-sync"])


async def authenticate_station(
    db: AsyncSession, station_code: str, station_key: str | None,
) -> Station:
    if not station_key:
        raise HTTPException(401, "Missing station credential")
    station = (
        await db.execute(select(Station).where(Station.station_code == station_code))
    ).scalar_one_or_none()
    if station is None or not station.is_active or not station.sync_key_hash:
        raise HTTPException(401, "Unknown or inactive station")
    if not verify_secret(station.sync_key_hash, station_key):
        raise HTTPException(401, "Invalid station credential")
    return station


async def _validate_package(db, station: Station, package_id: str) -> StationPackage:
    pkg = await db.get(StationPackage, package_id)
    if pkg is None or pkg.station_id != station.id:
        raise HTTPException(404, "Unknown package for this station")
    if pkg.revoked_at is not None:
        raise HTTPException(409, detail={"code": "PACKAGE_REVOKED", "message": "Package has been revoked."})
    return pkg


@router.post("/events")
async def sync_events(
    payload: SyncRequest,
    db: AsyncSession = Depends(get_session),
    x_station_key: str | None = Header(default=None, alias="X-Station-Key"),
):
    station = await authenticate_station(db, payload.station_code, x_station_key)
    pkg = await _validate_package(db, station, payload.package_id)
    events = [e.model_dump(mode="json") for e in payload.events]
    result = await station_sync.process_events(
        db, exam_id=station.exam_id, station_id=station.id, package_id=pkg.package_id, events=events,
    )
    return result


@router.get("/status")
async def sync_status(
    station_code: str, package_id: str,
    db: AsyncSession = Depends(get_session),
    x_station_key: str | None = Header(default=None, alias="X-Station-Key"),
):
    station = await authenticate_station(db, station_code, x_station_key)
    await _validate_package(db, station, package_id)
    accepted = await db.scalar(select(func.count()).select_from(SyncEventReceipt).where(
        SyncEventReceipt.station_id == station.id, SyncEventReceipt.status == "ACCEPTED"))
    rejected = await db.scalar(select(func.count()).select_from(SyncEventReceipt).where(
        SyncEventReceipt.station_id == station.id, SyncEventReceipt.status == "REJECTED"))
    return {"station_code": station_code, "accepted": accepted, "rejected": rejected}


@router.get("/package-status")
async def package_status(
    station_code: str, package_id: str,
    db: AsyncSession = Depends(get_session),
    x_station_key: str | None = Header(default=None, alias="X-Station-Key"),
):
    station = await authenticate_station(db, station_code, x_station_key)
    pkg = await _validate_package(db, station, package_id)
    return {"package_id": pkg.package_id, "package_version": pkg.package_version,
            "revoked": pkg.revoked_at is not None, "rules_version": pkg.rules_version}


class ReconcileScope(BaseModel):
    centre_number: str
    subject_code: str
    paper_type: PaperType
    local_digest: str
    local_counts: dict | None = None


class ReconcileIn(BaseModel):
    package_id: str
    station_code: str
    scopes: list[ReconcileScope]


@router.post("/reconcile")
async def reconcile(
    payload: ReconcileIn,
    db: AsyncSession = Depends(get_session),
    x_station_key: str | None = Header(default=None, alias="X-Station-Key"),
):
    station = await authenticate_station(db, payload.station_code, x_station_key)
    pkg = await _validate_package(db, station, payload.package_id)
    results = []
    all_matched = True
    for sc in payload.scopes:
        central_digest, central_counts = await station_sync.compute_central_scope_digest(
            db, exam_id=station.exam_id, centre_number=sc.centre_number,
            subject_code=sc.subject_code, paper_type=sc.paper_type)
        status = reconcile_digests(sc.local_digest, central_digest)
        all_matched = all_matched and (status == "MATCHED")
        results.append({
            "centre_number": sc.centre_number, "subject_code": sc.subject_code,
            "paper_type": sc.paper_type.value, "status": status,
            "central_digest": central_digest, "central_counts": central_counts,
        })
    # Persist an overall reconciliation record.
    db.add(StationReconciliation(
        station_id=station.id, package_id=pkg.package_id,
        pending_events=0, rejected_events=await db.scalar(
            select(func.count()).select_from(SyncEventReceipt).where(
                SyncEventReceipt.station_id == station.id, SyncEventReceipt.status == "REJECTED")),
        local_counts=None, central_counts={"scopes": len(results)},
        status=ReconciliationStatus.MATCHED if all_matched else ReconciliationStatus.MISMATCHED,
    ))
    await db.flush()
    return {"overall": "MATCHED" if all_matched else "MISMATCHED", "scopes": results}


# ---------------- portable (same service, different transport) ----------------

class PortableIn(BaseModel):
    station_code: str
    key: str          # provisioned separately from the medium (here for dev/tests)
    token: str        # sealed EVENTS envelope


@router.post("/portable/import")
async def portable_import(
    payload: PortableIn,
    db: AsyncSession = Depends(get_session),
    x_station_key: str | None = Header(default=None, alias="X-Station-Key"),
):
    from lazeims_common.portable import DIRECTION_ACKS, open_envelope, seal

    station = await authenticate_station(db, payload.station_code, x_station_key)
    opened = open_envelope(payload.token, key=payload.key, expected_recipient="CENTRAL")
    body = opened.payload  # {package_id, events}
    pkg = await _validate_package(db, station, body["package_id"])
    result = await station_sync.process_events(
        db, exam_id=station.exam_id, station_id=station.id, package_id=pkg.package_id,
        events=body["events"],
    )
    # Return a sealed acknowledgement envelope (same format, reverse direction).
    ack_token = seal(result, key=payload.key, sender="CENTRAL", recipient=station.station_code,
                     direction=DIRECTION_ACKS, sequence=opened.sequence)
    return {"ack_token": ack_token}
