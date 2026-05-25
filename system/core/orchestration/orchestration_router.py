"""FastAPI router for the FORGE Orchestration Engine.

Exposes endpoints to start, monitor, and control the autonomous pipeline
execution for a FORGE project.

Prefix: /orchestration
Tags:   orchestration
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from system.core.orchestration.event_bus import EventBus
from system.core.orchestration.event_schemas import ForgeEvent
from system.core.orchestration.retry_manager import RetryManager
from system.core.orchestration.state_manager import ExecutionState, StateManager
from system.core.orchestration.task_graph import TaskGraphEngine
from system.core.orchestration.workflow_engine import WorkflowEngine
from system.observability.logging.logger import get_logger
from system.shared.database import get_db
from system.shared.models import BaseForgeModel
from system.shared.redis_client import get_redis

logger = get_logger(__name__)

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


# ========================================================================== #
# Request / Response schemas
# ========================================================================== #


class StartWorkflowRequest(BaseForgeModel):
    """Request body for POST /orchestration/start."""

    project_id: str = Field(..., description="FORGE project identifier.")
    graph_id: str = Field(..., description="ID of the TaskGraph to execute.")


class StartWorkflowResponse(BaseForgeModel):
    """Response returned when workflow starts successfully."""

    project_id: str
    graph_id: str
    message: str = "Workflow started"


class ProgressResponse(BaseForgeModel):
    """Progress snapshot returned by the progress endpoint."""

    project_id: str
    phase: str
    total: int
    completed: int
    failed: int
    active: int
    blocked: int
    pending: int
    percent_complete: float


class SummaryResponse(BaseForgeModel):
    """Execution summary including task lists and phase info."""

    project_id: str
    phase: str
    is_paused: bool
    active_tasks: list[str]
    completed_tasks: list[str]
    failed_tasks: list[str]
    blocked_tasks: list[str]
    percent_complete: float | None = None


class EventListResponse(BaseForgeModel):
    """List of recent events for a project."""

    project_id: str
    events: list[dict[str, Any]]
    count: int


class ControlResponse(BaseForgeModel):
    """Response for pause/resume/abort control endpoints."""

    project_id: str
    action: str
    message: str


# ========================================================================== #
# Dependency factories
# ========================================================================== #


async def get_state_manager(
    redis: Any = Depends(get_redis),
) -> StateManager:
    return StateManager(redis=redis)


async def get_event_bus(
    redis: Any = Depends(get_redis),
) -> EventBus:
    return EventBus(redis=redis)


async def get_workflow_engine(
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
) -> WorkflowEngine:
    """Build a WorkflowEngine with all dependencies wired up."""
    from system.runtime.queues.task_queue import celery_app  # noqa: PLC0415

    state_mgr = StateManager(redis=redis)
    event_bus = EventBus(redis=redis)
    tge = TaskGraphEngine(db=db)
    retry_mgr = RetryManager(event_bus=event_bus, task_graph_engine=tge)

    return WorkflowEngine(
        task_graph_engine=tge,
        state_manager=state_mgr,
        event_bus=event_bus,
        retry_manager=retry_mgr,
        celery_app=celery_app,
    )


# ========================================================================== #
# Endpoints
# ========================================================================== #


@router.post(
    "/start",
    response_model=StartWorkflowResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start workflow",
    description="Start autonomous execution of a task graph for a project.",
)
async def start_workflow(
    body: StartWorkflowRequest,
    engine: WorkflowEngine = Depends(get_workflow_engine),
    db: AsyncSession = Depends(get_db),
) -> StartWorkflowResponse:
    """Initialise state and dispatch the first wave of agent tasks."""
    logger.info(
        "POST /orchestration/start",
        project_id=body.project_id,
        graph_id=body.graph_id,
    )
    tge = TaskGraphEngine(db=db)
    try:
        graph = await tge.load_graph(body.graph_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task graph '{body.graph_id}' not found: {exc}",
        )

    if graph.project_id != body.project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="graph_id does not belong to the specified project_id.",
        )

    try:
        await engine.start_workflow(project_id=body.project_id, graph=graph)
    except Exception as exc:
        logger.error(
            "Workflow start failed",
            project_id=body.project_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start workflow: {exc}",
        )

    return StartWorkflowResponse(
        project_id=body.project_id,
        graph_id=body.graph_id,
        message="Workflow started — tasks dispatched to agent queues.",
    )


@router.get(
    "/{project_id}/state",
    response_model=ExecutionState,
    summary="Get execution state",
    description="Retrieve the current execution state for a project.",
)
async def get_state(
    project_id: str,
    state_mgr: StateManager = Depends(get_state_manager),
) -> ExecutionState:
    """Return the raw ExecutionState for *project_id*."""
    logger.info("GET /orchestration/{project_id}/state", project_id=project_id)
    state = await state_mgr.get_state(project_id)
    return state


@router.get(
    "/{project_id}/progress",
    response_model=ProgressResponse,
    summary="Get execution progress",
    description="Return completion statistics and percentage for a project.",
)
async def get_progress(
    project_id: str,
    state_mgr: StateManager = Depends(get_state_manager),
    db: AsyncSession = Depends(get_db),
) -> ProgressResponse:
    """Return execution progress for *project_id*."""
    logger.info("GET /orchestration/{project_id}/progress", project_id=project_id)
    state = await state_mgr.get_state(project_id)
    graph_id: str | None = state.metadata.get("graph_id")

    if not graph_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No execution state or graph found for project '{project_id}'.",
        )

    tge = TaskGraphEngine(db=db)
    try:
        graph = await tge.load_graph(graph_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task graph '{graph_id}' not found: {exc}",
        )

    progress = await state_mgr.get_progress(project_id, graph)
    return ProgressResponse(
        project_id=project_id,
        phase=progress.get("phase", "unknown"),
        total=progress.get("total", 0),
        completed=progress.get("completed", 0),
        failed=progress.get("failed", 0),
        active=progress.get("active", 0),
        blocked=progress.get("blocked", 0),
        pending=progress.get("pending", 0),
        percent_complete=progress.get("percent_complete", 0.0),
    )


@router.get(
    "/{project_id}/events",
    response_model=EventListResponse,
    summary="Get recent events",
    description="Return the most recent orchestration events for a project.",
)
async def get_events(
    project_id: str,
    limit: int = 50,
    event_bus: EventBus = Depends(get_event_bus),
) -> EventListResponse:
    """Return recent events from the event bus history for *project_id*."""
    logger.info("GET /orchestration/{project_id}/events", project_id=project_id)
    try:
        raw_events: list[ForgeEvent] = await event_bus.get_event_history(project_id, limit=limit)
        events_list = [e.model_dump() for e in raw_events]
    except Exception as exc:
        logger.error(
            "Failed to fetch event history",
            project_id=project_id,
            error=str(exc),
        )
        events_list = []

    return EventListResponse(
        project_id=project_id,
        events=events_list,
        count=len(events_list),
    )


@router.post(
    "/{project_id}/pause",
    response_model=ControlResponse,
    summary="Pause workflow",
    description="Pause task dispatching for a project. In-flight tasks continue.",
)
async def pause_workflow(
    project_id: str,
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> ControlResponse:
    """Pause the workflow for *project_id*."""
    logger.info("POST /orchestration/{project_id}/pause", project_id=project_id)
    await engine.pause_workflow(project_id)
    return ControlResponse(
        project_id=project_id,
        action="pause",
        message="Workflow paused. In-flight tasks will complete; new tasks will not be dispatched.",
    )


@router.post(
    "/{project_id}/resume",
    response_model=ControlResponse,
    summary="Resume workflow",
    description="Resume task dispatching after a pause.",
)
async def resume_workflow(
    project_id: str,
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> ControlResponse:
    """Resume the workflow for *project_id*."""
    logger.info("POST /orchestration/{project_id}/resume", project_id=project_id)
    await engine.resume_workflow(project_id)
    return ControlResponse(
        project_id=project_id,
        action="resume",
        message="Workflow resumed. Ready tasks have been dispatched.",
    )


@router.post(
    "/{project_id}/abort",
    response_model=ControlResponse,
    status_code=status.HTTP_200_OK,
    summary="Abort workflow",
    description="Abort all execution for a project and clean up state.",
)
async def abort_workflow(
    project_id: str,
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> ControlResponse:
    """Abort the workflow for *project_id* and clean up Redis state."""
    logger.info("POST /orchestration/{project_id}/abort", project_id=project_id)
    await engine.abort_workflow(project_id)
    return ControlResponse(
        project_id=project_id,
        action="abort",
        message="Workflow aborted. All execution state has been cleaned up.",
    )


@router.get(
    "/{project_id}/summary",
    response_model=SummaryResponse,
    summary="Get execution summary",
    description="Return a full execution summary including all task lists.",
)
async def get_summary(
    project_id: str,
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> SummaryResponse:
    """Return detailed execution summary for *project_id*."""
    logger.info("GET /orchestration/{project_id}/summary", project_id=project_id)
    summary = await engine.get_execution_summary(project_id)
    return SummaryResponse(
        project_id=project_id,
        phase=summary.get("phase", "unknown"),
        is_paused=summary.get("is_paused", False),
        active_tasks=summary.get("active_tasks", []),
        completed_tasks=summary.get("completed_tasks", []),
        failed_tasks=summary.get("failed_tasks", []),
        blocked_tasks=summary.get("blocked_tasks", []),
        percent_complete=summary.get("percent_complete"),
    )
