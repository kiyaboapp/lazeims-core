"""Attendance transcription + effective-attendance resolution (DB-backed).

Wraps ``lazeims_common.validation.attendance`` (the single source of the
resolution rule) with the DB read/write around it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import AttendanceSource, PaperType
from lazeims_common.validation.attendance import (
    AttendanceRow,
    effective_attendance,
    has_specific_transcription,
)

from ..models.marks import Attendance


async def _rows_for(db: AsyncSession, exam_student_subject_id: int) -> list[Attendance]:
    return list(
        (
            await db.execute(
                select(Attendance).where(Attendance.exam_student_subject_id == exam_student_subject_id)
            )
        ).scalars().all()
    )


def _to_common(rows: list[Attendance]) -> list[AttendanceRow]:
    return [AttendanceRow(paper_type=r.paper_type, is_present=r.is_present) for r in rows]


async def resolve_effective_attendance(
    db: AsyncSession, exam_student_subject_id: int, paper_type: PaperType
) -> bool:
    rows = await _rows_for(db, exam_student_subject_id)
    return effective_attendance(_to_common(rows), paper_type)


async def has_transcription(
    db: AsyncSession, exam_student_subject_id: int, paper_type: PaperType
) -> bool:
    rows = await _rows_for(db, exam_student_subject_id)
    return has_specific_transcription(_to_common(rows), paper_type)


async def upsert_attendance(
    db: AsyncSession,
    *,
    exam_student_subject_id: int,
    paper_type: PaperType,
    is_present: bool,
    source: AttendanceSource,
    actor_id: int | None,
) -> Attendance:
    """Insert/update the attendance row for a specific paper (or ALL baseline).

    A Data Enterer confirming a specific paper always writes the ``paper_type=X``
    row — never the ALL baseline (enforced by the caller passing the real paper).
    """
    existing = (
        await db.execute(
            select(Attendance).where(
                Attendance.exam_student_subject_id == exam_student_subject_id,
                Attendance.paper_type == paper_type,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        row = Attendance(
            exam_student_subject_id=exam_student_subject_id,
            paper_type=paper_type,
            is_present=is_present,
            source=source,
            transcribed_by=actor_id,
            transcribed_at=now,
        )
        db.add(row)
    else:
        row = existing
        row.is_present = is_present
        row.source = source
        row.transcribed_by = actor_id
        row.transcribed_at = now
    await db.flush()
    return row
