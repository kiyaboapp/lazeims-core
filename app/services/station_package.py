"""Central station package generation.

The single canonical generator: Ed25519-signed manifest, scope-only seed,
per-package Argon2id machine credential, supersession pointer, applicable
papers, Data Enterer scope data, and admin username. There is one contract
(``station-package/v1``) — no legacy code paths in development.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common import RULES_VERSION
from lazeims_common.enums import PaperType, WriterMode
from lazeims_common.hashing import sha256_prefixed
from lazeims_common.schemas.station_package import (
    CONTRACT_VERSION,
    DataEntererScopeEntry,
    MachineCredentialMeta,
    MachineCredentialPayload,
    PackageErrorCode,
    PackageScope,
    SigningMeta,
    StationAdminEntry,
    StationPackageManifest,
)
from lazeims_common.signing import sign_package_manifest

from ..config import get_settings
from ..enums import ExamRoleName
from ..models.assignments import (
    DataEntererScope,
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
from ..models.registry import Council, Region, School, Subject
from ..models.scoring import Question, QuestionGroup, QuestionTopic
from ..models.station import StationMachineCredential, StationPackage
from ..security import hash_secret

SOFTWARE_MIN_VERSION = "1.0.0"


class ScopeConflictError(Exception):
    """Raised when ScopeWriteAssignment conflicts are detected."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} scope conflict(s) detected")


class PackagePreparationError(Exception):
    """Raised for actionable package preparation failures."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


# ── Scope collection-channel assignment ──────────────────────────────────────

async def validate_and_assign_scopes(
    db: AsyncSession,
    *,
    exam_id,
    station_id: int,
    school_ids: list[int],
    exam_subject_ids: list[int],
    paper_types: list[PaperType],
    actor_id: int | None = None,
) -> list[ScopeWriteAssignment]:
    """Create/validate ScopeWriteAssignment rows for every selected combination.

    Same-station reassignment is a no-op. Any other conflict raises
    :class:`ScopeConflictError` with actionable detail so the operator can
    reassign before regenerating the package.
    """
    conflicts: list[dict] = []
    assignments: list[ScopeWriteAssignment] = []

    # Batch-load all existing assignments for the requested scopes in one query
    existing_rows = (
        await db.execute(
            select(ScopeWriteAssignment).where(
                ScopeWriteAssignment.exam_id == exam_id,
                ScopeWriteAssignment.school_id.in_(school_ids),
                ScopeWriteAssignment.exam_subject_id.in_(exam_subject_ids),
                ScopeWriteAssignment.paper_type.in_(paper_types),
            )
        )
    ).scalars().all()
    existing_map = {
        (r.school_id, r.exam_subject_id, r.paper_type.value if hasattr(r.paper_type, 'value') else r.paper_type): r
        for r in existing_rows
    }

    for school_id in school_ids:
        for es_id in exam_subject_ids:
            for paper in paper_types:
                key = (school_id, es_id, paper.value if hasattr(paper, 'value') else paper)
                existing = existing_map.get(key)

                if existing is None:
                    new_assign = ScopeWriteAssignment(
                        exam_id=exam_id,
                        school_id=school_id,
                        exam_subject_id=es_id,
                        paper_type=paper,
                        writer_mode=WriterMode.STATION,
                        station_id=station_id,
                        assigned_by=actor_id,
                    )
                    db.add(new_assign)
                    assignments.append(new_assign)
                elif existing.writer_mode != WriterMode.STATION:
                    # ONLINE/CAL scope — reassign to this station (operator
                    # is explicitly choosing to use a station for this scope)
                    existing.writer_mode = WriterMode.STATION
                    existing.station_id = station_id
                    existing.assigned_by = actor_id
                    assignments.append(existing)
                elif existing.station_id != station_id:
                    conflicts.append({
                        "school_id": school_id,
                        "exam_subject_id": es_id,
                        "paper_type": paper.value,
                        "current_mode": "STATION",
                        "current_station_id": existing.station_id,
                        "conflict_type": "different_station",
                    })
                else:
                    assignments.append(existing)

    if conflicts:
        raise ScopeConflictError(conflicts)

    await db.flush()
    return assignments


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _in_scope_exam_subjects(db: AsyncSession, exam_id, subject_codes) -> list[ExamSubject]:
    rows = (
        await db.execute(
            select(ExamSubject).join(Subject, Subject.id == ExamSubject.subject_id)
            .where(ExamSubject.exam_id == exam_id, Subject.code.in_(subject_codes))
        )
    ).all()
    return [r[0] for r in rows]


async def _resolve_school_ids(db: AsyncSession, centre_numbers: list[str]) -> dict[str, int]:
    rows = (
        await db.execute(select(School).where(School.centre_number.in_(centre_numbers)))
    ).scalars().all()
    return {s.centre_number: s.id for s in rows}


async def _build_de_scopes(db: AsyncSession, station_id: int) -> list[DataEntererScopeEntry]:
    """Build DataEnterer scope entries for the station's DE credentials."""
    cred_rows = (
        await db.execute(
            select(StationCredential, ExamRoleAssignment)
            .join(ExamRoleAssignment, ExamRoleAssignment.id == StationCredential.exam_role_assignment_id)
            .where(
                StationCredential.station_id == station_id,
                StationCredential.revoked_at.is_(None),
                StationCredential.pin_hash.isnot(None),
                ExamRoleAssignment.role == ExamRoleName.DATA_ENTERER,
            )
        )
    ).all()

    entries: list[DataEntererScopeEntry] = []
    for cred, assignment in cred_rows:
        scopes = (
            await db.execute(
                select(DataEntererScope).where(
                    DataEntererScope.exam_role_assignment_id == assignment.id
                )
            )
        ).scalars().all()
        school_cns: list[str] = []
        subject_codes: list[str] = []
        for ds in scopes:
            if ds.school_id:
                school = await db.get(School, ds.school_id)
                if school and school.centre_number not in school_cns:
                    school_cns.append(school.centre_number)
            if ds.subject_id:
                subj = await db.get(Subject, ds.subject_id)
                if subj and subj.code not in subject_codes:
                    subject_codes.append(subj.code)
        entries.append(DataEntererScopeEntry(
            assignment_id=assignment.id,
            initials=cred.initials or "",
            pin_hash=cred.pin_hash or "",
            school_centre_numbers=school_cns,
            subject_codes=subject_codes,
        ))
    return entries


