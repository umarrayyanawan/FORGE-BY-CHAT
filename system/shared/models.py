"""Shared domain enums and base Pydantic models used across the entire FORGE system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field

# ========================================================================== #
# Enums
# ========================================================================== #


class TaskStatus(StrEnum):
    """Lifecycle states for any FORGE task."""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    RETRYING = "retrying"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class AgentType(StrEnum):
    """Available specialist agent roles."""

    ARCHITECT = "architect"
    BACKEND = "backend"
    FRONTEND = "frontend"
    INFRA = "infra"
    QA = "qa"
    SECURITY = "security"
    DOCS = "docs"
    REFACTOR = "refactor"


class ExecutionPhase(StrEnum):
    """High-level phases of the FORGE software-production pipeline."""

    INTENT = "intent"
    CLARIFICATION = "clarification"
    SPECIFICATION = "specification"
    ARCHITECTURE = "architecture"
    TASK_GRAPH = "task_graph"
    AGENT_ASSIGNMENT = "agent_assignment"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    DEPLOYMENT = "deployment"
    MONITORING = "monitoring"
    ITERATION = "iteration"


class Priority(StrEnum):
    """Task / work-item priority levels."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DeployTarget(StrEnum):
    """Supported deployment infrastructure targets."""

    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    VERCEL = "vercel"
    RAILWAY = "railway"
    AWS = "aws"
    GCP = "gcp"


class Platform(StrEnum):
    """Application platform / surface area."""

    WEB = "web"
    MOBILE = "mobile"
    DESKTOP = "desktop"
    CLI = "cli"
    API = "api"


class ValidationStatus(StrEnum):
    """Result states for a validation check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WARNING = "warning"


# ========================================================================== #
# Base model helpers
# ========================================================================== #


class BaseForgeModel(BaseModel):
    """Root Pydantic model with FORGE-wide configuration applied."""

    model_config = ConfigDict(
        use_enum_values=True,
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )


class TimestampedModel(BaseForgeModel):
    """Adds auto-generated id, created_at and updated_at to any model."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def touch(self) -> TimestampedModel:
        """Return a copy of this model with updated_at refreshed."""
        return self.model_copy(update={"updated_at": datetime.utcnow()})
