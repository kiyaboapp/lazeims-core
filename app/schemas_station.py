"""Pydantic models for the Central station domain (Phase 4)."""

from __future__ import annotations
import uuid

from pydantic import BaseModel, ConfigDict, Field

from lazeims_common.enums import PaperType


class StationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    station_code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=160)
    region_id: int | None = None
    council_id: int | None = None
    ward_id: int | None = None
    scope_mode: str | None = None  # 'LOCATION' or 'SCHOOLS'
    school_ids: list[int] | None = None
    subject_codes: list[str] | None = None  # optional subject restriction
    managed_by: int | None = None


class StationScopeIn(BaseModel):
    """PATCH payload for updating a station's scope."""
    model_config = ConfigDict(extra="forbid")
    scope_mode: str = Field(pattern="^(LOCATION|SCHOOLS)$")
    region_id: int | None = None
    council_id: int | None = None
    ward_id: int | None = None
    school_ids: list[int] | None = None
    subject_codes: list[str] | None = None  # optional subject restriction


class StationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    exam_id: uuid.UUID
    station_code: str
    name: str
    scope_mode: str | None
    region_id: int | None
    council_id: int | None
    ward_id: int | None
    subject_codes: list[str] | None = None
    school_ids: list[int] | None = None
    is_active: bool
    software_version: str | None


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_role_assignment_id: int
    kind: str = Field(pattern="^(DE|ADMIN)$")
    initials: str | None = Field(default=None, max_length=10)  # required for DE


class CredentialOut(BaseModel):
    """Returned ONCE: the plaintext PIN or password. Only hashes are stored."""
    credential_id: int
    kind: str
    pin: str | None = None
    initials: str | None = None
    password: str | None = None


class PackageGenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schools: list[str] = Field(min_length=1)      # centre numbers
    subjects: list[str] = Field(min_length=1)      # subject codes
    papers: list[PaperType] = Field(min_length=1)
    # Optional operator overrides for the default station-admin login. When
    # omitted, username defaults to the station code and a password is generated.
    admin_username: str | None = Field(default=None, max_length=60)
    admin_password: str | None = Field(default=None, max_length=128)


class PackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    package_id: str
    station_id: int
    exam_id: uuid.UUID
    package_version: int
    rules_version: str
    configuration_hash: str
    assigned_scope: dict
    revoked_at: object | None = None
    # One-time default station-admin login, present only in the response of the
    # generate call that created it. Never stored, never re-shown.
    station_admin_username: str | None = None
    station_admin_password: str | None = None
    station_admin_delivery: str | None = None  # EMAIL | IN_APP
