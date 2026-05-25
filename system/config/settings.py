"""FORGE global settings — loaded once at import time from env / .env file."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        """True when debug is off."""
        return not self.debug


# Singleton — imported everywhere as `from system.config.settings import settings`
settings = Settings()
