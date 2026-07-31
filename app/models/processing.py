"""Processing link: binds a Central exam to its ExaMetrics (backend-sis)
processing exam + purchased API key.

LAZEIMS is a collection product; results processing is performed by ExaMetrics.
An EXAM_ADMIN records, per exam, the ExaMetrics ``exam_id`` and the per-exam
``X-API-Key`` purchased from ExaMetrics. The key is stored here (not in
``Exam.settings``) so it is never serialized in ordinary exam responses.

The key is encrypted at rest via MultiFernet (see ``app.services.key_crypto``).
The ``api_key`` Python property transparently encrypts on set and decrypts on
get, so all call sites continue to use ``link.api_key`` unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, TimestampMixin


class ExamProcessingLink(Base, TimestampMixin):
    __tablename__ = "exam_processing_links"
    __table_args__ = (
        UniqueConstraint("exam_id", name="uq_exam_processing_links_exam_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True
    )
    # The exam identifier on the ExaMetrics side (its own uuid7 string).
    # Nullable: will be auto-provisioned in Phase 2 if not provided upfront.
    backend_exam_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # The per-exam key (X-API-Key), encrypted at rest via MultiFernet.
    # Access through the ``api_key`` property which handles encrypt/decrypt.
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # How this key got here. PROVISIONED = issued by ExaMetrics on Central's
    # request (the normal path, nobody typed anything); MANUAL = entered by hand
    # through the support escape hatch. Re-provisioning never overwrites MANUAL.
    key_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="MANUAL", default="MANUAL"
    )
    # Non-secret leading fragment of the key -- the only key-derived value that
    # may ever appear on screen.
    key_prefix: Mapped[str | None] = mapped_column(String(24), nullable=True)
    key_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Approval state of the paid scopes on the ExaMetrics side:
    # PENDING | APPROVED | REJECTED | NOT_REQUIRED. Free scopes work regardless.
    approval_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    last_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    configured_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Cached capabilities response from GET /integration/me.
    capabilities_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    capabilities_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Verified name of the ExaMetrics exam account this key is scoped to. Named
    # ``tenant_exam_name``, never bare "tenant": a "tenant" elsewhere in the
    # platform is a subscribing school, which is a different thing entirely.
    tenant_exam_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    @property
    def api_key(self) -> str:
        """Decrypt and return the plaintext API key."""
        from ..services.key_crypto import decrypt_key
        return decrypt_key(self.api_key_encrypted)

    @api_key.setter
    def api_key(self, value: str) -> None:
        """Encrypt and store the API key."""
        from ..services.key_crypto import encrypt_key
        self.api_key_encrypted = encrypt_key(value)


class ExamProcessingRequest(Base, TimestampMixin):
    """Mirrors the state of a processing request on the ExaMetrics side.

    Each request represents one attempt to get processing approved for a
    specific (exam, closeout_revision, configuration_hash) triple. The quote
    and state are mirrored from the remote side; Central never trusts them for
    billing but displays them for the operator.
    """
    __tablename__ = "exam_processing_requests"
    __table_args__ = (
        UniqueConstraint(
            "exam_id", "closeout_revision", "configuration_hash",
            name="uq_processing_requests_exam_rev_hash",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True
    )
    # Remote request_id from ExaMetrics (e.g. "prq_...")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_APPROVAL")
    # The revision and hash this request is valid for.
    closeout_revision: Mapped[int] = mapped_column(nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Server-computed quote (stored for display, never for billing).
    quote_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Who requested it (user id).
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Remote run id once processing starts.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Decision details (from polling or webhook).
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Last time we polled the remote state.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookReceipt(Base):
    """Idempotency record for inbound ExaMetrics webhooks.

    Keyed on (request_id, event, occurred_at) so redelivery of the same webhook
    is a no-op.
    """
    __tablename__ = "exametrics_webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "request_id", "event", "occurred_at",
            name="uq_webhook_receipts_req_event_time",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
