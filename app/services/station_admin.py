"""Default station-admin credential: provision + deliver.

Every station a Central operator hands out must be usable *immediately* by the
receiving station super-admin (the "chief") — without Central having to issue a
login by hand. So when a package is generated we ensure the station has a
default EXAM_ADMIN login (a password), baked into the package seed as a hash,
and we deliver the plaintext to the station super-admin:

  * by email when SMTP is configured (``SMTP_HOST`` …), and
  * always as an in-app notification + audit record.

The secret is also returned once to the console so it can be handed over even
when no email/SMS provider is wired yet. Only the Argon2 hash is ever stored.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..enums import ExamRoleName
from ..models.assignments import ExamRoleAssignment, Station, StationCredential
from ..models.registry import User
from ..security import hash_secret
from . import notifications as notif


async def _existing_admin_credential(db: AsyncSession, station_id: int) -> StationCredential | None:
    row = (
        await db.execute(
            select(StationCredential)
            .join(ExamRoleAssignment, ExamRoleAssignment.id == StationCredential.exam_role_assignment_id)
            .where(
                StationCredential.station_id == station_id,
                StationCredential.revoked_at.is_(None),
                StationCredential.password_hash.isnot(None),
                ExamRoleAssignment.role == ExamRoleName.EXAM_ADMIN,
            )
        )
    ).scalars().first()
    return row


async def ensure_default_station_admin(
    db: AsyncSession, *, exam_id: uuid.UUID, station: Station, actor: User,
    username: str | None = None, password: str | None = None,
) -> dict | None:
    """Ensure the station has a default admin login. Returns the one-time
    plaintext ``{username, password, assignment_id}`` when a NEW credential was
    created, or ``None`` if one already existed (nothing to show).

    ``username``/``password`` are optional operator overrides; when omitted a
    sensible default (username = station code) and a random password are
    generated. The chosen username is carried into the package so the station
    shows it on the admin sign-in.
    """
    if await _existing_admin_credential(db, station.id) is not None:
        return None

    login_username = (username or station.station_code).strip() or station.station_code
    login_password = (password or "").strip() or secrets.token_urlsafe(9)

    # Anchor the credential on the station's super-admin (its manager), falling
    # back to the acting operator. The EXAM_ADMIN assignment is created if absent.
    anchor_user_id = station.managed_by or actor.id
    assignment = (
        await db.execute(
            select(ExamRoleAssignment).where(
                ExamRoleAssignment.exam_id == exam_id,
                ExamRoleAssignment.user_id == anchor_user_id,
                ExamRoleAssignment.role == ExamRoleName.EXAM_ADMIN,
            )
        )
    ).scalars().first()
    if assignment is None:
        assignment = ExamRoleAssignment(
            exam_id=exam_id, user_id=anchor_user_id,
            role=ExamRoleName.EXAM_ADMIN, appointed_by=actor.id,
        )
        db.add(assignment)
        await db.flush()

    cred = StationCredential(
        exam_role_assignment_id=assignment.id, station_id=station.id,
        password_hash=hash_secret(login_password),
        initials=login_username[:10],  # carries the admin username into the package/station
        issued_at=datetime.now(timezone.utc),
    )
    db.add(cred)
    await db.flush()
    return {"username": login_username, "password": login_password, "assignment_id": assignment.id}


def _send_email(to_email: str, subject: str, body: str) -> bool:
    """Best-effort SMTP send. Returns True on success, False when unconfigured
    or on any error (delivery is never allowed to break package generation)."""
    settings = get_settings()
    if not settings.smtp_host or not to_email:
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = settings.notify_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            if settings.smtp_use_tls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return True
    except Exception:
        return False


async def deliver_station_admin(
    db: AsyncSession, *, exam_id: uuid.UUID, station: Station, actor: User, credential: dict
) -> dict:
    """Deliver a freshly-created station admin login to the station super-admin.

    Emails it when SMTP is configured; always records an audit entry and an
    in-app notification (never containing the secret). Returns delivery status.
    """
    recipient_id = station.managed_by or actor.id
    recipient = await db.get(User, recipient_id)
    email = getattr(recipient, "email", None)

    subject = f"LAZEIMS station login — {station.station_code}"
    body = (
        f"A station package for {station.station_code} ({station.name}) is ready.\n\n"
        f"Station admin login (open the station on the LAN, then Sign in as Admin):\n"
        f"  Username : {credential['username']}\n"
        f"  Password : {credential['password']}\n\n"
        f"Keep this secret. It grants station-admin access for this exam only.\n"
    )
    emailed = _send_email(email, subject, body) if email else False

    await notif.record(
        db, action="STATION_ADMIN_CREDENTIAL_ISSUED", entity_type="station",
        entity_id=station.id, actor_id=actor.id, exam_id=exam_id,
        after={"station_code": station.station_code, "recipient_user_id": recipient_id,
               "channel": "EMAIL" if emailed else "IN_APP"},
    )
    await notif.notify(
        db, type="STATION_ADMIN_CREDENTIAL", severity="INFO",
        message=(f"Default admin login for station {station.station_code} was "
                 + ("emailed to you." if emailed else "created — collect it from the operator.")),
        user_id=recipient_id, exam_id=exam_id, station_id=station.id,
        channel="EMAIL" if emailed else "IN_APP",
    )
    return {"delivered": emailed, "channel": "EMAIL" if emailed else "IN_APP", "recipient_email": email}
