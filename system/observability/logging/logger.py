"""Structured logging configuration for the FORGE platform.

Uses ``structlog`` for JSON-structured output in production and a
human-friendly coloured console renderer in development.

Call ``setup_logging()`` once at application start-up (e.g. in the FastAPI
lifespan handler or the Celery worker boot hook), then obtain per-module
loggers with ``get_logger(name)``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

# ========================================================================== #
# Internal helpers
# ========================================================================== #


def _add_service_name(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Inject the service name into every log record."""
    event_dict.setdefault("service", "forge")
    return event_dict


def _drop_color_message_key(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Remove the ``color_message`` key Uvicorn sometimes adds."""
    event_dict.pop("color_message", None)
    return event_dict


def _extract_from_record(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Copy useful stdlib LogRecord fields into the structlog event dict."""
    record: logging.LogRecord | None = event_dict.get("_record")
    if record is not None:
        event_dict.setdefault("filename", record.filename)
        event_dict.setdefault("lineno", record.lineno)
        event_dict.setdefault("func_name", record.funcName)
    return event_dict


# ========================================================================== #
# Public API
# ========================================================================== #


def setup_logging(debug: bool | None = None) -> None:
    """Configure the ``structlog`` + ``logging`` pipeline.

    Must be called **once** before any logger is used.

    Args:
        debug: If ``None``, reads from ``settings.debug``.
               Pass ``True`` to force pretty console output,
               ``False`` to force JSON output.
    """
    # Lazy import to avoid circular imports at module level
    from system.config.settings import settings  # noqa: PLC0415

    is_debug = settings.debug if debug is None else debug

    # ------------------------------------------------------------------ #
    # Standard library logging baseline
    # ------------------------------------------------------------------ #
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # Silence noisy third-party loggers in production
    if not is_debug:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
        logging.getLogger("neo4j").setLevel(logging.WARNING)

    # ------------------------------------------------------------------ #
    # Shared processors (both dev & prod)
    # ------------------------------------------------------------------ #
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_service_name,
        _drop_color_message_key,
    ]

    if is_debug:
        # Pretty console output for local development
        renderer: Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # JSON lines for production log aggregators (Loki, Datadog, etc.)
        renderer = structlog.processors.JSONRenderer()

    # ------------------------------------------------------------------ #
    # Wire structlog to stdlib logging
    # ------------------------------------------------------------------ #
    structlog.configure(
        processors=[
            *shared_processors,
            # Must be last before the renderer when using stdlib integration
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Attach a stdlib formatter that uses the structlog pipeline
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            _extract_from_record,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog ``BoundLogger`` for *name*.

    Usage::

        from system.observability.logging.logger import get_logger

        log = get_logger(__name__)
        log.info("task_started", task_id="abc-123", agent="backend")
    """
    return structlog.get_logger(name)


def bind_trace_id(trace_id: str) -> None:
    """Bind a trace ID into the current structlog context-var scope.

    Call this at the top of a request handler or Celery task so all log
    lines emitted during that scope automatically include the trace ID.
    """
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def clear_trace_id() -> None:
    """Clear all bound context vars (call at end of request/task)."""
    structlog.contextvars.clear_contextvars()


# ========================================================================== #
# Module-level logger
# ========================================================================== #

logger = get_logger(__name__)
