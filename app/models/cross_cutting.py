"""Cross-cutting append-only audit + notifications (Guide §2.9, Part 4).

* AuditLog — every administrative action and every accepted/rejected sync event
  writes one row. Append-only at the application layer: there is NO update or
  delete route for this table, ever.
* Notification — written in the SAME transaction as the event that caused it. A
  ``null user_id`` means "resolve the audience from role + geography at read
  time" (kept small; never fanned out to one row per recipient).
"""

from __future__ import annotations
import uuid

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    exam_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)  # MARKS_SAVED, PACKAGE_ISSUED, ...
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    before_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # CENTRAL_WEB|STATION_SYNC|EXCEL_IMPORT|SYSTEM
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)  # null = broadcast
    exam_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=True, index=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)  # INFO|WARNING|CRITICAL
    audience_roles: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # for broadcasts: standing/exam roles
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="IN_APP", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
