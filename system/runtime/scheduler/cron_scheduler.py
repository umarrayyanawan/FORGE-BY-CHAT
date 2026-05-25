"""Celery Beat scheduled task configuration for the FORGE platform.

Defines all recurring background jobs and installs them into the Celery Beat
schedule.  Import this module in the worker entry point to ensure Beat picks
up the full schedule.

Usage (Beat startup)::

    celery -A system.runtime.scheduler.cron_scheduler:celery_app beat \\
        --loglevel INFO --scheduler celery.beat:PersistentScheduler

Alternatively, run Beat alongside a worker::

    celery -A system.runtime.scheduler.cron_scheduler:celery_app worker \\
        --beat --queues forge.tasks --loglevel INFO
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from celery.schedules import crontab

from system.runtime.queues.task_queue import celery_app

# ---------------------------------------------------------------------------
# ScheduledTask descriptor
# ---------------------------------------------------------------------------


@dataclass
class ScheduledTask:
    """Descriptor for a single Beat-scheduled task.

    Attributes:
        name:     Unique Beat entry name (used as the dict key).
        task:     Celery task name (must match the ``name=`` in ``@celery_app.task``).
        schedule: Run frequency — either a float (seconds between runs) or a
                  ``celery.schedules.crontab`` instance for calendar-based scheduling.
        args:     Positional arguments passed to the task.
        kwargs:   Keyword arguments passed to the task.
        queue:    Queue to route the task to when Beat dispatches it.
    """

    name: str
    task: str
    schedule: Any  # float | crontab
    args: tuple = field(default_factory=tuple)
    kwargs: dict[str, Any] = field(default_factory=dict)
    queue: str = "forge.tasks"


# ---------------------------------------------------------------------------
# Master schedule definition
# ---------------------------------------------------------------------------

SCHEDULED_TASKS = [
    # ---- Infrastructure maintenance ------------------------------------
    ScheduledTask(
        name="cleanup-sessions",
        task="forge.cleanup_expired_sessions",
        schedule=3600.0,  # every hour
        queue="forge.tasks",
    ),
    # ---- Observability -------------------------------------------------
    ScheduledTask(
        name="metrics-aggregation",
        task="forge.aggregate_metrics",
        schedule=300.0,  # every 5 minutes
        queue="forge.tasks",
    ),
    # ---- Liveness ------------------------------------------------------
    ScheduledTask(
        name="health-check",
        task="forge.health_check",
        schedule=30.0,  # every 30 seconds
        queue="forge.tasks",
    ),
    # ---- Nightly consolidation -----------------------------------------
    ScheduledTask(
        name="daily-memory-consolidation",
        task="forge.consolidate_memory",
        schedule=crontab(hour=2, minute=0),  # 02:00 UTC daily
        queue="forge.tasks",
    ),
]


# ---------------------------------------------------------------------------
# Beat schedule builder
# ---------------------------------------------------------------------------


def get_beat_schedule() -> dict[str, dict[str, Any]]:
    """Convert SCHEDULED_TASKS into a Celery Beat schedule dict.

    The returned dict is suitable for assignment to
    ``celery_app.conf.beat_schedule``.

    Returns:
        Dict mapping Beat entry name → task configuration dict.
    """
    return {
        task.name: {
            "task": task.task,
            "schedule": task.schedule,
            "args": task.args,
            "kwargs": task.kwargs,
            "options": {"queue": task.queue},
        }
        for task in SCHEDULED_TASKS
    }


# ---------------------------------------------------------------------------
# Install schedule into Celery Beat
# ---------------------------------------------------------------------------

# Override whatever task_queue.py defined — this module owns the schedule
celery_app.conf.beat_schedule = get_beat_schedule()
