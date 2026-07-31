"""phase12: key provenance + approval state on exam_processing_links

Central now asks ExaMetrics for each exam's API key itself, so a link needs to
record where its key came from (``key_source``), a non-secret fragment support
can quote (``key_prefix``), when it was issued, and whether the paid scopes have
been approved yet. Existing rows hold hand-entered keys, hence the MANUAL
default: re-provisioning must never silently overwrite one.

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-07-31 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "exam_processing_links",
        sa.Column(
            "key_source",
            sa.String(length=16),
            nullable=False,
            server_default="MANUAL",
        ),
    )
    op.add_column(
        "exam_processing_links",
        sa.Column("key_prefix", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "exam_processing_links",
        sa.Column("key_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "exam_processing_links",
        sa.Column("approval_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "exam_processing_links",
        sa.Column("approval_note", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    for column in (
        "approval_note",
        "approval_status",
        "key_issued_at",
        "key_prefix",
        "key_source",
    ):
        op.drop_column("exam_processing_links", column)
