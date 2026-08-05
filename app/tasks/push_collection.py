"""Celery task for chunked collection push to ExaMetrics, one school at a time.

All heavy work happens here — the API endpoint just validates access and
dispatches this task. The task:
1. Builds payloads per-school (streaming, not all at once)
2. Computes diffs against the push cache
3. Starts a collection session on ExaMetrics
4. Uploads one school per chunk
5. Updates the push cache after each successful chunk
6. Reports progress throughout
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ..celery_app import celery_app

logger = logging.getLogger(__name__)


async def _build_and_push(
    exam_id: str,
    api_key: str,
    backend_exam_id: str,
    force: bool,
    task: Any,
    task_id: str,
) -> dict:
    """Build payloads per-school and push each one as a chunk."""
    from ..services import backend_sis
    from ..services.push_diff import (
        clear_cache,
        filter_school_payload_by_diff,
        load_cache,
        upsert_cache,
    )
    from ..services.processing_submit import build_school_payloads
    from ..db import async_sessionmaker_factory
    from ..models.exam import Exam
    from lazeims_common.exametrics_digest import collection_digest, merge_chunks

    exam_uuid = uuid.UUID(exam_id)

    # Step 1: Build per-school payloads (this is the heavy part)
    task.update_state(state="PROGRESS", meta={
        "total_schools": 0, "completed": 0, "phase": "building",
        "current_school": None, "schools_done": [], "schools_failed": [],
    })

    async with async_sessionmaker_factory()() as db:
        exam = await db.get(Exam, exam_uuid)
        if not exam:
            return {"error": "Exam not found", "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": []}

        # Sync exam definition (best-effort)
        try:
            from ..services.exametrics_provision import build_subjects_payload
            from ..models.exam import ExamLevel
            from ..config import get_settings
            level = await db.get(ExamLevel, exam.level_id)
            level_name = (level.name if level else "").upper()
            subjects = await build_subjects_payload(db, exam)
            filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
            await backend_sis.upsert_exam_definition(
                api_key,
                external_ref=str(exam.id),
                name=exam.name,
                level=level_name,
                zone_name=get_settings().zone_name or None,
                filling_mode=filling_mode,
                subjects=subjects,
            )
        except Exception as exc:
            logger.warning("Exam definition sync failed (non-fatal): %s", exc)

        # Build payloads
        school_payloads = await build_school_payloads(db, exam, skip_empty=True)

        # Load/clear cache
        if force:
            await clear_cache(db, exam_uuid)
            await db.commit()
            cache: dict = {}
        else:
            cache = await load_cache(db, exam_uuid)

    # Step 2: Filter by diff
    filtered_payloads: list[dict] = []
    cache_updates_by_school: list[list[tuple]] = []

    for payload in school_payloads:
        filtered, updates = filter_school_payload_by_diff(payload, cache)
        if filtered is None:
            continue
        filtered_payloads.append(filtered)
        cache_updates_by_school.append(updates)

    # Free the full payloads
    del school_payloads

    total = len(filtered_payloads)
    if total == 0:
        return {
            "total_schools": 0, "completed": 0,
            "schools_done": [], "schools_failed": [],
            "status": "no_changes",
        }

    # Step 3: Start collection session
    task.update_state(state="PROGRESS", meta={
        "total_schools": total, "completed": 0, "phase": "uploading",
        "current_school": None, "schools_done": [], "schools_failed": [],
    })

    digest = collection_digest(merge_chunks(filtered_payloads))
    manifest = chunk_manifest(filtered_payloads)

    session_payload = {
        "chunk_count": total,
        "expected_digest": digest,
    }
    session = await backend_sis.start_collection_session(api_key, backend_exam_id, session_payload)
    session_id = session.get("session_id") or session.get("id", "")

    # Step 4: Upload one school per chunk
    schools_done: list[dict] = []
    schools_failed: list[dict] = []

    for i, chunk in enumerate(filtered_payloads):
        school_info = chunk["schools"][0] if chunk.get("schools") else {}
        cn = school_info.get("centre_number", f"chunk-{i}")
        school_name = school_info.get("school_name", "")

        task.update_state(state="PROGRESS", meta={
            "total_schools": total,
            "completed": i,
            "phase": "uploading",
            "current_school": {"centre_number": cn, "name": school_name},
            "schools_done": schools_done[:],
            "schools_failed": schools_failed[:],
        })

        try:
            await backend_sis.put_chunk(api_key, session_id, i, chunk)
            schools_done.append({"centre_number": cn, "name": school_name})

            # Update push cache for this school
            if i < len(cache_updates_by_school):
                updates = cache_updates_by_school[i]
                if updates:
                    async with async_sessionmaker_factory()() as db:
                        await upsert_cache(db, exam_uuid, updates, task_id)
                        await db.commit()
        except Exception as exc:
            logger.error("Failed to push school %s: %s", cn, exc)
            schools_failed.append({"centre_number": cn, "name": school_name, "error": str(exc)})

    # Step 5: Complete session
    try:
        complete_payload = {"digest": digest}
        await backend_sis.complete_collection_session(api_key, session_id, complete_payload)
    except Exception as exc:
        logger.error("Failed to complete collection session: %s", exc)

    return {
        "total_schools": total,
        "completed": len(schools_done),
        "schools_done": schools_done,
        "schools_failed": schools_failed,
    }


@celery_app.task(bind=True, name="push_collection_chunked")
def push_collection_chunked(
    self,
    exam_id: str,
    api_key: str,
    backend_exam_id: str,
    force: bool = False,
    # Legacy args (ignored — task now builds its own payloads)
    school_payloads: list[dict] | None = None,
    cache_updates_by_school: list[list[tuple]] | None = None,
) -> dict:
    """Push collection data to ExaMetrics one school at a time.

    Builds payloads, computes diffs, uploads chunks, and reports progress.
    All heavy work happens here — the API endpoint is lightweight.
    """
    self.update_state(
        state="STARTED",
        meta={"total_schools": 0, "completed": 0, "phase": "starting", "schools_done": [], "schools_failed": []},
    )

    result = asyncio.run(
        _build_and_push(
            exam_id=exam_id,
            api_key=api_key,
            backend_exam_id=backend_exam_id,
            force=force,
            task=self,
            task_id=self.request.id or "",
        )
    )
    return result
