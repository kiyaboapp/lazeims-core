from __future__ import annotations
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base, TimestampMixin

class CollectionPushCache(Base, TimestampMixin):
    __tablename__ = "collection_push_cache"
    __table_args__ = (
        UniqueConstraint(
            "exam_id", "entity_type", "centre_number", "student_id", "subject_code", "paper_type",
            name="uq_push_cache_row",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False, default="marks")  # marks | student | school
    centre_number: Mapped[str] = mapped_column(String(20), nullable=False)
    student_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")  # empty for school-level
    subject_code: Mapped[str] = mapped_column(String(20), nullable=False, default="")  # empty for student/school
    paper_type: Mapped[str] = mapped_column(String(20), nullable=False, default="")  # empty for student/school
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    push_task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
