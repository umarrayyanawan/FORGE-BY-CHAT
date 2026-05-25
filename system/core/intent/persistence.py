"""State persistence for the FORGE Intent Engine.

Implements a two-tier storage strategy:
  1. Redis — hot path, 24-hour TTL, JSON serialised.
  2. PostgreSQL — durable store via SQLAlchemy async ORM.

Both :class:`IntentSession` and :class:`ProjectIntent` objects are persisted.
"""

from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from system.observability.logging.logger import get_logger
from system.shared.database import Base

from .schemas import IntentSession, IntentStatus, ProjectIntent

logger = get_logger(__name__)

# =========================================================================== #
# Redis key helpers & TTL
# =========================================================================== #

_SESSION_TTL_SECONDS = 86_400  # 24 hours
_INTENT_TTL_SECONDS = 86_400 * 7  # 7 days for project-level intent

_SESSION_KEY = "FORGE:INTENT:SESSION:{session_id}"
_INTENT_KEY = "FORGE:INTENT:PROJECT:{project_id}"
_PROJECT_SESSIONS_KEY = "FORGE:INTENT:PROJECT_SESSIONS:{project_id}"


def _session_key(session_id: str) -> str:
    return _SESSION_KEY.format(session_id=session_id)


def _intent_key(project_id: str) -> str:
    return _INTENT_KEY.format(project_id=project_id)


def _project_sessions_key(project_id: str) -> str:
    return _PROJECT_SESSIONS_KEY.format(project_id=project_id)


# =========================================================================== #
# SQLAlchemy ORM model
# =========================================================================== #


