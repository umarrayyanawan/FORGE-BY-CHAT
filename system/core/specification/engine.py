"""Specification Engine — Phase 3: Orchestrator.

The SpecificationEngine coordinates PRD generation, DB schema generation,
API contract creation, UI mapping, and dependency analysis into a single
ProjectSpec, which is persisted to PostgreSQL and returned to callers.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from system.core.intent.schemas import ProjectIntent
from system.core.specification.api_contract_generator import APIContractGenerator
from system.core.specification.dependency_analyzer import DependencyAnalyzer
from system.core.specification.prd_generator import PRDGenerator
from system.core.specification.schema_generator import SchemaGenerator
from system.core.specification.schemas import (
    DBSchema,
    PermissionMatrix,
    ProjectSpec,
    ServiceTopology,
)
from system.core.specification.ui_mapper import UIMapper
from system.observability.logging.logger import get_logger
from system.shared.database import Base
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import get_llm_client

logger = get_logger(__name__)


# ========================================================================== #
# SQLAlchemy ORM Model
# ========================================================================== #


class ProjectSpecDB(Base):
    """Persistent store for ProjectSpec objects."""

    __tablename__ = "forge_specs"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    spec_json: Mapped[str] = mapped_column(nullable=False)  # Full JSON serialisation
    prd_text: Mapped[str] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    estimated_complexity: Mapped[str] = mapped_column(nullable=False, default="medium")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# ========================================================================== #
# Internal helper: service topology and permission generation
# ========================================================================== #


def _derive_service_topology(intent: ProjectIntent, db_schema: DBSchema) -> ServiceTopology:
    """Derive a basic service topology from the intent without an LLM call.

    This uses deterministic rules so we don't waste tokens on straightforward
    topology decisions.
    """
    from system.shared.models import Platform

    services: list[dict[str, Any]] = []
    communication: list[dict[str, str]] = []

    # Always: API backend
    services.append(
        {
            "name": "api",
            "type": "api",
            "description": "Main FastAPI backend — REST endpoints + business logic",
            "port": 8000,
            "technology": "FastAPI / Python 3.12",
            "dependencies": ["database", "redis"],
            "replicas": 2,
            "resource_requirements": {"cpu": "500m", "memory": "512Mi"},
        }
    )

    # Always: PostgreSQL
    services.append(
        {
            "name": "database",
            "type": "database",
            "description": "Primary PostgreSQL database",
            "port": 5432,
            "technology": "PostgreSQL 16",
            "dependencies": [],
            "replicas": 1,
            "resource_requirements": {"cpu": "1000m", "memory": "2Gi"},
        }
    )

    # Always: Redis
    services.append(
        {
            "name": "redis",
            "type": "cache",
            "description": "In-memory cache and pub/sub broker",
            "port": 6379,
            "technology": "Redis 7",
            "dependencies": [],
            "replicas": 1,
            "resource_requirements": {"cpu": "250m", "memory": "512Mi"},
        }
    )

    # Frontend (for non-API projects)
    if intent.platform != Platform.API:
        services.append(
            {
                "name": "frontend",
                "type": "frontend",
                "description": "Next.js web application",
                "port": 3000,
                "technology": "Next.js 14 / TypeScript",
                "dependencies": ["api"],
                "replicas": 2,
                "resource_requirements": {"cpu": "250m", "memory": "512Mi"},
            }
        )
        communication.append(
            {
                "from": "frontend",
                "to": "api",
                "protocol": "HTTP",
                "description": "REST API calls via HTTPS",
            }
        )

    # Worker service if there are background-friendly features
    needs_worker = any(
        kw in " ".join(intent.core_features).lower()
        for kw in ("email", "notification", "report", "export", "schedule", "batch", "process")
    )
    if needs_worker or intent.integrations:
        services.append(
            {
                "name": "worker",
                "type": "worker",
                "description": "Celery background task worker",
                "port": None,
                "technology": "Celery / Python",
                "dependencies": ["redis", "database"],
                "replicas": 2,
                "resource_requirements": {"cpu": "500m", "memory": "512Mi"},
            }
        )
        communication.append(
            {
                "from": "api",
                "to": "worker",
                "protocol": "queue",
                "description": "Task dispatch via Redis queue",
            }
        )

    # ML service
    if intent.platform and hasattr(intent, "tech_preferences"):
        if "ml" in str(intent.tech_preferences).lower() or any(
            kw in " ".join(intent.core_features).lower()
            for kw in ("ml", "ai", "model", "prediction", "recommendation", "classification")
        ):
            services.append(
                {
                    "name": "ml-service",
                    "type": "ml",
                    "description": "Python ML inference service",
                    "port": 8001,
                    "technology": "FastAPI / PyTorch",
                    "dependencies": ["api"],
                    "replicas": 1,
                    "resource_requirements": {"cpu": "2000m", "memory": "4Gi"},
                }
            )
            communication.append(
                {
                    "from": "api",
                    "to": "ml-service",
                    "protocol": "HTTP",
                    "description": "Internal gRPC/HTTP for ML inference",
                }
            )

    communication.append(
        {
            "from": "api",
            "to": "database",
            "protocol": "TCP",
            "description": "SQLAlchemy async connection pool",
        }
    )
    communication.append(
        {
            "from": "api",
            "to": "redis",
            "protocol": "TCP",
            "description": "Cache reads/writes and pub/sub",
        }
    )

    return ServiceTopology(services=services, communication_patterns=communication)


def _derive_permissions_matrix(intent: ProjectIntent) -> PermissionMatrix:
    """Derive a role-based permission matrix from the intent."""
    features = " ".join(intent.core_features).lower()
    has_admin = "admin" in features or intent.platform and True

    roles = ["admin", "user"]
    if has_admin:
        roles = ["admin", "manager", "user"]

    # Standard permission strings per role
    user_permissions = [
        "profile:read",
        "profile:write",
        "dashboard:read",
    ]
    admin_permissions = [
        "*",  # superuser wildcard
    ]

    # Add feature-specific permissions
    for feature in intent.core_features:
        slug = feature.lower().replace(" ", "_")
        user_permissions.extend(
            [
                f"{slug}:read",
                f"{slug}:create",
                f"{slug}:update",
            ]
        )
        admin_permissions.extend(
            [
                f"{slug}:read",
                f"{slug}:create",
                f"{slug}:update",
                f"{slug}:delete",
                f"{slug}:manage",
            ]
        )

    permissions: dict[str, list[str]] = {
        "admin": admin_permissions,
        "user": list(dict.fromkeys(user_permissions)),  # deduplicate
    }

    if "manager" in roles:
        manager_permissions = [
            p for p in admin_permissions if not p.endswith(":delete") and p != "*"
        ]
        manager_permissions.extend(["reports:read", "reports:export"])
        permissions["manager"] = list(dict.fromkeys(manager_permissions))

    return PermissionMatrix(roles=roles, permissions=permissions)


def _determine_complexity(intent: ProjectIntent, db_schema: DBSchema) -> str:
    """Determine project complexity from signals in the intent and schema."""
    score = 0

    # Features count
    score += len(intent.core_features) * 2
    # Tables count
    score += len(db_schema.tables)
    # Integrations
    score += len(intent.integrations) * 3
    # Scale
    scale = (intent.scale_requirements or "").lower()
    if "million" in scale or "1m" in scale:
        score += 20
    elif "100k" in scale or "10k" in scale:
        score += 10
    elif "1k" in scale or "1000" in scale:
        score += 5
    # Compliance
    score += len(intent.security_requirements) * 5

    if score < 20:
        return "low"
    elif score < 50:
        return "medium"
    elif score < 100:
        return "high"
    else:
        return "enterprise"


def _determine_tech_stack(intent: ProjectIntent) -> dict[str, str]:
    """Determine tech stack from intent preferences and feature requirements."""
    from system.shared.models import Platform

    stack: dict[str, str] = {
        "backend": "FastAPI + Python 3.12",
        "database": "PostgreSQL 16",
        "cache": "Redis 7",
        "message_queue": "Celery + Redis",
        "containerisation": "Docker + Docker Compose",
        "ci_cd": "GitHub Actions",
    }

    # Frontend
    if intent.platform in (Platform.WEB, Platform.DESKTOP):
        stack["frontend"] = "Next.js 14 + TypeScript + Tailwind CSS"
    elif intent.platform == Platform.MOBILE:
        stack["frontend"] = "React Native + Expo"
    elif intent.platform != Platform.API:
        stack["frontend"] = "Next.js 14 + TypeScript + Tailwind CSS"

    # Tech preferences from user
    prefs = intent.tech_preferences or {}
    for layer, tech in prefs.items():
        stack[layer.lower()] = tech

    # ML
    features_text = " ".join(intent.core_features).lower()
    if any(kw in features_text for kw in ("ml", "ai", "model", "predict", "recommend")):
        stack["ml"] = "PyTorch + Hugging Face Transformers"

    # Payments
    if "stripe" in " ".join(intent.integrations).lower() or any(
        "payment" in f.lower() for f in intent.core_features
    ):
        stack["payments"] = "Stripe"

    # Search
    if any(kw in features_text for kw in ("search", "full-text", "elasticsearch")):
        stack["search"] = "PostgreSQL Full-Text Search + pgvector"

    # Monitoring
    stack["observability"] = "OpenTelemetry + Prometheus + Grafana"
    stack["logging"] = "Structlog + Loki"

    return stack


# ========================================================================== #
# SpecificationEngine
# ========================================================================== #


class SpecificationEngine:
    """Orchestrates the full specification pipeline.

    Produces a ProjectSpec from a ProjectIntent by running:
      PRDGenerator → SchemaGenerator → APIContractGenerator →
      UIMapper → DependencyAnalyzer → assembly → persistence
    """

    def __init__(
        self,
        llm_client: Any | None = None,
        db: AsyncSession | None = None,
    ) -> None:
        self._llm = llm_client or get_llm_client()
        self._db = db  # Optional — injected per-request via FastAPI Depends

        # Sub-components
        self._prd_gen = PRDGenerator(self._llm)
        self._schema_gen = SchemaGenerator(self._llm)
        self._api_gen = APIContractGenerator(self._llm)
        self._ui_mapper = UIMapper(self._llm)
        self._dep_analyzer = DependencyAnalyzer(self._llm)

    async def generate_spec(
        self,
        project_id: str,
        intent: ProjectIntent,
        db: AsyncSession | None = None,
    ) -> ProjectSpec:
        """Run the full specification pipeline.

        Args:
            project_id: UUID string linking this spec to the project.
            intent:     Validated ProjectIntent.
            db:         Optional AsyncSession override (for DI in routes).

        Returns:
            A fully-populated, persisted ProjectSpec.

        Raises:
            SpecificationError: On any pipeline step failure.
        """
        session = db or self._db
        logger.info("starting_spec_pipeline", project_id=project_id)

        try:
            # Step 1: Generate PRD
            logger.info("step_1_generating_prd", project_id=project_id)
            prd = await self._prd_gen.generate(intent)

            # Step 2: Generate DB schema
            logger.info("step_2_generating_db_schema", project_id=project_id)
            db_schema = await self._schema_gen.generate(intent, prd)

            # Step 3: Generate API contract
            logger.info("step_3_generating_api_contract", project_id=project_id)
            api_contract = await self._api_gen.generate(intent, db_schema)

            # Step 4: Generate UI structure
            logger.info("step_4_mapping_ui_structure", project_id=project_id)
            ui_structure = await self._ui_mapper.map(intent, api_contract)

            # Step 5: Derive service topology (deterministic)
            logger.info("step_5_deriving_service_topology", project_id=project_id)
            service_topology = _derive_service_topology(intent, db_schema)

            # Step 6: Derive permissions matrix (deterministic)
            logger.info("step_6_deriving_permissions_matrix", project_id=project_id)
            permissions_matrix = _derive_permissions_matrix(intent)

            # Step 7: Determine tech stack
            logger.info("step_7_determining_tech_stack", project_id=project_id)
            tech_stack = _determine_tech_stack(intent)

            # Step 8: Determine complexity
            estimated_complexity = _determine_complexity(intent, db_schema)

            # Step 9: Assemble partial spec (needed by dependency analyzer)
            partial_spec = ProjectSpec(
                project_id=project_id,
                intent=intent,
                prd=prd,
                db_schema=db_schema,
                api_contract=api_contract,
                ui_structure=ui_structure,
                service_topology=service_topology,
                permissions_matrix=permissions_matrix,
                feature_dependency_map=[],  # filled next
                tech_stack=tech_stack,
                estimated_complexity=estimated_complexity,
            )

            # Step 10: Analyze feature dependencies
            logger.info("step_10_analyzing_dependencies", project_id=project_id)
            feature_deps = await self._dep_analyzer.analyze(partial_spec)

            # Finalize spec
            spec = ProjectSpec(
                project_id=project_id,
                intent=intent,
                prd=prd,
                db_schema=db_schema,
                api_contract=api_contract,
                ui_structure=ui_structure,
                service_topology=service_topology,
                permissions_matrix=permissions_matrix,
                feature_dependency_map=feature_deps,
                tech_stack=tech_stack,
                estimated_complexity=estimated_complexity,
                version=1,
            )

            # Step 11: Persist to database
            if session:
                logger.info("step_11_persisting_spec", project_id=project_id)
                await self._persist_spec(spec, session)

            logger.info(
                "spec_pipeline_complete",
                project_id=project_id,
                tables=len(db_schema.tables),
                endpoints=api_contract.endpoint_count(),
                pages=len(ui_structure.pages),
                complexity=estimated_complexity,
            )

            return spec

        except SpecificationError:
            raise
        except Exception as exc:
            raise SpecificationError(
                f"Unexpected error during spec generation: {exc}",
                details={"project_id": project_id},
            ) from exc

    async def get_spec(
        self,
        project_id: str,
        db: AsyncSession | None = None,
    ) -> ProjectSpec | None:
        """Retrieve a persisted ProjectSpec by project ID.

        Args:
            project_id: The project UUID.
            db:         AsyncSession for DB access.

        Returns:
            ProjectSpec if found, None otherwise.
        """
        session = db or self._db
        if not session:
            raise SpecificationError("No database session available for get_spec")

        result = await session.execute(
            select(ProjectSpecDB).where(ProjectSpecDB.project_id == project_id)
        )
        row = result.scalar_one_or_none()

        if row is None:
            return None

        try:
            data = json.loads(row.spec_json)
            return ProjectSpec.model_validate(data)
        except Exception as exc:
            raise SpecificationError(
                f"Failed to deserialise spec for project {project_id}: {exc}",
            ) from exc

    async def update_spec(
        self,
        project_id: str,
        updates: dict[str, Any],
        db: AsyncSession | None = None,
    ) -> ProjectSpec:
        """Apply incremental updates to an existing ProjectSpec.

        Args:
            project_id: The project UUID.
            updates:    Dict of top-level fields to update (merged with existing).
            db:         AsyncSession for DB access.

        Returns:
            Updated ProjectSpec.

        Raises:
            SpecificationError: If the spec is not found or update fails.
        """
        session = db or self._db
        if not session:
            raise SpecificationError("No database session available for update_spec")

        existing = await self.get_spec(project_id, session)
        if existing is None:
            raise SpecificationError(
                f"ProjectSpec not found for project_id={project_id}",
                code="SPECIFICATION_NOT_FOUND",
            )

        # Merge updates into the existing spec
        existing_dict = existing.model_dump()
        existing_dict.update(updates)
        existing_dict["version"] = existing.version + 1
        existing_dict["updated_at"] = datetime.utcnow().isoformat()

        updated_spec = ProjectSpec.model_validate(existing_dict)

        # Persist the updated spec
        await session.execute(
            update(ProjectSpecDB)
            .where(ProjectSpecDB.project_id == project_id)
            .values(
                spec_json=updated_spec.model_dump_json(),
                version=updated_spec.version,
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()

        logger.info(
            "spec_updated",
            project_id=project_id,
            new_version=updated_spec.version,
            fields_updated=list(updates.keys()),
        )

        return updated_spec

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    async def _persist_spec(self, spec: ProjectSpec, session: AsyncSession) -> None:
        """Insert or replace a ProjectSpecDB row."""
        # Check for existing row
        result = await session.execute(
            select(ProjectSpecDB).where(ProjectSpecDB.project_id == spec.project_id)
        )
        existing_row = result.scalar_one_or_none()

        spec_json = spec.model_dump_json()

        if existing_row:
            await session.execute(
                update(ProjectSpecDB)
                .where(ProjectSpecDB.project_id == spec.project_id)
                .values(
                    spec_json=spec_json,
                    prd_text=spec.prd,
                    version=spec.version,
                    estimated_complexity=spec.estimated_complexity,
                    updated_at=datetime.utcnow(),
                )
            )
        else:
            new_row = ProjectSpecDB(
                id=spec.id,
                project_id=spec.project_id,
                spec_json=spec_json,
                prd_text=spec.prd,
                version=spec.version,
                estimated_complexity=spec.estimated_complexity,
                created_at=spec.created_at,
                updated_at=spec.updated_at,
            )
            session.add(new_row)

        await session.commit()

        logger.info(
            "spec_persisted",
            project_id=spec.project_id,
            version=spec.version,
            row_id=spec.id,
        )
