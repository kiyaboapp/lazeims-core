"""Factory seeder for fake CSEE (Form IV) candidates.

Seeds realistic fake students into the *current* exam's enrolled schools, with
subject combinations that follow real O-Level rules:

  Compulsory (every candidate):
      011 Civics, 012 History, 013 Geography, 021 Kiswahili,
      022 English Language, 033 Biology, 041 Basic Mathematics

  Science track (~30% of candidates):
      032 Chemistry + 031 Physics                (the majority)
      032 Chemistry only                         (~10% of science students)
      -> Physics is NEVER taken without Chemistry.

  Business track (a share of the non-science candidates):
      062 Book-keeping + 061 Commerce

  Free electives (any candidate, independent probabilities):
      024 Literature in English
      036 Information and Computer Studies

Idempotent per exam: a candidate_id already present in the exam is skipped.

Usage:
    python seed_fake_students.py                 # 10 enrolled schools
    python seed_fake_students.py --schools 25    # first 25 enrolled schools
    python seed_fake_students.py --all           # every enrolled school
    python seed_fake_students.py --min 30 --max 60
"""

from __future__ import annotations

import argparse
import asyncio
import random

from faker import Faker
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import Sex

from app.db import dispose_engine, get_sessionmaker
from app.models.exam import (
    Exam,
    ExamSchool,
    ExamStudent,
    ExamStudentSubject,
    ExamSubject,
)
from app.models.registry import School, Subject

# ── subject-code sets (CSEE natural codes) ───────────────────────────────────
COMPULSORY = ["011", "012", "013", "021", "022", "033", "041"]
PHYSICS = "031"
CHEMISTRY = "032"
BOOK_KEEPING = "062"
COMMERCE = "061"
LITERATURE = "024"
COMPUTER = "036"

# ── probabilities ────────────────────────────────────────────────────────────
P_SCIENCE = 0.30            # share of candidates on the science track
P_CHEM_ONLY = 0.10         # share of *science* students with Chemistry (no Physics)
P_BUSINESS = 0.55          # share of *non-science* students taking Book-keeping+Commerce
P_LITERATURE = 0.20        # free elective, any candidate
P_COMPUTER = 0.15          # free elective, any candidate


def pick_electives(rng: random.Random) -> list[str]:
    """Return the elective subject codes for one candidate, obeying the rules."""
    codes: list[str] = []
    if rng.random() < P_SCIENCE:
        # science track — Chemistry is the anchor; Physics rides on top of it.
        if rng.random() < P_CHEM_ONLY:
            codes.append(CHEMISTRY)                    # chemistry only
        else:
            codes.extend([CHEMISTRY, PHYSICS])         # chemistry + physics
    else:
        # non-science — some do a business combination.
        if rng.random() < P_BUSINESS:
            codes.extend([BOOK_KEEPING, COMMERCE])
    # free electives available to anyone
    if rng.random() < P_LITERATURE:
        codes.append(LITERATURE)
    if rng.random() < P_COMPUTER:
        codes.append(COMPUTER)
    return codes


async def resolve_subject_map(db: AsyncSession, exam_id) -> dict[str, int]:
    """code -> exam_subject_id for this exam."""
    rows = (
        await db.execute(
            select(Subject.code, ExamSubject.id)
            .join(ExamSubject, ExamSubject.subject_id == Subject.id)
            .where(ExamSubject.exam_id == exam_id)
        )
    ).all()
    return {code: es_id for code, es_id in rows}


