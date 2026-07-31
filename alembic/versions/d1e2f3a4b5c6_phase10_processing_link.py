"""phase10: exam_processing_links (ExaMetrics processing integration)

Binds a Central exam to its ExaMetrics processing exam + purchased API key.

Revision ID: d1e2f3a4b5c6
Revises: 162f0de8f093
Create Date: 2026-07-28 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "162f0de8f093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exam_processing_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exam_id", sa.Integer(), nullable=False),
        sa.Column("backend_exam_id", sa.String(length=36), nullable=False),
        sa.Column("api_key", sa.String(length=120), nullable=False),
        sa.Column("last_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=40), nullable=True),
        sa.Column("configured_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"]),
        sa.ForeignKeyConstraint(["configured_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exam_id", name="uq_exam_processing_links_exam_id"),
    )
    op.create_index(
        op.f("ix_exam_processing_links_exam_id"), "exam_processing_links", ["exam_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_exam_processing_links_exam_id"), table_name="exam_processing_links")
    op.drop_table("exam_processing_links")
