"""Scoring-config validation at save + immutable configuration-version sealing.

Validation reuses ``lazeims_common`` so the rules match the Station exactly:
    * per-question topic weights sum to 1.0;
    * group pick_count within member bounds, no duplicate questions, valid group refs.

Sealing builds a canonical snapshot of the whole exam's scoring config and hashes
it with ``lazeims_common.hashing`` — the same canonical form the Station and any
downstream consumer use, so the hash is reproducible everywhere.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lazeims_common.enums import PaperType, RejectionCode
from lazeims_common.errors import ValidationError
from lazeims_common.hashing import sha256_prefixed
from lazeims_common.validation.config import (
    PaperConfig,
    QuestionConfig,
    QuestionGroupConfig,
    TopicWeight,
)
from lazeims_common.validation.scoring import validate_paper_config

from ..models.exam import Exam, ExamSubject
from ..models.scoring import (
    ExamConfigurationVersion,
    Question,
    QuestionGroup,
    QuestionTopic,
)


async def _build_paper_configs(db: AsyncSession, exam_subject: ExamSubject) -> list[PaperConfig]:
    groups = (
        await db.execute(select(QuestionGroup).where(QuestionGroup.exam_subject_id == exam_subject.id))
    ).scalars().all()
    questions = (
        await db.execute(select(Question).where(Question.exam_subject_id == exam_subject.id))
    ).scalars().all()

    group_by_id = {g.id: g for g in groups}
    # topics per question
    q_ids = [q.id for q in questions]
    topics_by_q: dict[int, list[TopicWeight]] = {}
    if q_ids:
        qt_rows = (
            await db.execute(select(QuestionTopic).where(QuestionTopic.question_id.in_(q_ids)))
        ).scalars().all()
        for qt in qt_rows:
            topics_by_q.setdefault(qt.question_id, []).append(
                TopicWeight(str(qt.topic_id), Decimal(str(qt.weight)))
            )

    papers: dict[PaperType, list] = {}
    group_papers: dict[PaperType, list] = {}
    for g in groups:
        group_papers.setdefault(g.paper_type, []).append(
            QuestionGroupConfig(code=g.code, pick_count=g.pick_count)
        )
    for q in questions:
        group_code = group_by_id[q.group_id].code if q.group_id else None
        papers.setdefault(q.paper_type, []).append(
            QuestionConfig(
                question_number=q.question_number,
                max_marks=Decimal(str(q.max_marks)),
                group_code=group_code,
                topics=tuple(topics_by_q.get(q.id, [])),
            )
        )

    paper_max = {
        PaperType.THEORY1: exam_subject.total_marks_theory1,
        PaperType.THEORY2: exam_subject.total_marks_theory2,
        PaperType.PRACTICAL: exam_subject.total_marks_practical,
    }
    configs: list[PaperConfig] = []
    for paper_type, qs in papers.items():
        configs.append(PaperConfig(
            paper_type=paper_type,
            paper_max=Decimal(str(paper_max.get(paper_type, 0))),
            questions=tuple(qs),
            groups=tuple(group_papers.get(paper_type, [])),
        ))
    return configs


async def validate_subject_scoring(db: AsyncSession, exam_subject: ExamSubject) -> None:
    """Validate one subject's scoring config (raises ValidationError on failure)."""
    for paper in await _build_paper_configs(db, exam_subject):
        validate_paper_config(paper)


def _canonical_snapshot_from_configs(exam_id: str, configs_by_subject: dict[str, list[PaperConfig]]) -> dict:
    subjects = []
    for subject_code, configs in sorted(configs_by_subject.items()):
        papers = []
        for cfg in sorted(configs, key=lambda c: c.paper_type.value):
            papers.append({
                "paper_type": cfg.paper_type.value,
                "paper_max": str(cfg.paper_max),
                "questions": sorted(
                    [
                        {
                            "number": q.question_number,
                            "max_marks": str(q.max_marks),
                            "group": q.group_code,
                            "topics": sorted(
                                [{"code": t.topic_code, "weight": str(t.weight)} for t in q.topics],
                                key=lambda t: t["code"],
                            ),
                        }
                        for q in cfg.questions
                    ],
                    key=lambda q: q["number"],
                ),
                "groups": sorted(
                    [{"code": g.code, "pick_count": g.pick_count} for g in cfg.groups],
                    key=lambda g: g["code"],
                ),
            })
        subjects.append({"subject_code": subject_code, "papers": papers})
    return {"exam_id": exam_id, "subjects": subjects}


async def seal_configuration_version(db: AsyncSession, exam: Exam, sealed_by: int | None) -> ExamConfigurationVersion:
    """Validate all subjects, build a canonical snapshot, hash it, and persist a
    new immutable ExamConfigurationVersion. Updates ``exam.current_configuration_version``.
    """
    exam_subjects = (
        await db.execute(select(ExamSubject).where(ExamSubject.exam_id == exam.id))
    ).scalars().all()
    if not exam_subjects:
        raise ValidationError(
            RejectionCode.INCOMPLETE_QUESTION_SET,
            "Cannot seal configuration: no subjects offered.",
        )

    # subject code lookup
    from ..models.registry import Subject
    configs_by_subject: dict[str, list[PaperConfig]] = {}
    for es in exam_subjects:
        subject = await db.get(Subject, es.subject_id)
        configs = await _build_paper_configs(db, es)
        for cfg in configs:
            validate_paper_config(cfg)
        configs_by_subject[subject.code] = configs

    snapshot = _canonical_snapshot_from_configs(str(exam.id), configs_by_subject)
    config_hash = sha256_prefixed(snapshot)

    next_version = (exam.current_configuration_version or 0) + 1
    version = ExamConfigurationVersion(
        exam_id=exam.id,
        version=next_version,
        configuration_hash=config_hash,
        snapshot=snapshot,
        sealed_by=sealed_by,
    )
    db.add(version)
    exam.current_configuration_version = next_version
    await db.flush()
    return version
