"""Server-side session lifecycle (create / resolve / revoke).

Sessions are stored in the ``sessions`` table so they can be revoked
immediately — we never rely on a stateless token we cannot invalidate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DBSession

from ..config import get_settings
from ..models.registry import Session as SessionRow, User
from ..security import new_token


async def create_session(
    db: DBSession,
    user: User,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionRow:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    row = SessionRow(
        id=new_token(32),
        user_id=user.id,
        csrf_token=new_token(32),
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(row)
    await db.flush()
    return row


async def resolve_session(db: DBSession, session_id: str) -> SessionRow | None:
    """Return a live (non-revoked, non-expired) session row, or None."""
    if not session_id:
        return None
    row = await db.get(SessionRow, session_id)
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


async def revoke_session(db: DBSession, session_id: str) -> None:
    row = await db.get(SessionRow, session_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await db.flush()


async def get_user_for_session(db: DBSession, row: SessionRow) -> User | None:
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user
