"""Simple in-memory TTL cache for frequently-read, rarely-changed exam data.

Cache keys and their invalidation triggers:

  STABLE (long TTL — only change during REGISTRATION phase):
    "schools_list"       — enrolled schools with geography
                           invalidate: attach/detach/bulk_enroll school
    "entry_scopes"       — same data, different shape (cascading selector)
                           invalidate: same as schools_list
    "subjects_base"      — exam subjects with code/name/config
                           invalidate: attach/detach subject, set_subject_questions
    "questions:{es_id}"  — question config per exam_subject
                           invalidate: set_subject_questions
    "schools_stats"      — aggregated school stats (by region, by type)
                           invalidate: attach/detach school, register/remove student

  SEMI-STABLE (medium TTL — changes with student registration):
    "candidate_counts"   — candidate count per exam_subject_id
                           invalidate: register/remove student

  DYNAMIC (never cached):
    - marks, attendance, filling progress, incidents
"""

from __future__ import annotations

import time
from typing import Any

# Stable data rarely changes — 5 minutes
TTL_STABLE = 300
# Semi-stable changes with student registration — 60 seconds
TTL_SEMI = 60

_STABLE_KEYS = frozenset({
    "schools_list", "entry_scopes", "subjects_base", "schools_stats",
})

_store: dict[str, tuple[float, float, Any]] = {}  # key -> (timestamp, ttl, value)


def _key(exam_id, name: str) -> str:
    return f"{exam_id}:{name}"


def _ttl_for(name: str) -> float:
    base = name.split(":")[0] if ":" in name else name
    return TTL_STABLE if base in _STABLE_KEYS else TTL_SEMI


def get(exam_id, name: str) -> Any | None:
    """Return cached value or None if missing/expired."""
    k = _key(exam_id, name)
    entry = _store.get(k)
    if entry is None:
        return None
    ts, ttl, value = entry
    if time.monotonic() - ts > ttl:
        del _store[k]
        return None
    return value


def put(exam_id, name: str, value: Any) -> None:
    """Store a value with appropriate TTL based on key type."""
    ttl = _ttl_for(name)
    _store[_key(exam_id, name)] = (time.monotonic(), ttl, value)


def invalidate(exam_id=None, name: str | None = None) -> None:
    """Drop cached entries. No args = clear all. exam_id only = clear that exam."""
    if exam_id is None:
        _store.clear()
        return
    if name:
        _store.pop(_key(exam_id, name), None)
        return
    prefix = f"{exam_id}:"
    keys_to_drop = [k for k in _store if k.startswith(prefix)]
    for k in keys_to_drop:
        del _store[k]


def invalidate_schools(exam_id) -> None:
    """Invalidate all school-related caches."""
    invalidate(exam_id, "schools_list")
    invalidate(exam_id, "entry_scopes")
    invalidate(exam_id, "schools_stats")


def invalidate_subjects(exam_id) -> None:
    """Invalidate subject-related caches."""
    invalidate(exam_id, "subjects_base")
    invalidate(exam_id, "candidate_counts")


def invalidate_students(exam_id) -> None:
    """Invalidate caches affected by student registration changes."""
    invalidate(exam_id, "candidate_counts")
    invalidate(exam_id, "schools_stats")
