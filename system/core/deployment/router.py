"""FastAPI router for Deployment Engine endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from system.core.deployment.schemas import DeploymentConfig, DeploymentRecord

router = APIRouter(prefix="/deployment", tags=["deployment"])


def _get_engine() -> Any:
    from system.core.deployment.engine import DeploymentEngine
    from system.core.deployment.health_checker import HealthChecker
    from system.core.deployment.provisioner import InfraProvisioner
    from system.core.deployment.rollback import RollbackManager
    from system.core.deployment.secrets_manager import SecretsManager
    provisioner = InfraProvisioner()
    return DeploymentEngine(
        provisioner=provisioner,
        health_checker=HealthChecker(),
        rollback_manager=RollbackManager(provisioner=provisioner),
        secrets_manager=SecretsManager(),
    )


@router.post("/{project_id}/deploy", response_model=DeploymentRecord, status_code=201)
async def deploy_project(project_id: str, config: DeploymentConfig):
    """Deploy a project using the specified configuration."""
    engine = _get_engine()
    return await engine.deploy(config)


@router.post("/{project_id}/rollback")
async def rollback_deployment(project_id: str, deployment_id: str) -> Dict[str, Any]:
    """Rollback a deployment to the previous stable version."""
    engine = _get_engine()
    record = await engine.rollback(deployment_id)
    return {"project_id": project_id, "deployment_id": deployment_id, "status": record.status}


@router.get("/{project_id}/status")
async def get_deployment_status(project_id: str) -> Dict[str, Any]:
    """Get the current deployment status for a project."""
    engine = _get_engine()
    record = await engine.get_latest_deployment(project_id)
    if not record:
        raise HTTPException(status_code=404, detail="No deployments found for project")
    return {"project_id": project_id, "deployment": record.model_dump()}


@router.get("/{project_id}/health")
async def check_deployment_health(project_id: str) -> Dict[str, Any]:
    """Run a health check against the latest deployed endpoint."""
    engine = _get_engine()
    record = await engine.get_latest_deployment(project_id)
    if not record:
        raise HTTPException(status_code=404, detail="No deployments found for project")
    endpoint = getattr(record, "url", None)
    if not endpoint:
        raise HTTPException(status_code=404, detail="No endpoint URL on deployment record")
    health = await engine.health_checker.check(endpoint)
    return {"project_id": project_id, "health": health.model_dump()}


@router.get("/{project_id}/list", response_model=list)
async def list_deployments(project_id: str):
    """List all deployments for a project."""
    engine = _get_engine()
    records = await engine.list_deployments(project_id)
    return [r.model_dump() for r in records]
