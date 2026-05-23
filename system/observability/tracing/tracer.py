"""OpenTelemetry distributed tracing for the FORGE platform.

Usage
-----
Call ``setup_tracing("forge-api")`` once at application startup, then:

    from system.observability.tracing.tracer import get_tracer, trace_function

    tracer = get_tracer(__name__)

    # Manual span:
    with tracer.start_as_current_span("my_operation") as span:
        span.set_attribute("task.id", task_id)

    # Decorator:
    @trace_function("process_intent")
    async def process_intent(text: str) -> dict:
        ...
"""

from __future__ import annotations

import functools
import logging
from contextvars import ContextVar
from typing import Any, Callable, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Tracer, NonRecordingSpan, SpanContext

logger = logging.getLogger(__name__)

# ContextVar to propagate a plain string trace_id across async boundaries.
# Populated automatically from the active OTel span where available.
_current_trace_id: ContextVar[str] = ContextVar("trace_id", default="")

# Module-level provider reference (set by setup_tracing)
_provider: Optional[TracerProvider] = None


# ========================================================================== #
# Setup
# ========================================================================== #


def setup_tracing(service_name: str) -> None:
    """Initialise the OpenTelemetry tracing pipeline.

    Creates a ``TracerProvider`` with:
    - A ``BatchSpanProcessor`` exporting to the OTLP gRPC endpoint configured
      in ``settings.otel_exporter_otlp_endpoint`` (production).
    - A ``ConsoleSpanExporter`` (debug mode only) for local visibility.

    Call once at application startup before any spans are created.

    Args:
        service_name: The logical name of the service (e.g. ``"forge-api"``).
    """
    global _provider  # noqa: PLW0603

    from system.config.settings import settings  # noqa: PLC0415

    resource = Resource.create({SERVICE_NAME: service_name})

    provider = TracerProvider(resource=resource)

    # OTLP gRPC exporter → Jaeger / Tempo / Collector
    try:
        otlp_exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=True,
        )
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info(
            "OTel OTLP exporter configured: %s",
            settings.otel_exporter_otlp_endpoint,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not configure OTLP exporter: %s", exc)

    # Console exporter for development
    if settings.debug:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _provider = provider

    # Instrument asyncio automatically
    try:
        AsyncioInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.debug("AsyncioInstrumentor not available: %s", exc)

    logger.info("OpenTelemetry tracing initialised for service '%s'", service_name)


# ========================================================================== #
# Tracer factory
# ========================================================================== #


def get_tracer(name: str) -> Tracer:
    """Return an OTel ``Tracer`` for *name*.

    Safe to call before ``setup_tracing``; returns a no-op tracer in that case.

    Args:
        name: Typically ``__name__`` of the calling module.
    """
    return trace.get_tracer(name)


# ========================================================================== #
# Trace-ID helpers
# ========================================================================== #


def get_current_trace_id() -> str:
    """Return the hex trace ID of the current active OTel span.

    Falls back to the ContextVar value, then to an empty string.
    """
    span = trace.get_current_span()
    if not isinstance(span, NonRecordingSpan):
        ctx: SpanContext = span.get_span_context()
        if ctx.is_valid:
            tid = format(ctx.trace_id, "032x")
            _current_trace_id.set(tid)
            return tid
    return _current_trace_id.get()


def set_trace_id(trace_id: str) -> None:
    """Manually set the ContextVar trace ID (e.g. from an incoming header)."""
    _current_trace_id.set(trace_id)


# ========================================================================== #
# Decorator
# ========================================================================== #


def trace_function(
    span_name: Optional[str] = None,
    *,
    record_exception: bool = True,
    attributes: Optional[dict[str, Any]] = None,
) -> Callable:
    """Decorator that wraps a sync or async function in an OTel span.

    Args:
        span_name: Name for the span.  Defaults to the function's qualified name.
        record_exception: Whether to record exceptions on the span.
        attributes: Static key/value attributes to set on the span.

    Example::

        @trace_function("intent.parse")
        async def parse_intent(text: str) -> Intent:
            ...
    """

    def decorator(func: Callable) -> Callable:
        name = span_name or f"{func.__module__}.{func.__qualname__}"
        module_tracer = get_tracer(func.__module__)

        if _is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with module_tracer.start_as_current_span(name) as span:
                    _set_span_attrs(span, attributes)
                    try:
                        result = await func(*args, **kwargs)
                        return result
                    except Exception as exc:
                        if record_exception:
                            span.record_exception(exc)
                            span.set_status(
                                trace.StatusCode.ERROR, str(exc)
                            )
                        raise

            return async_wrapper

        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with module_tracer.start_as_current_span(name) as span:
                    _set_span_attrs(span, attributes)
                    try:
                        return func(*args, **kwargs)
                    except Exception as exc:
                        if record_exception:
                            span.record_exception(exc)
                            span.set_status(
                                trace.StatusCode.ERROR, str(exc)
                            )
                        raise

            return sync_wrapper

    return decorator


# ========================================================================== #
# Internal helpers
# ========================================================================== #


def _is_async(func: Callable) -> bool:
    import asyncio
    import inspect

    return asyncio.iscoroutinefunction(func) or inspect.iscoroutinefunction(func)


def _set_span_attrs(span: Any, attributes: Optional[dict[str, Any]]) -> None:
    if attributes:
        for k, v in attributes.items():
            try:
                span.set_attribute(k, v)
            except Exception:  # noqa: BLE001
                pass