async def _build_admin_entry(db: AsyncSession, station_id: int) -> StationAdminEntry | None:
    cred = (
        await db.execute(
            select(StationCredential, ExamRoleAssignment)
            .join(ExamRoleAssignment, ExamRoleAssignment.id == StationCredential.exam_role_assignment_id)
            .where(
                StationCredential.station_id == station_id,
                StationCredential.revoked_at.is_(None),
                StationCredential.password_hash.isnot(None),
                ExamRoleAssignment.role == ExamRoleName.EXAM_ADMIN,
            )
            .order_by(StationCredential.issued_at.desc())
        )
    ).first()
    if cred is None:
        return None
    sc, ra = cred
    username = sc.admin_username or sc.initials or "admin"
    return StationAdminEntry(
        assignment_id=ra.id,
        username=username,
        password_hash=sc.password_hash or "",
    )


def _generate_machine_credential() -> tuple[str, str, str]:
    """Return (credential_id, plaintext_secret, argon2id_hash)."""
    credential_id = f"mc_{secrets.token_hex(12)}"
    plaintext_secret = secrets.token_urlsafe(32)
    return credential_id, plaintext_secret, hash_secret(plaintext_secret)


# ── Seed builder (scope-only) ────────────────────────────────────────────────

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

    school_rows = (
        await db.execute(select(School).where(School.centre_number.in_(schools)))
    ).scalars().all()
    # Pre-fetch region/council names for inclusion in the seed
    region_ids = {s.region_id for s in school_rows if s.region_id}
    council_ids = {s.council_id for s in school_rows if s.council_id}
    region_names: dict[int, str] = {}
    council_names: dict[int, str] = {}
    if region_ids:
        rows = (await db.execute(select(Region).where(Region.id.in_(region_ids)))).scalars().all()
        region_names = {r.id: r.name for r in rows}
    if council_ids:
        rows = (await db.execute(select(Council).where(Council.id.in_(council_ids)))).scalars().all()
        council_names = {c.id: c.name for c in rows}
    school_by_id = {s.id: s for s in school_rows}
    in_scope_school_ids = set(school_by_id)

    exam_subjects = await _in_scope_exam_subjects(db, exam.id, subject_codes)
    es_by_id = {es.id: es for es in exam_subjects}
    subject_by_id: dict[int, Subject] = {}
    for es in exam_subjects:
        subject_by_id[es.id] = await db.get(Subject, es.subject_id)

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

    kept_student_ids = {e.exam_student_id for e in ess_rows}
    students_out = [
        {
            "student_id": student_by_id[sid].student_id,
            "centre_number": school_by_id[student_by_id[sid].school_id].centre_number,
            "first_name": student_by_id[sid].first_name,
            "middle_name": student_by_id[sid].middle_name,
            "surname": student_by_id[sid].surname,
            "sex": student_by_id[sid].sex.value,
        }
        for sid in kept_student_ids
    ]
    registrations_out = [
        {
            "student_id": student_by_id[e.exam_student_id].student_id,
            "subject_code": subject_by_id[e.exam_subject_id].code,
        }
        for e in ess_rows
    ]

    subjects_out: list[dict] = []
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
        questions_out = []
        for q in questions:
            topics = (
                await db.execute(select(QuestionTopic).where(QuestionTopic.question_id == q.id))
            ).scalars().all()
            questions_out.append({
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
            "groups": [
                {
                    "paper_type": g.paper_type.value,
                    "code": g.code,
                    "name": g.name,
                    "instruction": g.instruction,
                    "pick_count": g.pick_count,
                }
                for g in groups
            ],
            "questions": questions_out,
        })

    cred_rows = (
        await db.execute(
            select(StationCredential, ExamRoleAssignment)
            .join(ExamRoleAssignment, ExamRoleAssignment.id == StationCredential.exam_role_assignment_id)
            .where(
                StationCredential.station_id == station.id,
                StationCredential.revoked_at.is_(None),
            )
        )
    ).all()
    credentials_out = [
        {
            "assignment_id": ra.id,
            "role": ra.role.value,
            "pin_hash": sc.pin_hash,
            "initials": sc.initials,
            "password_hash": sc.password_hash,
            "admin_username": sc.admin_username,
        }
        for sc, ra in cred_rows
    ]

    return {
        "schools": [
            {
                "centre_number": s.centre_number,
                "name": s.name,
                "council_name": council_names.get(s.council_id) if s.council_id else None,
                "region_name": region_names.get(s.region_id) if s.region_id else None,
            }
            for s in school_rows
        ],
        "subjects": subjects_out,
        "students": students_out,
        "registrations": registrations_out,
        "credentials": credentials_out,
        # explicit assertion: no processing key is ever included
        "processing_api_key": None,
    }


