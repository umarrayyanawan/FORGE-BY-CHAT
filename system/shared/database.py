"""Async SQLAlchemy database setup for the FORGE platform.

Usage
-----
FastAPI dependency injection::

    from system.shared.database import get_db

    @router.get("/things")
    async def list_things(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(Thing))
        return result.scalars().all()

Alembic env.py::

    from system.shared.database import Base, sync_engine
    target_metadata = Base.metadata
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, declared_attr

from system.config.settings import settings

# ========================================================================== #
# Engines
# ========================================================================== #


def _build_async_engine() -> AsyncEngine:
    """Create the primary async engine from application settings."""
    kwargs: dict[str, Any] = {
        "echo": settings.debug,
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 3_600,
    }

    # asyncpg doesn't support QueuePool with NullPool in some setups;
    # for test environments you can pass NullPool explicitly.
    return create_async_engine(settings.database_url, **kwargs)


async_engine: AsyncEngine = _build_async_engine()

# Session factory — re-use across the application lifetime
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ========================================================================== #
# Declarative base
# ========================================================================== #


class Base(AsyncAttrs, DeclarativeBase):
    """SQLAlchemy declarative base shared by all ORM models.

    Subclass this to define a mapped table::

        class Project(Base):
            __tablename__ = "projects"
            id: Mapped[str] = mapped_column(primary_key=True, ...)
    """

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        """Derive table name from class name (lower-snake)."""
        import re

        name = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()
        return name


# ========================================================================== #
# FastAPI dependency
# ========================================================================== #


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session per-request.

    The session is automatically rolled back on exception and closed
    afterwards, regardless of success or failure.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ========================================================================== #
# Schema initialisation (non-Alembic environments)
# ========================================================================== #


async def init_db() -> None:
    """Create all tables declared on ``Base.metadata``.

    Use this only in development / testing.  Production should rely on
    Alembic migrations instead.
    """
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db() -> None:
    """Drop all tables — **DANGER**: use only in tests."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def check_db_connection() -> bool:
    """Return True if the database is reachable."""
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ========================================================================== #
# Alembic-compatible sync engine helper
# ========================================================================== #
# env.py can do:
#   from system.shared.database import get_sync_engine_url
#   connectable = create_engine(get_sync_engine_url())


def get_sync_engine_url() -> str:
    """Return the synchronous database URL for Alembic env.py."""
    return settings.database_sync_url
