"""Central station-sync intake — per-event processing.

For every event, in order (delivery plan §11.2):
  1. dedupe by event_id (+ payload-hash conflict check);
  2. validate station/package/version/revocation (done by the router/caller);
  3. validate writer assignment + package scope;
  4. resolve natural keys to Central rows;
  5. run the shared lazeims_common validation (inside the apply services);
  6. atomically apply + write a SyncEventReceipt;
  7. return an individual accepted/duplicate/rejected result.

The portable import path calls this SAME function — a different transport, never
different logic.
"""

from __future__ import annotations
import uuid

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import (
    AttendanceSource,
    EventStatus,
    FillingMode,
    IncidentStatus,
    IncidentType,
    PaperType,
    RejectionCode,
    WriterMode,
)
from lazeims_common.errors import ValidationError
from lazeims_common.hashing import sha256_prefixed

from ..models.exam import Exam, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.marks import ExamIncident
from ..models.registry import School, Subject
from ..models.station import StationPackage, SyncEventReceipt
from .attendance import upsert_attendance
from .finalize import finalize_scope
from .marks_apply import apply_student_paper_marks
from .scope_assignment import ResolvedStudentScope, enforce_writer_mode, is_scope_finalized


async def _resolve_by_codes(db, exam_id, student_id, subject_code) -> ResolvedStudentScope:
    exam = await db.get(Exam, exam_id)
    student = (
        await db.execute(select(ExamStudent).where(
            ExamStudent.exam_id == exam_id, ExamStudent.student_id == student_id))
    ).scalar_one_or_none()
    if student is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED, f"Unknown student {student_id}.")
    es = (
        await db.execute(select(ExamSubject).join(Subject, Subject.id == ExamSubject.subject_id)
                         .where(ExamSubject.exam_id == exam_id, Subject.code == subject_code))
    ).scalar_one_or_none()
    if es is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED, f"Subject {subject_code} not offered.")
    ess = (
        await db.execute(select(ExamStudentSubject).where(
            ExamStudentSubject.exam_student_id == student.id,
            ExamStudentSubject.exam_subject_id == es.id))
    ).scalar_one_or_none()
    if ess is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED,
                              f"Student {student_id} not registered for {subject_code}.")
    subject = await db.get(Subject, es.subject_id)
    return ResolvedStudentScope(exam, student, es, subject, ess)


async def _school_by_centre(db, centre_number) -> School:
    school = (
        await db.execute(select(School).where(School.centre_number == centre_number))
    ).scalar_one_or_none()
    if school is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED, f"Unknown centre {centre_number}.")
    return school


async def _exam_subject_by_code(db, exam_id, subject_code) -> ExamSubject:
    es = (
        await db.execute(select(ExamSubject).join(Subject, Subject.id == ExamSubject.subject_id)
                         .where(ExamSubject.exam_id == exam_id, Subject.code == subject_code))
    ).scalar_one_or_none()
    if es is None:
        raise ValidationError(RejectionCode.NOT_REGISTERED, f"Subject {subject_code} not offered.")
    return es


