"""Idempotent seed helpers: standing roles and a bootstrap super admin.

Run as a script::

    python -m app.seed                # seed roles only
    python -m app.seed --admin USER PW  # also create/reset a SUPER_ADMIN
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from lazeims_common.enums import Sex

from .db import get_sessionmaker
from .enums import RoleScopeLevel, StandingRoleName
from .models.registry import Role, User
from .security import hash_secret

# Standing role -> scope level (Guide §2.1).
_ROLE_SCOPES: dict[StandingRoleName, RoleScopeLevel] = {
    StandingRoleName.SUPER_ADMIN: RoleScopeLevel.GLOBAL,
    StandingRoleName.SYSTEM_ADMIN: RoleScopeLevel.GLOBAL,
    StandingRoleName.REO: RoleScopeLevel.REGION,
    StandingRoleName.RAO: RoleScopeLevel.REGION,
    StandingRoleName.DEO: RoleScopeLevel.COUNCIL,
    StandingRoleName.DAO: RoleScopeLevel.COUNCIL,
    StandingRoleName.REGION_ITS: RoleScopeLevel.REGION,
    StandingRoleName.COUNCIL_ITS: RoleScopeLevel.COUNCIL,
    StandingRoleName.SCHOOL_HEAD: RoleScopeLevel.SCHOOL,
}


async def seed_roles(db) -> int:
    existing = {r.name for r in (await db.execute(select(Role))).scalars().all()}
    added = 0
    for name, scope in _ROLE_SCOPES.items():
        if name not in existing:
            db.add(Role(name=name, scope_level=scope))
            added += 1
    await db.flush()
    return added


async def ensure_super_admin(db, username: str, password: str) -> User:
    role = (
        await db.execute(select(Role).where(Role.name == StandingRoleName.SUPER_ADMIN))
    ).scalar_one()
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None:
        user = User(
            first_name="Super",
            surname="Admin",
            sex=Sex.M,
            username=username,
            role_id=role.id,
            password_hash=hash_secret(password),
        )
        db.add(user)
    else:
        user.password_hash = hash_secret(password)
        user.is_active = True
    await db.flush()
    return user


async def _main(argv: list[str]) -> None:
    async with get_sessionmaker()() as db:
        added = await seed_roles(db)
        print(f"seed_roles: {added} role(s) added")
        if len(argv) >= 3 and argv[0] == "--admin":
            user = await ensure_super_admin(db, argv[1], argv[2])
            print(f"super admin ready: {user.username}")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
