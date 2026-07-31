"""Processing link: binds a Central exam to its ExaMetrics (backend-sis)
processing exam + purchased API key.

LAZEIMS is a collection product; results processing is performed by ExaMetrics.
An EXAM_ADMIN records, per exam, the ExaMetrics ``exam_id`` and the per-exam
``X-API-Key`` purchased from ExaMetrics. The key is stored here (not in
``Exam.settings``) so it is never serialized in ordinary exam responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
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
    # The per-exam key (X-API-Key). Kept out of Exam.settings so it is never
    # leaked through normal exam serialization, and never returned by any
    # endpoint: it is used server-side on the operator's behalf.
    api_key: Mapped[str] = mapped_column(String(120), nullable=False)

    # How this key got here. PROVISIONED = issued by ExaMetrics on Central's
    # request (the normal path, nobody typed anything); MANUAL = entered by hand
    # through the support escape hatch. Re-provisioning never overwrites MANUAL.
    key_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="MANUAL", default="MANUAL"
    )
    # Non-secret leading fragment of the key — the only key-derived value that
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
