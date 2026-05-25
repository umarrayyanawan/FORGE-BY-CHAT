"""Prometheus metrics collection for the FORGE platform.

Defines all application-level metrics and exposes a WSGI application for
the ``/metrics`` scrape endpoint.

Usage
-----
Instrument code::

    from system.observability.metrics.collector import (
        TASKS_TOTAL, TASK_DURATION, ACTIVE_TASKS
    )

    TASKS_TOTAL.labels(status="completed", agent_type="backend").inc()

    with TASK_DURATION.labels(agent_type="backend").time():
        await run_agent(...)

Expose the endpoint (add to FastAPI app)::

    from system.observability.metrics.collector import get_metrics_app

    app.mount("/metrics", get_metrics_app())
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import time

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,
)

# ========================================================================== #
# Registry
# ========================================================================== #

# Use a dedicated registry so tests can isolate metrics state.
REGISTRY = CollectorRegistry(auto_describe=True)


# ========================================================================== #
# Metric definitions
# ========================================================================== #

TASKS_TOTAL: Counter = Counter(
    name="forge_tasks_total",
    documentation="Total number of FORGE tasks processed.",
    labelnames=["status", "agent_type"],
    registry=REGISTRY,
)

TASK_DURATION: Histogram = Histogram(
    name="forge_task_duration_seconds",
    documentation="Wall-clock time spent executing a single FORGE task.",
    labelnames=["agent_type"],
    buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0),
    registry=REGISTRY,
)

ACTIVE_TASKS: Gauge = Gauge(
    name="forge_active_tasks",
    documentation="Number of tasks currently running.",
    labelnames=["agent_type"],
    registry=REGISTRY,
)

LLM_TOKENS_TOTAL: Counter = Counter(
    name="forge_llm_tokens_total",
    documentation="Total LLM tokens consumed.",
    labelnames=["model", "type"],  # type: input | output
    registry=REGISTRY,
)

ERRORS_TOTAL: Counter = Counter(
    name="forge_errors_total",
    documentation="Total number of application errors by error code.",
    labelnames=["error_code"],
    registry=REGISTRY,
)

AGENT_RETRIES_TOTAL: Counter = Counter(
    name="forge_agent_retries_total",
    documentation="Total number of agent task retries.",
    labelnames=["agent_type"],
    registry=REGISTRY,
)

LLM_REQUEST_DURATION: Histogram = Histogram(
    name="forge_llm_request_duration_seconds",
    documentation="Latency of LLM API requests.",
    labelnames=["model"],
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)

DEPLOYMENT_TOTAL: Counter = Counter(
    name="forge_deployments_total",
    documentation="Total deployments attempted.",
    labelnames=["target", "status"],
    registry=REGISTRY,
)

MEMORY_OPERATIONS_TOTAL: Counter = Counter(
    name="forge_memory_operations_total",
    documentation="Total memory subsystem operations.",
    labelnames=["operation", "status"],  # operation: read|write|search
    registry=REGISTRY,
)


# ========================================================================== #
# Context managers / helpers
# ========================================================================== #


@contextmanager
def track_task(agent_type: str) -> Generator[None, None, None]:
    """Context manager that instruments a task execution.

    Increments ``ACTIVE_TASKS`` on entry, decrements on exit, records
    duration in ``TASK_DURATION``, and increments ``TASKS_TOTAL`` with
    the appropriate status (``completed`` or ``failed``).

    Usage::

        with track_task("backend"):
            await run_backend_agent(task)
    """
    ACTIVE_TASKS.labels(agent_type=agent_type).inc()
    start = time.perf_counter()
    status = "completed"
    try:
        yield
    except Exception:
        status = "failed"
        raise
    finally:
        duration = time.perf_counter() - start
        TASK_DURATION.labels(agent_type=agent_type).observe(duration)
        ACTIVE_TASKS.labels(agent_type=agent_type).dec()
        TASKS_TOTAL.labels(status=status, agent_type=agent_type).inc()


def record_llm_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_seconds: float,
) -> None:
    """Record LLM token counts and request latency."""
    LLM_TOKENS_TOTAL.labels(model=model, type="input").inc(input_tokens)
    LLM_TOKENS_TOTAL.labels(model=model, type="output").inc(output_tokens)
    LLM_REQUEST_DURATION.labels(model=model).observe(duration_seconds)


def record_error(error_code: str) -> None:
    """Increment the error counter for *error_code*."""
    ERRORS_TOTAL.labels(error_code=error_code).inc()


# ========================================================================== #
# ASGI metrics endpoint
# ========================================================================== #


def get_metrics_app():
    """Return an ASGI application that serves the Prometheus /metrics page.

    Mount in FastAPI::

        app.mount("/metrics", get_metrics_app())

    Or serve standalone with Uvicorn::

        uvicorn system.observability.metrics.collector:metrics_app
    """
    return make_asgi_app(registry=REGISTRY)


# Standalone ASGI app reference
metrics_app = get_metrics_app()
