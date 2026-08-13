"""Celery task for chunked collection push to ExaMetrics, one school at a time.

STREAMING: never holds more than one school's data in memory. The old approach
loaded all 1750 schools (772K rows) at once and got OOM-killed on a 3.3GB server.
Now each school is: query → diff → upload → free.
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
    from sqlalchemy import update as sa_update
    from datetime import datetime, timezone

    from ..services import backend_sis
    from ..services.push_diff import (
        clear_cache,
        filter_school_payload_by_diff,
        load_cache,
        upsert_cache,
    )
    from ..services.processing_submit import (
        build_single_school_payload,
        get_exam_subjects,
        iter_school_centre_numbers,
    )
    from ..db import async_sessionmaker_factory
    from ..models.exam import Exam
    from ..models.processing import ExamProcessingLink
    from lazeims_common.exametrics_digest import collection_digest, merge_chunks

    exam_uuid = uuid.UUID(exam_id)
    _session_factory = async_sessionmaker_factory()

    task.update_state(state="PROGRESS", meta={
        "total_schools": 0, "completed": 0, "phase": "preparing",
        "current_school": None, "schools_done": [], "schools_failed": [],
    })

    # ── Step 1: lightweight queries (school list, subjects, cache) ──
    async with _session_factory() as db:
        exam = await db.get(Exam, exam_uuid)
        if not exam:
            return {"error": "Exam not found", "total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": []}

        # Sync exam definition (best-effort)
        try:
            from ..models.registry import ExamLevel
            from ..config import get_settings
            level = await db.get(ExamLevel, exam.level_id)
            level_name = (level.name if level else "").upper()
            filling_mode = (exam.settings or {}).get("filling_mode", "TOTAL_MARKS")
            await backend_sis.upsert_exam_definition(
                api_key,
                external_ref=str(exam.id),
                name=exam.name,
                level=level_name,
                zone_name=get_settings().zone_name or None,
                filling_mode=filling_mode,
                subjects=[],
            )
        except Exception as exc:
            logger.warning("Exam definition sync failed (non-fatal): %s", exc)

        school_list = await iter_school_centre_numbers(db, exam, skip_empty=True)
        subjects = await get_exam_subjects(db, exam)

        if force:
            await clear_cache(db, exam_uuid)
            await db.commit()
            cache: dict = {}
        else:
            cache = await load_cache(db, exam_uuid)

    total = len(school_list)
    if total == 0:
        try:
            async with _session_factory() as db:
                await db.execute(
                    sa_update(ExamProcessingLink)
                    .where(ExamProcessingLink.exam_id == exam_uuid)
                    .values(last_status="IDLE")
                )
                await db.commit()
        except Exception:
            pass
        return {"total_schools": 0, "completed": 0, "schools_done": [], "schools_failed": [], "status": "no_changes"}

    # ── Step 2: stream schools — build+diff one at a time ──
    task.update_state(state="PROGRESS", meta={
        "total_schools": total, "completed": 0, "phase": "diffing",
        "current_school": None, "schools_done": [], "schools_failed": [],
    })

    filtered_payloads: list[dict] = []
    cache_updates_by_school: list[list[tuple]] = []
    first_chunk = True

    async with _session_factory() as db:
        for i, (cn, school_name) in enumerate(school_list):
            payload = await build_single_school_payload(db, exam, cn)

            if first_chunk:
                payload["subjects"] = subjects

            filtered, updates = filter_school_payload_by_diff(payload, cache)
            del payload  # free immediately

            if filtered is None:
                continue

            if first_chunk:
                first_chunk = False

            filtered_payloads.append(filtered)
            cache_updates_by_school.append(updates)

            if i % 100 == 0:
                task.update_state(state="PROGRESS", meta={
                    "total_schools": total, "completed": 0,
                    "phase": f"diffing ({i}/{total})",
                    "current_school": {"centre_number": cn, "name": school_name},
                    "schools_done": [], "schools_failed": [],
                })

    total_to_push = len(filtered_payloads)
    if total_to_push == 0:
        try:
            async with _session_factory() as db:
                await db.execute(
                    sa_update(ExamProcessingLink)
                    .where(ExamProcessingLink.exam_id == exam_uuid)
                    .values(last_status="IDLE")
                )
                await db.commit()
        except Exception:
            pass
        return {"total_schools": total, "completed": 0, "schools_done": [], "schools_failed": [], "status": "no_changes"}

    # ── Step 3: start ExaMetrics session ──
    task.update_state(state="PROGRESS", meta={
        "total_schools": total_to_push, "completed": 0, "phase": "uploading",
        "current_school": None, "schools_done": [], "schools_failed": [],
    })

    digest = collection_digest(merge_chunks(filtered_payloads))
    session_payload = {"chunk_count": total_to_push, "expected_digest": digest}
    session = await backend_sis.start_collection_session(api_key, backend_exam_id, session_payload)
    session_id = session.get("session_id") or session.get("id", "")

    # ── Step 4: upload chunks ──
    _CHUNK_DELAY_SECS = 0.75
    _CONSECUTIVE_FAIL_ABORT = 5
    schools_done: list[dict] = []
    schools_failed: list[dict] = []
    consecutive_failures = 0

    async with _session_factory() as db:
        for i, chunk in enumerate(filtered_payloads):
            school_info = chunk["schools"][0] if chunk.get("schools") else {}
            cn = school_info.get("centre_number", f"chunk-{i}")
            school_name = school_info.get("school_name", "")

            task.update_state(state="PROGRESS", meta={
                "total_schools": total_to_push,
                "completed": i,
                "phase": "uploading",
                "current_school": {"centre_number": cn, "name": school_name},
                "schools_done": schools_done[-10:],
                "schools_failed": schools_failed[:],
            })

            max_retries = 3 if consecutive_failures == 0 else 1
            success = False
            for attempt in range(max_retries):
                try:
                    await backend_sis.put_chunk(api_key, session_id, i, chunk)
                    success = True
                    break
                except Exception as exc:
                    err_msg = str(exc).lower()
                    is_retryable = any(s in err_msg for s in ("too many", "429", "503", "timeout", "unreachable"))
                    if is_retryable and attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.error("Failed to push school %s: %s", cn, exc)
                        schools_failed.append({"centre_number": cn, "name": school_name, "error": str(exc)})
                        break

            if success:
                consecutive_failures = 0
                schools_done.append({"centre_number": cn, "name": school_name})
                if i < len(cache_updates_by_school):
                    updates = cache_updates_by_school[i]
                    if updates:
                        try:
                            await upsert_cache(db, exam_uuid, updates, task_id)
                            await db.commit()
                        except Exception as exc:
                            logger.warning("Cache update failed for %s: %s", cn, exc)
                            await db.rollback()
            else:
                consecutive_failures += 1
                if consecutive_failures >= _CONSECUTIVE_FAIL_ABORT:
                    logger.error("Aborting: %d consecutive failures", consecutive_failures)
                    schools_failed.append({
                        "centre_number": "—",
                        "name": f"ABORTED: {total_to_push - i - 1} remaining schools skipped",
                        "error": "Push aborted after consecutive failures.",
                    })
                    break

            await asyncio.sleep(_CHUNK_DELAY_SECS)

    # ── Step 5: complete session ──
    try:
        await backend_sis.complete_collection_session(api_key, session_id, {"digest": digest})
    except Exception as exc:
        logger.error("Failed to complete collection session: %s", exc)

    # ── Step 6: update status ──
    try:
        async with _session_factory() as db:
            status = "PUSHED" if not schools_failed else "PUSH_PARTIAL"
            await db.execute(
                sa_update(ExamProcessingLink)
                .where(ExamProcessingLink.exam_id == exam_uuid)
                .values(last_status=status, last_submitted_at=datetime.now(timezone.utc), active_task_id=None)
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to update processing link status: %s", exc)

    engine = _session_factory.kw.get("bind")
    if engine:
        await engine.dispose()

    return {
        "total_schools": total_to_push,
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
    school_payloads: list[dict] | None = None,
    cache_updates_by_school: list[list[tuple]] | None = None,
) -> dict:
    """Push collection data to ExaMetrics one school at a time (streaming)."""
    self.update_state(
        state="STARTED",
        meta={"total_schools": 0, "completed": 0, "phase": "starting", "schools_done": [], "schools_failed": []},
    )
    return asyncio.run(
        _build_and_push(
            exam_id=exam_id, api_key=api_key, backend_exam_id=backend_exam_id,
            force=force, task=self, task_id=self.request.id or "",
        )
    )
