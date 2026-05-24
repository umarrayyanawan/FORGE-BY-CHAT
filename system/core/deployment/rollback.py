"""Rollback manager — reverts a failed deployment to its previous stable state."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from system.core.deployment.schemas import DeploymentConfig, DeploymentRecord
from system.observability.logging.logger import get_logger
from system.shared.exceptions import DeploymentError

logger = get_logger(__name__)


class RollbackManager:
    """Manages deployment rollback lifecycle.

    In production *db* provides persistent storage; without it the manager
    uses an in-memory registry so tests and integration suites work without
    a live database.

    Args:
        provisioner: :class:`~system.core.deployment.provisioner.InfraProvisioner`
            used to re-provision the previous revision.
        docker_manager: Optional Docker SDK wrapper.
        k8s_manager: Optional Kubernetes client wrapper.
        db: SQLAlchemy async session or session factory.
    """

    def __init__(
        self,
        provisioner: Any,
        docker_manager: Optional[Any] = None,
        k8s_manager: Optional[Any] = None,
        db: Optional[Any] = None,
    ) -> None:
        self.provisioner = provisioner
        self.docker = docker_manager
        self.k8s = k8s_manager
        self.db = db
        # In-memory registry used when no DB is provided.
        self._records: Dict[str, DeploymentRecord] = {}

    # ------------------------------------------------------------------
    # Record management
    # ------------------------------------------------------------------

    def register(self, record: DeploymentRecord) -> None:
        """Register a deployment record so it can be rolled back later."""
        self._records[record.deployment_id] = record

    def _get_record(self, deployment_id: str) -> DeploymentRecord:
        record = self._records.get(deployment_id)
        if record is None:
            raise DeploymentError(
                f"Deployment {deployment_id!r} not found in rollback registry",
                "NOT_FOUND",
            )
        return record

    # ------------------------------------------------------------------
    # Rollback point
    # ------------------------------------------------------------------

    async def create_rollback_point(self, deployment_id: str) -> None:
        """Mark *deployment_id* as a valid rollback point.

        In production this would persist a snapshot of the deployment state to
        the database.  Here we log the intent and rely on the in-memory record.
        """
        logger.info("Rollback point created", deployment_id=deployment_id)

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def rollback(self, deployment_id: str) -> DeploymentRecord:
        """Roll back *deployment_id* to its predecessor.

        Returns the new :class:`DeploymentRecord` representing the rolled-back
        state.

        Raises:
            DeploymentError: If the deployment or its predecessor cannot be
                found, or if reprovisioning the predecessor fails.
        """
        current = self._get_record(deployment_id)
        logger.info(
            "Initiating rollback",
            from_deployment_id=deployment_id,
            project_id=current.project_id,
            environment=current.environment,
        )

        previous_id = current.previous_deployment_id
        if not previous_id:
            raise DeploymentError(
                f"Deployment {deployment_id!r} has no previous deployment to roll back to",
                "NO_ROLLBACK_TARGET",
            )
        previous = self._records.get(previous_id)
        if previous is None:
            raise DeploymentError(
                f"Previous deployment {previous_id!r} not found in rollback registry",
                "ROLLBACK_TARGET_MISSING",
            )

        # Re-provision using the previous configuration.
        resource_id = await self.provisioner.provision(previous.config)

        rollback_record = DeploymentRecord(
            deployment_id=str(uuid.uuid4()),
            project_id=current.project_id,
            target=previous.config.target,
            environment=previous.config.environment,
            status="success",
            image_tag=previous.image_tag,
            previous_deployment_id=deployment_id,
            health_status="healthy",
            config=previous.config,
            logs=f"Rolled back from {deployment_id} to {previous_id}. Resource: {resource_id}",
        )
        self._records[rollback_record.deployment_id] = rollback_record

        logger.info(
            "Rollback completed",
            rollback_deployment_id=rollback_record.deployment_id,
            from_deployment_id=deployment_id,
            to_deployment_id=previous_id,
            image_tag=previous.image_tag,
        )
        return rollback_record

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def can_rollback(self, deployment_id: str) -> bool:
        """Return ``True`` if *deployment_id* has a previous revision to revert to."""
        try:
            record = self._get_record(deployment_id)
        except DeploymentError:
            return False
        return bool(
            record.rollback_available
            and record.previous_deployment_id
            and record.previous_deployment_id in self._records
        )

    async def get_rollback_target(self, deployment_id: str) -> Optional[DeploymentRecord]:
        """Return the :class:`DeploymentRecord` that would be restored by a rollback."""
        try:
            record = self._get_record(deployment_id)
        except DeploymentError:
            return None
        if record.previous_deployment_id:
            return self._records.get(record.previous_deployment_id)
        return None

    async def get_rollback_history(self, project_id: str) -> List[DeploymentRecord]:
        """Return all recorded deployments for *project_id*, newest first."""
        records = [r for r in self._records.values() if r.project_id == project_id]
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    async def disable_rollback(self, deployment_id: str) -> None:
        """Prevent a deployment from being rolled back (e.g. after a migration)."""
        try:
            record = self._get_record(deployment_id)
            record.rollback_available = False
            logger.info("Rollback disabled", deployment_id=deployment_id)
        except DeploymentError:
            logger.warning("Cannot disable rollback — deployment not found", deployment_id=deployment_id)
