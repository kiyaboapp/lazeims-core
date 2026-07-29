"""FastAPI application entrypoint.

Mounts the versioned API under ``/api/v1``, exposes ``/health`` (liveness) and
``/readiness`` (DB connectivity), and installs a single handler that turns
``lazeims_common.ValidationError`` into the canonical error envelope.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from lazeims_common.errors import ValidationError

from .config import get_settings
from .db import dispose_engine, get_engine
from .routers import auth as auth_router
from .routers import closeout as closeout_router
from .routers import excel as excel_router
from .routers import exams as exams_router
from .routers import integration as integration_router
from .routers import marks as marks_router
from .routers import notifications as notifications_router
from .routers import registration as registration_router
from .routers import routers_registry as registry_router
from .routers import station_sync as station_sync_router
from .routers import stations as stations_router

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Touch settings early so a missing env var fails fast at startup.
    get_settings()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ValidationError)
    async def _handle_validation_error(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": exc.code.value,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": request.headers.get("x-request-id"),
                }
            },
        )

    @app.get("/health", tags=["ops"])
    async def health():
        return {"status": "ok"}

    @app.get("/readiness", tags=["ops"])
    async def readiness():
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ready", "database": "ok"}
        except Exception as exc:  # noqa: BLE001 - report readiness failure
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not-ready", "database": "error", "detail": str(exc)},
            )

    app.include_router(auth_router.router, prefix=API_PREFIX)
    app.include_router(registry_router.router, prefix=API_PREFIX)
    app.include_router(exams_router.router, prefix=API_PREFIX)
    # registration_router shares the /exams prefix; mounted after exams_router so
    # its literal sub-paths are matched without shadowing exam CRUD.
    app.include_router(registration_router.router, prefix=API_PREFIX)
    app.include_router(integration_router.router, prefix=API_PREFIX)
    app.include_router(marks_router.router, prefix=API_PREFIX)
    app.include_router(stations_router.router, prefix=API_PREFIX)
    app.include_router(station_sync_router.router, prefix=API_PREFIX)
    app.include_router(excel_router.router, prefix=API_PREFIX)
    app.include_router(closeout_router.router, prefix=API_PREFIX)
    app.include_router(closeout_router.snapshots_router, prefix=API_PREFIX)
    app.include_router(notifications_router.router, prefix=API_PREFIX)
    return app


app = create_app()
