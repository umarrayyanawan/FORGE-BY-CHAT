"""Redis client setup for the FORGE platform.

Provides:
- An async Redis client (redis.asyncio) for use in FastAPI / async code.
- A FastAPI ``get_redis`` dependency that yields a connection per-request.
- A synchronous Redis client for use in Celery tasks.
- Canonical key-prefix constants so every module uses the same namespacing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Optional

import redis
import redis.asyncio as aioredis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.connection import ConnectionPool as AsyncConnectionPool

from system.config.settings import settings

logger = logging.getLogger(__name__)

# ========================================================================== #
# Key prefix constants
# ========================================================================== #

KEY_PREFIX_INTENT: str = "FORGE:INTENT:"
KEY_PREFIX_SESSION: str = "FORGE:SESSION:"
KEY_PREFIX_TASK: str = "FORGE:TASK:"
KEY_PREFIX_MEMORY: str = "FORGE:MEMORY:"
KEY_PREFIX_LOCK: str = "FORGE:LOCK:"
KEY_PREFIX_RATE: str = "FORGE:RATE:"


def make_key(prefix: str, *parts: str) -> str:
    """Build a namespaced Redis key from a prefix and one or more parts.

    Example::

        make_key(KEY_PREFIX_TASK, "abc-123", "status")
        # -> "FORGE:TASK:abc-123:status"
    """
    return prefix + ":".join(parts)


# ========================================================================== #
# Async connection pool & client
# ========================================================================== #


@lru_cache(maxsize=1)
def _get_async_pool() -> AsyncConnectionPool:
    """Singleton async connection pool — created once, reused forever."""
    return aioredis.ConnectionPool.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
    )


def get_async_redis_client() -> AsyncRedis:
    """Return an async Redis client backed by the singleton pool."""
    return aioredis.Redis(connection_pool=_get_async_pool())


# ========================================================================== #
# FastAPI dependency
# ========================================================================== #


async def get_redis() -> AsyncGenerator[AsyncRedis, None]:
    """FastAPI dependency that yields an async Redis client per-request.

    The client is drawn from the shared connection pool; connections are
    returned to the pool automatically after the request completes.

    Usage::

        @router.get("/ping")
        async def ping(redis: AsyncRedis = Depends(get_redis)):
            return await redis.ping()
    """
    client = get_async_redis_client()
    try:
        yield client
    finally:
        # Pool manages actual connection lifecycle; no explicit close needed.
        pass


# ========================================================================== #
# Synchronous client (Celery, scripts)
# ========================================================================== #


@lru_cache(maxsize=1)
def get_sync_redis_client() -> redis.Redis:
    """Return a synchronous Redis client for Celery workers and scripts."""
    return redis.Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=30,
    )


# ========================================================================== #
# High-level helpers
# ========================================================================== #


async def redis_ping() -> bool:
    """Return True if Redis is reachable."""
    try:
        client = get_async_redis_client()
        return await client.ping()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis ping failed: %s", exc)
        return False


async def redis_set_json(
    key: str,
    value: dict,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Serialise *value* as JSON and store under *key* with optional TTL."""
    import json

    client = get_async_redis_client()
    raw = json.dumps(value, default=str)
    if ttl_seconds:
        await client.set(key, raw, ex=ttl_seconds)
    else:
        await client.set(key, raw)


async def redis_get_json(key: str) -> Optional[dict]:
    """Retrieve and deserialise a JSON value from Redis.  Returns None on miss."""
    import json

    client = get_async_redis_client()
    raw = await client.get(key)
    if raw is None:
        return None
    return json.loads(raw)


async def redis_delete(key: str) -> int:
    """Delete a key from Redis.  Returns the number of keys removed."""
    client = get_async_redis_client()
    return await client.delete(key)


async def redis_publish(channel: str, message: str) -> int:
    """Publish a message to a Redis Pub/Sub channel."""
    client = get_async_redis_client()
    return await client.publish(channel, message)
