"""Pydantic request/response models for attendance, incidents, marks, finalize."""

from __future__ import annotations
import uuid

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from lazeims_common.enums import (
    AttendanceSource,
    FillingMode,
    IncidentStatus,
    IncidentType,
    PaperType,
)


class AttendanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: str
    exam_subject_id: int
    paper_type: PaperType
    is_present: bool
    source: AttendanceSource = AttendanceSource.INVIGILATOR_ISAL_TRANSCRIPTION


class BulkAttendanceEntry(BaseModel):
    student_id: str
    is_present: bool


class BulkAttendanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_subject_id: int
    paper_type: PaperType
    entries: list[BulkAttendanceEntry] = Field(min_length=1)
    source: AttendanceSource = AttendanceSource.INVIGILATOR_ISAL_TRANSCRIPTION


class CalBaselineEntry(BaseModel):
    student_id: str
    is_present: bool


class CalBaselineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_subject_id: int
    entries: list[CalBaselineEntry] = Field(default_factory=list)


class IncidentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_subject_id: int
    school_id: int
    paper_type: PaperType
    student_id: str | None = None  # None => scope-wide
    incident_type: IncidentType = IncidentType.MISSING_SCRIPT
    documented_in_supervisor_cal: bool = False
    explanation: str | None = None


class IncidentStatusIn(BaseModel):
    status: IncidentStatus


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    exam_id: uuid.UUID
    exam_student_subject_id: int | None
    school_id: int | None
    exam_subject_id: int | None
    paper_type: PaperType
    incident_type: IncidentType
    status: IncidentStatus
    explanation: str | None


class ItemEntry(BaseModel):
    question_number: str
    marks: Decimal = Field(ge=0)


class StudentMarksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_subject_id: int
    paper_type: PaperType
    mode: FillingMode
    total_marks_obtained: Decimal | None = Field(default=None, ge=0)
    items: list[ItemEntry] | None = None

    def item_map(self) -> dict[str, Decimal]:
        return {i.question_number: i.marks for i in (self.items or [])}


class BatchStudentMarksIn(StudentMarksIn):
    student_id: str


class MarksBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submissions: list[BatchStudentMarksIn] = Field(min_length=1, max_length=500)


class ScopeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    school_id: int
    exam_subject_id: int
    paper_type: PaperType


class ReopenIn(ScopeRef):
    reason: str = Field(min_length=1)
