"""Application configuration.

Reads environment variables (and a local ``.env``) via pydantic-settings and
**fails fast** at import/startup if a required variable is missing — never
silently defaults a security-relevant value in a non-development environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Base URL of the ExaMetrics integration API, e.g.
    # "https://api.exametrics.example/api/exametrics/v1". Empty disables the
    # processing/results features (collection-only deployment).
    backend_sis_base_url: str = ""
    backend_sis_timeout_seconds: int = 120

    # Zone enrolment secret, issued by ExaMetrics once per zone. Lets this server
    # self-provision a per-exam key when an exam is created, instead of an admin
    # pasting one in by hand. Server-side only; never sent to a browser.
    backend_sis_provision_secret: str = ""
    # Which ExaMetrics board new exams are provisioned under (first call only).
    backend_sis_board_id: str = ""
    # Label ExaMetrics shows against keys provisioned by this deployment.
    backend_sis_partner_label: str = "LAZEIMS"

    @property
    def processing_enabled(self) -> bool:
        return bool(self.backend_sis_base_url.strip())

    @property
    def provisioning_enabled(self) -> bool:
        """Self-provisioning needs a base URL and the zone enrolment secret."""
        return self.processing_enabled and bool(self.backend_sis_provision_secret.strip())

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
