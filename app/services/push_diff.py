"""Diff-based push cache: compare current data against last push, only send changes."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.push_cache import CollectionPushCache


def _hash_dict(d: dict[str, Any]) -> str:
    """Deterministic sha256 of a dict's values."""
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode()
    ).hexdigest()


def marks_row_hash(row: dict) -> str:
    """Hash the marks-relevant fields of a marks row."""
    return _hash_dict({
        "theory_marks": row.get("theory_marks"),
        "theory_2_marks": row.get("theory_2_marks"),
        "practical_marks": row.get("practical_marks"),
        "sat_theory": row.get("sat_theory"),
        "sat_theory_2": row.get("sat_theory_2"),
        "sat_practical": row.get("sat_practical"),
    })


def student_row_hash(row: dict) -> str:
    """Hash the student identity fields."""
    return _hash_dict({
        "student_id": row.get("student_id"),
        "first_name": row.get("first_name"),
        "middle_name": row.get("middle_name"),
        "surname": row.get("surname"),
        "sex": row.get("sex"),
    })


def school_row_hash(row: dict) -> str:
    """Hash the school metadata fields."""
    return _hash_dict({
        "centre_number": row.get("centre_number"),
        "school_name": row.get("school_name"),
        "school_type": row.get("school_type"),
        "region_name": row.get("region_name"),
        "council_name": row.get("council_name"),
        "ward_name": row.get("ward_name"),
    })


async def load_cache(
    db: AsyncSession, exam_id: uuid.UUID
) -> dict[tuple[str, str, str, str, str], str]:
    """Load the push cache as a lookup dict. Key = (entity_type, centre_number, student_id, subject_code, paper_type) -> data_hash."""
    rows = (await db.execute(
        select(CollectionPushCache).where(CollectionPushCache.exam_id == exam_id)
    )).scalars().all()
    return {
        (r.entity_type, r.centre_number, r.student_id, r.subject_code, r.paper_type): r.data_hash
        for r in rows
    }


def filter_school_payload_by_diff(
    payload: dict, cache: dict[tuple[str, str, str, str, str], str]
) -> tuple[dict, list[tuple[str, str, str, str, str, str]]]:
    """Filter a single school's payload to only include changed rows.
    
    Returns (filtered_payload, cache_updates) where cache_updates is a list of
    (entity_type, centre_number, student_id, subject_code, paper_type, new_hash) tuples.
    """
    school = payload["schools"][0] if payload["schools"] else None
    if not school:
        return payload, []
    
    cn = school["centre_number"]
    cache_updates: list[tuple[str, str, str, str, str, str]] = []
    
    # Check school itself
    school_hash = school_row_hash(school)
    school_key = ("school", cn, "", "", "")
    school_changed = cache.get(school_key) != school_hash
    if school_changed:
        cache_updates.append(("school", cn, "", "", "", school_hash))
    
    # Filter students
    changed_students = []
    for s in payload.get("students", []):
        h = student_row_hash(s)
        key = ("student", cn, s["student_id"], "", "")
        if cache.get(key) != h:
            changed_students.append(s)
            cache_updates.append(("student", cn, s["student_id"], "", "", h))
    
    # Filter marks
    changed_marks = []
    for m in payload.get("marks", []):
        h = marks_row_hash(m)
        # Marks don't have paper_type in the row directly — they have theory_marks etc.
        # Use subject_code as the key differentiator
        key = ("marks", cn, m["student_id"], m["subject_code"], "")
        if cache.get(key) != h:
            changed_marks.append(m)
            cache_updates.append(("marks", cn, m["student_id"], m["subject_code"], "", h))
    
    # If nothing changed at all for this school (no school/student/marks changes), return empty
    has_changes = school_changed or changed_students or changed_marks
    
    if not has_changes:
        return None, []  # type: ignore — caller checks for None to skip
    
    filtered = {
        "schools": payload["schools"] if school_changed else [],
        "subjects": payload.get("subjects", []),  # subjects always pass through if present
        "students": changed_students,
        "marks": changed_marks,
    }
    return filtered, cache_updates


async def upsert_cache(
    db: AsyncSession,
    exam_id: uuid.UUID,
    updates: list[tuple[str, str, str, str, str, str]],
    task_id: str | None = None,
) -> None:
    """Upsert cache entries after a successful push."""
    if not updates:
        return
    now = datetime.now(timezone.utc)
    for entity_type, cn, student_id, subject_code, paper_type, data_hash in updates:
        stmt = pg_insert(CollectionPushCache).values(
            exam_id=exam_id,
            entity_type=entity_type,
            centre_number=cn,
            student_id=student_id,
            subject_code=subject_code,
            paper_type=paper_type,
            data_hash=data_hash,
            pushed_at=now,
            push_task_id=task_id,
        ).on_conflict_do_update(
            constraint="uq_push_cache_row",
            set_={
                "data_hash": data_hash,
                "pushed_at": now,
                "push_task_id": task_id,
            },
        )
        await db.execute(stmt)
    await db.flush()


async def clear_cache(db: AsyncSession, exam_id: uuid.UUID) -> int:
    """Clear all cache entries for an exam (used before force-push-all)."""
    result = await db.execute(
        delete(CollectionPushCache).where(CollectionPushCache.exam_id == exam_id)
    )
    await db.flush()
    return result.rowcount or 0
