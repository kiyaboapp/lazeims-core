"""Station package generation — strict scope-only export.

A generated package contains ONLY the data inside its own ``assigned_scope``
(schools + subjects + papers), plus the credential HASHES for this station's own
assignments. It never contains out-of-scope rows and never a processing key.

The manifest is built with the shared ``lazeims_common`` contract and signed with
an HMAC over its canonical JSON using ``STATION_PACKAGE_INTEGRITY_KEY``.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common import RULES_VERSION
from lazeims_common.enums import PaperType
from lazeims_common.hashing import canonical_bytes, sha256_prefixed
from lazeims_common.schemas.station_package import PackageScope, StationPackageManifest

from ..config import get_settings
from ..models.assignments import (
    ExamRoleAssignment,
    ScopeWriteAssignment,
    Station,
    StationCredential,
)
from ..models.exam import (
    Exam,
    ExamStudent,
    ExamStudentSubject,
    ExamSubject,
)
from ..models.registry import School, Subject
from ..models.scoring import Question, QuestionGroup, QuestionTopic
from ..models.station import StationPackage

SOFTWARE_MIN_VERSION = "1.0.0"


def sign_manifest(manifest: dict) -> str:
    key = get_settings().station_package_integrity_key.encode("utf-8")
    return "hmac-sha256:" + hmac.new(key, canonical_bytes(manifest), hashlib.sha256).hexdigest()


def verify_manifest_signature(manifest: dict, signature: str) -> bool:
    expected = sign_manifest(manifest)
    return hmac.compare_digest(expected, signature)


async def _in_scope_exam_subjects(db, exam_id, subject_codes) -> list[ExamSubject]:
    rows = (
        await db.execute(
            select(ExamSubject).join(Subject, Subject.id == ExamSubject.subject_id)
            .where(ExamSubject.exam_id == exam_id, Subject.code.in_(subject_codes))
        )
    ).all()
    return [r[0] for r in rows]


async def build_package_seed(
    db: AsyncSession,
    *,
    exam: Exam,
    station: Station,
    schools: list[str],
    subject_codes: list[str],
    papers: list[str],
) -> dict:
    """Build the scope-only seed payload (natural keys throughout)."""
    paper_set = {PaperType(p) for p in papers}

    # schools (only those in scope)
    school_rows = (
        await db.execute(select(School).where(School.centre_number.in_(schools)))
    ).scalars().all()
    school_by_id = {s.id: s for s in school_rows}
    in_scope_school_ids = set(school_by_id)

    # subjects (only in-scope)
    exam_subjects = await _in_scope_exam_subjects(db, exam.id, subject_codes)
    es_by_id = {es.id: es for es in exam_subjects}
    subject_by_id = {}
    for es in exam_subjects:
        subject_by_id[es.id] = await db.get(Subject, es.subject_id)

    # students: in-scope school AND registered for an in-scope subject
    student_rows = (
        await db.execute(
            select(ExamStudent).where(
                ExamStudent.exam_id == exam.id,
                ExamStudent.school_id.in_(in_scope_school_ids or {-1}),
            )
        )
    ).scalars().all()
    student_by_id = {s.id: s for s in student_rows}

    ess_rows = (
        await db.execute(
            select(ExamStudentSubject).where(
                ExamStudentSubject.exam_student_id.in_(list(student_by_id) or [-1]),
                ExamStudentSubject.exam_subject_id.in_(list(es_by_id) or [-1]),
            )
        )
    ).scalars().all()

    # only keep students that actually have an in-scope registration
    kept_student_ids = {e.exam_student_id for e in ess_rows}
    students_out = []
    for sid in kept_student_ids:
        s = student_by_id[sid]
        students_out.append({
            "student_id": s.student_id,
            "centre_number": school_by_id[s.school_id].centre_number,
            "first_name": s.first_name, "middle_name": s.middle_name,
            "surname": s.surname, "sex": s.sex.value,
        })
    registrations_out = [
        {"student_id": student_by_id[e.exam_student_id].student_id,
         "subject_code": subject_by_id[e.exam_subject_id].code}
        for e in ess_rows
    ]

    # subjects + scoring config restricted to in-scope papers
    subjects_out = []
    for es in exam_subjects:
        subj = subject_by_id[es.id]
        groups = (
            await db.execute(
                select(QuestionGroup).where(
                    QuestionGroup.exam_subject_id == es.id,
                    QuestionGroup.paper_type.in_(paper_set),
                )
            )
        ).scalars().all()
        questions = (
            await db.execute(
                select(Question).where(
                    Question.exam_subject_id == es.id,
                    Question.paper_type.in_(paper_set),
                )
            )
        ).scalars().all()
        gcode_by_id = {g.id: g.code for g in groups}
        q_out = []
        for q in questions:
            topics = (
                await db.execute(select(QuestionTopic).where(QuestionTopic.question_id == q.id))
            ).scalars().all()
            q_out.append({
                "paper_type": q.paper_type.value,
                "question_number": q.question_number,
                "group_code": gcode_by_id.get(q.group_id) if q.group_id else None,
                "max_marks": str(q.max_marks),
                "topics": [{"topic_id": t.topic_id, "weight": str(t.weight)} for t in topics],
            })
        subjects_out.append({
            "subject_code": subj.code,
            "name": subj.name,
            "papers": sorted(p.value for p in paper_set),
            "total_marks": {
                "THEORY1": es.total_marks_theory1,
                "THEORY2": es.total_marks_theory2,
                "PRACTICAL": es.total_marks_practical,
            },
            "groups": [{"paper_type": g.paper_type.value, "code": g.code, "name": g.name,
                        "instruction": g.instruction, "pick_count": g.pick_count} for g in groups],
            "questions": q_out,
        })

    # credentials: ONLY hashes for THIS station's assignments (never the user directory)
    cred_rows = (
        await db.execute(
            select(StationCredential, ExamRoleAssignment)
            .join(ExamRoleAssignment, ExamRoleAssignment.id == StationCredential.exam_role_assignment_id)
            .where(StationCredential.station_id == station.id, StationCredential.revoked_at.is_(None))
        )
    ).all()
    credentials_out = [
        {
            "assignment_id": ra.id,
            "role": ra.role.value,
            "pin_hash": sc.pin_hash,
            "initials": sc.initials,
            "password_hash": sc.password_hash,
        }
        for sc, ra in cred_rows
    ]

    return {
        "schools": [{"centre_number": s.centre_number, "name": s.name} for s in school_rows],
        "subjects": subjects_out,
        "students": students_out,
        "registrations": registrations_out,
        "credentials": credentials_out,
        # explicit assertion: no processing key is ever included
        "processing_api_key": None,
    }


async def generate_station_package(
    db: AsyncSession,
    *,
    station: Station,
    schools: list[str],
    subject_codes: list[str],
    papers: list[str],
) -> StationPackage:
    exam = await db.get(Exam, station.exam_id)

    # next version for this station
    last = (
        await db.execute(
            select(StationPackage.package_version).where(StationPackage.station_id == station.id)
            .order_by(StationPackage.package_version.desc())
        )
    ).scalars().first()
    version = (last or 0) + 1

    seed = await build_package_seed(
        db, exam=exam, station=station, schools=schools, subject_codes=subject_codes, papers=papers,
    )
    configuration_hash = sha256_prefixed(seed)
    package_id = "pkg_" + hashlib.sha256(
        f"{station.station_code}:{version}:{configuration_hash}".encode()
    ).hexdigest()[:24]

    manifest_model = StationPackageManifest(
        package_id=package_id,
        package_version=version,
        rules_version=RULES_VERSION,
        software_min_version=SOFTWARE_MIN_VERSION,
        station_code=station.station_code,
        exam_id=str(exam.id),
        configuration_hash=configuration_hash,
        issued_at=datetime.now(timezone.utc),
        scope=PackageScope(schools=schools, subjects=subject_codes, papers=papers),
    )
    manifest = manifest_model.model_dump(mode="json")
    manifest["signature"] = sign_manifest(manifest)

    pkg = StationPackage(
        package_id=package_id,
        station_id=station.id,
        exam_id=exam.id,
        package_version=version,
        rules_version=RULES_VERSION,
        software_min_version=SOFTWARE_MIN_VERSION,
        configuration_hash=configuration_hash,
        assigned_scope={"schools": schools, "subjects": subject_codes, "papers": papers},
        manifest={"manifest": manifest, "seed": seed},
        issued_at=datetime.now(timezone.utc),
    )
    db.add(pkg)
    await db.flush()
    return pkg
