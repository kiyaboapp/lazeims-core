"""Registry detail & paginated-list schemas.

Separate from the base schemas.py to avoid circular imports and keep the
paginated/detail layer cleanly separated from simple CRUD shapes.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from lazeims_common.enums import SchoolType

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Envelope for server-paginated list endpoints."""

    items: list[T]
    total: int
    page: int
    page_size: int


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- Detail schemas (profile endpoints) ----


class RegionDetail(ORMModel):
    id: int
    name: str
    council_count: int
    ward_count: int
    school_count: int
    student_count: int
    user_count: int


class CouncilDetail(ORMModel):
    id: int
    name: str
    region_id: int
    region_name: str
    ward_count: int
    school_count: int
    student_count: int
    user_count: int


class WardDetail(ORMModel):
    id: int
    name: str
    council_id: int
    council_name: str
    region_id: int
    region_name: str
    school_count: int


class SchoolDetail(ORMModel):
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
    student_count: int
    exam_count: int
    user_count: int
