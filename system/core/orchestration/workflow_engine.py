"""Workflow Engine — task dispatch and phase transition orchestration.

The WorkflowEngine drives the FORGE autonomous pipeline:
1. Dispatches ready tasks to Celery workers via agent-specific queues.
2. Reacts to TaskCompletedEvent / TaskFailedEvent to advance the DAG.
3. Advances phase transitions when all tasks in a phase are terminal.
4. Delegates retry logic to RetryManager.
5. Emits PhaseCompletedEvent on every phase advance.

Usage::

    engine = WorkflowEngine(
        task_graph_engine=tge,
        state_manager=state_mgr,
        event_bus=bus,
        retry_manager=retry_mgr,
        celery_app=celery_app,
    )
    await engine.start_workflow(project_id, graph)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from system.core.orchestration.event_schemas import (
    PhaseCompletedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
)
from system.core.orchestration.retry_manager import RetryManager
from system.core.orchestration.state_manager import StateManager
from system.core.orchestration.task_graph import TaskGraphEngine
from system.core.orchestration.task_schemas import TaskGraph, TaskNode
from system.observability.logging.logger import get_logger
from system.shared.exceptions import OrchestrationError
from system.shared.models import ExecutionPhase, TaskStatus

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Phase transition map
# ---------------------------------------------------------------------------

PHASE_TRANSITIONS: Dict[ExecutionPhase, Optional[ExecutionPhase]] = {
    ExecutionPhase.INTENT: ExecutionPhase.CLARIFICATION,
    ExecutionPhase.CLARIFICATION: ExecutionPhase.SPECIFICATION,
    ExecutionPhase.SPECIFICATION: ExecutionPhase.ARCHITECTURE,
    ExecutionPhase.ARCHITECTURE: ExecutionPhase.TASK_GRAPH,
    ExecutionPhase.TASK_GRAPH: ExecutionPhase.AGENT_ASSIGNMENT,
    ExecutionPhase.AGENT_ASSIGNMENT: ExecutionPhase.EXECUTION,
    ExecutionPhase.EXECUTION: ExecutionPhase.VERIFICATION,
    ExecutionPhase.VERIFICATION: ExecutionPhase.DEPLOYMENT,
    ExecutionPhase.DEPLOYMENT: ExecutionPhase.MONITORING,
    ExecutionPhase.MONITORING: ExecutionPhase.ITERATION,
    ExecutionPhase.ITERATION: None,  # terminal — pipeline complete
}

# Celery queue per agent type
_AGENT_QUEUE_MAP: Dict[str, str] = {
    "architect": "forge.agents.architect",
    "backend": "forge.agents.backend",
    "frontend": "forge.agents.frontend",
    "infra": "forge.agents.infra",
    "qa": "forge.agents.qa",
    "security": "forge.agents.backend",  # security tasks go to backend workers
    "docs": "forge.agents.backend",
    "refactor": "forge.agents.backend",
}


class WorkflowEngine:
    """Drives the FORGE multi-agent execution pipeline.

    Responsibilities:
    - Start workflows (initialise state, dispatch initial ready tasks).
    - React to completed / failed task events.
    - Advance pipeline phases.
    - Dispatch tasks to the correct Celery queue.
    - Support pause / resume / abort lifecycle control.
    """

    def __init__(
        self,
        task_graph_engine: TaskGraphEngine,
        state_manager: StateManager,
        event_bus: Any,  # EventBus — typed Any to avoid circular import
        retry_manager: RetryManager,
        celery_app: Any,  # Celery — typed Any to keep import optional
    ) -> None:
        self._tge = task_graph_engine
        self._state = state_manager
        self._bus = event_bus
        self._retry = retry_manager
        self._celery = celery_app
        self._paused_projects: set[str] = set()

    # ------------------------------------------------------------------
    # Workflow lifecycle
    # ------------------------------------------------------------------

    async def start_workflow(self, project_id: str, graph: TaskGraph) -> None:
        """Initialise execution state and dispatch the first wave of ready tasks.

        Args:
            project_id: FORGE project identifier.
            graph:       Fully-built TaskGraph for the execution phase.
        """
        logger.info(
            "Starting workflow",
            project_id=project_id,
            graph_id=graph.graph_id,
            total_tasks=len(graph.tasks),
        )

        # Step 1: Initialise Redis state
        await self._state.initialize_state(project_id, graph)

        # Step 2: Set phase to EXECUTION
        await self._state.set_phase(project_id, ExecutionPhase.EXECUTION)

        # Step 3: Find immediately ready tasks (no dependencies)
        ready = await self._state.get_ready_tasks(project_id, graph)

        if not ready:
            logger.warning(
                "No ready tasks at workflow start — graph may have unresolvable dependencies",
                project_id=project_id,
            )
            return

        # Step 4: Dispatch all ready tasks
        dispatch_results = await asyncio.gather(
            *[self.dispatch_task(task) for task in ready],
            return_exceptions=True,
        )
        for task, result in zip(ready, dispatch_results):
            if isinstance(result, Exception):
                logger.error(
                    "Failed to dispatch task at workflow start",
                    project_id=project_id,
                    task_id=task.task_id,
                    error=str(result),
                )
            else:
                await self._state.mark_task_running(project_id, task.task_id)
                logger.info(
                    "Task dispatched",
                    project_id=project_id,
                    task_id=task.task_id,
                    celery_id=result,
                )

    async def pause_workflow(self, project_id: str) -> None:
        """Pause dispatching for *project_id*.

        In-flight tasks continue to completion; new tasks are not dispatched.
        """
        self._paused_projects.add(project_id)
        logger.info("Workflow paused", project_id=project_id)

    async def resume_workflow(self, project_id: str) -> None:
        """Resume dispatching for *project_id*.

        After resuming, any ready tasks are immediately dispatched.
        """
        self._paused_projects.discard(project_id)
        logger.info("Workflow resumed", project_id=project_id)
        # Re-load graph and dispatch ready tasks
        # (In production, graph_id would be looked up from state metadata)
        state = await self._state.get_state(project_id)
        graph_id: Optional[str] = state.metadata.get("graph_id")
        if graph_id:
            try:
                graph = await self._tge.load_graph(graph_id)
                ready = await self._state.get_ready_tasks(project_id, graph)
                for task in ready:
                    celery_id = await self.dispatch_task(task)
                    await self._state.mark_task_running(project_id, task.task_id)
                    logger.info(
                        "Resumed task dispatched",
                        project_id=project_id,
                        task_id=task.task_id,
                        celery_id=celery_id,
                    )
            except Exception as exc:
                logger.error(
                    "Failed to re-dispatch tasks on resume",
                    project_id=project_id,
                    error=str(exc),
                )

    async def abort_workflow(self, project_id: str) -> None:
        """Abort all execution for *project_id* and clean up state."""
        self._paused_projects.discard(project_id)
        await self._state.cleanup_state(project_id)
        logger.info("Workflow aborted and state cleaned up", project_id=project_id)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_task_completed(self, event: TaskCompletedEvent) -> None:
        """React to a TaskCompletedEvent.

        1. Mark task completed in state.
        2. Find newly unblocked tasks.
        3. Dispatch ready tasks (if workflow is not paused).
        4. Advance phase if the current phase is fully complete.
        """
        project_id = event.project_id
        task_id = event.task_id

        if not task_id:
            logger.warning("TaskCompletedEvent missing task_id", event_id=event.event_id)
            return

        await self._state.mark_task_completed(
            project_id, task_id, event.output_artifacts
        )
        logger.info(
            "Task completed",
            project_id=project_id,
            task_id=task_id,
            artifacts=len(event.output_artifacts),
        )

        # Load graph from state metadata
        state = await self._state.get_state(project_id)
        graph_id: Optional[str] = state.metadata.get("graph_id")
        if not graph_id:
            logger.warning("No graph_id in state metadata", project_id=project_id)
            return

        graph = await self._tge.load_graph(graph_id)

        # Dispatch newly ready tasks
        if project_id not in self._paused_projects:
            newly_ready = await self._state.get_ready_tasks(project_id, graph)
            for task in newly_ready:
                try:
                    celery_id = await self.dispatch_task(task)
                    await self._state.mark_task_running(project_id, task.task_id)
                    logger.info(
                        "Newly ready task dispatched",
                        project_id=project_id,
                        task_id=task.task_id,
                        celery_id=celery_id,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to dispatch newly ready task",
                        project_id=project_id,
                        task_id=task.task_id,
                        error=str(exc),
                    )

        # Check phase completion
        current_phase = state.phase
        is_complete = await self._state.is_phase_complete(
            project_id, graph, current_phase
        )
        if is_complete:
            await self.advance_phase(project_id, current_phase)

    async def on_task_failed(self, event: TaskFailedEvent) -> None:
        """React to a TaskFailedEvent.

        Decides whether to retry or mark the task permanently failed.
        """
        project_id = event.project_id
        task_id = event.task_id

        if not task_id:
            logger.warning("TaskFailedEvent missing task_id", event_id=event.event_id)
            return

        # Load the task node
        state = await self._state.get_state(project_id)
        graph_id: Optional[str] = state.metadata.get("graph_id")
        if not graph_id:
            return

        graph = await self._tge.load_graph(graph_id)
        task = graph.task_by_id(task_id)
        if not task:
            logger.warning(
                "Task not found in graph",
                project_id=project_id,
                task_id=task_id,
            )
            return

        exc = RuntimeError(event.error_message)

        if await self._retry.should_retry(task, exc):
            await self._retry.schedule_retry(task, exc)
        else:
            await self._retry.handle_permanent_failure(task, exc)
            await self._state.mark_task_failed(project_id, task_id, event.error_message)

            # Block downstream tasks
            for blocking_task_id in task.blocking:
                await self._state.mark_task_blocked(project_id, blocking_task_id)

    # ------------------------------------------------------------------
    # Phase transition
    # ------------------------------------------------------------------

    async def advance_phase(
        self, project_id: str, current: ExecutionPhase
    ) -> None:
        """Transition from *current* to the next pipeline phase.

        Emits a PhaseCompletedEvent on every transition.
        """
        next_phase = PHASE_TRANSITIONS.get(current)

        if next_phase is None:
            logger.info(
                "Pipeline complete — no further phases",
                project_id=project_id,
                final_phase=current,
            )
        else:
            await self._state.set_phase(project_id, next_phase)
            logger.info(
                "Phase advanced",
                project_id=project_id,
                from_phase=current,
                to_phase=next_phase,
            )

        # Emit phase-completed event
        event = PhaseCompletedEvent(
            project_id=project_id,
            phase=current,
            next_phase=next_phase,
        )
        await self._bus.publish(event)

    # ------------------------------------------------------------------
    # Task dispatch
    # ------------------------------------------------------------------

    async def dispatch_task(self, task: TaskNode) -> str:
        """Send *task* to the appropriate Celery agent queue.

        Args:
            task: TaskNode to dispatch.

        Returns:
            Celery task ID string.

        Raises:
            OrchestrationError: If Celery fails to accept the task.
        """
        agent_type_str = (
            task.agent_type if isinstance(task.agent_type, str) else task.agent_type.value
        )
        queue = _AGENT_QUEUE_MAP.get(agent_type_str, "forge.agents.backend")

        try:
            result = self._celery.send_task(
                "forge.execute_agent_task",
                args=[task.model_dump()],
                queue=queue,
                countdown=0,
                retry=False,
            )
            logger.debug(
                "Celery task dispatched",
                task_id=task.task_id,
                celery_id=result.id,
                queue=queue,
                agent_type=agent_type_str,
            )
            return result.id
        except Exception as exc:
            logger.error(
                "Celery dispatch failed",
                task_id=task.task_id,
                queue=queue,
                error=str(exc),
            )
            raise OrchestrationError(
                f"Failed to dispatch task {task.task_id} to queue {queue}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Summary / introspection
    # ------------------------------------------------------------------

    async def get_execution_summary(self, project_id: str) -> Dict[str, Any]:
        """Return a human-readable execution summary for *project_id*."""
        state = await self._state.get_state(project_id)
        graph_id: Optional[str] = state.metadata.get("graph_id")
        progress: Dict[str, Any] = {}

        if graph_id:
            try:
                graph = await self._tge.load_graph(graph_id)
                progress = await self._state.get_progress(project_id, graph)
            except Exception as exc:
                logger.warning(
                    "Could not load graph for summary",
                    project_id=project_id,
                    graph_id=graph_id,
                    error=str(exc),
                )

        return {
            "project_id": project_id,
            "phase": state.phase if isinstance(state.phase, str) else state.phase.value,
            "is_paused": project_id in self._paused_projects,
            "active_tasks": state.active_tasks,
            "completed_tasks": state.completed_tasks,
            "failed_tasks": state.failed_tasks,
            "blocked_tasks": state.blocked_tasks,
            **progress,
        }
