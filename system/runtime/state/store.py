"""Generic Redis-backed state store for the FORGE platform.

The StateStore provides a typed, Pydantic-aware interface over Redis for
storing, retrieving, and manipulating structured state objects.

It is intentionally generic — any Pydantic BaseModel subclass can be stored
and retrieved without boilerplate.

Usage::

    from system.runtime.state.store import StateStore
    from system.shared.redis_client import get_redis

    redis = await get_redis()
    store = StateStore(redis=redis)

    # Store a model
    await store.set("FORGE:INTENT:abc", intent_obj, ttl=3600)

    # Retrieve it
    intent = await store.get("FORGE:INTENT:abc", ProjectIntent)

    # Atomic counter
    count = await store.increment("FORGE:COUNTERS:tasks_completed")
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class StateStore:
    """Typed, Pydantic-aware Redis state store.

    Wraps a Redis async connection and provides typed get/set operations,
    batch operations, list management, and atomic counters.

    All serialisation uses Pydantic's ``model_dump_json`` / ``model_validate_json``
    to preserve field types, validators, and enums across the boundary.
    """

    def __init__(self, redis: Any) -> None:
        """
        Args:
            redis: An async Redis connection (``redis.asyncio.Redis`` or compatible).
        """
        self.redis = redis

    # ------------------------------------------------------------------
    # Single-key operations
    # ------------------------------------------------------------------

    async def get(self, key: str, model_class: type[T]) -> T | None:
        """Retrieve a single Pydantic model from Redis.

        Args:
            key:         Redis key.
            model_class: Pydantic model class to deserialise into.

        Returns:
            Deserialised model instance, or ``None`` if the key does not exist.
        """
        raw = await self.redis.get(key)
        if raw is None:
            return None
        try:
            data = raw.decode() if isinstance(raw, bytes) else raw
            return model_class.model_validate_json(data)
        except Exception as exc:
            logger.error(
                "StateStore.get deserialisation failed",
                key=key,
                model=model_class.__name__,
                error=str(exc),
            )
            return None

    async def set(
        self,
        key: str,
        value: BaseModel,
        ttl: int | None = None,
    ) -> None:
        """Store a Pydantic model in Redis.

        Args:
            key:   Redis key.
            value: Pydantic model instance to store.
            ttl:   Optional TTL in seconds.  If None, the key does not expire.
        """
        serialised = value.model_dump_json()
        if ttl is not None and ttl > 0:
            await self.redis.setex(key, ttl, serialised)
        else:
            await self.redis.set(key, serialised)

    async def delete(self, key: str) -> None:
        """Delete *key* from Redis.  No-op if the key does not exist."""
        await self.redis.delete(key)

    async def exists(self, key: str) -> bool:
        """Return True if *key* exists in Redis."""
        return bool(await self.redis.exists(key))

    async def get_ttl(self, key: str) -> int:
        """Return the remaining TTL of *key* in seconds.

        Returns:
            Positive integer if TTL is set, -1 if key exists but has no TTL,
            -2 if the key does not exist.
        """
        return await self.redis.ttl(key)

    async def set_ttl(self, key: str, ttl: int) -> None:
        """Set or update the TTL of an existing key.

        Args:
            key: Redis key.
            ttl: New TTL in seconds.
        """
        await self.redis.expire(key, ttl)

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def get_many(
        self,
        keys: list[str],
        model_class: type[T],
    ) -> list[T]:
        """Retrieve multiple keys in a single MGET call.

        Keys that do not exist or fail deserialisation are silently skipped.

        Args:
            keys:        List of Redis keys to retrieve.
            model_class: Pydantic model class to deserialise each value into.

        Returns:
            List of successfully deserialised model instances (may be shorter than *keys*).
        """
        if not keys:
            return []
        raw_values = await self.redis.mget(*keys)
        results: list[T] = []
        for key, raw in zip(keys, raw_values):
            if raw is None:
                continue
            try:
                data = raw.decode() if isinstance(raw, bytes) else raw
                results.append(model_class.model_validate_json(data))
            except Exception as exc:
                logger.warning(
                    "StateStore.get_many skip: deserialisation failed",
                    key=key,
                    error=str(exc),
                )
        return results

    async def set_many(
        self,
        items: dict[str, BaseModel],
        ttl: int | None = None,
    ) -> None:
        """Store multiple key → model pairs in a single pipeline.

        Args:
            items: Mapping of Redis key → Pydantic model instance.
            ttl:   Optional TTL in seconds applied to every key.
        """
        if not items:
            return
        pipeline = self.redis.pipeline()
        for key, value in items.items():
            serialised = value.model_dump_json()
            if ttl is not None and ttl > 0:
                pipeline.setex(key, ttl, serialised)
            else:
                pipeline.set(key, serialised)
        await pipeline.execute()

    async def delete_many(self, keys: list[str]) -> None:
        """Delete multiple keys in a single call.

        Args:
            keys: Redis keys to delete.
        """
        if keys:
            await self.redis.delete(*keys)

    # ------------------------------------------------------------------
    # Atomic counter
    # ------------------------------------------------------------------

    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment an integer counter at *key*.

        The key is created with value 0 before incrementing if it does not exist.

        Args:
            key:    Redis key holding an integer counter.
            amount: Amount to increment by (may be negative to decrement).

        Returns:
            New counter value after increment.
        """
        return await self.redis.incrby(key, amount)

    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement an integer counter at *key*."""
        return await self.redis.decrby(key, amount)

    async def get_counter(self, key: str) -> int:
        """Return the current counter value for *key*, or 0 if not set."""
        raw = await self.redis.get(key)
        if raw is None:
            return 0
        return int(raw)

    # ------------------------------------------------------------------
    # List operations (capped append-left queues)
    # ------------------------------------------------------------------

    async def add_to_list(
        self,
        key: str,
        value: str,
        max_length: int = 1000,
    ) -> None:
        """Prepend *value* to a Redis list and cap its length at *max_length*.

        Uses LPUSH + LTRIM in a pipeline for atomicity.  Oldest entries
        (rightmost) are trimmed when the list exceeds *max_length*.

        Args:
            key:        Redis key for the list.
            value:      String value to prepend.
            max_length: Maximum number of entries to retain.
        """
        pipeline = self.redis.pipeline()
        pipeline.lpush(key, value)
        pipeline.ltrim(key, 0, max_length - 1)
        await pipeline.execute()

    async def get_list(
        self,
        key: str,
        start: int = 0,
        end: int = -1,
    ) -> list[str]:
        """Return a range of elements from a Redis list.

        Args:
            key:   Redis key for the list.
            start: Zero-based start index (inclusive).
            end:   Zero-based end index (inclusive). -1 means last element.

        Returns:
            List of string values.
        """
        raw = await self.redis.lrange(key, start, end)
        return [(v.decode() if isinstance(v, bytes) else v) for v in (raw or [])]

    async def list_length(self, key: str) -> int:
        """Return the number of elements in a Redis list."""
        return await self.redis.llen(key)

    # ------------------------------------------------------------------
    # Raw JSON operations (for non-Pydantic data)
    # ------------------------------------------------------------------

    async def get_json(self, key: str) -> Any | None:
        """Retrieve a raw JSON value from Redis and parse it.

        Returns:
            Parsed Python object (dict, list, etc.) or None.
        """
        raw = await self.redis.get(key)
        if raw is None:
            return None
        data = raw.decode() if isinstance(raw, bytes) else raw
        return json.loads(data)

    async def set_json(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        """Store any JSON-serialisable value in Redis.

        Args:
            key:   Redis key.
            value: Any JSON-serialisable Python object.
            ttl:   Optional TTL in seconds.
        """
        serialised = json.dumps(value, default=str)
        if ttl is not None and ttl > 0:
            await self.redis.setex(key, ttl, serialised)
        else:
            await self.redis.set(key, serialised)
