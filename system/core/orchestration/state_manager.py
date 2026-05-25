"""State Manager — Redis-backed execution state for the FORGE orchestration layer.

Maintains per-project execution state in a Redis hash so the WorkflowEngine can
track task progress, phase transitions, and readiness without hitting Postgres
on the hot path.

Redis key: FORGE:STATE:{project_id}

Usage::

    state_mgr = StateManager(redis=await get_redis())
    await state_mgr.initialize_state(project_id, graph)
    await state_mgr.mark_task_running(project_id, task_id)
    ready = await state_mgr.get_ready_tasks(project_id, graph)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from system.core.orchestration.task_schemas import TaskGraph, TaskNode
from system.observability.logging.logger import get_logger
from system.shared.models import ExecutionPhase, TaskStatus

logger = get_logger(__name__)

# Redis hash field names
_FIELD_PHASE = "phase"
_FIELD_ACTIVE = "active_tasks"
_FIELD_COMPLETED = "completed_tasks"
_FIELD_FAILED = "failed_tasks"
_FIELD_BLOCKED = "blocked_tasks"
_FIELD_ARTIFACTS = "task_artifacts"
_FIELD_ERRORS = "task_errors"
_FIELD_METADATA = "metadata"

# Key pattern
_STATE_KEY_PREFIX = "FORGE:STATE:"
_STATE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _state_key(project_id: str) -> str:
    return f"{_STATE_KEY_PREFIX}{project_id}"


# ---------------------------------------------------------------------------
# ExecutionState dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExecutionState:
    """Snapshot of a project's execution state."""

    project_id: str
    phase: ExecutionPhase = ExecutionPhase.INTENT
    active_tasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    blocked_tasks: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_redis_dict(self) -> dict[str, str]:
        """Serialise to a flat dict suitable for HSET."""
        return {
            _FIELD_PHASE: self.phase if isinstance(self.phase, str) else self.phase.value,
            _FIELD_ACTIVE: json.dumps(self.active_tasks),
            _FIELD_COMPLETED: json.dumps(self.completed_tasks),
            _FIELD_FAILED: json.dumps(self.failed_tasks),
            _FIELD_BLOCKED: json.dumps(self.blocked_tasks),
            _FIELD_METADATA: json.dumps(self.metadata),
        }

    @classmethod
    def from_redis_dict(cls, project_id: str, data: dict[str, str]) -> ExecutionState:
        """Deserialise from a flat dict returned by HGETALL."""
        return cls(
            project_id=project_id,
            phase=ExecutionPhase(data.get(_FIELD_PHASE, ExecutionPhase.INTENT.value)),
            active_tasks=json.loads(data.get(_FIELD_ACTIVE, "[]")),
            completed_tasks=json.loads(data.get(_FIELD_COMPLETED, "[]")),
            failed_tasks=json.loads(data.get(_FIELD_FAILED, "[]")),
            blocked_tasks=json.loads(data.get(_FIELD_BLOCKED, "[]")),
            metadata=json.loads(data.get(_FIELD_METADATA, "{}")),
        )


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------


