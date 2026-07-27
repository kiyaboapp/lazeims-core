"""Exam-scoped authorization.

A user's exam role is ALWAYS resolved server-side from ``ExamRoleAssignment`` —
never accepted from the request. SUPER_ADMIN / SYSTEM_ADMIN standing roles are
treated as implicit exam administrators for any exam.
"""

from __future__ import annotations
import uuid

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .deps import current_user
from .enums import ExamRoleName, StandingRoleName
from .models.assignments import ExamRoleAssignment
from .models.registry import User

_GLOBAL_ADMIN = {StandingRoleName.SUPER_ADMIN.value, StandingRoleName.SYSTEM_ADMIN.value}


async def get_exam_roles(db: AsyncSession, exam_id: uuid.UUID, user: User) -> set[str]:
    """Return the set of exam-role names this user holds for the exam."""
    rows = (
        await db.execute(
            select(ExamRoleAssignment.role).where(
                ExamRoleAssignment.exam_id == exam_id,
                ExamRoleAssignment.user_id == user.id,
            )
        )
    ).scalars().all()
    return {r.value if hasattr(r, "value") else str(r) for r in rows}


def require_exam_role(*allowed: ExamRoleName | str) -> Callable[..., Awaitable[User]]:
    """Dependency factory: require one of the given exam roles for the exam in
    the path (``{exam_id}``). Global admins always pass.
    """
    allowed_names = {a.value if isinstance(a, ExamRoleName) else str(a) for a in allowed}

    async def _check(
        exam_id: uuid.UUID = Path(...),
        db: AsyncSession = Depends(get_session),
        user: User = Depends(current_user),
    ) -> User:
        if user.role.name.value in _GLOBAL_ADMIN:
            return user
        roles = await get_exam_roles(db, exam_id, user)
        if not (roles & allowed_names):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Not permitted: required exam role missing",
            )
        return user

    return _check


def require_exam_admin() -> Callable[..., Awaitable[User]]:
    """EXAM_ADMIN (or global admin) — owns exam configuration & phase."""
    return require_exam_role(ExamRoleName.EXAM_ADMIN)
