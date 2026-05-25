"""FastAPI router for Evolution Engine endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from system.core.evolution.schemas import ChangeRequest, EvolutionRecord

router = APIRouter(prefix="/evolution", tags=["evolution"])


def _get_engine() -> Any:
    from system.core.evolution.engine import EvolutionEngine

    return EvolutionEngine()


@router.post("/plan", response_model=EvolutionRecord, status_code=201)
async def plan_evolution(change_request: ChangeRequest, project_path: str = "."):
    """Plan a software evolution from a change request."""
    engine = _get_engine()
    return await engine.plan_evolution(change_request, project_path)


@router.post("/{evolution_id}/apply", response_model=EvolutionRecord)
async def apply_evolution(evolution_id: str, project_path: str = "."):
    """Apply a planned evolution."""
    engine = _get_engine()
    record = await engine.apply_evolution(evolution_id, project_path)
    if not record:
        raise HTTPException(status_code=404, detail="Evolution record not found")
    return record


@router.get("/{evolution_id}", response_model=EvolutionRecord)
async def get_evolution(evolution_id: str):
    """Get the status of an evolution record."""
    engine = _get_engine()
    record = await engine.get_record(evolution_id)
    if not record:
        raise HTTPException(status_code=404, detail="Evolution record not found")
    return record


@router.get("/project/{project_id}", response_model=list[EvolutionRecord])
async def list_evolutions(project_id: str):
    """List all evolution records for a project."""
    engine = _get_engine()
    return await engine.list_records(project_id)


@router.get("/project/{project_id}/opportunities")
async def get_opportunities(project_id: str) -> dict[str, Any]:
    """Get improvement opportunities for a project."""
    engine = _get_engine()
    opportunities = await engine.find_improvement_opportunities(project_id)
    return {"project_id": project_id, "opportunities": opportunities}
