"""Idempotency for marks writes.

A client supplies an ``Idempotency-Key`` on every marks mutation. Replaying the
same key returns the stored result rather than reprocessing. A replay with the
same key but a DIFFERENT payload is a conflict (``EVENT_ID_PAYLOAD_CONFLICT``).
"""

from __future__ import annotations
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import RejectionCode
from lazeims_common.errors import ValidationError
from lazeims_common.hashing import sha256_prefixed

from ..models.marks import MarkBatchReceipt


async def check_replay(
    db: AsyncSession, idempotency_key: str, payload: dict
) -> dict | None:
    """If this key was seen before, return the original result (replay).

    Raises ``EVENT_ID_PAYLOAD_CONFLICT`` if the key was used with a different
    payload. Returns ``None`` if the key is new (caller should process + record).
    """
    existing = (
        await db.execute(
            select(MarkBatchReceipt).where(MarkBatchReceipt.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    if existing.payload_hash != sha256_prefixed(payload):
        raise ValidationError(
            RejectionCode.EVENT_ID_PAYLOAD_CONFLICT,
            "Idempotency key was already used with a different payload.",
            {"idempotency_key": idempotency_key},
        )
    return existing.result_snapshot


async def record_receipt(
    db: AsyncSession,
    *,
    idempotency_key: str,
    exam_id: uuid.UUID,
    actor_id: int | None,
    payload: dict,
    result: dict,
) -> None:
    db.add(MarkBatchReceipt(
        idempotency_key=idempotency_key,
        exam_id=exam_id,
        actor_id=actor_id,
        payload_hash=sha256_prefixed(payload),
        result_snapshot=result,
    ))
    await db.flush()
