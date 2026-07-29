"""Pydantic request/response models for exam setup + role model (Phase 2)."""

from __future__ import annotations
import uuid

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from lazeims_common.enums import ExamPhase, PaperType, Sex, WriterMode

from .enums import ExamRoleName


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- exam ----

class ExamSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    has_theory2: bool = False
    has_practical: bool = False
    has_filling_station: bool = False
    filling_mode: str = "TOTAL_MARKS"   # TOTAL_MARKS | ITEM_LEVEL
    display_mode: str = "NAME"          # NAME | ID_ONLY


class ExamIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    level_id: int
    board_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    settings: ExamSettings = Field(default_factory=ExamSettings)


class ExamOut(ORMModel):
    id: uuid.UUID
    name: str
    level_id: int
    board_id: int | None
    start_date: date | None
    end_date: date | None
    phase: ExamPhase
    settings: dict
    current_configuration_version: int | None


class ExamSettingsPatch(BaseModel):
    """Partial settings update. Every field optional so a caller can change one
    setting without restating the rest."""

    model_config = ConfigDict(extra="forbid")
    has_theory2: bool | None = None
    has_practical: bool | None = None
    has_filling_station: bool | None = None
    filling_mode: str | None = None   # TOTAL_MARKS | ITEM_LEVEL
    display_mode: str | None = None   # NAME | ID_ONLY


class ExamPatch(BaseModel):
    """Partial exam update.

    ``phase`` is intentionally absent: it moves only through the transitions
    endpoint, which enforces the state machine and its preconditions.
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    level_id: int | None = None
    board_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    settings: ExamSettingsPatch | None = None


class PhaseTransitionIn(BaseModel):
    target_phase: ExamPhase
    reason: str | None = None  # required for ENTRY_LOCKED -> ENTRY_OPEN reopen


# ---- exam registry attach ----

class ExamSchoolIn(BaseModel):
    school_id: int


class BulkExamSchoolIn(BaseModel):
    """Bulk school enrollment.

    Either supply explicit ``school_ids``, or supply a geography filter to
    enroll every eligible school in that area. The most specific geography
    filter provided wins (ward > council > region).
    """

    school_ids: list[int] = Field(default_factory=list)
    region_id: int | None = None
    council_id: int | None = None
    ward_id: int | None = None
    school_type: str | None = None


class ExamSubjectIn(BaseModel):
    subject_id: int
    has_theory2: bool = False
    has_practical: bool = False
    total_marks_theory1: int = 100
    total_marks_theory2: int = 0
    total_marks_practical: int = 0


class ExamSubjectPatch(BaseModel):
    """Partial update of one exam-subject's paper configuration."""

    has_theory2: bool | None = None
    has_practical: bool | None = None
    total_marks_theory1: int | None = Field(default=None, ge=0, le=1000)
    total_marks_theory2: int | None = Field(default=None, ge=0, le=1000)
    total_marks_practical: int | None = Field(default=None, ge=0, le=1000)


class ExamSubjectOut(ORMModel):
    id: int
    exam_id: uuid.UUID
    subject_id: int
    has_theory2: bool
    has_practical: bool
    total_marks_theory1: int
    total_marks_theory2: int
    total_marks_practical: int
    # Enriched inline so clients never bulk-load the subject registry just to
    # label this list.
    subject_code: str | None = None
    subject_name: str | None = None
    candidate_count: int | None = None


class ExamStudentIn(BaseModel):
    student_id: str = Field(min_length=1, max_length=60)
    school_id: int
    first_name: str
    middle_name: str | None = None
    surname: str
    sex: Sex
    subject_ids: list[int] = Field(default_factory=list)  # exam_subject ids


class ExamStudentOut(ORMModel):
    id: int
    student_id: str
    school_id: int
    first_name: str
    middle_name: str | None = None
    surname: str
    sex: Sex | None = None
    # Enriched inline for roster rendering.
    centre_number: str | None = None
    school_name: str | None = None
    subject_count: int | None = None


# ---- scoring config ----

class QuestionGroupIn(BaseModel):
    paper_type: PaperType
    code: str = Field(min_length=1, max_length=40)
    name: str
    instruction: str | None = None
    pick_count: int = Field(ge=1)


class QuestionTopicIn(BaseModel):
    topic_id: int
    weight: Decimal = Field(gt=0, le=1)


class QuestionIn(BaseModel):
    paper_type: PaperType
    question_number: str = Field(min_length=1, max_length=20)
    group_code: str | None = None
    max_marks: Decimal = Field(gt=0)
    topics: list[QuestionTopicIn] = Field(default_factory=list)


class SubjectQuestionsIn(BaseModel):
    """Replace the full question set for one exam-subject."""
    groups: list[QuestionGroupIn] = Field(default_factory=list)
    questions: list[QuestionIn] = Field(default_factory=list)


# ---- assignments ----

class RoleAssignmentIn(BaseModel):
    user_id: int
    role: ExamRoleName
    region_id: int | None = None
    council_id: int | None = None


class RoleAssignmentOut(ORMModel):
    id: int
    exam_id: uuid.UUID
    user_id: int
    role: ExamRoleName
    region_id: int | None
    council_id: int | None


class DataEntererScopeIn(BaseModel):
    exam_role_assignment_id: int
    subject_id: int | None = None
    school_id: int | None = None


class WriterAssignmentIn(BaseModel):
    school_id: int
    exam_subject_id: int
    paper_type: PaperType
    writer_mode: WriterMode
    station_id: int | None = None


class WriterAssignmentOut(ORMModel):
    id: int
    exam_id: uuid.UUID
    school_id: int
    exam_subject_id: int
    paper_type: PaperType
    writer_mode: WriterMode
    station_id: int | None


class ReadinessOut(BaseModel):
    ready: bool
    checks: list[dict]
    blocking: list[str]
