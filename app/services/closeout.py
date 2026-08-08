"""Collection closeout: readiness validation, immutable sealing, reopen, export.

Readiness aggregates every gate that must pass before a collection can be sealed;
each failure is a stable blocking code with evidence. Sealing is idempotent by
``(exam_id, closeout_revision, configuration_hash)`` and reproducible (same input
-> same content hash). Reopening increments the exam's closeout revision so the
old snapshot is preserved and the next seal is a new immutable revision.

Exports are SERVICE-NEUTRAL: collected data + configuration + integrity metadata
only. No ranking/analysis/processing logic ever appears in a snapshot or export.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common import COLLECTION_EXPORT_CONTRACT
from lazeims_common.enums import IncidentStatus, PaperType, WriterMode
from lazeims_common.errors import LazeimsError
from lazeims_common.hashing import sha256_prefixed
from lazeims_common.reconcile import (
    counts_from_records,
    normalize_item_record,
    normalize_total_record,
    scope_digest,
)

from ..models.assignments import FinalizedScope, Station
from ..models.collection import CollectionExportFile, CollectionReadinessRun, CollectionSnapshot
from ..models.exam import Exam, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.marks import ExamIncident, ItemMark, TotalMark
from ..models.registry import School, Subject
from ..models.scoring import Question
from ..models.station import StationPackage, StationReconciliation, SyncEventReceipt
from .attendance import resolve_effective_attendance
from .exam_phase import applicable_papers


class CloseoutBlocked(LazeimsError):
    def __init__(self, blockers: list[dict], evidence: dict):
        self.blockers = blockers
        self.evidence = evidence
        super().__init__("Collection closeout is blocked.")


@dataclass
class Readiness:
    ready: bool
    blockers: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ready": self.ready, "blockers": self.blockers, "evidence": self.evidence}


async def _expected_scopes(db, exam: Exam) -> set[tuple[int, int, str]]:
    subjects = {s.id: s for s in (await db.execute(
        select(ExamSubject).where(ExamSubject.exam_id == exam.id))).scalars().all()}
    reg_rows = (
        await db.execute(
            select(distinct(ExamStudent.school_id), ExamStudentSubject.exam_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam.id))
    ).all()
    expected = set()
    for school_id, es_id in reg_rows:
        subj = subjects.get(es_id)
        if subj is None:
            continue
        for paper in applicable_papers(subj):
            expected.add((school_id, es_id, paper.value))
    return expected


async def compute_readiness(db: AsyncSession, exam: Exam) -> Readiness:
    blockers: list[dict] = []
    evidence: dict = {}

    expected = await _expected_scopes(db, exam)
    evidence["expected_scope_count"] = len(expected)

    # finalized scopes complete
    fin_rows = (
        await db.execute(select(
            FinalizedScope.school_id, FinalizedScope.exam_subject_id, FinalizedScope.paper_type)
            .where(FinalizedScope.exam_id == exam.id))
    ).all()
    finalized = {(r[0], r[1], r[2].value if hasattr(r[2], "value") else r[2]) for r in fin_rows}
    not_final = expected - finalized
    evidence["finalized_scope_count"] = len(finalized)
    if not_final:
        blockers.append({"code": "SCOPE_NOT_FINALIZED",
                         "message": f"{len(not_final)} required scope(s) are not finalized.",
                         "evidence": {"unfinalized_count": len(not_final)}})

    # unresolved incidents
    open_incidents = await db.scalar(
        select(func.count()).select_from(ExamIncident).where(
            ExamIncident.exam_id == exam.id,
            ExamIncident.status.in_(list(IncidentStatus.unresolved()))))
    if open_incidents:
        blockers.append({"code": "UNRESOLVED_INCIDENT",
                         "message": f"{open_incidents} unresolved incident(s) block closeout.",
                         "evidence": {"open_incidents": open_incidents}})

    # rejected sync events
    rejected_events = await db.scalar(
        select(func.count()).select_from(SyncEventReceipt)
        .join(Station, Station.id == SyncEventReceipt.station_id)
        .where(Station.exam_id == exam.id, SyncEventReceipt.status == "REJECTED"))
    if rejected_events:
        blockers.append({"code": "REJECTED_SYNC_EVENTS",
                         "message": f"{rejected_events} rejected sync event(s) must be corrected.",
                         "evidence": {"rejected_events": rejected_events}})

    # station reconciliation: every station with a package must have a MATCHED reconciliation
    stations = (await db.execute(select(Station).where(Station.exam_id == exam.id))).scalars().all()
    unreconciled = []
    for st in stations:
        pkg_count = await db.scalar(select(func.count()).select_from(StationPackage).where(
            StationPackage.station_id == st.id))
        if not pkg_count:
            continue
        matched = await db.scalar(select(func.count()).select_from(StationReconciliation).where(
            StationReconciliation.station_id == st.id, StationReconciliation.status == "MATCHED"))
        if not matched:
            unreconciled.append(st.station_code)
    if unreconciled:
        blockers.append({"code": "STATION_NOT_RECONCILED",
                         "message": f"{len(unreconciled)} station(s) not reconciled.",
                         "evidence": {"stations": unreconciled}})

    return Readiness(ready=not blockers, blockers=blockers, evidence=evidence)


async def _scope_records(db, exam_id, school_id, es: ExamSubject, paper: PaperType) -> list[dict]:
    qrows = (await db.execute(select(Question.id, Question.question_number).where(
        Question.exam_subject_id == es.id, Question.paper_type == paper))).all()
    is_item = len(qrows) > 0
    qnum_by_id = {qid: qn for qid, qn in qrows}
    students = (
        await db.execute(select(ExamStudent, ExamStudentSubject)
                         .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
                         .where(ExamStudent.exam_id == exam_id, ExamStudent.school_id == school_id,
                                ExamStudentSubject.exam_subject_id == es.id))
    ).all()

    if not students:
        return []

    ess_ids = [ess.id for _, ess in students]

    # Batch attendance
    from ..models.marks import Attendance as _Att
    att_rows = (await db.execute(
        select(_Att.exam_student_subject_id, _Att.paper_type, _Att.is_present)
        .where(_Att.exam_student_subject_id.in_(ess_ids), _Att.paper_type.in_([paper, PaperType.ALL]))
    )).all()
    att_specific: dict[int, bool] = {}
    att_all_map: dict[int, bool] = {}
    for ess_id, pt, is_present in att_rows:
        pt_val = pt.value if hasattr(pt, 'value') else pt
        if pt_val == (paper.value if hasattr(paper, 'value') else paper):
            att_specific[ess_id] = is_present
        elif pt_val == 'ALL':
            att_all_map[ess_id] = is_present
    att_map = {ess_id: att_specific.get(ess_id, att_all_map.get(ess_id, True)) for ess_id in ess_ids}

    records = []
    if is_item:
        # Batch item marks
        im_rows = (await db.execute(
            select(ItemMark.exam_student_subject_id, ItemMark.question_id, ItemMark.marks_obtained)
            .where(ItemMark.exam_student_subject_id.in_(ess_ids), ItemMark.question_id.in_(list(qnum_by_id)))
        )).all()
        im_by_ess: dict[int, dict[str, Any]] = {}
        for ess_id, qid, v in im_rows:
            im_by_ess.setdefault(ess_id, {})[qnum_by_id[qid]] = v

        for student, ess in students:
            present = att_map.get(ess.id, True)
            items = im_by_ess.get(ess.id, {})
            if present or items:
                records.append(normalize_item_record(student.student_id, present, items))
    else:
        # Batch total marks
        tm_rows = (await db.execute(
            select(TotalMark.exam_student_subject_id, TotalMark.total_marks_obtained)
            .where(TotalMark.exam_student_subject_id.in_(ess_ids), TotalMark.paper_type == paper)
        )).all()
        tm_map = {r[0]: r[1] for r in tm_rows}

        for student, ess in students:
            present = att_map.get(ess.id, True)
            tm = tm_map.get(ess.id)
            records.append(normalize_total_record(student.student_id, present, tm))

    return records


async def _build_manifest_and_export(db, exam: Exam) -> tuple[dict, dict]:
    """Return (canonical_manifest_dict, export_content_dict)."""
    config_hash = None
    if exam.current_configuration_version is not None:
        from ..models.scoring import ExamConfigurationVersion
        cv = (await db.execute(select(ExamConfigurationVersion).where(
            ExamConfigurationVersion.exam_id == exam.id,
            ExamConfigurationVersion.version == exam.current_configuration_version))).scalar_one_or_none()
        config_hash = cv.configuration_hash if cv else None

    fin_rows = (await db.execute(select(FinalizedScope).where(FinalizedScope.exam_id == exam.id))).scalars().all()
    scope_counts = []
    export_scopes = []
    total_students = 0
    total_marks = 0
    for fs in sorted(fin_rows, key=lambda f: (f.school_id, f.exam_subject_id, f.paper_type.value)):
        school = await db.get(School, fs.school_id)
        es = await db.get(ExamSubject, fs.exam_subject_id)
        subject = await db.get(Subject, es.subject_id)
        paper = fs.paper_type
        records = await _scope_records(db, exam.id, fs.school_id, es, paper)
        counts = counts_from_records(records)
        digest = scope_digest(records)
        scope_counts.append({
            "school_code": school.centre_number, "subject_code": subject.code, "paper_type": paper.value,
            "present_count": counts.present, "absent_count": counts.absent,
            "marks_count": counts.with_marks, "scope_hash": digest,
        })
        export_scopes.append({
            "school_code": school.centre_number, "subject_code": subject.code, "paper_type": paper.value,
            "records": records,
        })
        total_students += counts.present + counts.absent
        total_marks += counts.with_marks

    if config_hash is None:
        # deterministic fallback hash of the collected scope set
        config_hash = sha256_prefixed(scope_counts)

    manifest = {
        "contract_version": COLLECTION_EXPORT_CONTRACT,
        "exam_id": str(exam.id),
        "configuration_version": exam.current_configuration_version,
        "configuration_hash": config_hash,
        "closeout_revision": exam.closeout_revision,
        "scope_counts": scope_counts,
        "total_students": total_students,
        "total_marks": total_marks,
    }
    export_content = {
        "contract_version": COLLECTION_EXPORT_CONTRACT,
        "exam_id": str(exam.id),
        "closeout_revision": exam.closeout_revision,
        "configuration_hash": config_hash,
        "scopes": export_scopes,   # collected natural-key records only
        "manifest": manifest,
    }
    return manifest, export_content


async def seal_snapshot(db: AsyncSession, exam: Exam, sealed_by: int | None) -> CollectionSnapshot:
    readiness = await compute_readiness(db, exam)
    if not readiness.ready:
        raise CloseoutBlocked(readiness.blockers, readiness.evidence)

    manifest, export_content = await _build_manifest_and_export(db, exam)
    content_hash = sha256_prefixed(manifest)
    config_hash = manifest["configuration_hash"]

    # Idempotent by (exam, closeout_revision, configuration_hash).
    existing = (
        await db.execute(select(CollectionSnapshot).where(
            CollectionSnapshot.exam_id == exam.id,
            CollectionSnapshot.closeout_revision == exam.closeout_revision,
            CollectionSnapshot.configuration_hash == config_hash))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    snapshot_id = "snp_" + hashlib.sha256(
        f"{str(exam.id)}:{exam.closeout_revision}:{content_hash}".encode()).hexdigest()[:24]
    snap = CollectionSnapshot(
        snapshot_id=snapshot_id, exam_id=exam.id, closeout_revision=exam.closeout_revision,
        configuration_version=exam.current_configuration_version, configuration_hash=config_hash,
        status="SEALED", manifest=manifest, content_hash=content_hash,
        sealed_by=sealed_by, sealed_at=datetime.now(timezone.utc),
    )
    db.add(snap)
    await db.flush()

    export_bytes = sha256_prefixed(export_content)
    db.add(CollectionExportFile(
        snapshot_id=snapshot_id, format="JSON", schema_version=COLLECTION_EXPORT_CONTRACT,
        media_type="application/json", size_bytes=len(str(export_content)),
        content_hash=export_bytes, content=export_content,
    ))
    await db.flush()
    return snap


async def reopen_closeout(db: AsyncSession, exam: Exam, reason: str, actor_id: int | None) -> int:
    """Increment the exam's closeout revision. Old snapshot(s) are preserved."""
    if not reason or not reason.strip():
        raise CloseoutBlocked([{"code": "REASON_REQUIRED", "message": "A reason is required to reopen closeout."}], {})
    exam.closeout_revision += 1
    await db.flush()
    return exam.closeout_revision
