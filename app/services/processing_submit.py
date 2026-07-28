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
            "first_name": fn,
            "middle_name": mn,
            "surname": sn,
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

    return {
        "schools": schools,
        "subjects": subjects,
        "students": students,
        "marks": list(marks.values()),
    }
