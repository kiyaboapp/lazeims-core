"""Scoring configuration models (Guide §2.4) and immutable config versions.

* QuestionGroup implements 'answer any N of M' via ``pick_count``.
* Question with ``group_id IS NULL`` is compulsory.
* QuestionTopic weights (analytics axis) must sum to 1.0 per question — enforced
  at config-save time by the service layer using ``lazeims_common``.
* ExamConfigurationVersion is an immutable canonical snapshot + hash sealed
  before entry opens; corrections create a NEW version, never mutate one.
"""

from __future__ import annotations
import uuid

from sqlalchemy import (
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from lazeims_common.enums import PaperType

from ..db import Base, TimestampMixin


def _paper_enum():
    return SAEnum(PaperType, name="paper_type", native_enum=False, validate_strings=True, length=20)


class QuestionGroup(Base, TimestampMixin):
    __tablename__ = "question_groups"
    __table_args__ = (
        UniqueConstraint("exam_subject_id", "paper_type", "code",
                         name="uq_question_groups_subject_paper_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_subject_id: Mapped[int] = mapped_column(ForeignKey("exam_subjects.id"), nullable=False, index=True)
    paper_type: Mapped[PaperType] = mapped_column(_paper_enum(), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    instruction: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pick_count: Mapped[int] = mapped_column(Integer, nullable=False)


class Question(Base, TimestampMixin):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("exam_subject_id", "paper_type", "question_number",
                         name="uq_questions_subject_paper_qnum"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_subject_id: Mapped[int] = mapped_column(ForeignKey("exam_subjects.id"), nullable=False, index=True)
    paper_type: Mapped[PaperType] = mapped_column(_paper_enum(), nullable=False)
    question_number: Mapped[str] = mapped_column(String(20), nullable=False)  # string: '2a' etc.
    group_id: Mapped[int | None] = mapped_column(ForeignKey("question_groups.id"), nullable=True, index=True)
    max_marks: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)


class QuestionTopic(Base, TimestampMixin):
    __tablename__ = "question_topics"
    __table_args__ = (
        UniqueConstraint("question_id", "topic_id", name="uq_question_topics_question_topic"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), nullable=False, index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False, index=True)
    weight: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)  # fraction; sums to 1.0 per question


class ExamConfigurationVersion(Base, TimestampMixin):
    """Immutable sealed snapshot of an exam's scoring configuration."""

    __tablename__ = "exam_configuration_versions"
    __table_args__ = (
        UniqueConstraint("exam_id", "version", name="uq_exam_config_versions_exam_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)  # canonical, hashable
    sealed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
