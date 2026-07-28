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
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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
    backend_exam_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # The purchased per-exam key (X-API-Key). Kept out of Exam.settings so it is
    # never leaked through normal exam serialization.
    api_key: Mapped[str] = mapped_column(String(120), nullable=False)

    last_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    configured_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
