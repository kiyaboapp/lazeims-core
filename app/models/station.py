"""Station package + sync bookkeeping models (Guide §2.6).

Station itself lives in ``assignments.py`` (created in Phase 2 as a real table).
This module adds the package and sync-tracking records that the station domain
and later sync phases depend on.
"""

from __future__ import annotations
import uuid

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from lazeims_common.enums import EventStatus, ReconciliationStatus

from ..db import Base, TimestampMixin


def _enum(py_enum, name, length=40):
    return SAEnum(py_enum, name=name, native_enum=False, validate_strings=True, length=length)


class StationPackage(Base, TimestampMixin):
    __tablename__ = "station_packages"
    __table_args__ = (
        UniqueConstraint("station_id", "package_version", name="uq_station_packages_station_version"),
    )

    package_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False, index=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(20), nullable=False)
    software_min_version: Mapped[str] = mapped_column(String(20), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    assigned_scope: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {schools, subjects, papers}
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncEventReceipt(Base, TimestampMixin):
    """Idempotency + audit record for a single applied sync event (Phase 6 uses
    this heavily; created now so the station domain schema is complete)."""

    __tablename__ = "sync_event_receipts"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("station_packages.package_id"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[EventStatus] = mapped_column(_enum(EventStatus, "event_status", 20), nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StationSyncLog(Base, TimestampMixin):
    __tablename__ = "station_sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("station_packages.package_id"), nullable=False, index=True)
    authorized_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sync_type: Mapped[str] = mapped_column(String(20), nullable=False, default="MANUAL")
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS|PARTIAL|FAILED
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StationReconciliation(Base, TimestampMixin):
    __tablename__ = "station_reconciliations"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("station_packages.package_id"), nullable=False, index=True)
    pending_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    local_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    central_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ReconciliationStatus] = mapped_column(
        _enum(ReconciliationStatus, "reconciliation_status", 20), nullable=False
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
