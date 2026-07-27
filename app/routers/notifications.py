"""Notifications (read + mark-read) and audit-log read endpoints (Part 4).

Only two notification endpoints exist (no per-type endpoints). The audit log is
read-only here — there is NO update or delete route for it, ever (append-only).
"""

from __future__ import annotations
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import current_user, require_role
from ..enums import StandingRoleName
from ..models.cross_cutting import AuditLog
from ..models.registry import User
from ..services import notifications as notif

router = APIRouter(tags=["notifications"])

_ADMIN = require_role(StandingRoleName.SUPER_ADMIN, StandingRoleName.SYSTEM_ADMIN)


@router.get("/notifications")
async def list_notifications(
    limit: int = 100, offset: int = 0,
    db: AsyncSession = Depends(get_session), user: User = Depends(current_user),
):
    rows = await notif.for_user(db, user, limit=min(limit, 200), offset=offset)
    return [{"id": n.id, "type": n.type, "severity": n.severity, "message": n.message,
             "exam_id": n.exam_id, "station_id": n.station_id, "created_at": n.created_at.isoformat(),
             "read_at": n.read_at.isoformat() if n.read_at else None} for n in rows]


@router.post("/notifications/{notification_id}/read")
async def read_notification(
    notification_id: int,
    db: AsyncSession = Depends(get_session), user: User = Depends(current_user),
):
    ok = await notif.mark_read(db, user, notification_id)
    if not ok:
        raise HTTPException(404, "notification not found or not addressed to you")
    return {"read": True}


@router.get("/audit-logs", dependencies=[Depends(_ADMIN)])
async def list_audit_logs(
    exam_id: uuid.UUID | None = None, limit: int = 100, offset: int = 0,
    db: AsyncSession = Depends(get_session),
):
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500)).offset(offset)
    if exam_id is not None:
        stmt = stmt.where(AuditLog.exam_id == exam_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [{"id": a.id, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
             "actor_id": a.actor_id, "exam_id": a.exam_id, "source": a.source,
             "occurred_at": a.occurred_at.isoformat()} for a in rows]
