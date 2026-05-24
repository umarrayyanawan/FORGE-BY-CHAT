"""Celery application and queue configuration for the FORGE platform.

All agent tasks, scheduled maintenance jobs, and priority tasks are routed
through the queues defined here.  Import ``celery_app`` from this module
wherever you need to send tasks or configure Celery Beat.

Usage (sending a task)::

    from system.runtime.queues.task_queue import celery_app

    result = celery_app.send_task(
        "forge.execute_agent_task",
        args=[task_data],
        queue="forge.tasks",
    )

Usage (worker startup)::

    celery -A system.runtime.queues.task_queue:celery_app worker \\
        --queues forge.tasks,forge.agents.backend \\
        --concurrency 4 --loglevel INFO
"""

from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from system.config.settings import settings
from system.shared.constants import TASK_QUEUE_DEFAULT, TASK_QUEUE_PRIORITY

# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------

celery_app = Celery(
    "forge",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Timezone
    timezone="UTC",
    enable_utc=True,
    # Reliability settings
    worker_prefetch_multiplier=1,       # fetch one task at a time — fairer scheduling
    task_acks_late=True,                # ack only after completion — prevents silent loss
    task_reject_on_worker_lost=True,    # re-queue if worker dies mid-task
    task_track_started=True,            # report STARTED state to backend
    # Result backend
    result_expires=86400,               # keep results for 24 h
    # Task routes — map task name patterns → specific queues
    task_routes={
        "forge.execute_agent_task": {"queue": TASK_QUEUE_DEFAULT},
        "forge.priority_task": {"queue": TASK_QUEUE_PRIORITY},
        "forge.agents.architect.*": {"queue": "forge.agents.architect"},
        "forge.agents.backend.*": {"queue": "forge.agents.backend"},
        "forge.agents.frontend.*": {"queue": "forge.agents.frontend"},
        "forge.agents.infra.*": {"queue": "forge.agents.infra"},
        "forge.agents.qa.*": {"queue": "forge.agents.qa"},
    },
    # Explicit queue declarations with exchange bindings
    task_queues=(
        Queue(
            TASK_QUEUE_DEFAULT,
            Exchange("forge"),
            routing_key="forge.tasks",
        ),
        Queue(
            TASK_QUEUE_PRIORITY,
            Exchange("forge.priority"),
            routing_key="forge.priority",
        ),
        Queue(
            "forge.agents.architect",
            Exchange("forge.agents"),
            routing_key="forge.agents.architect",
        ),
        Queue(
            "forge.agents.backend",
            Exchange("forge.agents"),
            routing_key="forge.agents.backend",
        ),
        Queue(
            "forge.agents.frontend",
            Exchange("forge.agents"),
            routing_key="forge.agents.frontend",
        ),
        Queue(
            "forge.agents.infra",
            Exchange("forge.agents"),
            routing_key="forge.agents.infra",
        ),
        Queue(
            "forge.agents.qa",
            Exchange("forge.agents"),
            routing_key="forge.agents.qa",
        ),
    ),
    # Default queue for tasks without explicit routing
    task_default_queue=TASK_QUEUE_DEFAULT,
    task_default_exchange="forge",
    task_default_routing_key="forge.tasks",
    # Celery Beat scheduled tasks
    beat_schedule={
        "cleanup-expired-sessions": {
            "task": "forge.cleanup_expired_sessions",
            "schedule": 3600.0,
            "options": {"queue": TASK_QUEUE_DEFAULT},
        },
        "metrics-aggregation": {
            "task": "forge.aggregate_metrics",
            "schedule": 300.0,
            "options": {"queue": TASK_QUEUE_DEFAULT},
        },
    },
)
