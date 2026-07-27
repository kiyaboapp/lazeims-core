"""Audit + notification services.

Both write within the caller's session/transaction (same-transaction guarantee).
Audit is append-only (this module offers ``record`` only — never update/delete).
A broadcast notification (``user_id is None``) carries ``audience_roles`` and is
resolved to concrete recipients at READ time (never fanned out).
"""

from __future__ import annotations
import uuid

from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.cross_cutting import AuditLog, Notification
from ..models.registry import User


async def record(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    actor_id: int | None = None,
    exam_id: uuid.UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    source: str = "CENTRAL_WEB",
    ip_address: str | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_id=actor_id, exam_id=exam_id, action=action, entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        before_snapshot=before, after_snapshot=after, source=source, ip_address=ip_address,
    )
    db.add(row)
    await db.flush()
    return row


async def notify(
    db: AsyncSession,
    *,
    type: str,
    severity: str,
    message: str,
    user_id: int | None = None,
    exam_id: uuid.UUID | None = None,
    station_id: int | None = None,
    audience_roles: list[str] | None = None,
    channel: str = "IN_APP",
) -> Notification:
    row = Notification(
        user_id=user_id, exam_id=exam_id, station_id=station_id, type=type,
        severity=severity, message=message, audience_roles=audience_roles, channel=channel,
    )
    db.add(row)
    await db.flush()
    return row


async def for_user(db: AsyncSession, user: User, *, limit: int = 100, offset: int = 0) -> list[Notification]:
    """Resolve the caller's notifications: directly-addressed rows, plus
    broadcasts whose ``audience_roles`` include the user's standing role.

    (Exam-scoped audience resolution can be layered on later; standing-role
    broadcast + direct address covers the current notification types.)
    """
    role_name = user.role.name.value
    stmt = (
        select(Notification)
        .where(
            or_(
                Notification.user_id == user.id,
                and_(
                    Notification.user_id.is_(None),
                    Notification.audience_roles.isnot(None),
                    Notification.audience_roles.contains([role_name]),
                ),
            )
        )
        .order_by(Notification.created_at.desc())
        .limit(limit).offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())


async def mark_read(db: AsyncSession, user: User, notification_id: int) -> bool:
    row = await db.get(Notification, notification_id)
    if row is None:
        return False
    # Only the addressed user (or a broadcast recipient) may mark read.
    if row.user_id is not None and row.user_id != user.id:
        return False
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        await db.flush()
    return True
