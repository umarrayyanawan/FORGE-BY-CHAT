"""Deployment Engine — orchestrates the full deploy→health-check→rollback lifecycle."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from system.core.deployment.cicd import CICDBuilder
from system.core.deployment.health_checker import HealthChecker
from system.core.deployment.provisioner import InfraProvisioner
from system.core.deployment.rollback import RollbackManager
from system.core.deployment.schemas import DeploymentConfig, DeploymentRecord
from system.core.deployment.secrets_manager import SecretsManager
from system.observability.logging.logger import get_logger
from system.shared.exceptions import DeploymentError

logger = get_logger(__name__)


class DeploymentEngine:
    """Orchestrates infrastructure provisioning, health validation, and rollback.

    Args:
        provisioner: Provisions cloud/container resources.
        health_checker: Probes HTTP health endpoints.
        rollback_manager: Manages deployment history and rollback operations.
        secrets_manager: Injects encrypted secrets at deploy time.
        cicd_builder: Optional CI/CD workflow generator.
        db: SQLAlchemy async session factory for persistent deployment records.
    """

    def __init__(
        self,
        provisioner: InfraProvisioner,
        health_checker: HealthChecker,
        rollback_manager: RollbackManager,
        secrets_manager: SecretsManager,
        cicd_builder: Optional[CICDBuilder] = None,
        db: Optional[Any] = None,
    ) -> None:
        self.provisioner = provisioner
        self.health_checker = health_checker
        self.rollback_manager = rollback_manager
        self.secrets_manager = secrets_manager
        self.cicd_builder = cicd_builder
        self.db = db
        # In-memory registry: deployment_id → DeploymentRecord
        self._records: Dict[str, DeploymentRecord] = {}

    # ------------------------------------------------------------------
    # Core deploy flow
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> DeploymentRecord:
        """Deploy *config* and return the resulting :class:`DeploymentRecord`.

        Flow:
        1. Inject secrets into env_vars.
        2. Find any existing deployment for this project/env (to enable rollback).
        3. Provision infrastructure.
        4. Poll the health endpoint until healthy or failed.
        5. Create a rollback point.
        6. Persist and return the record.
        """
        logger.info(
            "Starting deployment",
            project_id=config.project_id,
            target=config.target,
            environment=config.environment,
            image_tag=config.image_tag,
        )

        # 1. Inject secrets.
        secrets = await self.secrets_manager.inject_into_env(
            config.project_id, config.environment
        )
        merged_env = {**config.env_vars, **secrets}
        effective_config = DeploymentConfig(
            **{**config.model_dump(), "env_vars": merged_env}
        )

        # 2. Find the current live deployment (to set as previous for rollback).
        previous_id = self._find_latest_deployment_id(
            config.project_id, config.environment
        )

        # 3. Provision infrastructure.
        resource_id = await self.provisioner.provision(effective_config)

        # 4. Create in-progress record.
        record = DeploymentRecord(
            project_id=config.project_id,
            target=config.target,
            environment=config.environment,
            status="in_progress",
            image_tag=config.image_tag,
            previous_deployment_id=previous_id,
            config=config,  # Store original config (no injected secrets).
            logs=f"Resource provisioned: {resource_id}",
        )

        # 5. Health-check.
        if config.health_check_path:
            health_url = f"http://localhost:{config.port}{config.health_check_path}"
            logger.info(
                "Waiting for service to become healthy",
                url=health_url,
                timeout=config.health_check_timeout,
            )
            is_healthy = await self.health_checker.wait_for_healthy(
                health_url,
                timeout_seconds=config.health_check_timeout,
            )
            record.status = "success" if is_healthy else "failed"
            record.health_status = "healthy" if is_healthy else "unhealthy"
            if not is_healthy:
                record.logs += f"\nHealth check failed after {config.health_check_timeout}s"
                logger.warning(
                    "Deployment health check failed",
                    deployment_id=record.deployment_id,
                    project_id=config.project_id,
                )
        else:
            record.status = "success"
            record.health_status = "unknown"

        # 6. Register with rollback manager and persist.
        self.rollback_manager.register(record)
        await self.rollback_manager.create_rollback_point(record.deployment_id)
        self._records[record.deployment_id] = record

        logger.info(
            "Deployment complete",
            deployment_id=record.deployment_id,
            status=record.status,
            health_status=record.health_status,
        )
        return record

    async def deploy_with_rollback(self, config: DeploymentConfig) -> DeploymentRecord:
        """Deploy *config* and automatically roll back if the deployment fails.

        Returns the final :class:`DeploymentRecord` (either the successful
        primary deployment or the rollback).
        """
        record = await self.deploy(config)
        if record.status != "success":
            logger.warning(
                "Deployment failed — attempting automatic rollback",
                deployment_id=record.deployment_id,
                project_id=config.project_id,
            )
            can_roll = await self.rollback_manager.can_rollback(record.deployment_id)
            if can_roll:
                try:
                    rollback_record = await self.rollback_manager.rollback(
                        record.deployment_id
                    )
                    self._records[rollback_record.deployment_id] = rollback_record
                    logger.info(
                        "Automatic rollback succeeded",
                        rollback_deployment_id=rollback_record.deployment_id,
                    )
                    return rollback_record
                except DeploymentError as exc:
                    logger.error(
                        "Automatic rollback failed",
                        error=str(exc),
                        deployment_id=record.deployment_id,
                    )
            else:
                logger.warning(
                    "No rollback target available",
                    deployment_id=record.deployment_id,
                )
        return record

    # ------------------------------------------------------------------
    # Query / introspection
    # ------------------------------------------------------------------

    async def get_deployment(self, deployment_id: str) -> Optional[DeploymentRecord]:
        """Return the :class:`DeploymentRecord` for *deployment_id*, or ``None``."""
        return self._records.get(deployment_id)

    async def list_deployments(self, project_id: str) -> List[DeploymentRecord]:
        """Return all deployments for *project_id*, newest first."""
        records = [r for r in self._records.values() if r.project_id == project_id]
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    async def get_latest_deployment(
        self, project_id: str, environment: str
    ) -> Optional[DeploymentRecord]:
        """Return the most recent deployment for *project_id* + *environment*."""
        records = [
            r
            for r in self._records.values()
            if r.project_id == project_id and r.environment == environment
        ]
        if not records:
            return None
        return max(records, key=lambda r: r.created_at)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def rollback(self, deployment_id: str) -> DeploymentRecord:
        """Manually trigger a rollback of *deployment_id*."""
        record = await self.rollback_manager.rollback(deployment_id)
        self._records[record.deployment_id] = record
        return record

    async def check_health(self, deployment_id: str, url: str) -> bool:
        """Probe *url* and update the health status of *deployment_id*.

        Returns ``True`` if the service is healthy.
        """
        status = await self.health_checker.check(url)
        record = self._records.get(deployment_id)
        if record:
            record.health_status = "healthy" if status.healthy else "unhealthy"
        return status.healthy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_latest_deployment_id(
        self, project_id: str, environment: str
    ) -> Optional[str]:
        """Return the deployment_id of the current live deployment, if any."""
        candidates = [
            r
            for r in self._records.values()
            if r.project_id == project_id
            and r.environment == environment
            and r.status == "success"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.created_at).deployment_id
