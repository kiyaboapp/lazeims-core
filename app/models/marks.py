"""Attendance, incidents, and marks models (Guide §2.3, §2.4).

Constraints:
    * attendance unique (exam_student_subject_id, paper_type) — at most one ALL
      baseline + one row per real paper;
    * total_marks unique (exam_student_subject_id, paper_type);
    * item_marks unique (exam_student_subject_id, question_id);
    * mark_batch_receipts.idempotency_key unique — replay returns original result.
"""

from __future__ import annotations
import uuid

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from lazeims_common.enums import (
    AttendanceSource,
    IncidentStatus,
    IncidentType,
    PaperType,
)

from ..db import Base, TimestampMixin


def _enum(py_enum, name, length=40):
    return SAEnum(py_enum, name=name, native_enum=False, validate_strings=True, length=length)


class Attendance(Base, TimestampMixin):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("exam_student_subject_id", "paper_type",
                         name="uq_attendance_ess_paper"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_student_subject_id: Mapped[int] = mapped_column(
        ForeignKey("exam_student_subjects.id"), nullable=False, index=True
    )
    paper_type: Mapped[PaperType] = mapped_column(_enum(PaperType, "paper_type", 20), nullable=False)
    is_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[AttendanceSource] = mapped_column(_enum(AttendanceSource, "attendance_source", 60), nullable=False)
    transcribed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    transcribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExamIncident(Base, TimestampMixin):
    __tablename__ = "exam_incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    # nullable: some incidents are school-wide, not tied to one student
    exam_student_subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("exam_student_subjects.id"), nullable=True, index=True
    )
    # scope coordinates (for scope-wide incidents + finalize checks)
    school_id: Mapped[int | None] = mapped_column(ForeignKey("schools.id"), nullable=True, index=True)
    exam_subject_id: Mapped[int | None] = mapped_column(ForeignKey("exam_subjects.id"), nullable=True, index=True)
    paper_type: Mapped[PaperType] = mapped_column(_enum(PaperType, "paper_type", 20), nullable=False)
    incident_type: Mapped[IncidentType] = mapped_column(_enum(IncidentType, "incident_type", 40), nullable=False)
    documented_in_supervisor_cal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    explanation: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status", 20), default=IncidentStatus.OPEN, nullable=False
    )
    raised_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    raised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TotalMark(Base, TimestampMixin):
    __tablename__ = "total_marks"
    __table_args__ = (
        UniqueConstraint("exam_student_subject_id", "paper_type", name="uq_total_marks_ess_paper"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_student_subject_id: Mapped[int] = mapped_column(
        ForeignKey("exam_student_subjects.id"), nullable=False, index=True
    )
    paper_type: Mapped[PaperType] = mapped_column(_enum(PaperType, "paper_type", 20), nullable=False)
    total_marks_obtained: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)  # never null
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ItemMark(Base, TimestampMixin):
    __tablename__ = "item_marks"
    __table_args__ = (
        UniqueConstraint("exam_student_subject_id", "question_id", name="uq_item_marks_ess_question"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_student_subject_id: Mapped[int] = mapped_column(
        ForeignKey("exam_student_subjects.id"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), nullable=False, index=True)
    marks_obtained: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)  # never null
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarkBatchReceipt(Base, TimestampMixin):
    """Idempotency record for a marks write. Replaying the same key returns the
    stored result rather than reprocessing."""

    __tablename__ = "mark_batch_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
