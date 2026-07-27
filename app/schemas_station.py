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
    managed_by: int | None = None


class StationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    exam_id: uuid.UUID
    station_code: str
    name: str
    region_id: int | None
    council_id: int | None
    is_active: bool
    software_version: str | None


class StationKeyOut(BaseModel):
    """Returned ONCE at station creation: the plaintext machine sync key."""
    station_id: int
    station_code: str
    sync_key: str  # shown once; only the hash is stored


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
