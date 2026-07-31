"""phase11: capabilities cache on exam_processing_links

Adds capability caching columns and makes backend_exam_id nullable so
a link can be created with just an API key (auto-provisioning in Phase 2).

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-07-31 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Make backend_exam_id nullable (key-only links allowed now).
    op.alter_column(
        "exam_processing_links",
        "backend_exam_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    # Cache the full capabilities response from GET /integration/me.
    op.add_column(
        "exam_processing_links",
        sa.Column("capabilities_json", JSONB, nullable=True),
    )
    # Timestamp of when capabilities were last fetched.
    op.add_column(
        "exam_processing_links",
        sa.Column("capabilities_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Verified tenant name extracted from the identity response.
    op.add_column(
        "exam_processing_links",
        sa.Column("tenant_name", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("exam_processing_links", "tenant_name")
    op.drop_column("exam_processing_links", "capabilities_fetched_at")
    op.drop_column("exam_processing_links", "capabilities_json")
    op.alter_column(
        "exam_processing_links",
        "backend_exam_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
