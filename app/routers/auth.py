"""Human authentication endpoints (session-cookie based)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..db import get_session
from ..deps import current_user
from ..models.registry import User
from ..schemas import LoginIn, MeOut
from ..security import verify_secret
from ..services.sessions import create_session, revoke_session

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/login", response_model=MeOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> MeOut:
    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.username == payload.username)
    )
    user = result.scalar_one_or_none()

    # Uniform failure to avoid leaking which usernames exist.
    if user is None or not user.is_active or not verify_secret(user.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    row = await create_session(
        db,
        user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, row.id)
    return MeOut.from_user(user, row.csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> Response:
    settings = get_settings()
    session_id = request.cookies.get(settings.session_cookie_name, "")
    await revoke_session(db, session_id)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut)
async def me(
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> MeOut:
    # Re-read the CSRF token bound to this session for the client to echo back.
    from ..services.sessions import resolve_session

    session_id = request.cookies.get(get_settings().session_cookie_name, "")
    row = await resolve_session(db, session_id)
    csrf = row.csrf_token if row else ""
    return MeOut.from_user(user, csrf)
