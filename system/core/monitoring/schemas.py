"""Monitoring schemas — health snapshots, metric samples, and alert configs."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import Field

from system.shared.models import BaseForgeModel, TimestampedModel


class MetricSample(BaseForgeModel):
    metric_name: str
    value: float
    unit: str = ""
    labels: Dict[str, str] = {}


class HealthSnapshot(TimestampedModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    deployment_id: str
    status: str = "healthy"
    endpoint_url: str = ""
    response_time_ms: Optional[float] = None
    status_code: Optional[int] = None
    error: Optional[str] = None
    metrics: List[MetricSample] = []


class AlertRule(BaseForgeModel):
    rule_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    metric_name: str
    threshold: float
    operator: str = "gt"
    severity: str = "warning"
    enabled: bool = True


class MonitoringConfig(BaseForgeModel):
    project_id: str
    deployment_id: str
    endpoint_url: str
    check_interval_seconds: int = 60
    alert_rules: List[AlertRule] = []
    enabled: bool = True


class MonitoringReport(TimestampedModel):
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    snapshots: List[HealthSnapshot] = []
    uptime_percentage: float = 100.0
    avg_response_time_ms: Optional[float] = None
    total_checks: int = 0
    failed_checks: int = 0
    alerts_triggered: int = 0
