"""Pydantic schemas for the FORGE Architecture Planning Engine (Phase 4).

Defines the ArchitecturePlan produced by the ArchitectureEngine from a
ProjectSpec.  This plan is consumed by the TaskGraphEngine to decompose the
work into agent-executable tasks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from system.shared.models import BaseForgeModel, TimestampedModel, DeployTarget


# ========================================================================== #
# Service Definition
# ========================================================================== #


class ServiceDefinition(BaseForgeModel):
    """A single deployable service in the architecture."""

    name: str = Field(..., description="Unique service name, e.g. 'api', 'worker', 'frontend'.")
    service_type: str = Field(
        ...,
        description="Category: 'backend' | 'frontend' | 'worker' | 'database' | 'cache' | 'gateway'.",
    )
    technology: str = Field(..., description="Primary technology, e.g. 'FastAPI', 'React', 'PostgreSQL'.")
    language: str = Field(default="", description="Programming language, e.g. 'Python', 'TypeScript'.")
    port: int = Field(default=8000, gt=0, description="Primary listen port.")
    dependencies: List[str] = Field(
        default_factory=list,
        description="Names of other services this service depends on at runtime.",
    )
    environment_variables: Dict[str, str] = Field(
        default_factory=dict,
        description="Required env-var names (values are placeholders or descriptions).",
    )
    scaling: Dict[str, Any] = Field(
        default_factory=dict,
        description="Scaling configuration: min_replicas, max_replicas, cpu_threshold, etc.",
    )
    health_check_path: str = Field(default="/health", description="HTTP path for liveness probes.")
    description: str = Field(default="", description="Human-readable service description.")


# ========================================================================== #
# Infra Component
# ========================================================================== #


class InfraComponent(BaseForgeModel):
    """An infrastructure component (not a service) required by the architecture."""

    name: str = Field(..., description="Component name, e.g. 'postgres', 'redis', 'rabbitmq'.")
    component_type: str = Field(
        ...,
        description="Kind: 'database' | 'cache' | 'queue' | 'storage' | 'cdn' | 'lb'.",
    )
    technology: str = Field(..., description="Concrete technology, e.g. 'PostgreSQL 16'.")
    managed: bool = Field(
        default=False,
        description="True if using a managed cloud service; False for self-hosted.",
    )
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Component-specific configuration knobs.",
    )
    description: str = Field(default="", description="What this component is used for.")


# ========================================================================== #
# Security Architecture
# ========================================================================== #


class SecurityArchitecture(BaseForgeModel):
    """Security controls baked into the architecture."""

    auth_mechanism: str = Field(
        default="JWT",
        description="Primary authentication: 'JWT' | 'OAuth2' | 'API_KEY' | 'mTLS'.",
    )
    authorization_model: str = Field(
        default="RBAC",
        description="Authorization model: 'RBAC' | 'ABAC' | 'ACL'.",
    )
    encryption_at_rest: bool = Field(default=True)
    encryption_in_transit: bool = Field(default=True)
    secrets_management: str = Field(
        default="environment",
        description="'environment' | 'vault' | 'aws_secrets_manager' | 'k8s_secrets'.",
    )
    additional_controls: List[str] = Field(
        default_factory=list,
        description="Additional controls: WAF, DDoS protection, IP allowlist, etc.",
    )


# ========================================================================== #
# Architecture Plan (top-level)
# ========================================================================== #


class ArchitecturePlan(TimestampedModel):
    """Complete system architecture produced by the ArchitectureEngine.

    Consumed by the TaskGraphEngine to decompose work into agent tasks.
    Each field drives a different category of task generation:
    - services       → backend, frontend, infra tasks per service
    - infra_components → infra tasks for databases/caches/queues
    - deployment_target → deployment tasks (Docker / K8s / Terraform)
    """

    plan_id: str = Field(..., description="Unique plan identifier.")
    project_id: str = Field(..., description="FORGE project this plan belongs to.")
    spec_id: str = Field(..., description="ProjectSpec this plan was derived from.")

    services: List[ServiceDefinition] = Field(
        default_factory=list,
        description="All application services in the system.",
    )
    infra_components: List[InfraComponent] = Field(
        default_factory=list,
        description="Infrastructure components required by the services.",
    )
    deployment_target: DeployTarget = Field(
        default=DeployTarget.DOCKER,
        description="Primary deployment infrastructure target.",
    )
    security: SecurityArchitecture = Field(
        default_factory=SecurityArchitecture,
        description="Security architecture decisions.",
    )

    # High-level decisions
    architecture_pattern: str = Field(
        default="monolith",
        description="Top-level pattern: 'monolith' | 'microservices' | 'serverless' | 'event-driven'.",
    )
    database_strategy: str = Field(
        default="single",
        description="DB strategy: 'single' | 'per-service' | 'cqrs' | 'event-sourcing'.",
    )
    api_gateway: bool = Field(
        default=False,
        description="Whether an API gateway is in front of the services.",
    )
    event_driven: bool = Field(
        default=False,
        description="Whether the architecture includes an event bus.",
    )

    # Mermaid diagram (optional, generated by ArchitectureEngine)
    architecture_diagram: str = Field(
        default="",
        description="Mermaid C4 or architecture diagram source.",
    )
    adr_notes: List[str] = Field(
        default_factory=list,
        description="Architecture Decision Records captured during planning.",
    )

    version: int = Field(default=1, description="Plan revision number.")

    @field_validator("architecture_pattern")
    @classmethod
    def validate_pattern(cls, v: str) -> str:
        valid = {"monolith", "microservices", "serverless", "event-driven"}
        return v if v in valid else "monolith"

    def backend_services(self) -> List[ServiceDefinition]:
        """Return only backend/worker service definitions."""
        return [s for s in self.services if s.service_type in {"backend", "worker"}]

    def frontend_services(self) -> List[ServiceDefinition]:
        """Return only frontend service definitions."""
        return [s for s in self.services if s.service_type == "frontend"]

    def infra_services(self) -> List[ServiceDefinition]:
        """Return only gateway/infrastructure service definitions."""
        return [s for s in self.services if s.service_type in {"gateway", "database", "cache"}]
