"""add full_name generated column to exam_students

Revision ID: 9b8f664039ac
Revises: c1d2e3f4a5b6
Create Date: 2026-08-02 08:38:28.468627
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '9b8f664039ac'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('exam_students', sa.Column(
        'full_name',
        sa.String(length=242),
        sa.Computed(
            "UPPER(first_name) || ' ' || COALESCE(UPPER(middle_name) || ' ', '') || UPPER(surname)",
            persisted=True,
        ),
        nullable=False,
    ))


def downgrade() -> None:
    op.drop_column('exam_students', 'full_name')
