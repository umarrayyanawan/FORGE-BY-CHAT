"""Monitoring Engine — orchestrates continuous health monitoring for deployed projects."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from system.core.monitoring.collector import MetricsCollector
from system.core.monitoring.schemas import (
    AlertRule,
    HealthSnapshot,
    MonitoringConfig,
    MonitoringReport,
)
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class MonitoringEngine:
    def __init__(
        self,
        collector: Optional[MetricsCollector] = None,
        alerter: Any = None,
        db: Any = None,
    ) -> None:
        self.collector = collector or MetricsCollector()
        self.alerter = alerter
        self.db = db
        self._configs: Dict[str, MonitoringConfig] = {}
        self._snapshots: Dict[str, List[HealthSnapshot]] = {}
        self._running: Dict[str, bool] = {}

    async def start_monitoring(self, config: MonitoringConfig) -> None:
        self._configs[config.project_id] = config
        self._snapshots.setdefault(config.project_id, [])
        self._running[config.project_id] = True
        logger.info(
            "Monitoring started",
            project_id=config.project_id,
            endpoint=config.endpoint_url,
            interval=config.check_interval_seconds,
        )
        asyncio.create_task(self._monitoring_loop(config))

    async def stop_monitoring(self, project_id: str) -> None:
        self._running[project_id] = False
        logger.info("Monitoring stopped", project_id=project_id)

    async def _monitoring_loop(self, config: MonitoringConfig) -> None:
        while self._running.get(config.project_id, False):
            try:
                snapshot = await self.collector.collect_health(config)
                self._snapshots[config.project_id].append(snapshot)
                if len(self._snapshots[config.project_id]) > 1000:
                    self._snapshots[config.project_id] = self._snapshots[config.project_id][-500:]

                if snapshot.status != "healthy":
                    logger.warning(
                        "Health check failed",
                        project_id=config.project_id,
                        status=snapshot.status,
                        error=snapshot.error,
                    )
                    await self._evaluate_alerts(config, snapshot)

            except Exception as exc:
                logger.error("Monitoring loop error", project_id=config.project_id, error=str(exc))

            await asyncio.sleep(config.check_interval_seconds)

    async def _evaluate_alerts(
        self, config: MonitoringConfig, snapshot: HealthSnapshot
    ) -> None:
        if not self.alerter or not config.alert_rules:
            return
        for rule in config.alert_rules:
            if not rule.enabled:
                continue
            for metric in snapshot.metrics:
                if metric.metric_name != rule.metric_name:
                    continue
                triggered = (
                    (rule.operator == "gt" and metric.value > rule.threshold)
                    or (rule.operator == "lt" and metric.value < rule.threshold)
                    or (rule.operator == "eq" and metric.value == rule.threshold)
                )
                if triggered:
                    try:
                        await self.alerter.send(
                            level=rule.severity,
                            title=f"Alert: {rule.metric_name} threshold breached",
                            message=f"Value {metric.value} {rule.operator} {rule.threshold} for project {config.project_id}",
                        )
                    except Exception as exc:
                        logger.warning("Alert delivery failed", error=str(exc))

    async def get_report(self, project_id: str) -> MonitoringReport:
        snapshots = self._snapshots.get(project_id, [])
        if not snapshots:
            return MonitoringReport(project_id=project_id)

        total = len(snapshots)
        failed = sum(1 for s in snapshots if s.status != "healthy")
        uptime = ((total - failed) / total * 100) if total > 0 else 100.0

        response_times = [
            s.response_time_ms for s in snapshots if s.response_time_ms is not None
        ]
        avg_rt = sum(response_times) / len(response_times) if response_times else None

        return MonitoringReport(
            project_id=project_id,
            snapshots=snapshots[-50:],
            uptime_percentage=round(uptime, 2),
            avg_response_time_ms=round(avg_rt, 2) if avg_rt is not None else None,
            total_checks=total,
            failed_checks=failed,
        )

    async def run_single_check(self, project_id: str, endpoint_url: str) -> HealthSnapshot:
        config = MonitoringConfig(
            project_id=project_id,
            deployment_id="adhoc",
            endpoint_url=endpoint_url,
        )
        return await self.collector.collect_health(config)
