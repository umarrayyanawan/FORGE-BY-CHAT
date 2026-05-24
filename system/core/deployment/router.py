"""FastAPI router for Deployment Engine endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from system.core.deployment.schemas import DeploymentConfig, DeploymentRecord

router = APIRouter(prefix="/deployment", tags=["deployment"])


def _get_engine() -> Any:
    from system.core.deployment.engine import DeploymentEngine
    return DeploymentEngine()


@router.post("/{project_id}/deploy", response_model=DeploymentRecord, status_code=201)
async def deploy_project(project_id: str, config: DeploymentConfig):
    """Deploy a project using the specified configuration."""
    engine = _get_engine()
    record = await engine.deploy(project_id, config)
    return record


@router.post("/{project_id}/rollback")
async def rollback_deployment(project_id: str, deployment_id: str) -> Dict[str, Any]:
    """Rollback a deployment to the previous stable version."""
    engine = _get_engine()
    success = await engine.rollback(deployment_id)
    return {"project_id": project_id, "deployment_id": deployment_id, "rolled_back": success}


@router.get("/{project_id}/status")
async def get_deployment_status(project_id: str) -> Dict[str, Any]:
    """Get the current deployment status for a project."""
    engine = _get_engine()
    record = await engine.get_latest(project_id)
    if not record:
        raise HTTPException(status_code=404, detail="No deployments found for project")
    return {"project_id": project_id, "deployment": record.model_dump()}


@router.get("/{project_id}/health")
async def check_deployment_health(project_id: str) -> Dict[str, Any]:
    """Run health checks against the deployed project."""
    engine = _get_engine()
    record = await engine.get_latest(project_id)
    if not record or not record.endpoint_url:
        raise HTTPException(status_code=404, detail="No deployment endpoint found")
    health = await engine.health_checker.check(record.endpoint_url)
    return {"project_id": project_id, "health": health.model_dump()}
