"""Exam-scoped assignments, writer ownership, station credentials, and scope
lifecycle records (Guide §2.5, §2.9).

* ExamRoleAssignment — exam-scoped roles (EXAM_ADMIN, reps, DATA_ENTERER),
  resolved server-side; never trusted from a request.
* DataEntererScope — optional subject/school narrowing for a DE assignment.
* ScopeWriteAssignment — exactly one writer channel per (exam, school,
  exam_subject, paper). Checked before ANY write.
* StationCredential — PIN(+initials) for a DE or password for a station admin,
  tied to one assignment + one station.
* FinalizedScope / ScopeRevision — durable 'this scope is done' + reopen history.

Station is a minimal placeholder here (full station domain arrives in Phase 4);
it exists now so StationCredential / ScopeWriteAssignment FKs are real.
"""

from __future__ import annotations
import uuid

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from lazeims_common.enums import PaperType, WriterMode

from ..db import Base, TimestampMixin
from ..enums import ExamRoleName


def _enum(py_enum, name, length=40):
    return SAEnum(py_enum, name=name, native_enum=False, validate_strings=True, length=length)


class Station(Base, TimestampMixin):
    """Minimal station registry placeholder (extended in Phase 4)."""

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    station_code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    scope_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'LOCATION' or 'SCHOOLS'
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True)
    council_id: Mapped[int | None] = mapped_column(ForeignKey("councils.id"), nullable=True)
    ward_id: Mapped[int | None] = mapped_column(ForeignKey("wards.id"), nullable=True)
    managed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sync_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    software_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class StationSchool(Base, TimestampMixin):
    """Association table: which schools are explicitly assigned to a station (SCHOOLS scope mode)."""

    __tablename__ = "station_schools"
    __table_args__ = (
        UniqueConstraint("station_id", "school_id", name="uq_station_schools_station_school"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)


class ExamRoleAssignment(Base, TimestampMixin):
    __tablename__ = "exam_role_assignments"
    __table_args__ = (
        UniqueConstraint("exam_id", "user_id", "role", name="uq_exam_role_assignments_exam_user_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[ExamRoleName] = mapped_column(_enum(ExamRoleName, "exam_role_name"), nullable=False)
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True, index=True)
    council_id: Mapped[int | None] = mapped_column(ForeignKey("councils.id"), nullable=True, index=True)
    appointed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class DataEntererScope(Base, TimestampMixin):
    __tablename__ = "data_enterer_scopes"

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_role_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("exam_role_assignments.id"), nullable=False, index=True
    )
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    school_id: Mapped[int | None] = mapped_column(ForeignKey("schools.id"), nullable=True)


class ScopeWriteAssignment(Base, TimestampMixin):
    __tablename__ = "scope_write_assignments"
    __table_args__ = (
        UniqueConstraint(
            "exam_id", "school_id", "exam_subject_id", "paper_type",
            name="uq_scope_write_assignments_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    exam_subject_id: Mapped[int] = mapped_column(ForeignKey("exam_subjects.id"), nullable=False, index=True)
    paper_type: Mapped[PaperType] = mapped_column(_enum(PaperType, "paper_type", 20), nullable=False)
    writer_mode: Mapped[WriterMode] = mapped_column(_enum(WriterMode, "writer_mode"), nullable=False)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class StationCredential(Base, TimestampMixin):
    __tablename__ = "station_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_role_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("exam_role_assignments.id"), nullable=False, index=True
    )
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), nullable=False, index=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # DATA_ENTERER only
    initials: Mapped[str | None] = mapped_column(String(10), nullable=True)  # DATA_ENTERER only
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # station EXAM_ADMIN only
    admin_username: Mapped[str | None] = mapped_column(String(60), nullable=True)  # station EXAM_ADMIN username
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FinalizedScope(Base, TimestampMixin):
    __tablename__ = "finalized_scopes"
    __table_args__ = (
        UniqueConstraint(
            "exam_id", "school_id", "exam_subject_id", "paper_type",
            name="uq_finalized_scopes_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    exam_subject_id: Mapped[int] = mapped_column(ForeignKey("exam_subjects.id"), nullable=False, index=True)
    paper_type: Mapped[PaperType] = mapped_column(_enum(PaperType, "paper_type", 20), nullable=False)
    writer_mode: Mapped[WriterMode] = mapped_column(_enum(WriterMode, "writer_mode"), nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    finalized_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    completion_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ScopeRevision(Base, TimestampMixin):
    """History of reopen actions for a scope (Exam Admin + reason)."""

    __tablename__ = "scope_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    exam_subject_id: Mapped[int] = mapped_column(ForeignKey("exam_subjects.id"), nullable=False, index=True)
    paper_type: Mapped[PaperType] = mapped_column(_enum(PaperType, "paper_type", 20), nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
