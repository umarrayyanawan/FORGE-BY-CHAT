"""FastAPI router for Monitoring Engine endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from system.core.monitoring.schemas import HealthSnapshot, MonitoringConfig, MonitoringReport

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _get_engine() -> Any:
    from system.core.monitoring.engine import MonitoringEngine
    return MonitoringEngine()


@router.post("/{project_id}/start", status_code=201)
async def start_monitoring(project_id: str, config: MonitoringConfig) -> Dict[str, Any]:
    """Start continuous health monitoring for a deployed project."""
    engine = _get_engine()
    await engine.start_monitoring(config)
    return {"project_id": project_id, "status": "monitoring_started", "interval_seconds": config.check_interval_seconds}


@router.post("/{project_id}/stop")
async def stop_monitoring(project_id: str) -> Dict[str, Any]:
    """Stop monitoring for a project."""
    engine = _get_engine()
    await engine.stop_monitoring(project_id)
    return {"project_id": project_id, "status": "monitoring_stopped"}


@router.get("/{project_id}/report", response_model=MonitoringReport)
async def get_report(project_id: str):
    """Get a monitoring report with uptime, response times, and recent snapshots."""
    engine = _get_engine()
    return await engine.get_report(project_id)


@router.post("/{project_id}/check", response_model=HealthSnapshot)
async def run_check(project_id: str, endpoint_url: str):
    """Run a single on-demand health check against an endpoint."""
    engine = _get_engine()
    return await engine.run_single_check(project_id, endpoint_url)
