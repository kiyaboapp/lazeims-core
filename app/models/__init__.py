"""SQLAlchemy models. Importing this package registers every table on
``Base.metadata`` so Alembic autogenerate sees them all.
"""

from __future__ import annotations

from .assignments import (
    DataEntererScope,
    ExamRoleAssignment,
    FinalizedScope,
    ScopeRevision,
    ScopeWriteAssignment,
    Station,
    StationCredential,
)
from .exam import (
    Exam,
    ExamSchool,
    ExamStudent,
    ExamStudentSubject,
    ExamSubject,
)
from .excel import (
    ExcelImportBatch,
    ExcelImportRow,
    ExcelWorkbook,
)
from .collection import (
    CollectionExportFile,
    CollectionReadinessRun,
    CollectionSnapshot,
)
from .cross_cutting import AuditLog, Notification
from .marks import (
    Attendance,
    ExamIncident,
    ItemMark,
    MarkBatchReceipt,
    TotalMark,
)
from .registry import (
    Board,
    Council,
    ExamLevel,
    Region,
    Role,
    School,
    Session,
    Subject,
    Topic,
    User,
    Ward,
)
from .scoring import (
    ExamConfigurationVersion,
    Question,
    QuestionGroup,
    QuestionTopic,
)
from .station import (
    StationPackage,
    StationReconciliation,
    StationSyncLog,
    SyncEventReceipt,
)

__all__ = [
    # registry
    "Board", "Council", "ExamLevel", "Region", "Role", "School", "Session",
    "Subject", "Topic", "User", "Ward",
    # exam core
    "Exam", "ExamSchool", "ExamSubject", "ExamStudent", "ExamStudentSubject",
    # attendance / marks
    "Attendance", "ExamIncident", "TotalMark", "ItemMark", "MarkBatchReceipt",
    # excel
    "ExcelWorkbook", "ExcelImportBatch", "ExcelImportRow",
    # collection closeout / export
    "CollectionReadinessRun", "CollectionSnapshot", "CollectionExportFile",
    # cross-cutting
    "AuditLog", "Notification",
    # scoring
    "QuestionGroup", "Question", "QuestionTopic", "ExamConfigurationVersion",
    # assignments / scope
    "Station", "ExamRoleAssignment", "DataEntererScope", "ScopeWriteAssignment",
    "StationCredential", "FinalizedScope", "ScopeRevision",
    # station domain
    "StationPackage", "SyncEventReceipt", "StationSyncLog", "StationReconciliation",
]
