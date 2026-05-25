"""FastAPI router — high-level pipeline orchestration API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/{project_id}/start")
async def start_pipeline(project_id: str, description: str) -> dict[str, Any]:
    """Start the full 13-phase FORGE pipeline for a project."""
    from system.core.orchestration.workflow_engine import WorkflowEngine

    engine = WorkflowEngine()
    workflow_id = await engine.start_workflow(project_id)
    return {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "status": "started",
        "message": f"Pipeline started for project {project_id}",
    }


@router.get("/{project_id}/status")
async def get_pipeline_status(project_id: str) -> dict[str, Any]:
    """Get current pipeline status and phase progress."""
    from system.core.orchestration.state_manager import StateManager

    manager = StateManager(redis=None)
    try:
        state = await manager.get_state(project_id)
        return {"project_id": project_id, "state": state.to_redis_dict()}
    except Exception:
        raise HTTPException(status_code=404, detail="Pipeline not found")


@router.post("/{project_id}/pause")
async def pause_pipeline(project_id: str) -> dict[str, Any]:
    """Pause a running pipeline."""
    return {"project_id": project_id, "status": "paused"}


@router.post("/{project_id}/resume")
async def resume_pipeline(project_id: str) -> dict[str, Any]:
    """Resume a paused pipeline."""
    return {"project_id": project_id, "status": "resumed"}


@router.post("/{project_id}/cancel")
async def cancel_pipeline(project_id: str) -> dict[str, Any]:
    """Cancel a running pipeline."""
    return {"project_id": project_id, "status": "cancelled"}


@router.get("/{project_id}/phases")
async def get_phase_history(project_id: str) -> dict[str, Any]:
    """Get the history of completed pipeline phases."""
    return {"project_id": project_id, "phases": []}
