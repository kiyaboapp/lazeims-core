"""Reusable geography-consistency validator (Guide §1.5).

A school's region/council/ward are each optional, but when present must be
internally consistent:
    * the school's council must belong to the school's region;
    * the school's ward must belong to the school's council.

Call :func:`validate_school_geography` from every place a school is created or
edited — never inline the checks in a single form handler.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import RejectionCode
from lazeims_common.errors import ValidationError

from ..models.registry import Council, Ward


async def validate_school_geography(
    session: AsyncSession,
    *,
    region_id: int | None,
    council_id: int | None,
    ward_id: int | None,
) -> None:
    """Raise :class:`ValidationError` if the geography links are inconsistent.

    Uses a dedicated rejection code (``CONFIGURATION_MISMATCH``) so callers can
    translate it to an HTTP 422 with a specific message.
    """
    council: Council | None = None
    if council_id is not None:
        council = await session.get(Council, council_id)
        if council is None:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                f"Council {council_id} does not exist.",
                {"council_id": council_id},
            )
        if region_id is not None and council.region_id != region_id:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "School council does not belong to the school region.",
                {"council_id": council_id, "region_id": region_id,
                 "council_region_id": council.region_id},
            )

    if ward_id is not None:
        ward = await session.get(Ward, ward_id)
        if ward is None:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                f"Ward {ward_id} does not exist.",
                {"ward_id": ward_id},
            )
        if council_id is not None and ward.council_id != council_id:
            raise ValidationError(
                RejectionCode.CONFIGURATION_MISMATCH,
                "School ward does not belong to the school council.",
                {"ward_id": ward_id, "council_id": council_id,
                 "ward_council_id": ward.council_id},
            )
        # If ward is set but council isn't, we can still cross-check the ward's
        # council against the region.
        if council_id is None and region_id is not None:
            ward_council = await session.get(Council, ward.council_id)
            if ward_council is not None and ward_council.region_id != region_id:
                raise ValidationError(
                    RejectionCode.CONFIGURATION_MISMATCH,
                    "School ward's council does not belong to the school region.",
                    {"ward_id": ward_id, "region_id": region_id},
                )
