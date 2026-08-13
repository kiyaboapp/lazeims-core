"""Build the ExaMetrics collection payload from Central's own data.

Central stores schools/subjects/students/marks in its own normalized tables. To
hand a collection to ExaMetrics for processing we flatten those into the natural-
key contract ExaMetrics expects (student_id + centre_number + subject_code +
paper marks). Supports both TOTAL_MARKS (TotalMark) and ITEM_LEVEL (ItemMark
summed per paper) filling modes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import PaperType

from ..models.exam import Exam, ExamSchool, ExamStudent, ExamStudentSubject, ExamSubject
from ..models.marks import Attendance, ItemMark, TotalMark
from ..models.registry import Council, Region, School, Subject, Ward
from ..models.scoring import Question

_PAPER_TO_FIELD = {
    PaperType.THEORY1: "theory_marks",
    PaperType.THEORY2: "theory_2_marks",
    PaperType.PRACTICAL: "practical_marks",
}
_PAPER_TO_SAT = {
    PaperType.THEORY1: "sat_theory",
    PaperType.THEORY2: "sat_theory_2",
    PaperType.PRACTICAL: "sat_practical",
}


def _pv(paper: Any) -> PaperType:
    return paper if isinstance(paper, PaperType) else PaperType(paper)


async def build_collection_payload(db: AsyncSession, exam: Exam) -> dict:
    exam_id = exam.id

    # ── Schools ──
    school_rows = (
        await db.execute(
            select(
                School.centre_number, School.name, School.school_type,
                Region.name, Council.name, Ward.name,
            )
            .join(ExamSchool, ExamSchool.school_id == School.id)
            .outerjoin(Region, Region.id == School.region_id)
            .outerjoin(Council, Council.id == School.council_id)
            .outerjoin(Ward, Ward.id == School.ward_id)
            .where(ExamSchool.exam_id == exam_id)
        )
    ).all()
    schools = [
        {
            "centre_number": cn,
            "school_name": name,
            "school_type": st.value if hasattr(st, "value") else st,
            "region_name": region,
            "council_name": council,
            "ward_name": ward,
        }
        for cn, name, st, region, council, ward in school_rows
    ]

    # ── Subjects ──
    subject_rows = (
        await db.execute(
            select(
                Subject.code, Subject.name,
                ExamSubject.has_theory2, ExamSubject.has_practical,
                ExamSubject.total_marks_theory1, ExamSubject.total_marks_theory2,
                ExamSubject.total_marks_practical,
                Subject.is_primary, Subject.is_olevel, Subject.is_alevel,
            )
            .join(ExamSubject, ExamSubject.subject_id == Subject.id)
            .where(ExamSubject.exam_id == exam_id)
        )
    ).all()
    subjects = [
        {
            "subject_code": code,
            "subject_name": name,
            "subject_short": code,
            "has_theory_2": bool(t2),
            "has_practical": bool(prac),
            "theory_max": float(m1) if m1 is not None else None,
            "theory_2_max": float(m2) if m2 else None,
            "practical_max": float(mp) if mp else None,
            "is_primary": bool(is_primary),
            "is_olevel": bool(is_olevel),
            "is_alevel": bool(is_alevel),
        }
        for code, name, t2, prac, m1, m2, mp, is_primary, is_olevel, is_alevel in subject_rows
    ]

    # ── Students ──
    student_rows = (
        await db.execute(
            select(
                ExamStudent.student_id, School.centre_number,
                ExamStudent.first_name, ExamStudent.middle_name, ExamStudent.surname,
                ExamStudent.sex,
            )
            .join(School, School.id == ExamStudent.school_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).all()
    students = [
        {
            "student_id": sid,
            "centre_number": cn,
            "first_name": (fn or "").upper(),
            "middle_name": mn.upper() if mn else None,
            "surname": (sn or "").upper(),
            "sex": sex.value if hasattr(sex, "value") else sex,
        }
        for sid, cn, fn, mn, sn, sex in student_rows
    ]

    # ── Marks: one row per (student, subject) registration ──
    # Base map keyed by exam_student_subject id.
    base_rows = (
        await db.execute(
            select(
                ExamStudentSubject.id, ExamStudent.student_id, School.centre_number, Subject.code,
            )
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .join(School, School.id == ExamStudent.school_id)
            .join(ExamSubject, ExamSubject.id == ExamStudentSubject.exam_subject_id)
            .join(Subject, Subject.id == ExamSubject.subject_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).all()
    marks: dict[int, dict] = {
        ess_id: {"student_id": sid, "centre_number": cn, "subject_code": code}
        for ess_id, sid, cn, code in base_rows
    }

    # Total marks per paper.
    tm_rows = (
        await db.execute(
            select(TotalMark.exam_student_subject_id, TotalMark.paper_type, TotalMark.total_marks_obtained)
            .join(ExamStudentSubject, ExamStudentSubject.id == TotalMark.exam_student_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).all()
    for ess_id, paper, value in tm_rows:
        entry = marks.get(ess_id)
        if entry is None:
            continue
        field = _PAPER_TO_FIELD.get(_pv(paper))
        if field:
            entry[field] = float(value) if value is not None else None

    # Item marks summed per paper (fills papers not covered by TotalMark).
    im_rows = (
        await db.execute(
            select(ItemMark.exam_student_subject_id, Question.paper_type, ItemMark.marks_obtained)
            .join(Question, Question.id == ItemMark.question_id)
            .join(ExamStudentSubject, ExamStudentSubject.id == ItemMark.exam_student_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).all()
    for ess_id, paper, value in im_rows:
        entry = marks.get(ess_id)
        if entry is None:
            continue
        field = _PAPER_TO_FIELD.get(_pv(paper))
        if field and value is not None:
            entry[field] = float(entry.get(field) or 0) + float(value)

    # Attendance → sat_* flags.
    att_rows = (
        await db.execute(
            select(Attendance.exam_student_subject_id, Attendance.paper_type, Attendance.is_present)
            .join(ExamStudentSubject, ExamStudentSubject.id == Attendance.exam_student_subject_id)
            .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
            .where(ExamStudent.exam_id == exam_id)
        )
    ).all()
    for ess_id, paper, present in att_rows:
        entry = marks.get(ess_id)
        if entry is None:
            continue
        sat_field = _PAPER_TO_SAT.get(_pv(paper))
        if sat_field:
            entry[sat_field] = bool(present)

    # Filter out marks entries with no actual data (bare registrations)
    # AND marks that ExaMetrics will reject (sat=True but no corresponding marks value).
    _MARK_FIELDS = frozenset(("theory_marks", "theory_2_marks", "practical_marks"))
    _SAT_FIELDS = frozenset(("sat_theory", "sat_theory_2", "sat_practical"))
    _SAT_TO_MARK = {
        "sat_theory": "theory_marks",
        "sat_theory_2": "theory_2_marks",
        "sat_practical": "practical_marks",
    }

    def _is_valid_mark(m: dict) -> bool:
        has_any_mark = any(m.get(k) is not None for k in _MARK_FIELDS)
        has_any_sat = any(m.get(k) is not None for k in _SAT_FIELDS)
        if not has_any_mark and not has_any_sat:
            return False  # empty registration row
        # Reject if sat=True but no corresponding marks value
        for sat_field, mark_field in _SAT_TO_MARK.items():
            if m.get(sat_field) is True and m.get(mark_field) is None:
                return False
        return True

    marks_with_data = [m for m in marks.values() if _is_valid_mark(m)]

    return {
        "schools": schools,
        "subjects": subjects,
        "students": students,
        "marks": marks_with_data,
    }


async def iter_school_centre_numbers(
    db: AsyncSession, exam: Exam, *, skip_empty: bool = True
) -> list[tuple[str, str]]:
    """Return (centre_number, school_name) for schools to push.

    When skip_empty=True, only schools with at least one marks row are included.
    Lightweight query — no student/marks data loaded.
    """
    exam_id = exam.id

    if skip_empty:
        from sqlalchemy import union_all
        tm_schools = (
            select(School.centre_number, School.name)
            .join(ExamStudent, ExamStudent.school_id == School.id)
            .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
            .join(TotalMark, TotalMark.exam_student_subject_id == ExamStudentSubject.id)
            .where(ExamStudent.exam_id == exam_id)
            .group_by(School.centre_number, School.name)
        )
        im_schools = (
            select(School.centre_number, School.name)
            .join(ExamStudent, ExamStudent.school_id == School.id)
            .join(ExamStudentSubject, ExamStudentSubject.exam_student_id == ExamStudent.id)
            .join(ItemMark, ItemMark.exam_student_subject_id == ExamStudentSubject.id)
            .where(ExamStudent.exam_id == exam_id)
            .group_by(School.centre_number, School.name)
        )
        combined = union_all(tm_schools, im_schools).subquery()
        rows = (await db.execute(
            select(combined.c.centre_number, combined.c.name)
            .distinct()
            .order_by(combined.c.centre_number)
        )).all()
    else:
        rows = (await db.execute(
            select(School.centre_number, School.name)
            .join(ExamSchool, ExamSchool.school_id == School.id)
            .where(ExamSchool.exam_id == exam_id)
            .order_by(School.centre_number)
        )).all()

    return [(cn, name) for cn, name in rows]


async def build_single_school_payload(
    db: AsyncSession, exam: Exam, centre_number: str
) -> dict:
    """Build the payload for exactly one school. Memory-efficient: queries only
    this school's students, marks, and attendance.
    """
    exam_id = exam.id

    # School metadata
    school_row = (await db.execute(
        select(
            School.centre_number, School.name, School.school_type,
            Region.name, Council.name, Ward.name,
        )
        .join(ExamSchool, ExamSchool.school_id == School.id)
        .outerjoin(Region, Region.id == School.region_id)
        .outerjoin(Council, Council.id == School.council_id)
        .outerjoin(Ward, Ward.id == School.ward_id)
        .where(ExamSchool.exam_id == exam_id)
        .where(School.centre_number == centre_number)
    )).first()

    if not school_row:
        return {"schools": [], "subjects": [], "students": [], "marks": []}

    cn, name, st, region, council, ward = school_row
    school = {
        "centre_number": cn,
        "school_name": name,
        "school_type": st.value if hasattr(st, "value") else st,
        "region_name": region,
        "council_name": council,
        "ward_name": ward,
    }

    # Students for this school
    student_rows = (await db.execute(
        select(
            ExamStudent.student_id,
            ExamStudent.first_name, ExamStudent.middle_name, ExamStudent.surname,
            ExamStudent.sex,
        )
        .join(School, School.id == ExamStudent.school_id)
        .where(ExamStudent.exam_id == exam_id)
        .where(School.centre_number == centre_number)
    )).all()

    students = [
        {
            "student_id": sid,
            "centre_number": centre_number,
            "first_name": (fn or "").upper(),
            "middle_name": mn.upper() if mn else None,
            "surname": (sn or "").upper(),
            "sex": sex.value if hasattr(sex, "value") else sex,
        }
        for sid, fn, mn, sn, sex in student_rows
    ]

    # Marks base (exam_student_subject rows for this school)
    base_rows = (await db.execute(
        select(
            ExamStudentSubject.id, ExamStudent.student_id, Subject.code,
        )
        .join(ExamStudent, ExamStudent.id == ExamStudentSubject.exam_student_id)
        .join(School, School.id == ExamStudent.school_id)
        .join(ExamSubject, ExamSubject.id == ExamStudentSubject.exam_subject_id)
        .join(Subject, Subject.id == ExamSubject.subject_id)
        .where(ExamStudent.exam_id == exam_id)
        .where(School.centre_number == centre_number)
    )).all()

    marks: dict[int, dict] = {
        ess_id: {"student_id": sid, "centre_number": centre_number, "subject_code": code}
        for ess_id, sid, code in base_rows
    }

    if not marks:
        return {"schools": [school], "subjects": [], "students": students, "marks": []}

    ess_ids = list(marks.keys())

    # Total marks
    tm_rows = (await db.execute(
        select(TotalMark.exam_student_subject_id, TotalMark.paper_type, TotalMark.total_marks_obtained)
        .where(TotalMark.exam_student_subject_id.in_(ess_ids))
    )).all()
    for ess_id, paper, value in tm_rows:
        entry = marks.get(ess_id)
        if entry is None:
            continue
        field = _PAPER_TO_FIELD.get(_pv(paper))
        if field:
            entry[field] = float(value) if value is not None else None

    # Item marks summed per paper
    im_rows = (await db.execute(
        select(ItemMark.exam_student_subject_id, Question.paper_type, ItemMark.marks_obtained)
        .join(Question, Question.id == ItemMark.question_id)
        .where(ItemMark.exam_student_subject_id.in_(ess_ids))
    )).all()
    for ess_id, paper, value in im_rows:
        entry = marks.get(ess_id)
        if entry is None:
            continue
        field = _PAPER_TO_FIELD.get(_pv(paper))
        if field and value is not None:
            entry[field] = float(entry.get(field) or 0) + float(value)

    # Attendance
    att_rows = (await db.execute(
        select(Attendance.exam_student_subject_id, Attendance.paper_type, Attendance.is_present)
        .where(Attendance.exam_student_subject_id.in_(ess_ids))
    )).all()
    for ess_id, paper, present in att_rows:
        entry = marks.get(ess_id)
        if entry is None:
            continue
        sat_field = _PAPER_TO_SAT.get(_pv(paper))
        if sat_field:
            entry[sat_field] = bool(present)

    # Filter marks
    _MARK_FIELDS = frozenset(("theory_marks", "theory_2_marks", "practical_marks"))
    _SAT_FIELDS = frozenset(("sat_theory", "sat_theory_2", "sat_practical"))
    _SAT_TO_MARK = {
        "sat_theory": "theory_marks",
        "sat_theory_2": "theory_2_marks",
        "sat_practical": "practical_marks",
    }

    def _is_valid_mark(m: dict) -> bool:
        has_any_mark = any(m.get(k) is not None for k in _MARK_FIELDS)
        has_any_sat = any(m.get(k) is not None for k in _SAT_FIELDS)
        if not has_any_mark and not has_any_sat:
            return False
        for sat_f, mark_f in _SAT_TO_MARK.items():
            if m.get(sat_f) is True and m.get(mark_f) is None:
                return False
        return True

    marks_with_data = [m for m in marks.values() if _is_valid_mark(m)]

    return {
        "schools": [school],
        "subjects": [],  # subjects attached separately by the caller for chunk 0
        "students": students,
        "marks": marks_with_data,
    }


async def get_exam_subjects(db: AsyncSession, exam: Exam) -> list[dict]:
    """Get the subjects list for the exam (lightweight, for chunk 0)."""
    subject_rows = (await db.execute(
        select(
            Subject.code, Subject.name,
            ExamSubject.has_theory2, ExamSubject.has_practical,
            ExamSubject.total_marks_theory1, ExamSubject.total_marks_theory2,
            ExamSubject.total_marks_practical,
            Subject.is_primary, Subject.is_olevel, Subject.is_alevel,
        )
        .join(ExamSubject, ExamSubject.subject_id == Subject.id)
        .where(ExamSubject.exam_id == exam.id)
    )).all()
    return [
        {
            "subject_code": code,
            "subject_name": name,
            "subject_short": code,
            "has_theory_2": bool(t2),
            "has_practical": bool(prac),
            "theory_max": float(m1) if m1 is not None else None,
            "theory_2_max": float(m2) if m2 else None,
            "practical_max": float(mp) if mp else None,
            "is_primary": bool(is_primary),
            "is_olevel": bool(is_olevel),
            "is_alevel": bool(is_alevel),
        }
        for code, name, t2, prac, m1, m2, mp, is_primary, is_olevel, is_alevel in subject_rows
    ]


async def build_school_payloads(
    db: AsyncSession, exam: Exam, *, skip_empty: bool = True
) -> list[dict]:
    """Build per-school chunk payloads for chunked upload.
    
    Each chunk contains one school + its students + its marks.
    Chunk 0 also carries the full subjects list. Subsequent chunks have subjects=[].
    
    When skip_empty=True, schools with no students/marks are excluded (for daily sync).
    When skip_empty=False, all enrolled schools are included (for submit-for-processing).
    """
    full_payload = await build_collection_payload(db, exam)
    
    schools = full_payload["schools"]
    subjects = full_payload["subjects"]
    all_students = full_payload["students"]
    all_marks = full_payload["marks"]
    
    # Group students and marks by centre_number
    from collections import defaultdict
    students_by_school: dict[str, list] = defaultdict(list)
    marks_by_school: dict[str, list] = defaultdict(list)
    
    for s in all_students:
        students_by_school[s["centre_number"]].append(s)
    for m in all_marks:
        marks_by_school[m["centre_number"]].append(m)
    
    payloads = []
    # When skip_empty, only include subjects that appear in the marks we're pushing
    if skip_empty:
        used_codes = {m["subject_code"] for marks_list in marks_by_school.values() for m in marks_list}
        subjects = [s for s in subjects if s["subject_code"] in used_codes]

    for i, school in enumerate(schools):
        cn = school["centre_number"]
        school_students = students_by_school.get(cn, [])
        school_marks = marks_by_school.get(cn, [])
        
        if skip_empty and not school_marks:
            continue
        
        payloads.append({
            "schools": [school],
            "subjects": subjects if len(payloads) == 0 else [],
            "students": school_students,
            "marks": school_marks,
        })
    
    return payloads