class IntentSessionDB(Base):  # type: ignore[valid-type,misc]
    """Durable store for intent sessions in PostgreSQL.

    Table: ``forge_intent_sessions``
    """

    __tablename__ = "forge_intent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    intent_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    clarification_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clarification_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    def to_intent_session(self) -> IntentSession:
        """Deserialise the ORM row into a :class:`IntentSession`."""
        intent_data = json.loads(self.intent_json)
        history_data = json.loads(self.clarification_history_json or "[]")

        return IntentSession(
            id=str(self.id),
            session_id=self.session_id,
            project_id=self.project_id,
            raw_prompt=self.raw_prompt,
            intent=ProjectIntent.model_validate(intent_data),
            clarification_round=self.clarification_round,
            clarification_history=history_data,
            status=IntentStatus(self.status),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_intent_session(cls, session: IntentSession) -> IntentSessionDB:
        """Serialise an :class:`IntentSession` into an ORM row."""
        return cls(
            session_id=session.session_id,
            project_id=session.project_id,
            raw_prompt=session.raw_prompt,
            intent_json=session.intent.model_dump_json(),
            status=str(session.status),
            clarification_round=session.clarification_round,
            clarification_history_json=json.dumps(session.clarification_history),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


# =========================================================================== #
# Persistence class
# =========================================================================== #


class IntentPersistence:
    """Two-tier persistence layer for intent sessions and project intents.

    Parameters
    ----------
    redis:
        An async Redis client (e.g. from ``system.shared.redis_client.get_redis``).
    db_session:
        An async SQLAlchemy session (``AsyncSession``).
    """

    def __init__(self, redis: object, db_session: AsyncSession) -> None:
        self._redis = redis
        self._db = db_session
        self._log = logger

    # ---------------------------------------------------------------------- #
    # Session operations
    # ---------------------------------------------------------------------- #

    async def save_session(self, session: IntentSession) -> None:
        """Persist an :class:`IntentSession` to Redis and PostgreSQL.

        Redis write is the primary (hot-path) store with a 24-hour TTL.
        The Postgres write is always attempted; failures are logged but do not
        abort the request.

        Parameters
        ----------
        session:
            Session to persist.
        """
        self._log.info("saving_session", session_id=session.session_id)

        # --- Redis ---
        try:
            session_json = session.model_dump_json()
            await self._redis.setex(  # type: ignore[attr-defined]
                _session_key(session.session_id),
                _SESSION_TTL_SECONDS,
                session_json,
            )
            # Track session IDs per project (Redis set)
            await self._redis.sadd(  # type: ignore[attr-defined]
                _project_sessions_key(session.project_id),
                session.session_id,
            )
            await self._redis.expire(  # type: ignore[attr-defined]
                _project_sessions_key(session.project_id),
                _SESSION_TTL_SECONDS,
            )
        except Exception as exc:
            self._log.error(
                "redis_session_save_failed", error=str(exc), session_id=session.session_id
            )

        # --- PostgreSQL ---
        try:
            existing = await self._db.execute(
                select(IntentSessionDB).where(IntentSessionDB.session_id == session.session_id)
            )
            row: IntentSessionDB | None = existing.scalar_one_or_none()

            if row is None:
                row = IntentSessionDB.from_intent_session(session)
                self._db.add(row)
            else:
                row.intent_json = session.intent.model_dump_json()
                row.status = str(session.status)
                row.clarification_round = session.clarification_round
                row.clarification_history_json = json.dumps(session.clarification_history)
                row.updated_at = datetime.utcnow()

            await self._db.commit()
        except Exception as exc:
            await self._db.rollback()
            self._log.error("db_session_save_failed", error=str(exc), session_id=session.session_id)
            # Non-fatal — the Redis write succeeded

    async def load_session(self, session_id: str) -> IntentSession | None:
        """Load a session from Redis (hot path) or fall back to Postgres.

        Parameters
        ----------
        session_id:
            The unique session identifier.

        Returns
        -------
        Optional[IntentSession]
            The session, or None if not found in either store.
        """
        self._log.debug("loading_session", session_id=session_id)

        # --- Redis first ---
        try:
            raw = await self._redis.get(_session_key(session_id))  # type: ignore[attr-defined]
            if raw:
                data = json.loads(raw)
                return IntentSession.model_validate(data)
        except Exception as exc:
            self._log.warning("redis_session_load_failed", error=str(exc), session_id=session_id)

        # --- Postgres fallback ---
        try:
            result = await self._db.execute(
                select(IntentSessionDB).where(IntentSessionDB.session_id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            session = row.to_intent_session()
            # Back-fill Redis for subsequent requests
            await self._warm_redis_session(session)
            return session
        except Exception as exc:
            self._log.error("db_session_load_failed", error=str(exc), session_id=session_id)
            return None

    # ---------------------------------------------------------------------- #
    # Project-level intent operations
    # ---------------------------------------------------------------------- #

    async def save_intent(self, project_id: str, intent: ProjectIntent) -> None:
        """Persist the final validated intent for a project.

        Stores to Redis (7-day TTL) and upserts a canonical record in
        Postgres under the ``__intent__`` session_id for the project.

        Parameters
        ----------
        project_id:
            The FORGE project identifier.
        intent:
            The validated :class:`ProjectIntent`.
        """
        self._log.info("saving_project_intent", project_id=project_id)

        # --- Redis ---
        try:
            await self._redis.setex(  # type: ignore[attr-defined]
                _intent_key(project_id),
                _INTENT_TTL_SECONDS,
                intent.model_dump_json(),
            )
        except Exception as exc:
            self._log.error("redis_intent_save_failed", error=str(exc), project_id=project_id)

        # --- Postgres: store under a synthetic canonical session_id ---
        canonical_session_id = f"intent__{project_id}"
        try:
            result = await self._db.execute(
                select(IntentSessionDB).where(IntentSessionDB.session_id == canonical_session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = IntentSessionDB(
                    session_id=canonical_session_id,
                    project_id=project_id,
                    raw_prompt=intent.raw_prompt,
                    intent_json=intent.model_dump_json(),
                    status=str(intent.status),
                    clarification_round=0,
                    clarification_history_json="[]",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                self._db.add(row)
            else:
                row.intent_json = intent.model_dump_json()
                row.status = str(intent.status)
                row.updated_at = datetime.utcnow()
            await self._db.commit()
        except Exception as exc:
            await self._db.rollback()
            self._log.error("db_intent_save_failed", error=str(exc), project_id=project_id)

    async def load_intent(self, project_id: str) -> ProjectIntent | None:
        """Load the validated project intent.

        Parameters
        ----------
        project_id:
            The FORGE project identifier.

        Returns
        -------
        Optional[ProjectIntent]
            The validated intent, or None if not yet persisted.
        """
        self._log.debug("loading_project_intent", project_id=project_id)

        # --- Redis first ---
        try:
            raw = await self._redis.get(_intent_key(project_id))  # type: ignore[attr-defined]
            if raw:
                return ProjectIntent.model_validate(json.loads(raw))
        except Exception as exc:
            self._log.warning("redis_intent_load_failed", error=str(exc), project_id=project_id)

        # --- Postgres fallback ---
        canonical_session_id = f"intent__{project_id}"
        try:
            result = await self._db.execute(
                select(IntentSessionDB).where(IntentSessionDB.session_id == canonical_session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            intent = ProjectIntent.model_validate(json.loads(row.intent_json))
            # Back-fill Redis
            try:
                await self._redis.setex(  # type: ignore[attr-defined]
                    _intent_key(project_id),
                    _INTENT_TTL_SECONDS,
                    intent.model_dump_json(),
                )
            except Exception:
                pass
            return intent
        except Exception as exc:
            self._log.error("db_intent_load_failed", error=str(exc), project_id=project_id)
            return None

    # ---------------------------------------------------------------------- #
    # Session listing
    # ---------------------------------------------------------------------- #

    async def list_sessions(self, project_id: str) -> list[IntentSession]:
        """Return all sessions for a given project, newest first.

        Queries Postgres directly for a consistent, ordered view.

        Parameters
        ----------
        project_id:
            The FORGE project identifier.

        Returns
        -------
        List[IntentSession]
            Ordered list of sessions.
        """
        self._log.debug("listing_sessions", project_id=project_id)
        try:
            result = await self._db.execute(
                select(IntentSessionDB)
                .where(IntentSessionDB.project_id == project_id)
                .where(IntentSessionDB.session_id.not_like("intent__%"))
                .order_by(IntentSessionDB.created_at.desc())
            )
            rows = result.scalars().all()
            return [row.to_intent_session() for row in rows]
        except Exception as exc:
            self._log.error("db_list_sessions_failed", error=str(exc), project_id=project_id)
            return []

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session from both Redis and Postgres.

        Parameters
        ----------
        session_id:
            The session to delete.

        Returns
        -------
        bool
            True if the session was found and deleted, False otherwise.
        """
        self._log.info("deleting_session", session_id=session_id)
        found = False

        # Redis
        try:
            deleted = await self._redis.delete(_session_key(session_id))  # type: ignore[attr-defined]
            if deleted:
                found = True
        except Exception as exc:
            self._log.warning("redis_delete_failed", error=str(exc))

        # Postgres
        try:
            result = await self._db.execute(
                select(IntentSessionDB).where(IntentSessionDB.session_id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                await self._db.delete(row)
                await self._db.commit()
                found = True
        except Exception as exc:
            await self._db.rollback()
            self._log.error("db_delete_failed", error=str(exc))

        return found

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    async def _warm_redis_session(self, session: IntentSession) -> None:
        """Write a session back to Redis after a Postgres cold-read."""
        try:
            await self._redis.setex(  # type: ignore[attr-defined]
                _session_key(session.session_id),
                _SESSION_TTL_SECONDS,
                session.model_dump_json(),
            )
        except Exception as exc:
            self._log.warning("redis_warm_failed", error=str(exc), session_id=session.session_id)
