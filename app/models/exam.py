"""Exam core models (Guide §2.2).

Constraints enforced at the DB level:
    * (exam_id, school_id) unique in exam_schools;
    * (exam_id, subject_id) unique in exam_subjects;
    * (exam_id, student_id) unique — student_id supplied as-is, never generated;
    * (exam_student_id, exam_subject_id) unique in exam_student_subjects.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    Computed,
    Date,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazeims_common.enums import ExamPhase, Sex

from ..db import Base, TimestampMixin


def _enum(py_enum, name):
    return SAEnum(py_enum, name=name, native_enum=False, validate_strings=True, length=40)


# Default settings blob for a new exam (Guide §2.2).
DEFAULT_EXAM_SETTINGS = {
    "has_theory2": False,
    "has_practical": False,
    "has_filling_station": False,
    "filling_mode": "TOTAL_MARKS",
    "display_mode": "NAME",
}


class Exam(Base, TimestampMixin):
    __tablename__ = "exams"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    level_id: Mapped[int] = mapped_column(ForeignKey("exam_levels.id"), nullable=False, index=True)
    board_id: Mapped[int | None] = mapped_column(ForeignKey("boards.id"), nullable=True, index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    phase: Mapped[ExamPhase] = mapped_column(
        _enum(ExamPhase, "exam_phase"), default=ExamPhase.REGISTRATION, nullable=False
    )
    settings: Mapped[dict] = mapped_column(JSONB, default=lambda: dict(DEFAULT_EXAM_SETTINGS), nullable=False)
    # Points at the currently-sealed immutable configuration version, if any.
    current_configuration_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Monotonic closeout generation; a reopen increments it so the next seal is
    # a new immutable revision that preserves the old snapshot.
    closeout_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    subjects: Mapped[list["ExamSubject"]] = relationship(back_populates="exam")


class ExamSchool(Base, TimestampMixin):
    __tablename__ = "exam_schools"
    __table_args__ = (
        UniqueConstraint("exam_id", "school_id", name="uq_exam_schools_exam_id_school_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)


class ExamSubject(Base, TimestampMixin):
    __tablename__ = "exam_subjects"
    __table_args__ = (
        UniqueConstraint("exam_id", "subject_id", name="uq_exam_subjects_exam_id_subject_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), nullable=False, index=True)
    has_theory2: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_practical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_marks_theory1: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    total_marks_theory2: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_marks_practical: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    exam: Mapped[Exam] = relationship(back_populates="subjects")


class ExamStudent(Base, TimestampMixin):
    __tablename__ = "exam_students"
    __table_args__ = (
        # student_id is unique WITHIN an exam only (never cross-exam identity).
        UniqueConstraint("exam_id", "student_id", name="uq_exam_students_exam_id_student_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[str] = mapped_column(String(60), nullable=False)  # supplied as-is
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    surname: Mapped[str] = mapped_column(String(80), nullable=False)
    sex: Mapped[Sex] = mapped_column(_enum(Sex, "sex"), nullable=False)
    full_name: Mapped[str] = mapped_column(
        String(242),
        Computed(
            "UPPER(first_name) || ' ' || COALESCE(UPPER(middle_name) || ' ', '') || UPPER(surname)",
            persisted=True,
        ),
    )


class ExamStudentSubject(Base, TimestampMixin):
    __tablename__ = "exam_student_subjects"
    __table_args__ = (
        UniqueConstraint(
            "exam_student_id", "exam_subject_id",
            name="uq_exam_student_subjects_student_subject",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_student_id: Mapped[int] = mapped_column(ForeignKey("exam_students.id"), nullable=False, index=True)
    exam_subject_id: Mapped[int] = mapped_column(ForeignKey("exam_subjects.id"), nullable=False, index=True)
