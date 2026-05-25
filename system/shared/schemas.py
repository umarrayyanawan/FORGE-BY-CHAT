"""Shared Pydantic request / response schemas used across FORGE API layers."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, TypeVar

from pydantic import Field, field_validator

from system.shared.models import BaseForgeModel, ValidationStatus

# Generic type variable for paginated item lists
T = TypeVar("T")


# ========================================================================== #
# Pagination
# ========================================================================== #


class PaginationParams(BaseForgeModel):
    """Query parameters for paginated list endpoints."""

    page: int = Field(default=1, ge=1, description="1-based page number")
    page_size: int = Field(default=20, ge=1, le=100, description="Items per page")

    @property
    def offset(self) -> int:
        """SQL OFFSET value derived from page / page_size."""
        return (self.page - 1) * self.page_size


class PaginatedResponse[T](BaseForgeModel):
    """Generic envelope for paginated list API responses."""

    items: list[Any] = Field(description="The current page of results")
    total: int = Field(ge=0, description="Total number of matching items")
    page: int = Field(ge=1, description="Current 1-based page number")
    page_size: int = Field(ge=1, description="Items per page")
    pages: int = Field(ge=0, description="Total number of pages")

    @classmethod
    def build(
        cls,
        items: list[Any],
        total: int,
        params: PaginationParams,
    ) -> PaginatedResponse:
        """Construct a PaginatedResponse from a query result."""
        pages = math.ceil(total / params.page_size) if total else 0
        return cls(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            pages=pages,
        )


# ========================================================================== #
# Health-check
# ========================================================================== #


class HealthCheckResponse(BaseForgeModel):
    """Response schema for GET /health."""

    status: str = Field(description="'ok' or 'degraded'")
    version: str = Field(description="FORGE application version")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    components: dict[str, str] = Field(
        default_factory=dict,
        description="Per-component health status map",
    )


# ========================================================================== #
# Error & success envelopes
# ========================================================================== #


class ErrorResponse(BaseForgeModel):
    """Standardised error response body returned on 4xx / 5xx responses."""

    error: str = Field(description="Human-readable error description")
    code: str = Field(description="Machine-readable error code (SCREAMING_SNAKE_CASE)")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured context about the error",
    )
    request_id: str | None = Field(
        default=None,
        description="Trace / request ID for correlation with logs",
    )


class SuccessResponse(BaseForgeModel):
    """Generic success envelope for simple mutation endpoints."""

    message: str = Field(description="Human-readable success description")
    data: Any = Field(default=None, description="Optional payload")


# ========================================================================== #
# Agent contract
# ========================================================================== #


class AgentContractSchema(BaseForgeModel):
    """Describes the scoped contract handed to each specialist agent.

    An agent contract explicitly declares what the agent is allowed to do,
    what files it may touch, and the criteria by which its output is judged.
    """

    identity: str = Field(description="The agent's role identifier (e.g. 'backend', 'qa')")
    objective: str = Field(description="Plain-English statement of what this agent must accomplish")
    allowed_files: list[str] = Field(
        default_factory=list,
        description="Glob patterns / explicit paths the agent is permitted to read/write",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Hard rules the agent must not violate",
    )
    validation_rules: list[str] = Field(
        default_factory=list,
        description="Automated checks run against the agent's output",
    )
    success_criteria: list[str] = Field(
        default_factory=list,
        description="Human-readable criteria defining 'done'",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary structured context injected into the agent prompt",
    )

    @field_validator("identity")
    @classmethod
    def identity_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("identity must not be blank")
        return v.strip()

    @field_validator("objective")
    @classmethod
    def objective_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("objective must not be blank")
        return v.strip()


# ========================================================================== #
# Validation result
# ========================================================================== #


class ValidationResultSchema(BaseForgeModel):
    """Structured result of running automated validation checks."""

    status: ValidationStatus = Field(description="Overall pass/fail/warn status")
    checks: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of individual check results: {'name', 'status', 'message'}",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Blocking errors that must be resolved before proceeding",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking issues that should be reviewed",
    )
    duration_ms: float | None = Field(
        default=None,
        description="Wall-clock time taken to run all checks, in milliseconds",
    )

    @property
    def passed(self) -> bool:
        return self.status == ValidationStatus.PASSED

    @property
    def failed(self) -> bool:
        return self.status == ValidationStatus.FAILED