async def _apply_event(db, *, exam_id: uuid.UUID, station_id: int, event: dict) -> None:
    """Apply a single coarse event. Raises ValidationError to reject."""
    etype = event["entity_type"]
    nk = event.get("natural_key", {})
    value = event.get("value") or {}

    if etype == "ATTENDANCE_TRANSCRIBED":
        paper = PaperType(nk["paper_type"])
        scope = await _resolve_by_codes(db, exam_id, nk["student_id"], nk["subject_code"])
        await enforce_writer_mode(db, exam_id=exam_id, school_id=scope.student.school_id,
                                  exam_subject_id=scope.exam_subject.id, paper_type=paper,
                                  expected_mode=WriterMode.STATION, station_id=station_id)
        if await is_scope_finalized(db, exam_id=exam_id, school_id=scope.student.school_id,
                                    exam_subject_id=scope.exam_subject.id, paper_type=paper):
            raise ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized.")
        await upsert_attendance(db, exam_student_subject_id=scope.exam_student_subject.id,
                                paper_type=paper, is_present=bool(value["is_present"]),
                                source=AttendanceSource(value.get("source", "INVIGILATOR_ISAL_TRANSCRIPTION")),
                                actor_id=None)

    elif etype == "STUDENT_PAPER_MARKS_REPLACED":
        paper = PaperType(nk["paper_type"])
        scope = await _resolve_by_codes(db, exam_id, nk["student_id"], nk["subject_code"])
        await enforce_writer_mode(db, exam_id=exam_id, school_id=scope.student.school_id,
                                  exam_subject_id=scope.exam_subject.id, paper_type=paper,
                                  expected_mode=WriterMode.STATION, station_id=station_id)
        if await is_scope_finalized(db, exam_id=exam_id, school_id=scope.student.school_id,
                                    exam_subject_id=scope.exam_subject.id, paper_type=paper):
            raise ValidationError(RejectionCode.SCOPE_ALREADY_FINALIZED, "Scope is finalized.")
        mode = FillingMode(value["mode"])
        total = value.get("total")
        items = value.get("items")
        await apply_student_paper_marks(
            db, scope=scope, paper_type=paper, mode=mode,
            total_marks_obtained=(None if total in (None, "") else total),
            items={k: v for k, v in (items or {}).items()} if mode == FillingMode.ITEM_LEVEL else None,
            actor_id=None,
        )

    elif etype == "INCIDENT_RAISED":
        paper = PaperType(nk["paper_type"])
        es = await _exam_subject_by_code(db, exam_id, nk["subject_code"])
        student_id = value.get("student_id")
        ess_id = None
        school_id = None
        if student_id:
            scope = await _resolve_by_codes(db, exam_id, student_id, nk["subject_code"])
            ess_id = scope.exam_student_subject.id
            school_id = scope.student.school_id
        db.add(ExamIncident(
            exam_id=exam_id, exam_student_subject_id=ess_id, school_id=school_id,
            exam_subject_id=es.id, paper_type=paper,
            incident_type=IncidentType(value.get("incident_type", "MISSING_SCRIPT")),
            explanation=value.get("explanation"), status=IncidentStatus.OPEN,
            raised_at=datetime.now(timezone.utc),
        ))
        await db.flush()

    elif etype == "SCOPE_FINALIZED":
        paper = PaperType(nk["paper_type"])
        school = await _school_by_centre(db, nk["centre_number"])
        es = await _exam_subject_by_code(db, exam_id, nk["subject_code"])
        exam = await db.get(Exam, exam_id)
        await enforce_writer_mode(db, exam_id=exam_id, school_id=school.id,
                                  exam_subject_id=es.id, paper_type=paper,
                                  expected_mode=WriterMode.STATION, station_id=station_id)
        finalized, result = await finalize_scope(
            db, exam=exam, school_id=school.id, exam_subject=es, paper_type=paper,
            writer_mode=WriterMode.STATION, finalized_by=None)
        if not finalized:
            raise ValidationError(RejectionCode.BLANK_MARK_NOT_ALLOWED,
                                  "Scope is not complete on Central.", result)
    else:
        raise ValidationError(RejectionCode.CONFIGURATION_MISMATCH, f"Unknown event type {etype}.")


async def _audit(db, exam_id, event_id, entity_type, status, code):
    from . import notifications as _notif
    await _notif.record(
        db, action=f"SYNC_{status}", entity_type=entity_type, entity_id=event_id,
        exam_id=exam_id, source="STATION_SYNC", after={"status": status, "code": code},
    )


