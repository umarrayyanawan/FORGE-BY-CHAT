"""Pydantic schemas for the FORGE Architecture Planning Engine (Phase 4).

Defines the ArchitecturePlan produced by the ArchitectureEngine from a
ProjectSpec.  This plan is consumed by the TaskGraphEngine to decompose the
work into agent-executable tasks.
"""

from __future__ import annotations

import uuid
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


# ========================================================================== #
# Repo Topology
# ========================================================================== #


class RepoTopology(BaseForgeModel):
    """Repository organisation strategy and service layout."""

    topology_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique topology identifier.",
    )
    project_id: str = Field(..., description="FORGE project this topology belongs to.")
    repo_type: str = Field(
        default="monorepo",
        description="'monorepo' | 'polyrepo' — organisation of source repositories.",
    )
    services: List[ServiceDefinition] = Field(
        default_factory=list,
        description="All deployable services in this topology.",
    )
    monorepo_root: Optional[str] = Field(
        default=".",
        description="Root path of the monorepo (if repo_type == 'monorepo').",
    )
    directory_structure: Dict[str, Any] = Field(
        default_factory=dict,
        description="Nested dict representing the repo file tree.",
    )

    @field_validator("repo_type")
    @classmethod
    def validate_repo_type(cls, v: str) -> str:
        if v not in {"monorepo", "polyrepo"}:
            return "monorepo"
        return v


# ========================================================================== #
# Infrastructure Plan
# ========================================================================== #


class InfrastructurePlan(BaseForgeModel):
    """Cloud infrastructure plan derived from the stack and deployment target."""

    cloud_provider: str = Field(
        default="aws",
        description="Target cloud provider: 'aws' | 'gcp' | 'azure' | 'self-hosted'.",
    )
    cloud_services: List[str] = Field(
        default_factory=list,
        description="Required managed cloud services, e.g. ['RDS', 'ElastiCache', 'EKS'].",
    )
    estimated_monthly_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated monthly infrastructure cost in USD.",
    )
    cost_breakdown: Dict[str, float] = Field(
        default_factory=dict,
        description="Per-service cost breakdown: {'RDS': 150.0, 'EKS': 300.0, ...}.",
    )
    regions: List[str] = Field(
        default_factory=list,
        description="Deployment regions, e.g. ['us-east-1', 'eu-west-1'].",
    )
    high_availability: bool = Field(
        default=False,
        description="Whether multi-AZ / multi-region HA is configured.",
    )
    disaster_recovery: str = Field(
        default="none",
        description="DR strategy: 'none' | 'warm-standby' | 'hot-standby' | 'multi-region'.",
    )
    notes: List[str] = Field(
        default_factory=list,
        description="Additional infrastructure notes and recommendations.",
    )


# ========================================================================== #
# Scalability Profile
# ========================================================================== #


class ScalabilityProfile(BaseForgeModel):
    """Scalability assessment for the proposed architecture."""

    expected_users: str = Field(
        default="",
        description="Expected concurrent / total user count from spec requirements.",
    )
    requests_per_second: int = Field(
        default=0,
        ge=0,
        description="Estimated peak requests per second.",
    )
    data_volume_gb: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated data volume in GB.",
    )
    bottlenecks: List[str] = Field(
        default_factory=list,
        description="Identified scaling bottlenecks.",
    )
    recommendations: List[str] = Field(
        default_factory=list,
        description="Specific recommendations to address bottlenecks.",
    )
    horizontal_scaling: bool = Field(
        default=True,
        description="Whether services are designed for horizontal scaling.",
    )
    caching_strategy: str = Field(
        default="none",
        description="Primary caching approach: 'none' | 'redis' | 'cdn' | 'multi-layer'.",
    )
    database_scaling: str = Field(
        default="vertical",
        description="Database scaling strategy: 'vertical' | 'read-replicas' | 'sharding' | 'cqrs'.",
    )


# ========================================================================== #
# Security Profile
# ========================================================================== #


class SecurityProfile(BaseForgeModel):
    """Detailed security posture for the project."""

    auth_method: str = Field(
        default="JWT",
        description="Authentication method: 'JWT' | 'OAuth2' | 'API_KEY' | 'mTLS' | 'session'.",
    )
    https_enforced: bool = Field(default=True, description="HTTPS enforced on all endpoints.")
    rate_limiting: bool = Field(default=True, description="API rate limiting enabled.")
    input_validation: bool = Field(default=True, description="Server-side input validation.")
    sql_injection_protection: bool = Field(default=True)
    xss_protection: bool = Field(default=True)
    csrf_protection: bool = Field(default=True)
    cors_configured: bool = Field(default=True)
    secrets_in_env: bool = Field(
        default=True,
        description="Secrets managed via environment variables / secrets manager.",
    )
    compliance: List[str] = Field(
        default_factory=list,
        description="Required compliance standards: ['GDPR', 'SOC2', 'HIPAA', ...].",
    )
    additional_controls: List[str] = Field(
        default_factory=list,
        description="Additional security controls: WAF, DDoS protection, audit logging, etc.",
    )
    vulnerability_scanning: bool = Field(default=False)
    penetration_testing: bool = Field(default=False)
