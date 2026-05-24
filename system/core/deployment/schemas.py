"""Pydantic schemas for the FORGE Deployment Engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import Field

from system.shared.models import BaseForgeModel, DeployTarget, TimestampedModel


class DeploymentConfig(BaseForgeModel):
    """Full configuration required to deploy a project to a target environment."""

    target: DeployTarget
    environment: str = "dev"
    project_id: str
    image_tag: str = "latest"
    replicas: int = 1
    env_vars: Dict[str, str] = {}
    secret_refs: List[str] = []
    health_check_path: str = "/health"
    health_check_timeout: int = 30
    rollout_strategy: str = "rolling"
    port: int = 8000


class DeploymentRecord(TimestampedModel):
    """Persisted record of a single deployment attempt."""

    deployment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    target: DeployTarget
    environment: str
    status: str = "pending"
    url: Optional[str] = None
    image_tag: str
    previous_deployment_id: Optional[str] = None
    health_status: str = "unknown"
    deployed_by: str = "forge-system"
    rollback_available: bool = True
    config: DeploymentConfig
    logs: str = ""


class HealthStatus(BaseForgeModel):
    """Result of a single health-check probe."""

    healthy: bool
    status_code: int = 200
    response_time_ms: int = 0
    details: Dict[str, Any] = {}
    checked_at: datetime = Field(default_factory=datetime.utcnow)
