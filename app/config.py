"""Application configuration.

Reads environment variables (and a local ``.env``) via pydantic-settings and
**fails fast** at import/startup if a required variable is missing — never
silently defaults a security-relevant value in a non-development environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── ExaMetrics (backend-sis) processing engine ────────────────────────────────
# The shared results-processing service. LAZEIMS collects; ExaMetrics processes.
# The host is baked in as a sensible production default so processing works out
# of the box; override BACKEND_SIS_BASE_URL per environment if needed.
EXAMETRICS_DEFAULT_BASE_URL = "https://api.shuleyetu.co.tz"
# Path where the ExaMetrics integration API is mounted (see backend-sis main.py:
# exametrics_router at /api/exametrics/v1, integration router prefix /integration).
EXAMETRICS_INTEGRATION_PREFIX = "/api/exametrics/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "LAZEIMS Central"
    app_env: str = "development"

    # Required secrets — no usable default in production.
    session_secret_key: str = Field(min_length=8)
    database_url: str
    station_package_integrity_key: str = Field(min_length=8)

    zone_name: str = ""
    allowed_cors_origins: str = "http://localhost:3000"

    # ExaMetrics (backend-sis) processing integration.
    # Accepts either the bare host ("https://api.shuleyetu.co.tz") or the full
    # integration URL ("https://api.shuleyetu.co.tz/api/exametrics/v1"); the
    # integration prefix is normalized in `integration_base_url`. Set to "" to
    # disable processing/results (collection-only deployment).
    backend_sis_base_url: str = EXAMETRICS_DEFAULT_BASE_URL
    backend_sis_timeout_seconds: int = 120

    @property
    def processing_enabled(self) -> bool:
        return bool(self.backend_sis_base_url.strip())

    @property
    def integration_base_url(self) -> str:
        """Fully-qualified ExaMetrics integration base URL (host + prefix).

        Idempotent: appending the prefix only when it is not already present, so
        both a bare host and a full URL resolve to the same value.
        """
        base = self.backend_sis_base_url.strip().rstrip("/")
        if not base:
            return ""
        if base.endswith(EXAMETRICS_INTEGRATION_PREFIX):
            return base
        return f"{base}{EXAMETRICS_INTEGRATION_PREFIX}"

    session_cookie_name: str = "lazeims_session"
    session_ttl_seconds: int = 43_200
    session_cookie_secure: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_cors_origins.split(",") if o.strip()]

    @field_validator("session_secret_key", "station_package_integrity_key")
    @classmethod
    def _no_insecure_default_in_prod(cls, v: str, info) -> str:
        # This validator runs per-field; production-time cross-checks happen in
        # ``validate_production`` after the model is built.
        return v

    def validate_production(self) -> None:
        """Extra guardrails applied only when running in production."""
        if not self.is_production:
            return
        insecure_markers = ("dev-only", "dev-local", "change-me", "insecure")
        for name, value in (
            ("SESSION_SECRET_KEY", self.session_secret_key),
            ("STATION_PACKAGE_INTEGRITY_KEY", self.station_package_integrity_key),
        ):
            if any(m in value.lower() for m in insecure_markers):
                raise RuntimeError(
                    f"{name} looks like a development placeholder; set a real secret in production."
                )
        if self.database_url.startswith("postgresql+asyncpg://postgres:postgres@"):
            raise RuntimeError("Refusing to run in production with default postgres/postgres credentials.")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # raises pydantic ValidationError if a required var is missing
    settings.validate_production()
    return settings
