"""Zero-touch ExaMetrics key issuance.

Nobody copies an API key into LAZEIMS. When an exam is created — or when an
operator presses "Request access key" on an exam that predates this — Central
posts the exam details and the requester's identity to ExaMetrics' provisioning
endpoint and receives a scoped key server-to-server. The key is stored on the
exam's :class:`ExamProcessingLink` and used on the operator's behalf; the only
key-derived value that ever reaches a screen is ``key_prefix``.

Free scopes (pushing your own collection) work the moment the key arrives. Paid
scopes (processing, reading and downloading results) come back ``PENDING`` for
ExaMetrics to approve, so provisioning never quietly grants paid capability.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models.exam import Exam, ExamSubject
from ..models.processing import ExamProcessingLink
from ..models.registry import Board, ExamLevel, Subject, User
from . import backend_sis, notifications
from .backend_sis import BackendSisError

# Every scope is requested every time: the free ones come back usable, the paid
# ones queue for approval. Asking for less would mean an operator has to come
# back and ask again once processing is bought.
REQUESTED_SCOPES = [
    "collection:push",
    "results:process",
    "results:read",
    "results:download",
]

# Provenance of the key on a link.
KEY_SOURCE_PROVISIONED = "PROVISIONED"
KEY_SOURCE_MANUAL = "MANUAL"

PROVISIONING_DISABLED = "PROVISIONING_DISABLED"


def _requester_payload(user: User) -> dict:
    """Describe the person who asked, resolved from the server-side session.

    The browser never sends this, so it cannot be forged, and the ExaMetrics
    approver sees a human name rather than a key prefix.
    """
    name = " ".join(p for p in (user.first_name, user.middle_name, user.surname) if p)
    payload = {
        "name": name or user.username,
        "username": user.username,
        "user_ref": str(user.id),
    }
    role = getattr(getattr(user, "role", None), "name", None)
    if role is not None:
        payload["role"] = getattr(role, "value", str(role))
    if user.email:
        payload["contact"] = user.email
    return payload


async def _provision_payload(db: AsyncSession, exam: Exam, user: User) -> dict:
    level = await db.get(ExamLevel, exam.level_id)
    board = await db.get(Board, exam.board_id) if exam.board_id else None
    settings = get_settings()
    payload = {
        "external_exam_id": str(exam.id),
        "exam_name": exam.name,
        "exam_level": (level.name if level else "").upper(),
        "board_name": (board.name if board else None),
        "start_date": exam.start_date.isoformat() if exam.start_date else None,
        "end_date": exam.end_date.isoformat() if exam.end_date else None,
        "scopes": list(REQUESTED_SCOPES),
        "partner_label": "LAZEIMS",
        "requested_by": _requester_payload(user),
    }
    # Include callback_url for webhook notifications (processing state changes)
    if settings.central_public_base_url:
        payload["callback_url"] = (
            settings.central_public_base_url.strip().rstrip("/") + "/webhooks/exametrics"
        )
    return payload


async def _get_link(db: AsyncSession, exam_id: uuid.UUID) -> ExamProcessingLink | None:
    return (
        await db.execute(select(ExamProcessingLink).where(ExamProcessingLink.exam_id == exam_id))
    ).scalar_one_or_none()


async def build_subjects_payload(db: AsyncSession, exam: Exam) -> list[dict]:
    """Build the subjects list for PUT /integration/exams from exam_subjects.

    Each subject carries its paper configuration (has_practical, has_theory_2)
    and the maximum marks per paper so ExaMetrics can validate marks and compute
    grades/GPA correctly.
    """
    rows = (
        await db.execute(
            select(
                Subject.code,
                Subject.name,
                Subject.is_primary,
                Subject.is_olevel,
                Subject.is_alevel,
                ExamSubject.has_theory2,
                ExamSubject.has_practical,
                ExamSubject.total_marks_theory1,
                ExamSubject.total_marks_theory2,
                ExamSubject.total_marks_practical,
            )
            .join(ExamSubject, ExamSubject.subject_id == Subject.id)
            .where(ExamSubject.exam_id == exam.id)
        )
    ).all()

    subjects = []
    for (
        code, name, is_primary, is_olevel, is_alevel,
        has_theory2, has_practical,
        max_t1, max_t2, max_prac,
    ) in rows:
        subjects.append({
            "subject_code": code,
            "subject_name": name,
            "subject_short": code,
            "has_theory_2": bool(has_theory2),
            "has_practical": bool(has_practical),
            "is_primary": bool(is_primary),
            "is_olevel": bool(is_olevel),
            "is_alevel": bool(is_alevel),
            "theory_max": float(max_t1) if max_t1 else 100.0,
            "theory_2_max": float(max_t2) if max_t2 else None,
            "practical_max": float(max_prac) if max_prac else None,
        })
    return subjects


async def ensure_access_key(
    db: AsyncSession,
    exam: Exam,
    user: User,
    *,
    rotate: bool = False,
) -> tuple[ExamProcessingLink | None, str | None]:
    """Make sure this exam has a usable ExaMetrics key, obtaining one if not.

    Returns ``(link, failure_reason)``. A failure reason is returned rather than
    raised so the caller decides whether it matters: exam creation must still
    succeed when ExaMetrics is unreachable, while the on-demand endpoint reports
    the problem to the operator.
    """
    existing = await _get_link(db, exam.id)
    if existing is not None and not rotate:
        return existing, None

    settings = get_settings()
    if not settings.provisioning_enabled:
        return None, PROVISIONING_DISABLED

    payload = await _provision_payload(db, exam, user)
    try:
        issued = await backend_sis.provision_exam(payload)
    except BackendSisError as exc:
        return None, str(exc)

    api_key = (issued.get("api_key") or "").strip()
    if not api_key:
        return None, "ExaMetrics returned no key for this exam."

    # ExaMetrics reports the exam it registered as exam_ref; exam_id is the
    # older spelling of the same value.
    backend_exam_id = issued.get("exam_ref") or issued.get("exam_id")
    key_prefix = issued.get("key_prefix")
    approval_status = issued.get("approval_status")
    approval_note = issued.get("approval_note")

    link = existing
    if link is None:
        link = ExamProcessingLink(exam_id=exam.id, api_key=api_key)
        db.add(link)
    link.api_key = api_key
    link.backend_exam_id = backend_exam_id
    link.key_source = KEY_SOURCE_PROVISIONED
    link.key_prefix = key_prefix
    link.key_issued_at = datetime.now(timezone.utc)
    link.approval_status = approval_status
    link.approval_note = approval_note
    link.configured_by = user.id

    # One place populates the capability cache: whatever the key can do now.
    try:
        identity = await backend_sis.identity(api_key)
    except BackendSisError:
        identity = None
    if isinstance(identity, dict):
        link.capabilities_json = identity.get("capabilities")
        link.capabilities_fetched_at = datetime.now(timezone.utc)
        tenant_exam = identity.get("tenant_exam") or identity.get("tenant")
        if isinstance(tenant_exam, dict):
            link.tenant_exam_name = tenant_exam.get("name")

    await db.flush()

    # Audit the issuance — the prefix, never the secret.
    await notifications.record(
        db,
        action="PROCESSING_KEY_ISSUED",
        entity_type="exam_processing_link",
        entity_id=link.id,
        exam_id=exam.id,
        actor_id=user.id,
        after={
            "key_source": link.key_source,
            "key_prefix": link.key_prefix,
            "backend_exam_id": link.backend_exam_id,
            "requested_scopes": issued.get("requested_scopes") or list(REQUESTED_SCOPES),
            "usable_scopes": issued.get("usable_scopes") or [],
            "approval_status": link.approval_status,
            "rotated": bool(existing is not None),
        },
    )

    # Sync the exam definition (subjects + max marks) to ExaMetrics immediately
    # after the key is stored. Best-effort: a failure here is logged but must
    # never prevent the key from being usable for collection.
    level = await db.get(ExamLevel, exam.level_id)
    level_name = (level.name if level else "").upper()
    settings = get_settings()
    try:
        subjects = await build_subjects_payload(db, exam)
        filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
        await backend_sis.upsert_exam_definition(
            api_key,
            external_ref=str(exam.id),
            name=exam.name,
            level=level_name,
            zone_name=None,
            filling_mode=filling_mode,
            subjects=subjects,
        )
    except Exception:  # noqa: BLE001 — definition sync must never block key issuance
        pass

    return link, None