# ── Entry point ──────────────────────────────────────────────────────────────

async def generate_station_package(
    db: AsyncSession,
    *,
    station: Station,
    schools: list[str],
    subject_codes: list[str],
    papers: list[str],
    actor_id: int | None = None,
) -> tuple[StationPackage, str]:
    """Generate a station package.

    Returns (StationPackage, plaintext_machine_secret). Central stores only the
    Argon2id hash of the machine secret; the plaintext appears once in the
    bundle payload and is never persisted centrally.
    """
    settings = get_settings()
    exam = await db.get(Exam, station.exam_id)

    central_url = settings.central_public_base_url.strip()
    if settings.is_production and not central_url:
        raise PackagePreparationError(
            PackageErrorCode.CENTRAL_URL_NOT_CONFIGURED,
            "CENTRAL_PUBLIC_BASE_URL is not configured. Stations need this "
            "for online-first setup and direct sync.",
        )

    school_map = await _resolve_school_ids(db, schools)
    missing_schools = set(schools) - set(school_map.keys())
    if missing_schools:
        raise PackagePreparationError(
            PackageErrorCode.NO_REGISTERED_STUDENTS,
            f"Schools not found: {sorted(missing_schools)}",
            {"missing": sorted(missing_schools)},
        )
    school_ids = list(school_map.values())

    # Validate schools fall within the station's geographic scope (if set)
    if station.scope_mode == "LOCATION" and (station.council_id or station.region_id):
        out_of_scope: list[str] = []
        for centre, sid in school_map.items():
            school_obj = await db.get(School, sid)
            if station.council_id and school_obj and school_obj.council_id != station.council_id:
                out_of_scope.append(centre)
            elif not station.council_id and station.region_id and school_obj and school_obj.region_id != station.region_id:
                out_of_scope.append(centre)
        if out_of_scope:
            raise PackagePreparationError(
                PackageErrorCode.SCOPE_VIOLATION,
                f"{len(out_of_scope)} school(s) are outside this station's "
                f"geographic scope. Remove them or update the station scope.",
                {"out_of_scope_schools": sorted(out_of_scope)[:20], "total": len(out_of_scope)},
            )

    paper_set = [PaperType(p) for p in papers]

    exam_subjects = await _in_scope_exam_subjects(db, exam.id, subject_codes)
    es_ids = [es.id for es in exam_subjects]
    if not es_ids:
        raise PackagePreparationError(
            PackageErrorCode.MISSING_APPLICABLE_PAPERS,
            f"No exam subjects found for codes: {subject_codes}",
        )

    await validate_and_assign_scopes(
        db,
        exam_id=exam.id,
        station_id=station.id,
        school_ids=school_ids,
        exam_subject_ids=es_ids,
        paper_types=paper_set,
        actor_id=actor_id,
    )

    seed = await build_package_seed(
        db, exam=exam, station=station,
        schools=schools, subject_codes=subject_codes, papers=papers,
    )

    last_pkg = (
        await db.execute(
            select(StationPackage).where(StationPackage.station_id == station.id)
            .order_by(StationPackage.package_version.desc())
        )
    ).scalars().first()
    version = (last_pkg.package_version if last_pkg else 0) + 1
    supersedes_id = last_pkg.package_id if last_pkg else None

    configuration_hash = sha256_prefixed(seed)
    package_id = "pkg_" + hashlib.sha256(
        f"{station.station_code}:{version}:{configuration_hash}".encode()
    ).hexdigest()[:24]

    credential_id, plaintext_secret, secret_hash = _generate_machine_credential()

    de_scopes = await _build_de_scopes(db, station.id)
    admin_entry = await _build_admin_entry(db, station.id)

    manifest_model = StationPackageManifest(
        package_id=package_id,
        package_version=version,
        supersedes_package_id=supersedes_id,
        rules_version=RULES_VERSION,
        software_min_version=SOFTWARE_MIN_VERSION,
        station_code=station.station_code,
        exam_id=str(exam.id),
        exam_code=getattr(exam, "exam_code", "") or "",
        exam_name=getattr(exam, "name", "") or "",
        configuration_hash=configuration_hash,
        issued_at=datetime.now(timezone.utc),
        scope=PackageScope(schools=schools, subjects=subject_codes, papers=papers),
        central_base_url=central_url,
        machine_credential=MachineCredentialMeta(
            credential_id=credential_id,
            algorithm="argon2id",
        ),
        signing=SigningMeta(algorithm="ed25519"),
        station_admin=admin_entry,
        data_enterers=de_scopes,
    )
    manifest = manifest_model.model_dump(mode="json")

    # Ensure the signing key path env var is populated when configured.
    import os as _os
    if settings.package_signing_private_key_path:
        _os.environ.setdefault(
            "PACKAGE_SIGNING_PRIVATE_KEY_PATH",
            settings.package_signing_private_key_path,
        )
    signature = sign_package_manifest(manifest)

    machine_credential_payload = MachineCredentialPayload(
        credential_id=credential_id,
        package_id=package_id,
        station_code=station.station_code,
        secret=plaintext_secret,
        central_base_url=central_url,
    ).model_dump(mode="json")

    package_bundle = {
        "manifest": manifest,
        "seed": seed,
        "signature": signature,
        "contract_version": CONTRACT_VERSION,
        "machine_credential": machine_credential_payload,
    }

    pkg = StationPackage(
        package_id=package_id,
        station_id=station.id,
        exam_id=exam.id,
        package_version=version,
        rules_version=RULES_VERSION,
        software_min_version=SOFTWARE_MIN_VERSION,
        configuration_hash=configuration_hash,
        assigned_scope={"schools": schools, "subjects": subject_codes, "papers": papers},
        manifest=package_bundle,
        issued_at=datetime.now(timezone.utc),
        supersedes_package_id=supersedes_id,
    )
    db.add(pkg)
    await db.flush()

    db.add(StationMachineCredential(
        credential_id=credential_id,
        package_id=package_id,
        station_id=station.id,
        secret_hash=secret_hash,
        algorithm="argon2id",
        status="ACTIVE",
        issued_at=datetime.now(timezone.utc),
    ))
    await db.flush()

    return pkg, plaintext_secret
