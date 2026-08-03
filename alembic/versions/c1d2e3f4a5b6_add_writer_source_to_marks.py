"""add writer_source to total_marks and item_marks

Revision ID: c1d2e3f4a5b6
Revises: b3c4d5e6f7a8
Create Date: 2026-08-01

Records which channel wrote a mark row: ONLINE, STATION, or EXCEL.
Pure audit label — no enforcement, any channel can write any mark.
"""

from alembic import op
import sqlalchemy as sa

revision = 'c1d2e3f4a5b6'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'total_marks',
        sa.Column('writer_source', sa.String(10), nullable=True),
    )
    op.add_column(
        'item_marks',
        sa.Column('writer_source', sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('total_marks', 'writer_source')
    op.drop_column('item_marks', 'writer_source')
