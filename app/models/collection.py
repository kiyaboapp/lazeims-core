"""Collection closeout & export models (Guide §2.8-equivalent, delivery §7.8).

* CollectionReadinessRun — an immutable validation result with blocking codes +
  evidence references.
* CollectionSnapshot — an immutable sealed snapshot: canonical manifest + hash,
  unique by (exam_id, closeout_revision, configuration_hash) so re-sealing is
  idempotent and reopening produces a NEW revision while old ones persist.
* CollectionExportFile — a deterministic, service-neutral export artifact
  (collected data + integrity metadata only; never processing logic).
"""

from __future__ import annotations
import uuid

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, TimestampMixin


class CollectionReadinessRun(Base, TimestampMixin):
    __tablename__ = "collection_readiness_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    closeout_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blockers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    run_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class CollectionSnapshot(Base, TimestampMixin):
    __tablename__ = "collection_snapshots"
    __table_args__ = (
        UniqueConstraint("exam_id", "closeout_revision", "configuration_hash",
                         name="uq_collection_snapshots_exam_rev_hash"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    closeout_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    configuration_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="SEALED", nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)  # canonical, hashable
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    sealed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollectionExportFile(Base, TimestampMixin):
    __tablename__ = "collection_export_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("collection_snapshots.snapshot_id"), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(20), default="JSON", nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    media_type: Mapped[str] = mapped_column(String(80), default="application/json", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)  # inline for dev; object storage in prod
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
