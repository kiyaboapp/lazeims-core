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
    # Zone enrolment secret (``X-Provision-Secret``), issued once by ExaMetrics.
    # It lets Central ask ExaMetrics for a per-exam API key server-to-server, so
    # no operator ever handles a key. Empty means keys must be entered by hand.
    backend_sis_provision_secret: str = ""

    # Comma-separated list of base64 Fernet keys for encrypting API keys at rest.
    # The first key encrypts; all keys decrypt (supports rotation). When empty,
    # a key is derived from session_secret_key so existing deployments do not
    # break on upgrade.
    processing_key_encryption_keys: str = ""

    # Public base URL of this Central instance, used for webhook callback
    # registration. Empty disables webhook registration.
    central_public_base_url: str = ""

    # ── Complete Station Bundle assembly ─────────────────────────────────────
    # Filesystem locations of the station app and the shared contract package,
    # vendored into the downloadable one-click bundle. Defaults assume the repos
    # sit side-by-side (as they do in this workspace).
    station_app_dir: str = "../lazeims-station"
    lazeims_common_dir: str = "../lazeims-common"
    # Optional prebuilt wheelhouse for fully-offline first-run installs. When
    # set and present, its wheels are copied into the bundle.
    station_wheelhouse_dir: str = ""
    # Optional bundled Python runtime (a venv-capable build, e.g.
    # python-build-standalone) copied into the bundle as runtime/ for a true
    # zero-install double-click on machines without Python.
    station_runtime_dir: str = ""

    # ── Ed25519 package signing ──────────────────────────────────────────────
    # Path to the PEM-encoded Ed25519 private key for signing station packages.
    # When empty, a deterministic dev/test key is used (ONLY acceptable in
    # development). Production MUST set this to a real key path.
    package_signing_private_key_path: str = ""

    # ── Credential delivery (station admin login) ────────────────────────────
    # When SMTP is configured, a station's default admin login is emailed to the
    # station super-admin on package generation. Empty host => in-app only (the
    # secret is still shown once in the console so it can be handed over).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    notify_from_email: str = "no-reply@lazeims.local"

    @property
    def processing_enabled(self) -> bool:
        return bool(self.backend_sis_base_url.strip())

    @property
    def provisioning_enabled(self) -> bool:
        """True when Central can obtain exam keys by itself."""
        return self.processing_enabled

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
