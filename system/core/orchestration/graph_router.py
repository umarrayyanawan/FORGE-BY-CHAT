"""FastAPI router for the FORGE Task Graph Engine.

Endpoints:
    POST /tasks/generate             – generate a task graph from spec + arch
    GET  /tasks/{graph_id}           – retrieve a task graph
    GET  /tasks/{graph_id}/status    – lightweight execution-status summary
    PUT  /tasks/{graph_id}/tasks/{task_id} – update a single task's status
    GET  /tasks/{graph_id}/ready     – tasks with all dependencies satisfied
    GET  /tasks/{graph_id}/critical-path – ordered critical-path task list
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from redis.asyncio import Redis

from system.core.orchestration.task_graph import TaskGraphEngine
from system.core.orchestration.task_schemas import (
    GenerateGraphRequest,
    GraphStatusSummary,
    TaskGraph,
    TaskGraphUpdate,
    TaskNode,
)
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.database import AsyncSessionLocal, get_db
from system.shared.exceptions import NotFoundError, OrchestrationError, ValidationError
from system.shared.models import BaseForgeModel
from system.shared.redis_client import get_redis

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["Task Graph"])

# Module-level engine singleton (stateless, safe to share)
_engine = TaskGraphEngine()


# ========================================================================== #
# Request / Response Models
# ========================================================================== #


class StartGraphRequest(BaseForgeModel):
    """Request body for POST /tasks/generate."""

    project_id: str = Field(..., description="FORGE project identifier.")
    spec: ProjectSpec = Field(..., description="Finalized project specification.")
    arch: ArchitecturePlan = Field(..., description="Finalized architecture plan.")


class ReadyTasksResponse(BaseForgeModel):
    """Response for GET /tasks/{graph_id}/ready."""

    graph_id: str
    ready_tasks: List[TaskNode]
    count: int


class CriticalPathResponse(BaseForgeModel):
    """Response for GET /tasks/{graph_id}/critical-path."""

    graph_id: str
    critical_path: List[str]
    estimated_minutes: int


# ========================================================================== #
# Route handlers
# ========================================================================== #


@router.post(
    "/generate",
    response_model=TaskGraph,
    status_code=status.HTTP_201_CREATED,
    summary="Generate task graph",
    description=(
        "Build a complete DAG of agent tasks for the given project spec and "
        "architecture plan.  Returns the fully populated TaskGraph including "
        "execution order levels and critical path."
    ),
)
async def generate_task_graph(
    request: StartGraphRequest,
) -> TaskGraph:
    """Generate and persist a task graph for the given project."""
    logger.info(
        "generate_task_graph_request",
        project_id=request.project_id,
        spec_id=request.spec.id,
        arch_id=request.arch.plan_id,
    )
    try:
        graph = await _engine.generate(
            project_id=request.project_id,
            spec=request.spec,
            arch=request.arch,
        )
        await _engine.persist_graph(graph)
        return graph
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.to_dict())
    except OrchestrationError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.to_dict())


@router.get(
    "/{graph_id}",
    response_model=TaskGraph,
    summary="Get task graph",
    description="Retrieve the full task graph including all TaskNodes and metadata.",
)
async def get_task_graph(graph_id: str) -> TaskGraph:
    """Return the full task graph for *graph_id*."""
    graph = await _engine.load_graph(graph_id)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Task graph '{graph_id}' not found.", "code": "NOT_FOUND_ERROR"},
        )
    return graph


@router.get(
    "/{graph_id}/status",
    response_model=GraphStatusSummary,
    summary="Get execution status summary",
    description=(
        "Return a lightweight snapshot of task completion progress, "
        "without returning the full task list."
    ),
)
async def get_graph_status(graph_id: str) -> GraphStatusSummary:
    """Return aggregated execution status for *graph_id*."""
    graph = await _engine.load_graph(graph_id)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Task graph '{graph_id}' not found.", "code": "NOT_FOUND_ERROR"},
        )
    return GraphStatusSummary(
        graph_id=graph.graph_id,
        project_id=graph.project_id,
        phase=str(graph.phase),
        total_tasks=graph.total_tasks,
        completed_tasks=graph.completed_tasks,
        failed_tasks=graph.failed_tasks,
        running_tasks=graph.running_tasks,
        pending_tasks=graph.pending_tasks,
        progress_pct=graph.progress_pct,
        estimated_duration_minutes=graph.estimated_duration_minutes,
        critical_path=graph.critical_path,
    )


@router.put(
    "/{graph_id}/tasks/{task_id}",
    response_model=Dict[str, Any],
    summary="Update task status",
    description=(
        "Update the status, error message, or output artifacts of a single "
        "task inside the graph.  Triggers aggregate counter updates."
    ),
)
async def update_task_status(
    graph_id: str,
    task_id: str,
    update: TaskGraphUpdate,
) -> Dict[str, Any]:
    """Apply *update* to task *task_id* within graph *graph_id*."""
    if update.task_id != task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "task_id in URL and request body do not match.",
                "code": "VALIDATION_ERROR",
            },
        )
    try:
        await _engine.update_task_status(graph_id, task_id, update)
    except OrchestrationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.to_dict())
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.to_dict())

    return {"ok": True, "graph_id": graph_id, "task_id": task_id, "status": update.status}


@router.get(
    "/{graph_id}/ready",
    response_model=ReadyTasksResponse,
    summary="Get ready tasks",
    description=(
        "Return all PENDING tasks whose dependency tasks have all completed.  "
        "These are safe to dispatch to agents immediately."
    ),
)
async def get_ready_tasks(graph_id: str) -> ReadyTasksResponse:
    """List tasks ready to execute (all deps satisfied) in *graph_id*."""
    graph = await _engine.load_graph(graph_id)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Task graph '{graph_id}' not found.", "code": "NOT_FOUND_ERROR"},
        )

    ready = _engine.get_ready_tasks(graph)
    return ReadyTasksResponse(
        graph_id=graph_id,
        ready_tasks=ready,
        count=len(ready),
    )


@router.get(
    "/{graph_id}/critical-path",
    response_model=CriticalPathResponse,
    summary="Get critical path",
    description=(
        "Return the pre-computed critical path (longest dependency chain) "
        "for this task graph, measured by estimated token consumption."
    ),
)
async def get_critical_path(graph_id: str) -> CriticalPathResponse:
    """Return the critical path for *graph_id*."""
    graph = await _engine.load_graph(graph_id)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Task graph '{graph_id}' not found.", "code": "NOT_FOUND_ERROR"},
        )

    return CriticalPathResponse(
        graph_id=graph_id,
        critical_path=graph.critical_path,
        estimated_minutes=graph.estimated_duration_minutes,
    )
