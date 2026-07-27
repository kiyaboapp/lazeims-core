"""Enums used only inside Central (DB + API), not part of the Central/Station
cross-boundary contract. Contract enums live in ``lazeims_common.enums``.
"""

from __future__ import annotations

from enum import Enum


class RoleScopeLevel(str, Enum):
    GLOBAL = "GLOBAL"
    REGION = "REGION"
    COUNCIL = "COUNCIL"
    SCHOOL = "SCHOOL"


class StandingRoleName(str, Enum):
    """Permanent, exam-independent roles. EXAM_ADMIN / DATA_ENTERER are
    deliberately NOT here — those are exam-scoped assignments."""

    SUPER_ADMIN = "SUPER_ADMIN"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"
    REO = "REO"
    RAO = "RAO"
    DEO = "DEO"
    DAO = "DAO"
    REGION_ITS = "REGION_ITS"
    COUNCIL_ITS = "COUNCIL_ITS"
    SCHOOL_HEAD = "SCHOOL_HEAD"


class ExamRoleName(str, Enum):
    """Exam-scoped roles, assigned per exam, expiring with it. Resolved
    server-side from ExamRoleAssignment — never trusted from a request."""

    EXAM_ADMIN = "EXAM_ADMIN"
    REGIONAL_EXAM_REP = "REGIONAL_EXAM_REP"
    COUNCIL_EXAM_REP = "COUNCIL_EXAM_REP"
    DATA_ENTERER = "DATA_ENTERER"
