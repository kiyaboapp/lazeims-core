"""Shared FastAPI dependencies: authentication, CSRF, and authorization.

This is the single authorization layer. Every route declares which of these it
uses — a route with no explicit authorization dependency must fail review.

Two entirely separate auth schemes:
  * ``current_user``       — human browser session (signed cookie -> DB session).
  * ``require_station_auth`` — machine station credential (``X-Station-Key``).
The two never overlap: a station key cannot reach a human route and vice versa.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .enums import StandingRoleName
from .models.registry import User
from .services.sessions import get_user_for_session, resolve_session

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the authenticated human user from the session cookie.

    Raises 401 if there is no valid, live session. The user's role/scope are
    always loaded from the database here — never trusted from the request.
    """
    settings = get_settings()
    session_id = request.cookies.get(settings.session_cookie_name, "")
    row = await resolve_session(db, session_id)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    # CSRF: enforce a matching header on state-changing requests.
    if request.method not in _SAFE_METHODS:
        header_token = request.headers.get("x-csrf-token", "")
        if not header_token or header_token != row.csrf_token:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")

    user = await get_user_for_session(db, row)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session user is inactive or missing")
    # Ensure the standing role is loaded for downstream authorization.
    await db.refresh(user, attribute_names=["role"])
    return user


def require_role(*allowed: StandingRoleName | str) -> Callable[..., Awaitable[User]]:
    """Dependency factory: allow only the given standing roles.

    Implements one row of the permissions matrix. Usage::

        @router.post(..., dependencies=[Depends(require_role("SUPER_ADMIN"))])
    """
    allowed_names = {a.value if isinstance(a, StandingRoleName) else str(a) for a in allowed}

    async def _check(user: User = Depends(current_user)) -> User:
        if user.role.name.value not in allowed_names:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted for this role")
        return user

    return _check


def require_geography_scope(
    *,
    region_param: str = "region_id",
    council_param: str = "council_id",
) -> Callable[..., Awaitable[User]]:
    """Dependency factory: constrain access to the caller's geography.

    * GLOBAL-scoped roles (SUPER_ADMIN, SYSTEM_ADMIN) may access anything.
    * REGION-scoped roles may only access their own region.
    * COUNCIL-scoped roles may only access their own council.
    * SCHOOL-scoped roles may only access within their school's council/region.

    The target region/council is read from path parameters. A request that
    targets a geography outside the caller's scope gets 403.
    """
    global_roles = {StandingRoleName.SUPER_ADMIN.value, StandingRoleName.SYSTEM_ADMIN.value}

    async def _check(request: Request, user: User = Depends(current_user)) -> User:
        if user.role.name.value in global_roles:
            return user

        target_region = request.path_params.get(region_param)
        target_council = request.path_params.get(council_param)

        def _as_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        target_region = _as_int(target_region)
        target_council = _as_int(target_council)

        if target_region is not None and user.region_id is not None:
            if target_region != user.region_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Outside your geographic scope")
        if target_council is not None and user.council_id is not None:
            if target_council != user.council_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Outside your geographic scope")
        return user

    return _check


async def require_station_auth(
    x_station_key: str | None = Header(default=None, alias="X-Station-Key"),
) -> str:
    """Machine authentication for station sync routes.

    NOTE: full station-key resolution against ``Station.sync_key_hash`` is built
    in the station-domain phase. This dependency establishes the separate scheme
    and rejects missing credentials now, so no human route ever shares it.
    """
    if not x_station_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing station credential")
    return x_station_key
