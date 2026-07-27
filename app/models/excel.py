"""Controlled Excel collection models (Guide §2.7).

* ExcelWorkbook — a scoped, single-school workbook (only issued when the school
  has ``can_download_template = true``).
* ExcelImportBatch — one return/import event; idempotent by ``idempotency_key``.
* ExcelImportRow — durable staged rows + per-row validation outcome, so a full
  preview/error report exists before anything is applied.
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

from ..db import Base, TimestampMixin


def _enum(py_enum, name, length=40):
    return SAEnum(py_enum, name=name, native_enum=False, validate_strings=True, length=length)


class ExcelWorkbook(Base, TimestampMixin):
    __tablename__ = "excel_workbooks"

    workbook_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    exam_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("exams.id"), nullable=False, index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    subject_scope: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {subjects, papers}
    configuration_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    generated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ISSUED", nullable=False)  # ISSUED|RETURNED|IMPORTED|EXPIRED


class ExcelImportBatch(Base, TimestampMixin):
    __tablename__ = "excel_import_batches"

    import_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workbook_id: Mapped[str] = mapped_column(ForeignKey("excel_workbooks.workbook_id"), nullable=False, index=True)
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # WEB_UPLOAD|REMOVABLE_MEDIA
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="STAGED", nullable=False)  # STAGED|APPLIED
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExcelImportRow(Base, TimestampMixin):
    __tablename__ = "excel_import_rows"
    __table_args__ = (
        UniqueConstraint("import_id", "row_no", name="uq_excel_import_rows_import_row"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("excel_import_batches.import_id"), nullable=False, index=True)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    student_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    subject_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    paper_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # parsed values
    status: Mapped[str] = mapped_column(String(10), default="OK", nullable=False)  # OK|ERROR|APPLIED
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(400), nullable=True)
