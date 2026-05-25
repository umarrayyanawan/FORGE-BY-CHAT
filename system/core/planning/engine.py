"""Architecture Engine — Phase 4 of the FORGE pipeline.

Orchestrates all planning sub-components (stack recommender, topology engine,
architect planner, scalability validator) to produce a complete ArchitecturePlan
from a ProjectSpec.  Persists and loads plans via PostgreSQL using SQLAlchemy.

Usage::

    engine = ArchitectureEngine(llm_client=get_llm_client(), db=session)
    plan   = await engine.plan(project_id, spec)
    loaded = await engine.get_plan(project_id)
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
import uuid

from sqlalchemy import DateTime, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from system.core.planning.architect import ArchitecturePlanner
from system.core.planning.scalability_validator import ScalabilityValidator
from system.core.planning.schemas import (
    ArchitecturePlan,
    InfraComponent,
    InfrastructurePlan,
    RepoTopology,
    ScalabilityProfile,
    SecurityArchitecture,
    SecurityProfile,
)
from system.core.planning.stack_recommender import StackRecommender
from system.core.planning.topology_engine import TopologyEngine
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.database import Base
from system.shared.exceptions import ArchitectureError
from system.shared.models import DeployTarget

logger = get_logger(__name__)


# ========================================================================== #
# SQLAlchemy ORM model
# ========================================================================== #


class ArchitecturePlanDB(Base):
    """Persistent storage for ArchitecturePlan documents."""

    __tablename__ = "forge_architecture_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


# ========================================================================== #
# Architecture Engine
# ========================================================================== #


class ArchitectureEngine:
    """Full planning pipeline: from ProjectSpec → ArchitecturePlan.

    Orchestrates:
    1. StackRecommender   — pick the technology stack
    2. TopologyEngine     — define services and repo layout
    3. ArchitecturePlanner — infra plan, scalability, security, ADRs, diagram
    4. ScalabilityValidator — validate the resulting plan
    5. Persistence        — store plan in PostgreSQL
    """

    def __init__(self, llm_client: Any, db: AsyncSession) -> None:
        self._db = db
        self._stack_recommender = StackRecommender(llm_client=llm_client)
        self._topology_engine = TopologyEngine(llm_client=llm_client)
        self._planner = ArchitecturePlanner(llm_client=llm_client)
        self._validator = ScalabilityValidator()

    # ------------------------------------------------------------------
    # Main planning entry point
    # ------------------------------------------------------------------

    async def plan(self, project_id: str, spec: ProjectSpec) -> ArchitecturePlan:
        """Run the full architecture planning pipeline and return an ArchitecturePlan.

        Args:
            project_id: FORGE project identifier.
            spec:       Finalised ProjectSpec to plan from.

        Returns:
            A fully populated and validated ArchitecturePlan.

        Raises:
            ArchitectureError: If any planning step fails critically.
        """
        logger.info("Architecture planning started", project_id=project_id)

        try:
            # ---- Step 1: Recommend technology stack ---------------------
            stack: dict[str, str] = await self._stack_recommender.recommend(spec.intent, spec)
            logger.info("Stack recommended", stack=stack)

            # ---- Step 2: Generate repo topology -------------------------
            topology: RepoTopology = await self._topology_engine.generate(spec, stack)
            dir_structure = self._topology_engine.generate_directory_structure(topology)
            topology = topology.model_copy(update={"directory_structure": dir_structure})
            logger.info(
                "Topology generated",
                repo_type=topology.repo_type,
                services=len(topology.services),
            )

            # ---- Step 3: Infrastructure plan ----------------------------
            infra: InfrastructurePlan = await self._planner.plan_infra(spec, stack)

            # ---- Step 4: Scalability assessment -------------------------
            scalability: ScalabilityProfile = await self._planner.assess_scalability(spec, stack)

            # ---- Step 5: Security profile --------------------------------
            security: SecurityProfile = await self._planner.generate_security_profile(spec)

            # ---- Step 6: ADRs -------------------------------------------
            adr_decisions = [
                f"Adopt {stack.get('backend', 'FastAPI')} as the primary backend framework",
                f"Use {stack.get('database', 'PostgreSQL')} as the primary database",
                f"Deploy via {stack.get('infra', 'Docker')}",
                f"Authenticate with {stack.get('auth', 'JWT')}",
            ]
            adrs: list[dict[str, str]] = await self._planner.generate_adr(adr_decisions, stack)

            # ---- Step 7: Mermaid diagram ---------------------------------
            diagram: str = await self._planner.generate_mermaid_diagram(topology)

            # ---- Step 8: Build ArchitecturePlan -------------------------
            # Convert topology services → ServiceDefinitions on the plan
            services = topology.services

            # Convert infra cloud services → InfraComponents
            infra_components: list[InfraComponent] = [
                InfraComponent(
                    name=svc_name.lower().replace(" ", "_").replace("(", "").replace(")", ""),
                    component_type=_infer_component_type(svc_name),
                    technology=svc_name,
                    managed=infra.cloud_provider not in {"self-hosted", "docker"},
                    config={
                        "monthly_cost_usd": infra.cost_breakdown.get(svc_name, 0.0),
                        "region": infra.regions[0] if infra.regions else "us-east-1",
                    },
                    description=f"Managed {svc_name} provided by {infra.cloud_provider}.",
                )
                for svc_name in infra.cloud_services
            ]

            # Build SecurityArchitecture from SecurityProfile
            security_arch = SecurityArchitecture(
                auth_mechanism=security.auth_method,
                authorization_model="RBAC",
                encryption_at_rest=True,
                encryption_in_transit=security.https_enforced,
                secrets_management="aws_secrets_manager"
                if infra.cloud_provider == "aws"
                else "environment",
                additional_controls=security.additional_controls,
            )

            # Determine architecture pattern
            backend_count = sum(1 for s in services if s.service_type == "backend")
            if backend_count >= 3:
                arch_pattern = "microservices"
            elif any("event" in f.lower() for f in spec.intent.core_features):
                arch_pattern = "event-driven"
            else:
                arch_pattern = "monolith"

            # ADR notes as strings
            adr_notes = [adr["title"] + ": " + adr["decision"] for adr in adrs]

            plan = ArchitecturePlan(
                plan_id=str(uuid.uuid4()),
                project_id=project_id,
                spec_id=spec.id,
                services=services,
                infra_components=infra_components,
                deployment_target=_stack_to_deploy_target(stack),
                security=security_arch,
                architecture_pattern=arch_pattern,
                database_strategy=_infer_db_strategy(scalability, spec),
                api_gateway=any(s.service_type == "gateway" for s in services),
                event_driven="event" in arch_pattern,
                architecture_diagram=diagram,
                adr_notes=adr_notes,
                version=1,
            )

            # ---- Step 9: Validate ----------------------------------------
            validation = self._validator.validate(plan, spec)
            if not validation.is_valid:
                logger.warning(
                    "Architecture plan has scalability issues",
                    issues=validation.issues,
                    project_id=project_id,
                )
                # Append issues to adr_notes for transparency — do NOT abort
                for issue in validation.issues:
                    plan.adr_notes.append(f"[SCALABILITY ISSUE] {issue}")

            for rec in validation.recommendations:
                logger.info("Scalability recommendation", recommendation=rec)

            # ---- Step 10: Persist ----------------------------------------
            await self.persist_plan(plan)

            logger.info(
                "Architecture planning complete",
                project_id=project_id,
                plan_id=plan.plan_id,
                services=len(services),
                infra_components=len(infra_components),
            )
            return plan

        except ArchitectureError:
            raise
        except Exception as exc:
            logger.error(
                "Architecture planning failed",
                project_id=project_id,
                error=str(exc),
                exc_info=True,
            )
            raise ArchitectureError(
                f"Failed to generate architecture plan for project {project_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def persist_plan(self, plan: ArchitecturePlan) -> None:
        """Upsert an ArchitecturePlan into PostgreSQL.

        Uses project_id as the natural key — subsequent calls overwrite the
        existing record so there is always exactly one plan per project.
        """
        plan_json = json.loads(plan.model_dump_json())

        # Check for existing record
        stmt = select(ArchitecturePlanDB).where(ArchitecturePlanDB.project_id == plan.project_id)
        result = await self._db.execute(stmt)
        existing: ArchitecturePlanDB | None = result.scalar_one_or_none()

        if existing:
            existing.plan_json = plan_json
            existing.updated_at = datetime.utcnow()
            logger.debug("Updated existing architecture plan", project_id=plan.project_id)
        else:
            db_record = ArchitecturePlanDB(
                id=plan.plan_id,
                project_id=plan.project_id,
                plan_json=plan_json,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            self._db.add(db_record)
            logger.debug("Created new architecture plan record", project_id=plan.project_id)

        await self._db.flush()

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get_plan(self, project_id: str) -> ArchitecturePlan | None:
        """Load an ArchitecturePlan from PostgreSQL by project_id.

        Returns None if no plan exists for the project.
        """
        stmt = select(ArchitecturePlanDB).where(ArchitecturePlanDB.project_id == project_id)
        result = await self._db.execute(stmt)
        record: ArchitecturePlanDB | None = result.scalar_one_or_none()

        if record is None:
            return None

        try:
            return ArchitecturePlan.model_validate(record.plan_json)
        except Exception as exc:
            logger.error(
                "Failed to deserialise architecture plan",
                project_id=project_id,
                error=str(exc),
            )
            raise ArchitectureError(
                f"Corrupt architecture plan in DB for project {project_id}: {exc}"
            ) from exc


# ========================================================================== #
# Internal helpers
# ========================================================================== #


def _infer_component_type(service_name: str) -> str:
    """Guess the InfraComponent type from the cloud service name."""
    s = service_name.lower()
    if any(kw in s for kw in ["rds", "sql", "postgres", "mysql", "database", "db"]):
        return "database"
    if any(kw in s for kw in ["redis", "elasticache", "memorystore", "cache", "memcache"]):
        return "cache"
    if any(kw in s for kw in ["sqs", "pub/sub", "pubsub", "rabbitmq", "kafka", "queue"]):
        return "queue"
    if any(kw in s for kw in ["s3", "storage", "blob", "gcs", "minio"]):
        return "storage"
    if any(kw in s for kw in ["cloudfront", "cdn", "cloudflare"]):
        return "cdn"
    if any(kw in s for kw in ["alb", "elb", "load balancing", "nginx", "haproxy"]):
        return "lb"
    return "storage"


def _stack_to_deploy_target(stack: dict[str, str]) -> DeployTarget:
    """Map infra stack value to a DeployTarget enum."""
    infra = stack.get("infra", "docker").lower()
    if "kubernetes" in infra or "eks" in infra or "gke" in infra or "k8s" in infra:
        return DeployTarget.KUBERNETES
    if "vercel" in infra:
        return DeployTarget.VERCEL
    if "railway" in infra:
        return DeployTarget.RAILWAY
    if "aws" in infra:
        return DeployTarget.AWS
    if "gcp" in infra:
        return DeployTarget.GCP
    return DeployTarget.DOCKER


def _infer_db_strategy(scalability: ScalabilityProfile, spec: ProjectSpec) -> str:
    """Infer database strategy from scalability profile and spec."""
    if scalability.database_scaling == "sharding":
        return "event-sourcing"
    if scalability.database_scaling == "read-replicas":
        return "cqrs"
    features = " ".join(spec.intent.core_features).lower()
    if "event" in features or "audit" in features:
        return "event-sourcing"
    services_count = len(spec.service_topology.services) if spec.service_topology else 1
    if services_count > 3:
        return "per-service"
    return "single"
