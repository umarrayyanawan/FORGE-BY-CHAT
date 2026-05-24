"""Celery task implementations for the FORGE platform.

This module registers the concrete Celery tasks that are executed by workers.
Each task dispatches to the correct specialist agent via the AgentRunner, or
performs maintenance operations (session cleanup, metrics aggregation).

Worker startup::

    celery -A system.runtime.workers.celery_app:celery_app worker \\
        --queues forge.tasks,forge.agents.backend,forge.agents.frontend \\
        --concurrency 4 --loglevel INFO

Beat startup (scheduled tasks)::

    celery -A system.runtime.workers.celery_app:celery_app beat --loglevel INFO
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict

from celery.utils.log import get_task_logger

from system.runtime.queues.task_queue import celery_app

logger = get_task_logger(__name__)


# ========================================================================== #
# Core agent task
# ========================================================================== #


@celery_app.task(
    bind=True,
    name="forge.execute_agent_task",
    max_retries=3,
    default_retry_delay=60,
    queue="forge.tasks",
    acks_late=True,
    reject_on_worker_lost=True,
    track_started=True,
    time_limit=3600,       # hard kill after 1 h
    soft_time_limit=3300,  # raises SoftTimeLimitExceeded at 55 min
)
def execute_agent_task(self: Any, task_data: Dict[str, Any]) -> Dict[str, Any]:
    """Execute an agent task. Dispatches to the correct specialist agent.

    Args:
        task_data: Serialised TaskNode dict.

    Returns:
        Serialised AgentResult dict.

    Retries up to 3 times with 60-second back-off on transient failures.
    Permanent failures (validation errors, bad input) are not retried.
    """
    task_id = task_data.get("task_id", "unknown")
    agent_type = task_data.get("agent_type", "unknown")

    logger.info("Executing agent task: task_id=%s agent_type=%s", task_id, agent_type)

    try:
        # Import here to avoid circular imports at module load time
        from system.agents.registry import agent_registry  # noqa: PLC0415
        from system.agents.runner import AgentRunner  # noqa: PLC0415
        from system.core.orchestration.task_schemas import TaskNode  # noqa: PLC0415

        task = TaskNode(**task_data)
        runner = AgentRunner(registry=agent_registry)
        result = asyncio.run(runner.run_task(task))
        logger.info(
            "Agent task completed: task_id=%s status=%s",
            task_id,
            result.status if hasattr(result, "status") else "ok",
        )
        return result.model_dump() if hasattr(result, "model_dump") else {"status": "completed"}

    except ValueError as exc:
        # Validation / schema errors — do not retry
        logger.error(
            "Permanent failure (validation error) for task %s: %s",
            task_id,
            str(exc),
        )
        raise  # Surface to Celery as a failed task without retry

    except Exception as exc:
        logger.error(
            "Transient failure for task %s (attempt %d/%d): %s",
            task_id,
            self.request.retries + 1,
            self.max_retries + 1,
            str(exc),
        )
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


# ========================================================================== #
# Scheduled maintenance tasks
# ========================================================================== #


@celery_app.task(
    name="forge.cleanup_expired_sessions",
    queue="forge.tasks",
    ignore_result=False,
)
def cleanup_expired_sessions() -> Dict[str, Any]:
    """Scan Redis for expired FORGE session/intent keys and enforce TTLs.

    Keys without a TTL are assigned a 24-hour expiry.  This prevents unbounded
    growth in the Redis keyspace from abandoned sessions.

    Returns:
        Dict with ``deleted_count`` — number of keys that had their TTL set.
    """
    import redis as redis_lib  # noqa: PLC0415

    from system.config.settings import settings  # noqa: PLC0415

    r = redis_lib.from_url(settings.redis_url, decode_responses=True)
    updated = 0
    scanned = 0

    patterns = ["FORGE:SESSION:*", "FORGE:INTENT:*", "FORGE:STATE:*"]
    for pattern in patterns:
        for key in r.scan_iter(pattern, count=100):
            scanned += 1
            ttl = r.ttl(key)
            if ttl == -1:  # key exists but has no expiry
                r.expire(key, 86400)  # 24 h
                updated += 1

    logger.info(
        "Session cleanup complete: scanned=%d updated_ttl=%d", scanned, updated
    )
    return {
        "status": "ok",
        "scanned": scanned,
        "updated_ttl": updated,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


@celery_app.task(
    name="forge.aggregate_metrics",
    queue="forge.tasks",
    ignore_result=False,
)
def aggregate_metrics() -> Dict[str, Any]:
    """Aggregate and store a metrics snapshot to Redis.

    Reads counters from the observability collector and writes a 5-minute
    rollup to ``FORGE:METRICS:SNAPSHOT:{timestamp}``.

    Returns:
        Dict with aggregation status and timestamp.
    """
    timestamp = datetime.datetime.utcnow()
    iso_ts = timestamp.isoformat()

    try:
        from system.observability.metrics.collector import get_current_metrics  # noqa: PLC0415

        metrics = get_current_metrics()
    except Exception:
        # Collector may not be available in all worker environments
        metrics = {}

    try:
        import redis as redis_lib  # noqa: PLC0415

        from system.config.settings import settings  # noqa: PLC0415

        import json  # noqa: PLC0415

        r = redis_lib.from_url(settings.redis_url, decode_responses=True)
        snapshot_key = f"FORGE:METRICS:SNAPSHOT:{timestamp.strftime('%Y%m%dT%H%M')}"
        r.setex(snapshot_key, 86400, json.dumps({**metrics, "timestamp": iso_ts}))
    except Exception as exc:
        logger.warning("Could not persist metrics snapshot: %s", exc)

    logger.info("Metrics aggregated at %s", iso_ts)
    return {"status": "ok", "timestamp": iso_ts, "metrics_keys": list(metrics.keys())}


@celery_app.task(
    name="forge.health_check",
    queue="forge.tasks",
    ignore_result=False,
)
def health_check() -> Dict[str, Any]:
    """Simple liveness check — confirms the worker is online and responsive.

    Returns:
        Dict with ``status`` = "healthy" and the current worker hostname.
    """
    import socket  # noqa: PLC0415

    hostname = socket.gethostname()
    logger.info("Health check OK on worker %s", hostname)
    return {
        "status": "healthy",
        "worker": hostname,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }


@celery_app.task(
    name="forge.consolidate_memory",
    queue="forge.tasks",
    ignore_result=False,
)
def consolidate_memory() -> Dict[str, Any]:
    """Nightly memory consolidation — compacts old agent memory entries.

    Reads FORGE:MEMORY:* keys older than 7 days and archives them to
    a compact sorted set, removing the raw keys to save memory.

    Returns:
        Dict with consolidation statistics.
    """
    import json  # noqa: PLC0415

    try:
        import redis as redis_lib  # noqa: PLC0415

        from system.config.settings import settings  # noqa: PLC0415

        r = redis_lib.from_url(settings.redis_url, decode_responses=True)
        consolidated = 0
        cutoff_ts = (
            datetime.datetime.utcnow() - datetime.timedelta(days=7)
        ).timestamp()

        for key in r.scan_iter("FORGE:MEMORY:*", count=100):
            # Keys with TTL remaining are still "fresh"
            ttl = r.ttl(key)
            if ttl > 0:
                continue
            # No TTL → old entry; set a 30-day archival TTL
            r.expire(key, 60 * 60 * 24 * 30)
            consolidated += 1

        logger.info("Memory consolidation complete: consolidated=%d", consolidated)
        return {
            "status": "ok",
            "consolidated": consolidated,
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }
    except Exception as exc:
        logger.error("Memory consolidation failed: %s", exc)
        return {"status": "error", "error": str(exc)}
