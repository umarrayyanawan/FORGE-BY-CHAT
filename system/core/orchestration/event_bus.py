"""Event bus implementation using Redis Pub/Sub.

The EventBus connects every FORGE component via a lightweight publish/subscribe
mechanism backed by Redis.  Events are:

1. Published to a Redis Pub/Sub channel for real-time delivery.
2. Stored in a Redis sorted set (score = timestamp ms) for replay / auditing.

Key namespacing:
    Channel: forge:events:{project_id}          (Pub/Sub)
    History: FORGE:EVENTS:{project_id}           (Sorted set, score = timestamp_ms)
    Global:  forge:events:*                      (all-events channel for supervisors)

Usage::

    bus = EventBus(redis)
    await bus.publish(TaskCompletedEvent(project_id="abc", task_id="xyz", ...))
    await bus.subscribe("abc", my_async_handler)
    history = await bus.get_event_history("abc", limit=50)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from system.core.orchestration.event_schemas import (
    EVENT_TYPE_REGISTRY,
    ForgeEvent,
    deserialize_event,
)
from system.observability.logging.logger import get_logger
from system.shared.constants import EVENT_BUS_CHANNEL

logger = get_logger(__name__)

# Redis key patterns
_CHANNEL_PREFIX = "forge:events"
_HISTORY_PREFIX = "FORGE:EVENTS"
_MAX_HISTORY_ENTRIES = 10_000  # capped per project to prevent unbounded growth
_HISTORY_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _project_channel(project_id: str) -> str:
    return f"{_CHANNEL_PREFIX}:{project_id}"


def _history_key(project_id: str) -> str:
    return f"{_HISTORY_PREFIX}:{project_id}"


# ========================================================================== #
# EventBus
# ========================================================================== #


class EventBus:
    """Redis-backed publish/subscribe event bus for FORGE pipeline events.

    The bus is safe to share across the application (one instance per process).
    Each subscriber runs in a background asyncio task so it does not block
    the publisher.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        # project_id → list of async callables
        self._handlers: Dict[str, List[Callable]] = {}
        # project_id → background listener task
        self._listener_tasks: Dict[str, asyncio.Task] = {}
        # "all" listeners (subscribe_all)
        self._global_handlers: List[Callable] = []
        self._global_listener_task: Optional[asyncio.Task] = None
        self._global_pubsub: Optional[PubSub] = None

    # ------------------------------------------------------------------ #
    # Publish
    # ------------------------------------------------------------------ #

    async def publish(self, event: ForgeEvent) -> None:
        """Serialize *event* and broadcast it on the project channel.

        Also persists the event in the project's history sorted set.

        Args:
            event: Any ForgeEvent or subclass instance.
        """
        serialized = self._serialize_event(event)
        channel = _project_channel(event.project_id)

        # Publish to Pub/Sub
        await self._redis.publish(channel, serialized)

        # Also publish on the global channel for supervisory listeners
        await self._redis.publish(EVENT_BUS_CHANNEL, serialized)

        # Persist to sorted set (score = timestamp ms for ordering / range queries)
        history_key = _history_key(event.project_id)
        await self._redis.zadd(
            history_key,
            {serialized: event.timestamp_ms},
        )
        # Cap the sorted set size to prevent unbounded growth
        await self._redis.zremrangebyrank(history_key, 0, -(_MAX_HISTORY_ENTRIES + 1))
        # Refresh TTL on each write
        await self._redis.expire(history_key, _HISTORY_TTL_SECONDS)

        logger.debug(
            "event_published",
            event_type=event.event_type,
            event_id=event.event_id,
            project_id=event.project_id,
            channel=channel,
        )

    # ------------------------------------------------------------------ #
    # Subscribe
    # ------------------------------------------------------------------ #

    async def subscribe(self, project_id: str, handler: Callable) -> None:
        """Subscribe *handler* to events for *project_id*.

        Multiple handlers can be registered for the same project.  Each call
        to publish() will invoke all registered handlers sequentially in the
        listener task.

        Args:
            project_id: Project whose events to subscribe to.
            handler:    Async callable ``async def handler(event: ForgeEvent) -> None``.
        """
        if project_id not in self._handlers:
            self._handlers[project_id] = []
        self._handlers[project_id].append(handler)

        # Ensure a listener task is running for this project
        if project_id not in self._listener_tasks or self._listener_tasks[project_id].done():
            task = asyncio.create_task(
                self._listen(project_id),
                name=f"forge-event-listener-{project_id}",
            )
            self._listener_tasks[project_id] = task
            logger.info("event_bus_subscribed", project_id=project_id)

    async def subscribe_all(self, handler: Callable) -> None:
        """Subscribe *handler* to ALL events across all projects.

        Uses the global EVENT_BUS_CHANNEL channel.

        Args:
            handler: Async callable ``async def handler(event: ForgeEvent) -> None``.
        """
        self._global_handlers.append(handler)

        if self._global_listener_task is None or self._global_listener_task.done():
            self._global_pubsub = self._redis.pubsub()
            await self._global_pubsub.subscribe(EVENT_BUS_CHANNEL)
            self._global_listener_task = asyncio.create_task(
                self._listen_global(),
                name="forge-event-listener-global",
            )
            logger.info("event_bus_global_subscribed")

    # ------------------------------------------------------------------ #
    # Unsubscribe
    # ------------------------------------------------------------------ #

    async def unsubscribe(self, project_id: str) -> None:
        """Remove all handlers and stop the listener task for *project_id*.

        Args:
            project_id: Project to unsubscribe from.
        """
        self._handlers.pop(project_id, None)

        task = self._listener_tasks.pop(project_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        logger.info("event_bus_unsubscribed", project_id=project_id)

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #

    async def get_event_history(
        self,
        project_id: str,
        limit: int = 100,
        since_timestamp_ms: Optional[float] = None,
    ) -> List[ForgeEvent]:
        """Retrieve the most recent events for *project_id* from the sorted set.

        Args:
            project_id:         Project whose history to query.
            limit:              Maximum number of events to return (most-recent first).
            since_timestamp_ms: If provided, only return events after this timestamp.

        Returns:
            List of ForgeEvent instances, ordered newest-first.
        """
        history_key = _history_key(project_id)

        if since_timestamp_ms is not None:
            # Range query: score from since_timestamp_ms to +inf
            raw_entries: List[str] = await self._redis.zrangebyscore(
                history_key,
                min=since_timestamp_ms,
                max="+inf",
                start=0,
                num=limit,
            )
        else:
            # Most recent `limit` entries (reverse order = newest first)
            raw_entries = await self._redis.zrevrange(
                history_key, 0, limit - 1
            )

        events: List[ForgeEvent] = []
        for raw in raw_entries:
            try:
                event = self._deserialize_event(raw)
                events.append(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "event_deserialize_failed",
                    error=str(exc),
                    raw_length=len(raw),
                )

        return events

    async def get_event_count(self, project_id: str) -> int:
        """Return the total number of events stored for *project_id*."""
        return await self._redis.zcard(_history_key(project_id))

    async def clear_history(self, project_id: str) -> None:
        """Delete all stored events for *project_id*.  Use with caution."""
        await self._redis.delete(_history_key(project_id))
        logger.info("event_history_cleared", project_id=project_id)

    # ------------------------------------------------------------------ #
    # Serialization helpers
    # ------------------------------------------------------------------ #

    def _serialize_event(self, event: ForgeEvent) -> str:
        """Serialize a ForgeEvent to a JSON string."""
        return event.model_dump_json()

    def _deserialize_event(self, data: str) -> ForgeEvent:
        """Deserialize a JSON string into the most specific ForgeEvent subclass."""
        try:
            raw: Dict[str, Any] = json.loads(data)
            return deserialize_event(raw)
        except Exception as exc:
            raise ValueError(f"Cannot deserialize event: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Internal listener coroutines
    # ------------------------------------------------------------------ #

    async def _listen(self, project_id: str) -> None:
        """Background coroutine: listen on the project channel and call handlers."""
        channel = _project_channel(project_id)
        pubsub: PubSub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        logger.debug("event_listener_started", project_id=project_id, channel=channel)

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                raw: str = message["data"]
                await self._dispatch_to_handlers(project_id, raw)
        except asyncio.CancelledError:
            logger.debug("event_listener_cancelled", project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "event_listener_error",
                project_id=project_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def _listen_global(self) -> None:
        """Background coroutine: listen on the global channel and call global handlers."""
        assert self._global_pubsub is not None
        logger.debug("global_event_listener_started")

        try:
            async for message in self._global_pubsub.listen():
                if message["type"] != "message":
                    continue
                raw: str = message["data"]
                try:
                    event = self._deserialize_event(raw)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("global_event_deserialize_failed", error=str(exc))
                    continue

                for handler in list(self._global_handlers):
                    try:
                        await handler(event)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "global_event_handler_error",
                            handler=getattr(handler, "__name__", repr(handler)),
                            error=str(exc),
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            logger.debug("global_event_listener_cancelled")
        except Exception as exc:  # noqa: BLE001
            logger.error("global_event_listener_error", error=str(exc), exc_info=True)
        finally:
            await self._global_pubsub.unsubscribe(EVENT_BUS_CHANNEL)
            await self._global_pubsub.aclose()

    async def _dispatch_to_handlers(self, project_id: str, raw: str) -> None:
        """Deserialize *raw* and invoke all handlers registered for *project_id*."""
        try:
            event = self._deserialize_event(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "event_deserialize_failed",
                project_id=project_id,
                error=str(exc),
            )
            return

        handlers = list(self._handlers.get(project_id, []))
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "event_handler_error",
                    project_id=project_id,
                    event_type=event.event_type,
                    handler=getattr(handler, "__name__", repr(handler)),
                    error=str(exc),
                    exc_info=True,
                )

    # ------------------------------------------------------------------ #
    # Context manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "EventBus":
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Cancel all listener tasks on exit."""
        for task in list(self._listener_tasks.values()):
            if not task.done():
                task.cancel()
        if self._global_listener_task and not self._global_listener_task.done():
            self._global_listener_task.cancel()
