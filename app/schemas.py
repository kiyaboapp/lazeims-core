"""Pydantic request/response models for the Central API (Phase 1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from lazeims_common.enums import SchoolType, Sex

from .enums import RoleScopeLevel, StandingRoleName


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth ----

class LoginIn(BaseModel):
    username: str
    password: str


class MeOut(ORMModel):
    id: int
    username: str
    first_name: str
    surname: str
    role: StandingRoleName
    region_id: int | None = None
    council_id: int | None = None
    school_id: int | None = None
    csrf_token: str

    @classmethod
    def from_user(cls, user, csrf_token: str) -> "MeOut":
        return cls(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            surname=user.surname,
            role=user.role.name,
            region_id=user.region_id,
            council_id=user.council_id,
            school_id=user.school_id,
            csrf_token=csrf_token,
        )


# ---- registry: regions/councils/wards ----

class RegionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class RegionOut(ORMModel):
    id: int
    name: str


class CouncilIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    region_id: int


class CouncilOut(ORMModel):
    id: int
    name: str
    region_id: int


class WardIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    council_id: int


class WardOut(ORMModel):
    id: int
    name: str
    council_id: int


# ---- registry: schools ----

class SchoolIn(BaseModel):
    centre_number: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    school_type: SchoolType = SchoolType.UNKNOWN
    region_id: int | None = None
    council_id: int | None = None
    ward_id: int | None = None
    can_download_template: bool = False


class SchoolOut(ORMModel):
    id: int
    centre_number: str
    name: str
    school_type: SchoolType
    is_primary: bool
    is_olevel: bool
    is_alevel: bool
    region_id: int | None
    council_id: int | None
    ward_id: int | None
    can_download_template: bool


class SchoolExamSummary(BaseModel):
    id: uuid.UUID
    name: str
    phase: str
    start_date: str | None = None
    end_date: str | None = None
    student_count: int


class RegionProfileOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    council_count: int
    ward_count: int
    school_count: int
    government_school_count: int
    private_school_count: int
    unknown_school_count: int
    scoped_user_count: int
    station_count: int
    exam_participation_count: int
    student_count: int
    councils: list[CouncilOut]
    wards: list[WardOut]
    schools: list[SchoolOut]


class CouncilProfileOut(BaseModel):
    id: int
    name: str
    region_id: int
    region_name: str
    created_at: datetime
    updated_at: datetime
    ward_count: int
    school_count: int
    government_school_count: int
    private_school_count: int
    unknown_school_count: int
    scoped_user_count: int
    station_count: int
    exam_participation_count: int
    student_count: int
    wards: list[WardOut]
    schools: list[SchoolOut]


class WardProfileOut(BaseModel):
    id: int
    name: str
    council_id: int
    council_name: str
    region_id: int
    region_name: str
    created_at: datetime
    updated_at: datetime
    school_count: int
    government_school_count: int
    private_school_count: int
    unknown_school_count: int
    exam_participation_count: int
    student_count: int
    schools: list[SchoolOut]


class SchoolProfileOut(BaseModel):
    id: int
    centre_number: str
    name: str
    school_type: SchoolType
    region_id: int | None
    region_name: str | None
    council_id: int | None
    council_name: str | None
    ward_id: int | None
    ward_name: str | None
    can_download_template: bool
    created_at: datetime
    updated_at: datetime
    scoped_user_count: int
    exam_count: int
    student_count: int
    data_enterer_scope_count: int
    write_assignment_count: int
    station_count: int
    exams: list[SchoolExamSummary]


# ---- registry: subjects ----

class SubjectIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=160)
    is_olevel: bool = False
    is_alevel: bool = False
    is_primary: bool = False
    has_theory2: bool = False
    has_practical: bool = False


class SubjectPatch(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=20)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    is_olevel: bool | None = None
    is_alevel: bool | None = None
    is_primary: bool | None = None
    has_theory2: bool | None = None
    has_practical: bool | None = None


class SubjectOut(ORMModel):
    id: int
    code: str
    name: str
    is_olevel: bool
    is_alevel: bool
    is_primary: bool
    has_theory2: bool
    has_practical: bool


class ExamLevelIn(BaseModel):
    name: str = Field(min_length=1, max_length=20)


class ExamLevelOut(ORMModel):
    id: int
    name: str


class BoardIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    region_id: int | None = None
    council_id: int | None = None


class BoardOut(ORMModel):
    id: int
    name: str
    region_id: int | None
    council_id: int | None


# ---- users ----

class UserIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    middle_name: str | None = Field(default=None, max_length=80)
    surname: str = Field(min_length=1, max_length=80)
    sex: Sex
    username: str = Field(min_length=3, max_length=80)
    email: EmailStr | None = None
    role: StandingRoleName
    region_id: int | None = None
    council_id: int | None = None
    school_id: int | None = None
    password: str = Field(min_length=8, max_length=128)


class UserOut(ORMModel):
    id: int
    username: str
    first_name: str
    middle_name: str | None
    surname: str
    sex: Sex
    email: str | None
    role: StandingRoleName
    region_id: int | None
    council_id: int | None
    school_id: int | None
    is_active: bool

    @classmethod
    def from_user(cls, user) -> "UserOut":
        return cls(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            middle_name=user.middle_name,
            surname=user.surname,
            sex=user.sex,
            email=user.email,
            role=user.role.name,
            region_id=user.region_id,
            council_id=user.council_id,
            school_id=user.school_id,
            is_active=user.is_active,
        )
