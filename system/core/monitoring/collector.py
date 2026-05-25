"""Metrics collector — gathers health signals from deployed endpoints."""

from __future__ import annotations

import time
from typing import Any

import httpx

from system.core.monitoring.schemas import HealthSnapshot, MetricSample, MonitoringConfig
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class MetricsCollector:
    def __init__(self, http_client: Any | None = None, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._client = http_client

    async def collect_health(self, config: MonitoringConfig) -> HealthSnapshot:
        start = time.monotonic()
        status = "healthy"
        status_code: int | None = None
        error: str | None = None
        metrics: list[MetricSample] = []

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(config.endpoint_url)
                status_code = response.status_code
                elapsed_ms = (time.monotonic() - start) * 1000

                if status_code >= 500:
                    status = "unhealthy"
                elif status_code >= 400:
                    status = "degraded"

                metrics = [
                    MetricSample(
                        metric_name="http_response_time_ms", value=round(elapsed_ms, 2), unit="ms"
                    ),
                    MetricSample(metric_name="http_status_code", value=float(status_code)),
                ]

                try:
                    body = response.json()
                    if isinstance(body, dict) and "components" in body:
                        for comp, comp_status in body["components"].items():
                            metrics.append(
                                MetricSample(
                                    metric_name=f"component_{comp}",
                                    value=1.0 if comp_status == "ok" else 0.0,
                                    labels={"component": comp},
                                )
                            )
                except Exception:
                    pass

        except httpx.TimeoutException:
            status = "unhealthy"
            error = f"Health check timed out after {self.timeout}s"
            elapsed_ms = self.timeout * 1000
        except Exception as exc:
            status = "unhealthy"
            error = str(exc)
            elapsed_ms = (time.monotonic() - start) * 1000

        return HealthSnapshot(
            project_id=config.project_id,
            deployment_id=config.deployment_id,
            status=status,
            endpoint_url=config.endpoint_url,
            response_time_ms=round((time.monotonic() - start) * 1000, 2),
            status_code=status_code,
            error=error,
            metrics=metrics,
        )

    async def collect_system_metrics(self, project_id: str) -> list[MetricSample]:
        try:
            import psutil

            return [
                MetricSample(metric_name="cpu_percent", value=psutil.cpu_percent(), unit="%"),
                MetricSample(
                    metric_name="memory_percent", value=psutil.virtual_memory().percent, unit="%"
                ),
                MetricSample(
                    metric_name="disk_percent", value=psutil.disk_usage("/").percent, unit="%"
                ),
            ]
        except ImportError:
            return []
