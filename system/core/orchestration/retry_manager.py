"""Retry Manager for the FORGE orchestration layer.

Implements exponential back-off retry logic for failed agent tasks, with
configurable retry policies per agent type, automatic re-enqueuing via
Celery, and downstream task blocking on permanent failures.

Usage::

    retry_mgr = RetryManager(event_bus=bus, task_graph_engine=engine)
    if await retry_mgr.should_retry(task, exc):
        await retry_mgr.schedule_retry(task, exc)
    else:
        await retry_mgr.handle_permanent_failure(task, exc)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from system.core.orchestration.event_schemas import (
    RetryTriggeredEvent,
    TaskFailedEvent,
)
from system.core.orchestration.task_graph import TaskGraphEngine
from system.core.orchestration.task_schemas import TaskGraphUpdate, TaskNode
from system.observability.logging.logger import get_logger
from system.shared.constants import MAX_AGENT_RETRIES
from system.shared.exceptions import (
    AgentError,
    ExecutionError,
    OrchestrationError,
    RateLimitError,
    ToolError,
)
from system.shared.models import AgentType, Priority, TaskStatus

logger = get_logger(__name__)

# ========================================================================== #
# Retry Policy
# ========================================================================== #

# Error types that are transient and should be retried
_RETRYABLE_ERROR_TYPES: set[type[Exception]] = {
    AgentError,
    ExecutionError,
    ToolError,
    RateLimitError,
    TimeoutError,
    ConnectionError,
    OSError,
}

# Error types that are permanent and must NOT be retried
_PERMANENT_ERROR_TYPES: set[type[Exception]] = {
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
}


@dataclass
class RetryPolicy:
    """Configuration for exponential back-off retry behaviour.

    Delay formula:
        delay = min(initial_delay_s * (backoff_multiplier ** (attempt - 1)), max_delay_s)
        With jitter: delay += uniform(0, delay * jitter_factor)
    """

    max_retries: int = MAX_AGENT_RETRIES
    """Maximum number of attempts (initial + retries)."""

    initial_delay_s: float = 2.0
    """Delay before the first retry in seconds."""

    max_delay_s: float = 60.0
    """Maximum delay cap regardless of back-off multiplication."""

    backoff_multiplier: float = 2.0
    """Exponential growth factor per attempt."""

    jitter_factor: float = 0.25
    """Fraction of computed delay to add as random jitter (reduces thundering herd)."""

    retryable_errors: set[type[Exception]] = field(
        default_factory=lambda: set(_RETRYABLE_ERROR_TYPES)
    )
    """Exception classes that should trigger a retry (sub-classes match too)."""

    critical_agent_types: set[AgentType] = field(
        default_factory=lambda: {AgentType.ARCHITECT, AgentType.INFRA}
    )
    """Agent types whose permanent failures trigger alerting."""


# ---- Built-in policies ----

DEFAULT_RETRY_POLICY = RetryPolicy(
    max_retries=3,
    initial_delay_s=2.0,
    max_delay_s=60.0,
    backoff_multiplier=2.0,
    jitter_factor=0.25,
)

AGGRESSIVE_RETRY_POLICY = RetryPolicy(
    max_retries=5,
    initial_delay_s=1.0,
    max_delay_s=120.0,
    backoff_multiplier=1.5,
    jitter_factor=0.1,
)

NO_RETRY_POLICY = RetryPolicy(
    max_retries=0,
    initial_delay_s=0.0,
    max_delay_s=0.0,
    backoff_multiplier=1.0,
    jitter_factor=0.0,
)

# Per-agent-type policy overrides
_AGENT_POLICIES: dict[AgentType, RetryPolicy] = {
    AgentType.ARCHITECT: AGGRESSIVE_RETRY_POLICY,
    AgentType.BACKEND: DEFAULT_RETRY_POLICY,
    AgentType.FRONTEND: DEFAULT_RETRY_POLICY,
    AgentType.INFRA: AGGRESSIVE_RETRY_POLICY,
    AgentType.QA: DEFAULT_RETRY_POLICY,
    AgentType.SECURITY: DEFAULT_RETRY_POLICY,
    AgentType.DOCS: NO_RETRY_POLICY,
    AgentType.REFACTOR: DEFAULT_RETRY_POLICY,
}


# ========================================================================== #
# Delay calculation
# ========================================================================== #


def calculate_delay(attempt: int, policy: RetryPolicy) -> float:
    """Compute the exponential back-off delay for a given *attempt*.

    Args:
        attempt: 1-based attempt number (attempt=1 → initial_delay_s,
                 attempt=2 → initial_delay_s * multiplier, …).
        policy:  The RetryPolicy governing this task's retries.

    Returns:
        Delay in seconds (capped at policy.max_delay_s).

    Formula::
        base_delay = initial_delay_s * (backoff_multiplier ** (attempt - 1))
        capped     = min(base_delay, max_delay_s)
        # No jitter here — jitter is applied in schedule_retry to avoid
        # importing random in a pure-function context.
    """
    if attempt <= 0:
        attempt = 1
    exponent = attempt - 1
    base_delay = policy.initial_delay_s * (policy.backoff_multiplier**exponent)
    return min(base_delay, policy.max_delay_s)


def calculate_delay_with_jitter(attempt: int, policy: RetryPolicy) -> float:
    """Like calculate_delay but adds random jitter."""
    import random  # local import keeps top-level import-time side-effects minimal

    base = calculate_delay(attempt, policy)
    jitter = random.uniform(0, base * policy.jitter_factor)
    return base + jitter


# ========================================================================== #
# Retry Manager
# ========================================================================== #


class RetryManager:
    """Manages retry scheduling and permanent-failure handling for TaskNodes.

    The RetryManager is injected with the EventBus and TaskGraphEngine so it
    can emit events and update task state after each decision.
    """

    def __init__(
        self,
        event_bus: Any,  # EventBus — typed as Any to avoid circular import
        task_graph_engine: TaskGraphEngine,
        default_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._event_bus = event_bus
        self._tge = task_graph_engine
        self._default_policy = default_policy

    # ------------------------------------------------------------------ #
    # Policy resolution
    # ------------------------------------------------------------------ #

    def _get_policy(self, task: TaskNode) -> RetryPolicy:
        """Return the retry policy for *task*'s agent type."""
        agent_type = (
            AgentType(task.agent_type) if isinstance(task.agent_type, str) else task.agent_type
        )
        return _AGENT_POLICIES.get(agent_type, self._default_policy)

    # ------------------------------------------------------------------ #
    # Decision: should we retry?
    # ------------------------------------------------------------------ #

    async def should_retry(self, task: TaskNode, error: Exception) -> bool:
        """Determine whether *task* should be retried after *error*.

        Returns True only when:
        1. The error type is retryable (not a permanent / programming error).
        2. The task's retry_count has not reached max_retries.

        Args:
            task:  The failing TaskNode.
            error: The exception raised by the agent.

        Returns:
            True → schedule a retry; False → mark as permanently failed.
        """
        policy = self._get_policy(task)

        # Check retry budget
        if task.retry_count >= policy.max_retries:
            logger.info(
                "retry_budget_exhausted",
                task_id=task.task_id,
                retry_count=task.retry_count,
                max_retries=policy.max_retries,
            )
            return False

        # Check error type
        error_is_retryable = any(
            isinstance(error, err_type) for err_type in policy.retryable_errors
        )
        if not error_is_retryable:
            logger.info(
                "error_not_retryable",
                task_id=task.task_id,
                error_type=type(error).__name__,
            )
            return False

        return True

    # ------------------------------------------------------------------ #
    # Schedule a retry
    # ------------------------------------------------------------------ #

    async def schedule_retry(
        self,
        task: TaskNode,
        error: Exception,
        graph_id: str | None = None,
        celery_app: Any | None = None,
    ) -> None:
        """Increment retry_count, compute delay, publish event, and re-enqueue.

        Args:
            task:       The failing TaskNode (will be mutated in place).
            error:      The exception from the last attempt.
            graph_id:   Graph ID needed to persist the update (optional but recommended).
            celery_app: Celery application for re-queuing (if None, logs a warning).
        """
        policy = self._get_policy(task)

        # Increment retry counter on the task object
        task.retry_count += 1
        task.status = TaskStatus.RETRYING
        task.error_message = str(error)

        # Calculate delay with jitter
        delay = calculate_delay_with_jitter(task.retry_count, policy)

        logger.info(
            "retry_scheduled",
            task_id=task.task_id,
            attempt=task.retry_count,
            max_attempts=policy.max_retries,
            delay_seconds=round(delay, 2),
            error=str(error),
        )

        # Publish RetryTriggeredEvent
        event = RetryTriggeredEvent(
            project_id=task.project_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            phase=task.phase,
            attempt=task.retry_count,
            max_attempts=policy.max_retries,
            delay_seconds=delay,
            error_message=str(error),
        )
        await self._event_bus.publish(event)

        # Persist updated status if graph_id is provided
        if graph_id:
            update = TaskGraphUpdate(
                task_id=task.task_id,
                status=TaskStatus.RETRYING,
                error_message=str(error),
            )
            try:
                await self._tge.update_task_status(graph_id, task.task_id, update)
            except OrchestrationError as exc:
                logger.warning("retry_state_persist_failed", task_id=task.task_id, error=str(exc))

        # Re-enqueue via Celery with a countdown
        if celery_app is not None:
            task_data = task.model_dump(mode="json")
            try:
                celery_app.send_task(
                    "system.runtime.workers.celery_app.execute_agent_task",
                    args=[task_data],
                    countdown=int(math.ceil(delay)),
                    queue=self._resolve_queue(task),
                )
                logger.info(
                    "task_re_enqueued",
                    task_id=task.task_id,
                    countdown_s=int(math.ceil(delay)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "task_re_enqueue_failed",
                    task_id=task.task_id,
                    error=str(exc),
                    exc_info=True,
                )
        else:
            # Fallback: sleep then allow caller to re-dispatch
            logger.warning(
                "celery_app_not_provided_sleeping",
                task_id=task.task_id,
                delay_seconds=delay,
            )
            # We do NOT await asyncio.sleep here to keep retry scheduling
            # non-blocking; the caller is responsible for actual dispatch.

    # ------------------------------------------------------------------ #
    # Handle permanent failure
    # ------------------------------------------------------------------ #

    async def handle_permanent_failure(
        self,
        task: TaskNode,
        error: Exception,
        graph_id: str | None = None,
        graph: Any | None = None,  # TaskGraph — typed Any to avoid circular import
    ) -> None:
        """Mark *task* as permanently FAILED and propagate to downstream tasks.

        Steps:
        1. Update task status to FAILED.
        2. Persist update to the task graph.
        3. Publish TaskFailedEvent (is_permanent=True).
        4. Block any downstream tasks that depend on this task.
        5. Trigger alerting for critical agent types.

        Args:
            task:     The TaskNode that has permanently failed.
            error:    The final exception.
            graph_id: Graph ID for persistence.
            graph:    Full TaskGraph for downstream propagation (optional).
        """
        task.status = TaskStatus.FAILED
        task.error_message = str(error)

        logger.error(
            "task_permanent_failure",
            task_id=task.task_id,
            project_id=task.project_id,
            agent_type=task.agent_type,
            error=str(error),
            retry_count=task.retry_count,
        )

        # Persist failure state
        if graph_id:
            update = TaskGraphUpdate(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error_message=str(error),
            )
            try:
                await self._tge.update_task_status(graph_id, task.task_id, update)
            except OrchestrationError as exc:
                logger.warning("failure_state_persist_failed", task_id=task.task_id, error=str(exc))

        # Publish permanent failure event
        event = TaskFailedEvent(
            project_id=task.project_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            phase=task.phase,
            error_message=str(error),
            error_type=type(error).__name__,
            retry_count=task.retry_count,
            is_permanent=True,
        )
        await self._event_bus.publish(event)

        # Block downstream tasks
        if graph is not None:
            await self._block_downstream_tasks(task, graph, graph_id)

        # Alert for critical agents
        policy = self._get_policy(task)
        agent_type = (
            AgentType(task.agent_type) if isinstance(task.agent_type, str) else task.agent_type
        )
        if agent_type in policy.critical_agent_types:
            await self._trigger_alert(task, error)

    async def _block_downstream_tasks(
        self,
        failed_task: TaskNode,
        graph: Any,  # TaskGraph
        graph_id: str | None,
    ) -> None:
        """Mark all tasks that depend (directly or transitively) on *failed_task* as BLOCKED."""
        # Collect all directly blocked task_ids
        blocked_ids = set(failed_task.blocking)
        if not blocked_ids:
            return

        task_map = {t.task_id: t for t in graph.tasks}

        # BFS to find all transitively blocked tasks
        to_process = list(blocked_ids)
        visited: set[str] = set()

        while to_process:
            tid = to_process.pop()
            if tid in visited:
                continue
            visited.add(tid)

            blocked_task = task_map.get(tid)
            if blocked_task is None:
                continue

            # Only block if the task hasn't already completed
            if blocked_task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                blocked_task.status = TaskStatus.BLOCKED
                blocked_task.error_message = (
                    f"Blocked because dependency '{failed_task.task_id}' permanently failed."
                )

                if graph_id:
                    update = TaskGraphUpdate(
                        task_id=blocked_task.task_id,
                        status=TaskStatus.BLOCKED,
                        error_message=blocked_task.error_message,
                    )
                    try:
                        await self._tge.update_task_status(graph_id, blocked_task.task_id, update)
                    except OrchestrationError as exc:
                        logger.warning(
                            "blocked_task_persist_failed",
                            task_id=blocked_task.task_id,
                            error=str(exc),
                        )

                logger.info(
                    "task_blocked",
                    task_id=blocked_task.task_id,
                    because_of=failed_task.task_id,
                )

            # Continue BFS through this task's blocking list
            to_process.extend(blocked_task.blocking)

    async def _trigger_alert(self, task: TaskNode, error: Exception) -> None:
        """Send an alert for a critical task failure.

        In production this would integrate with PagerDuty, Slack, or Sentry.
        Currently logs at CRITICAL level and publishes a payload to the event bus.
        """
        logger.critical(
            "CRITICAL_TASK_FAILURE",
            task_id=task.task_id,
            agent_type=task.agent_type,
            project_id=task.project_id,
            error=str(error),
            retry_count=task.retry_count,
        )

        # Publish a special high-priority alert payload
        alert_event = TaskFailedEvent(
            project_id=task.project_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            phase=task.phase,
            error_message=f"[CRITICAL ALERT] {error}",
            error_type=type(error).__name__,
            retry_count=task.retry_count,
            is_permanent=True,
            payload={"alert": True, "severity": "critical"},
        )
        await self._event_bus.publish(alert_event)

    # ------------------------------------------------------------------ #
    # Queue routing helper
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_queue(task: TaskNode) -> str:
        """Map task agent_type to the appropriate Celery queue name."""
        from system.shared.constants import TASK_QUEUE_DEFAULT, TASK_QUEUE_PRIORITY

        priority = Priority(task.priority) if isinstance(task.priority, str) else task.priority
        if priority == Priority.CRITICAL:
            return TASK_QUEUE_PRIORITY

        agent_queue_map: dict[str, str] = {
            AgentType.ARCHITECT: "forge.agents.architect",
            AgentType.BACKEND: "forge.agents.backend",
            AgentType.FRONTEND: "forge.agents.frontend",
            AgentType.INFRA: "forge.agents.infra",
            AgentType.QA: "forge.agents.qa",
            AgentType.SECURITY: "forge.agents.security",
            AgentType.DOCS: "forge.agents.docs",
            AgentType.REFACTOR: "forge.agents.refactor",
        }
        agent_type = (
            AgentType(task.agent_type) if isinstance(task.agent_type, str) else task.agent_type
        )
        return agent_queue_map.get(agent_type, TASK_QUEUE_DEFAULT)
