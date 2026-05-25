"""Secrets manager with Fernet symmetric encryption backed by PostgreSQL."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
from typing import Any
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import Column, DateTime, String, Text, delete, select
from sqlalchemy.dialects.postgresql import UUID

from system.config.settings import settings
from system.observability.logging.logger import get_logger
from system.shared.database import Base

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# ORM model
# --------------------------------------------------------------------------- #


class SecretDB(Base):
    """Encrypted secret storage table."""

    __tablename__ = "forge_secrets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(String(255), nullable=False, index=True)
    environment = Column(String(64), nullable=False, index=True)
    key = Column(String(255), nullable=False)
    encrypted_value = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<SecretDB project_id={self.project_id!r} env={self.environment!r} key={self.key!r}>"
        )


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class SecretsManager:
    """Store, retrieve, and inject secrets with envelope encryption.

    The master key is derived from ``settings.secret_key`` via SHA-256 so that
    the Fernet key is always exactly 32 bytes, regardless of the raw secret
    length.

    If a database session factory is provided the manager persists secrets in
    PostgreSQL; otherwise it falls back to an in-memory store (useful for
    integration tests and dry-run environments).
    """

    def __init__(self, db_session_factory: Any | None = None) -> None:
        # Derive a 32-byte key from the application secret and encode it as
        # URL-safe base64 as required by Fernet.
        key_bytes = hashlib.sha256(settings.secret_key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(key_bytes)
        self._fernet = Fernet(fernet_key)
        self._db_factory = db_session_factory
        # In-memory fallback: project_id -> environment -> key -> encrypted_value
        self._store: dict[str, dict[str, dict[str, str]]] = {}

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, encrypted: str) -> str:
        return self._fernet.decrypt(encrypted.encode()).decode()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def store_secret(
        self,
        key: str,
        value: str,
        project_id: str,
        environment: str,
    ) -> None:
        """Encrypt *value* and persist it under *key* for the given project/env."""
        encrypted = self._encrypt(value)
        logger.info(
            "Storing secret",
            project_id=project_id,
            key=key,
            environment=environment,
        )
        if self._db_factory is not None:
            async with self._db_factory() as session:
                session = session  # type: AsyncSession
                # Upsert: delete existing and insert new.
                await session.execute(
                    delete(SecretDB).where(
                        SecretDB.project_id == project_id,
                        SecretDB.environment == environment,
                        SecretDB.key == key,
                    )
                )
                session.add(
                    SecretDB(
                        project_id=project_id,
                        environment=environment,
                        key=key,
                        encrypted_value=encrypted,
                    )
                )
                await session.commit()
        else:
            bucket = self._store.setdefault(project_id, {}).setdefault(environment, {})
            bucket[key] = encrypted

    async def get_secret(
        self,
        key: str,
        project_id: str,
        environment: str,
    ) -> str | None:
        """Return the decrypted secret value, or *None* if not found."""
        logger.info(
            "Retrieving secret",
            project_id=project_id,
            key=key,
            environment=environment,
        )
        if self._db_factory is not None:
            async with self._db_factory() as session:
                result = await session.execute(
                    select(SecretDB).where(
                        SecretDB.project_id == project_id,
                        SecretDB.environment == environment,
                        SecretDB.key == key,
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                return self._decrypt(row.encrypted_value)
        bucket = self._store.get(project_id, {}).get(environment, {})
        encrypted = bucket.get(key)
        if encrypted is None:
            return None
        return self._decrypt(encrypted)

    async def delete_secret(
        self,
        key: str,
        project_id: str,
        environment: str,
    ) -> None:
        """Remove a secret from the store."""
        logger.info(
            "Deleting secret",
            project_id=project_id,
            key=key,
            environment=environment,
        )
        if self._db_factory is not None:
            async with self._db_factory() as session:
                await session.execute(
                    delete(SecretDB).where(
                        SecretDB.project_id == project_id,
                        SecretDB.environment == environment,
                        SecretDB.key == key,
                    )
                )
                await session.commit()
        else:
            self._store.get(project_id, {}).get(environment, {}).pop(key, None)

    async def list_secrets(self, project_id: str, environment: str) -> list[str]:
        """Return the list of secret *keys* (not values) for the given project/env."""
        if self._db_factory is not None:
            async with self._db_factory() as session:
                result = await session.execute(
                    select(SecretDB.key).where(
                        SecretDB.project_id == project_id,
                        SecretDB.environment == environment,
                    )
                )
                return [row[0] for row in result.fetchall()]
        return list(self._store.get(project_id, {}).get(environment, {}).keys())

    async def inject_into_env(self, project_id: str, environment: str) -> dict[str, str]:
        """Return a dict of decrypted key→value pairs ready to merge into env_vars."""
        keys = await self.list_secrets(project_id, environment)
        result: dict[str, str] = {}
        for key in keys:
            value = await self.get_secret(key, project_id, environment)
            if value is not None:
                result[key] = value
        return result

    async def rotate_master_key(
        self,
        new_secret_key: str,
        project_id: str,
        environment: str,
    ) -> None:
        """Re-encrypt all secrets under a new master key.

        This is an atomic best-effort operation; callers should coordinate
        application restarts around key rotation.
        """
        new_key_bytes = hashlib.sha256(new_secret_key.encode()).digest()
        new_fernet = Fernet(base64.urlsafe_b64encode(new_key_bytes))
        keys = await self.list_secrets(project_id, environment)
        for key in keys:
            plaintext = await self.get_secret(key, project_id, environment)
            if plaintext is not None:
                new_encrypted = new_fernet.encrypt(plaintext.encode()).decode()
                if self._db_factory is not None:
                    async with self._db_factory() as session:
                        result = await session.execute(
                            select(SecretDB).where(
                                SecretDB.project_id == project_id,
                                SecretDB.environment == environment,
                                SecretDB.key == key,
                            )
                        )
                        row = result.scalar_one_or_none()
                        if row:
                            row.encrypted_value = new_encrypted
                            row.updated_at = datetime.utcnow()
                            await session.commit()
                else:
                    bucket = self._store.get(project_id, {}).get(environment, {})
                    bucket[key] = new_encrypted
        self._fernet = Fernet(base64.urlsafe_b64encode(new_key_bytes))
        logger.info(
            "Master key rotated",
            project_id=project_id,
            environment=environment,
            key_count=len(keys),
        )