class StateManager:
    """Redis-backed per-project execution state manager."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def initialize_state(self, project_id: str, graph: TaskGraph) -> None:
        """Create a fresh ExecutionState for a project in Redis.

        All tasks start as PENDING; no active / completed / failed tasks.
        Overwrites any existing state for the project.
        """
        initial = ExecutionState(
            project_id=project_id,
            phase=ExecutionPhase.EXECUTION,
            metadata={
                "graph_id": graph.graph_id,
                "total_tasks": len(graph.tasks),
            },
        )
        key = _state_key(project_id)
        await self._redis.hset(key, mapping=initial.to_redis_dict())
        await self._redis.expire(key, _STATE_TTL_SECONDS)
        logger.info(
            "Execution state initialised",
            project_id=project_id,
            graph_id=graph.graph_id,
            total_tasks=len(graph.tasks),
        )

    # ------------------------------------------------------------------
    # State retrieval
    # ------------------------------------------------------------------

    async def get_state(self, project_id: str) -> ExecutionState:
        """Load ExecutionState from Redis.

        Returns a blank state if no record exists (first-time access).
        """
        key = _state_key(project_id)
        data: dict[str, str] = await self._redis.hgetall(key)
        if not data:
            logger.warning(
                "No execution state found — returning blank state",
                project_id=project_id,
            )
            return ExecutionState(project_id=project_id)
        # Redis may return bytes; decode if necessary
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in data.items()
        }
        return ExecutionState.from_redis_dict(project_id, decoded)

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    async def set_phase(self, project_id: str, phase: ExecutionPhase) -> None:
        """Update the execution phase for *project_id*."""
        key = _state_key(project_id)
        phase_value = phase if isinstance(phase, str) else phase.value
        await self._redis.hset(key, _FIELD_PHASE, phase_value)
        logger.info("Phase updated", project_id=project_id, phase=phase_value)

    # ------------------------------------------------------------------
    # Task lifecycle mutations
    # ------------------------------------------------------------------

    async def mark_task_running(self, project_id: str, task_id: str) -> None:
        """Move *task_id* to the active_tasks list."""
        state = await self.get_state(project_id)
        # Remove from any other list
        for lst in (state.completed_tasks, state.failed_tasks, state.blocked_tasks):
            if task_id in lst:
                lst.remove(task_id)
        if task_id not in state.active_tasks:
            state.active_tasks.append(task_id)
        await self._save_lists(project_id, state)
        logger.debug("Task marked running", project_id=project_id, task_id=task_id)

    async def mark_task_completed(
        self,
        project_id: str,
        task_id: str,
        artifacts: list[str],
    ) -> None:
        """Move *task_id* from active → completed and record artifacts."""
        state = await self.get_state(project_id)
        if task_id in state.active_tasks:
            state.active_tasks.remove(task_id)
        for lst in (state.failed_tasks, state.blocked_tasks):
            if task_id in lst:
                lst.remove(task_id)
        if task_id not in state.completed_tasks:
            state.completed_tasks.append(task_id)
        await self._save_lists(project_id, state)

        # Store artifacts separately
        if artifacts:
            key = _state_key(project_id)
            artifacts_field = f"artifacts:{task_id}"
            await self._redis.hset(key, artifacts_field, json.dumps(artifacts))

        logger.debug(
            "Task marked completed",
            project_id=project_id,
            task_id=task_id,
            artifacts=len(artifacts),
        )

    async def mark_task_failed(
        self,
        project_id: str,
        task_id: str,
        error: str,
    ) -> None:
        """Move *task_id* from active → failed and record error message."""
        state = await self.get_state(project_id)
        if task_id in state.active_tasks:
            state.active_tasks.remove(task_id)
        for lst in (state.completed_tasks, state.blocked_tasks):
            if task_id in lst:
                lst.remove(task_id)
        if task_id not in state.failed_tasks:
            state.failed_tasks.append(task_id)
        await self._save_lists(project_id, state)

        # Store error
        key = _state_key(project_id)
        await self._redis.hset(key, f"error:{task_id}", error)
        logger.warning(
            "Task marked failed",
            project_id=project_id,
            task_id=task_id,
            error=error[:200],
        )

    async def mark_task_blocked(self, project_id: str, task_id: str) -> None:
        """Move *task_id* to blocked_tasks."""
        state = await self.get_state(project_id)
        for lst in (state.active_tasks, state.completed_tasks, state.failed_tasks):
            if task_id in lst:
                lst.remove(task_id)
        if task_id not in state.blocked_tasks:
            state.blocked_tasks.append(task_id)
        await self._save_lists(project_id, state)
        logger.warning("Task blocked", project_id=project_id, task_id=task_id)

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    async def get_ready_tasks(self, project_id: str, graph: TaskGraph) -> list[TaskNode]:
        """Return tasks whose dependencies are all completed and are still PENDING.

        A task is ready when:
        - All its declared dependencies are in completed_tasks
        - It is not currently active, completed, failed, or blocked
        """
        state = await self.get_state(project_id)
        completed_set = set(state.completed_tasks)
        in_flight = (
            set(state.active_tasks)
            | set(state.completed_tasks)
            | set(state.failed_tasks)
            | set(state.blocked_tasks)
        )
        ready: list[TaskNode] = []
        for task in graph.tasks:
            if task.task_id in in_flight:
                continue
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}:
                continue
            if task.can_start(completed_set):
                ready.append(task)

        logger.debug(
            "Ready tasks computed",
            project_id=project_id,
            ready_count=len(ready),
        )
        return ready

    # ------------------------------------------------------------------
    # Phase completion
    # ------------------------------------------------------------------

    async def is_phase_complete(
        self,
        project_id: str,
        graph: TaskGraph,
        phase: ExecutionPhase,
    ) -> bool:
        """Return True when all tasks in *phase* are completed or failed.

        A phase is complete when no task in that phase is still pending/active.
        """
        state = await self.get_state(project_id)
        terminal = set(state.completed_tasks) | set(state.failed_tasks) | set(state.blocked_tasks)

        phase_tasks = graph.tasks_by_phase(phase)
        if not phase_tasks:
            return True

        return all(t.task_id in terminal for t in phase_tasks)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    async def get_progress(self, project_id: str, graph: TaskGraph) -> dict[str, Any]:
        """Return a progress summary dict.

        Keys: total, completed, failed, active, blocked, percent_complete.
        """
        state = await self.get_state(project_id)
        total = len(graph.tasks)
        completed = len(state.completed_tasks)
        failed = len(state.failed_tasks)
        active = len(state.active_tasks)
        blocked = len(state.blocked_tasks)
        pct = round((completed / total) * 100, 2) if total else 0.0

        return {
            "project_id": project_id,
            "phase": state.phase if isinstance(state.phase, str) else state.phase.value,
            "total": total,
            "completed": completed,
            "failed": failed,
            "active": active,
            "blocked": blocked,
            "pending": total - completed - failed - active - blocked,
            "percent_complete": pct,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup_state(self, project_id: str) -> None:
        """Delete all Redis state for *project_id*."""
        key = _state_key(project_id)
        await self._redis.delete(key)
        logger.info("Execution state cleaned up", project_id=project_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _save_lists(self, project_id: str, state: ExecutionState) -> None:
        """Persist the mutable list fields of state back to Redis."""
        key = _state_key(project_id)
        await self._redis.hset(
            key,
            mapping={
                _FIELD_ACTIVE: json.dumps(state.active_tasks),
                _FIELD_COMPLETED: json.dumps(state.completed_tasks),
                _FIELD_FAILED: json.dumps(state.failed_tasks),
                _FIELD_BLOCKED: json.dumps(state.blocked_tasks),
            },
        )