async def process_events(
    db: AsyncSession, *, exam_id: uuid.UUID, station_id: int, package_id: str, events: list[dict],
) -> dict:
    """Process a batch; return the SyncResponse dict (per-event outcomes)."""
    accepted, duplicates, rejected = [], [], []

    # Phase gate: reject events when the exam is past ENTRY_LOCKED (section 10.2).
    from lazeims_common.enums import ExamPhase as _EP
    exam = await db.get(Exam, exam_id)
    phase_closed = exam is not None and exam.phase not in (_EP.ENTRY_OPEN, _EP.ENTRY_LOCKED)

    for event in events:
        event_id = event["event_id"]

        # If the exam phase does not allow writes, reject per-event so
        # other events in the batch still get dedupe-checked.
        if phase_closed:
            payload_hash = sha256_prefixed(event.get("value"))
            existing = await db.get(SyncEventReceipt, event_id)
            if existing is not None:
                if existing.payload_hash and existing.payload_hash != payload_hash:
                    rejected.append({"event_id": event_id, "code": RejectionCode.EVENT_ID_PAYLOAD_CONFLICT.value,
                                     "message": "Event id already used with a different payload."})
                else:
                    duplicates.append({"event_id": event_id})
                continue
            rejected.append({"event_id": event_id, "code": "PHASE_NOT_OPEN",
                             "message": f"Exam is past ENTRY_LOCKED (current: {exam.phase.value}). No new data accepted."})
            continue

        payload_hash = sha256_prefixed(event.get("value"))
        existing = await db.get(SyncEventReceipt, event_id)
        if existing is not None:
            # Idempotent: same event already processed. Conflict if payload differs.
            if existing.payload_hash and existing.payload_hash != payload_hash:
                rejected.append({"event_id": event_id, "code": RejectionCode.EVENT_ID_PAYLOAD_CONFLICT.value,
                                 "message": "Event id already used with a different payload."})
            else:
                duplicates.append({"event_id": event_id})
            continue

        # Use a SAVEPOINT so one bad event never aborts the whole batch.
        sp = await db.begin_nested()
        try:
            await _apply_event(db, exam_id=exam_id, station_id=station_id, event=event)
            db.add(SyncEventReceipt(
                event_id=event_id, station_id=station_id, package_id=package_id,
                entity_type=event["entity_type"], status=EventStatus.ACCEPTED,
                payload_hash=payload_hash, received_at=datetime.now(timezone.utc),
            ))
            await db.flush()
            await sp.commit()
            accepted.append({"event_id": event_id, "central_version": event.get("local_version", 1)})
            await _audit(db, exam_id, event_id, event["entity_type"], "ACCEPTED", None)
        except ValidationError as exc:
            await sp.rollback()
            db.add(SyncEventReceipt(
                event_id=event_id, station_id=station_id, package_id=package_id,
                entity_type=event["entity_type"], status=EventStatus.REJECTED,
                rejection_code=exc.code.value, payload_hash=payload_hash,
                received_at=datetime.now(timezone.utc),
            ))
            await db.flush()
            rejected.append({"event_id": event_id, "code": exc.code.value, "message": exc.message})
            await _audit(db, exam_id, event_id, event["entity_type"], "REJECTED", exc.code.value)

    if rejected:
        from . import notifications as _notif
        await _notif.notify(
            db, type="REJECTED_EVENTS", severity="WARNING", exam_id=exam_id, station_id=station_id,
            audience_roles=["EXAM_ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN", "REGION_ITS", "COUNCIL_ITS"],
            message=f"{len(rejected)} station sync event(s) were rejected and need correction.",
        )

    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


async def compute_central_scope_digest(
    db: AsyncSession, *, exam_id: uuid.UUID, centre_number: str, subject_code: str, paper_type: PaperType,
) -> tuple[str, dict]:
    """Build the canonical scope records from Central's authoritative store and
    return (digest, counts) using the SHARED reconcile helper — identical shape
    to what the station computes locally."""
    from lazeims_common.reconcile import (
        counts_from_records,
        normalize_item_record,
        normalize_total_record,
        scope_digest,
    )
    from ..models.marks import ItemMark, TotalMark
    from ..models.scoring import Question
    from .attendance import resolve_effective_attendance

    school = await _school_by_centre(db, centre_number)
    es = await _exam_subject_by_code(db, exam_id, subject_code)

    qrows = (
        await db.execute(select(Question.id, Question.question_number).where(
            Question.exam_subject_id == es.id, Question.paper_type == paper_type))
    ).all()
    is_item = len(qrows) > 0
    qnum_by_id = {qid: qn for qid, qn in qrows}

    students = (
        await db.execute(
            select(ExamStudent, ExamStudentSubject)
            .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
            .where(ExamStudent.exam_id == exam_id, ExamStudent.school_id == school.id,
                   ExamStudentSubject.exam_subject_id == es.id)
        )
    ).all()

    records = []
    for student, ess in students:
        present = await resolve_effective_attendance(db, ess.id, paper_type)
        if is_item:
            rows = (
                await db.execute(select(ItemMark.question_id, ItemMark.marks_obtained).where(
                    ItemMark.exam_student_subject_id == ess.id,
                    ItemMark.question_id.in_(list(qnum_by_id))))
            ).all()
            items = {qnum_by_id[qid]: v for qid, v in rows}
            if present or items:
                records.append(normalize_item_record(student.student_id, present, items))
        else:
            tm = (
                await db.execute(select(TotalMark.total_marks_obtained).where(
                    TotalMark.exam_student_subject_id == ess.id, TotalMark.paper_type == paper_type))
            ).scalar_one_or_none()
            records.append(normalize_total_record(student.student_id, present, tm))

    return scope_digest(records), counts_from_records(records).as_dict()
