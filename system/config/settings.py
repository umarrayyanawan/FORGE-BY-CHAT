"""FORGE global settings — loaded once at import time from env / .env file."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.main import PydanticBaseSettingsSource


class _SafeEnvSource(EnvSettingsSource):
    """Env source that treats empty-string values as absent for complex fields.

    Pydantic-settings 2.14+ calls json.loads() on every string value before
    field validators run.  An empty or comma-separated CORS_ORIGINS= in .env
    would raise JSONDecodeError.  This subclass short-circuits that.
    """

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None  # Let the field default take effect
            if not (stripped.startswith("[") or stripped.startswith("{")):
                return value  # Not JSON — pass through for field_validator
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class _SafeDotEnvSource(DotEnvSettingsSource):
    """DotEnv variant of _SafeEnvSource."""

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if not (stripped.startswith("[") or stripped.startswith("{")):
                return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    """Central configuration object.

    All values can be overridden via environment variables or a .env file.
    Required fields (no default) **must** be set in the environment; the
    application will refuse to start if they are missing.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Application
    # ------------------------------------------------------------------ #
    app_name: str = "FORGE"
    version: str = "0.1.0"
    debug: bool = False
    secret_key: str = Field(
        default="dev-secret-key-change-in-production-min32chars",
        min_length=32,
        description="HMAC / JWT signing key",
    )
    cors_origins: list[str] = ["http://localhost:3000"]
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v: Any) -> Any:
        """Accept JSON array, comma-separated string, or plain URL."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return ["http://localhost:3000"]
            if v.startswith("["):
                return json.loads(v)
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    # ------------------------------------------------------------------ #
    # PostgreSQL / SQLAlchemy
    # ------------------------------------------------------------------ #
    database_url: str = Field(
        default="postgresql+asyncpg://forge:forge@localhost:5432/forge_db",
        description="Async SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host/db",
    )
    database_sync_url: str = Field(
        default="postgresql+psycopg2://forge:forge@localhost:5432/forge_db",
        description="Sync SQLAlchemy URL used by Alembic, e.g. postgresql+psycopg2://...",
    )

    # ------------------------------------------------------------------ #
    # Redis
    # ------------------------------------------------------------------ #
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ------------------------------------------------------------------ #
    # Neo4j
    # ------------------------------------------------------------------ #
    neo4j_url: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(default="forge", description="Neo4j password")

    # ------------------------------------------------------------------ #
    # Temporal
    # ------------------------------------------------------------------ #
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "forge-default"

    # ------------------------------------------------------------------ #
    # AI / LLM
    # ------------------------------------------------------------------ #
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    openai_api_key: str = ""

    # ------------------------------------------------------------------ #
    # GitHub
    # ------------------------------------------------------------------ #
    github_token: str = ""
    github_app_id: str = ""

    # ------------------------------------------------------------------ #
    # Deployment targets
    # ------------------------------------------------------------------ #
    vercel_token: str = ""
    railway_token: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    sentry_dsn: str = ""
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    # ------------------------------------------------------------------ #
    # Agent runtime
    # ------------------------------------------------------------------ #
    max_agent_tokens: int = 8192
    max_retries: int = 3
    retry_delay_seconds: float = 2.0

    # ------------------------------------------------------------------ #
    # Source customisation
    # ------------------------------------------------------------------ #
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _SafeEnvSource(settings_cls),
            _SafeDotEnvSource(settings_cls),
            file_secret_settings,
        )

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        """True when debug is off."""
        return not self.debug


# Singleton — imported everywhere as `from system.config.settings import settings`
settings = Settings()