async def main() -> None:
    ap = argparse.ArgumentParser(description="Seed fake CSEE candidates.")
    ap.add_argument("--schools", type=int, default=10, help="number of enrolled schools to seed")
    ap.add_argument("--all", action="store_true", help="seed every enrolled school")
    ap.add_argument("--min", type=int, default=25, help="min candidates per school")
    ap.add_argument("--max", type=int, default=45, help="max candidates per school")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    ap.add_argument("--exam-id", type=str, default=None, help="target exam id (defaults to the only exam)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    fake = Faker()
    Faker.seed(args.seed)
    Session = get_sessionmaker()

    async with Session() as db:
        # 1. target exam
        if args.exam_id:
            exam = await db.get(Exam, args.exam_id)
        else:
            exams = (await db.execute(select(Exam))).scalars().all()
            if len(exams) != 1:
                names = ", ".join(f"{e.name} ({e.id})" for e in exams)
                raise SystemExit(f"Expected exactly one exam; found {len(exams)}: {names}. Use --exam-id.")
            exam = exams[0]
        print(f"Target exam: {exam.name}  ({exam.id})  phase={exam.phase.value}")

        # 2. subject code -> exam_subject_id
        subject_map = await resolve_subject_map(db, exam.id)
        missing = [c for c in COMPULSORY + [PHYSICS, CHEMISTRY, BOOK_KEEPING, COMMERCE, LITERATURE, COMPUTER]
                   if c not in subject_map]
        if missing:
            raise SystemExit(f"Exam is missing required subject codes: {missing}")

        # 3. enrolled schools
        school_rows = (
            await db.execute(
                select(School)
                .join(ExamSchool, ExamSchool.school_id == School.id)
                .where(ExamSchool.exam_id == exam.id)
                .order_by(School.centre_number)
            )
        ).scalars().all()
        if not args.all:
            school_rows = school_rows[: args.schools]
        print(f"Seeding {len(school_rows)} enrolled school(s), {args.min}-{args.max} candidates each.")

        totals = {"students": 0, "registrations": 0, "science": 0, "chem_only": 0,
                  "business": 0, "male": 0, "female": 0}

        for school in school_rows:
            # existing candidate_ids for this school (idempotency)
            existing = set(
                (
                    await db.execute(
                        select(ExamStudent.student_id).where(
                            ExamStudent.exam_id == exam.id,
                            ExamStudent.school_id == school.id,
                        )
                    )
                ).scalars().all()
            )
            start_seq = len(existing) + 1
            count = rng.randint(args.min, args.max)

            for i in range(count):
                seq = start_seq + i
                candidate_id = f"{school.centre_number}/{seq:04d}"
                if candidate_id in existing:
                    continue

                is_male = rng.random() < 0.5
                sex = Sex.M if is_male else Sex.F
                if is_male:
                    first = fake.first_name_male()
                    middle = fake.first_name_male() if rng.random() < 0.7 else None
                else:
                    first = fake.first_name_female()
                    middle = fake.first_name_female() if rng.random() < 0.7 else None
                surname = fake.last_name()

                student = ExamStudent(
                    student_id=candidate_id, exam_id=exam.id, school_id=school.id,
                    first_name=first, middle_name=middle, surname=surname, sex=sex,
                )
                db.add(student)
                await db.flush()  # assign student.id

                electives = pick_electives(rng)
                codes = COMPULSORY + electives
                for code in codes:
                    db.add(ExamStudentSubject(
                        exam_student_id=student.id,
                        exam_subject_id=subject_map[code],
                    ))

                totals["students"] += 1
                totals["registrations"] += len(codes)
                totals["male" if is_male else "female"] += 1
                if CHEMISTRY in electives:
                    totals["science"] += 1
                    if PHYSICS not in electives:
                        totals["chem_only"] += 1
                if BOOK_KEEPING in electives:
                    totals["business"] += 1

            await db.flush()
            print(f"  {school.centre_number}  {school.name[:40]:40}  +{count} candidates")

        await db.commit()

    await dispose_engine()

    print("\n── Seed summary ─────────────────────────────")
    print(f"  Candidates:     {totals['students']}  (M={totals['male']}, F={totals['female']})")
    print(f"  Registrations:  {totals['registrations']}")
    print(f"  Science track:  {totals['science']}  (of which Chemistry-only: {totals['chem_only']})")
    print(f"  Business combo: {totals['business']}")


if __name__ == "__main__":
    asyncio.run(main())
