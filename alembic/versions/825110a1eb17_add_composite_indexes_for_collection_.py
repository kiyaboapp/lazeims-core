"""add composite indexes for collection progress perf

Revision ID: 825110a1eb17
Revises: 91012f76325c
Create Date: 2026-08-04 10:35:46.284764
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '825110a1eb17'
down_revision: Union[str, None] = '91012f76325c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_ess_subject_student', 'exam_student_subjects', ['exam_subject_id', 'exam_student_id'], unique=False)
    op.create_index('ix_exam_students_exam_school', 'exam_students', ['exam_id', 'school_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_exam_students_exam_school', table_name='exam_students')
    op.drop_index('ix_ess_subject_student', table_name='exam_student_subjects')
