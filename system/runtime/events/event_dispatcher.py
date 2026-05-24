"""Event Dispatcher — in-process fan-out of ForgeEvents to registered handlers.

The EventDispatcher is the in-process complement to the Redis-backed EventBus.
While the EventBus handles cross-process pub/sub, the EventDispatcher handles
synchronous and asynchronous in-process routing to registered handler callables.

Usage::

    from system.runtime.events.event_dispatcher import dispatcher

    # Register a handler
    dispatcher.register("task_completed", my_async_handler)
    dispatcher.register_global(audit_logger)

    # Dispatch an event
    await dispatcher.dispatch(TaskCompletedEvent(project_id="abc", ...))
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List

from system.core.orchestration.event_schemas import ForgeEvent
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class EventDispatcher:
    """In-process event fan-out dispatcher.

    Maintains two registries:
    - ``_handlers``: event_type → list of callables (type-specific)
    - ``_global_handlers``: list of callables invoked for every event

    Handlers may be sync or async callables.  All handlers for a given
    ``dispatch()`` call are run concurrently via ``asyncio.gather``.
    Exceptions in individual handlers are caught, logged, and do not
    prevent other handlers from running.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable]] = {}
        self._global_handlers: List[Callable] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, event_type: str, handler: Callable) -> None:
        """Register *handler* for events of *event_type*.

        Args:
            event_type: The event_type string (e.g. "task_completed").
            handler:    Sync or async callable accepting one ForgeEvent arg.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug(
            "Handler registered",
            event_type=event_type,
            handler=getattr(handler, "__name__", repr(handler)),
        )

    def unregister(self, event_type: str, handler: Callable) -> None:
        """Remove a previously registered handler.

        No-op if the handler is not found.
        """
        handlers = self._handlers.get(event_type, [])
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    def register_global(self, handler: Callable) -> None:
        """Register *handler* to be invoked for every event regardless of type.

        Useful for audit logging, metrics collection, and debugging.

        Args:
            handler: Sync or async callable accepting one ForgeEvent arg.
        """
        self._global_handlers.append(handler)
        logger.debug(
            "Global handler registered",
            handler=getattr(handler, "__name__", repr(handler)),
        )

    def unregister_global(self, handler: Callable) -> None:
        """Remove a global handler. No-op if not found."""
        try:
            self._global_handlers.remove(handler)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, event: ForgeEvent) -> None:
        """Dispatch *event* to all registered handlers concurrently.

        Type-specific handlers (registered via ``register``) and global
        handlers (registered via ``register_global``) are all invoked.

        Individual handler exceptions are caught and logged; they do not
        propagate to the caller.

        Args:
            event: The ForgeEvent to dispatch.
        """
        specific = self._handlers.get(event.event_type, [])
        all_handlers = specific + self._global_handlers

        if not all_handlers:
            logger.debug(
                "No handlers for event type",
                event_type=event.event_type,
                event_id=event.event_id,
            )
            return

        loop = asyncio.get_event_loop()
        tasks = []
        for handler in all_handlers:
            if asyncio.iscoroutinefunction(handler):
                tasks.append(handler(event))
            else:
                # Run sync handlers in the thread pool to avoid blocking
                tasks.append(loop.run_in_executor(None, handler, event))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for handler, result in zip(all_handlers, results):
            if isinstance(result, Exception):
                handler_name = getattr(handler, "__name__", repr(handler))
                logger.error(
                    "Event handler raised an exception",
                    handler=handler_name,
                    event_type=event.event_type,
                    event_id=event.event_id,
                    error=str(result),
                )

        logger.debug(
            "Event dispatched",
            event_type=event.event_type,
            event_id=event.event_id,
            handler_count=len(all_handlers),
        )

    async def dispatch_batch(self, events: List[ForgeEvent]) -> None:
        """Dispatch a list of events sequentially.

        Events are dispatched in order.  Each event's handlers are run
        concurrently, but successive events wait for the previous to complete.

        Args:
            events: Ordered list of ForgeEvents to dispatch.
        """
        for event in events:
            await self.dispatch(event)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def handler_count(self, event_type: str) -> int:
        """Return the number of handlers registered for *event_type*."""
        return len(self._handlers.get(event_type, []))

    def global_handler_count(self) -> int:
        """Return the number of global handlers."""
        return len(self._global_handlers)

    def registered_event_types(self) -> List[str]:
        """Return all event types that have at least one handler."""
        return [k for k, v in self._handlers.items() if v]


# ---------------------------------------------------------------------------
# Module-level singleton dispatcher
# ---------------------------------------------------------------------------

dispatcher = EventDispatcher()
