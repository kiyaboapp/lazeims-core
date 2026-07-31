"""Collection closeout & export endpoints (delivery §8.10).

EXAM_ADMIN / global admin only. Sealing is readiness-gated and idempotent;
download is server-side authorized. No endpoint sends data to any external
processor.
"""

from __future__ import annotations
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import current_user
from ..deps_exam import require_exam_admin
from ..models.collection import CollectionExportFile, CollectionReadinessRun, CollectionSnapshot
from ..models.exam import Exam
from ..models.registry import User
from ..services import closeout

router = APIRouter(prefix="/exams", tags=["closeout"])
snapshots_router = APIRouter(tags=["closeout"])


async def _get_exam(db, exam_id) -> Exam:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404, "Exam not found")
    return exam


@router.get("/{exam_id}/collection-readiness")
async def collection_readiness(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    exam = await _get_exam(db, exam_id)
    r = await closeout.compute_readiness(db, exam)
    return r.as_dict()


@router.post("/{exam_id}/collection-readiness/runs")
async def create_readiness_run(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    exam = await _get_exam(db, exam_id)
    r = await closeout.compute_readiness(db, exam)
    run = CollectionReadinessRun(
        exam_id=exam_id, closeout_revision=exam.closeout_revision, ready=r.ready,
        blockers=r.blockers, evidence=r.evidence, run_by=user.id)
    db.add(run)
    await db.flush()
    return {"run_id": run.id, **r.as_dict()}


@router.post("/{exam_id}/collection-snapshots")
async def create_snapshot(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    exam = await _get_exam(db, exam_id)

    # Phase gate: sealing is only allowed from ENTRY_LOCKED.
    from lazeims_common.enums import ExamPhase
    if exam.phase != ExamPhase.ENTRY_LOCKED:
        raise HTTPException(
            409,
            detail={
                "code": "CLOSEOUT_BLOCKED",
                "blockers": [{"code": "PHASE_NOT_ENTRY_LOCKED", "detail": f"current: {exam.phase.value}"}],
                "evidence": {},
            },
        )

    try:
        snap = await closeout.seal_snapshot(db, exam, sealed_by=user.id)
    except closeout.CloseoutBlocked as exc:
        raise HTTPException(409, detail={"code": "CLOSEOUT_BLOCKED", "blockers": exc.blockers, "evidence": exc.evidence})
    from ..services import notifications as notif
    await notif.record(db, action="COLLECTION_SEALED", entity_type="collection_snapshot",
                       entity_id=snap.snapshot_id, actor_id=user.id, exam_id=exam_id,
                       after={"closeout_revision": snap.closeout_revision, "content_hash": snap.content_hash})
    return {"snapshot_id": snap.snapshot_id, "closeout_revision": snap.closeout_revision,
            "configuration_hash": snap.configuration_hash, "content_hash": snap.content_hash,
            "status": snap.status}


@router.get("/{exam_id}/collection-snapshots")
async def list_snapshots(exam_id: uuid.UUID, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    rows = (await db.execute(select(CollectionSnapshot).where(CollectionSnapshot.exam_id == exam_id)
            .order_by(CollectionSnapshot.closeout_revision))).scalars().all()
    return [{"snapshot_id": s.snapshot_id, "closeout_revision": s.closeout_revision,
             "configuration_hash": s.configuration_hash, "content_hash": s.content_hash,
             "status": s.status} for s in rows]


class ReopenIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/{exam_id}/collection-reopen")
async def reopen(exam_id: uuid.UUID, payload: ReopenIn, db: AsyncSession = Depends(get_session), user: User = Depends(require_exam_admin())):
    exam = await _get_exam(db, exam_id)
    try:
        new_rev = await closeout.reopen_closeout(db, exam, payload.reason, actor_id=user.id)
    except closeout.CloseoutBlocked as exc:
        raise HTTPException(422, detail={"code": "CLOSEOUT_BLOCKED", "blockers": exc.blockers})
    return {"closeout_revision": new_rev}


# ---- snapshot-scoped routes (not under /exams) ----

async def _snapshot_admin(snapshot_id: str, db: AsyncSession, user: User) -> CollectionSnapshot:
    snap = await db.get(CollectionSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, "snapshot not found")
    # authorize: global admin or EXAM_ADMIN for this snapshot's exam
    from ..deps_exam import get_exam_roles
    from ..enums import ExamRoleName, StandingRoleName
    if user.role.name.value not in {StandingRoleName.SUPER_ADMIN.value, StandingRoleName.SYSTEM_ADMIN.value}:
        roles = await get_exam_roles(db, snap.exam_id, user)
        if ExamRoleName.EXAM_ADMIN.value not in roles:
            raise HTTPException(403, "Not permitted for this exam")
    return snap


@snapshots_router.get("/collection-snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str, db: AsyncSession = Depends(get_session), user: User = Depends(current_user)):
    snap = await _snapshot_admin(snapshot_id, db, user)
    return {"snapshot_id": snap.snapshot_id, "exam_id": snap.exam_id,
            "closeout_revision": snap.closeout_revision, "configuration_hash": snap.configuration_hash,
            "content_hash": snap.content_hash, "status": snap.status, "manifest": snap.manifest}


@snapshots_router.get("/collection-snapshots/{snapshot_id}/download")
async def download_snapshot(snapshot_id: str, db: AsyncSession = Depends(get_session), user: User = Depends(current_user)):
    snap = await _snapshot_admin(snapshot_id, db, user)
    ef = (await db.execute(select(CollectionExportFile).where(
        CollectionExportFile.snapshot_id == snapshot_id).order_by(CollectionExportFile.id.desc()))).scalars().first()
    if ef is None:
        raise HTTPException(404, "export file not found")
    return {"schema_version": ef.schema_version, "content_hash": ef.content_hash, "content": ef.content}
