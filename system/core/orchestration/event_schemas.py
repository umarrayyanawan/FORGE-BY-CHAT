"""Event bus schemas for the FORGE orchestration layer.

Every significant pipeline transition is captured as a typed ForgeEvent
subclass.  Events are published on the Redis pub-sub channel and stored in
a Redis sorted set for replay / auditing.

Event hierarchy:
    ForgeEvent
    ├── TaskCompletedEvent
    ├── TaskFailedEvent
    ├── DeploymentFailedEvent
    ├── TestFailedEvent
    ├── RetryTriggeredEvent
    └── PhaseCompletedEvent
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import Field

from system.shared.models import AgentType, BaseForgeModel, ExecutionPhase

# ========================================================================== #
# Base Event
# ========================================================================== #


class ForgeEvent(BaseForgeModel):
    """Root event class.  All domain events inherit from this.

    event_type is a snake_case string that identifies the event kind.
    Subscribers can use it to route events to the correct handler without
    needing to inspect the payload.
    """

    event_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique event identifier (UUID v4).",
    )
    event_type: str = Field(
        ...,
        description="Machine-readable event kind, e.g. 'task_completed'.",
    )
    project_id: str = Field(
        ...,
        description="FORGE project this event belongs to.",
    )
    task_id: str | None = Field(
        default=None,
        description="task_id of the TaskNode this event relates to (if applicable).",
    )
    agent_type: AgentType | None = Field(
        default=None,
        description="Agent type that produced this event (if applicable).",
    )
    phase: ExecutionPhase | None = Field(
        default=None,
        description="Execution phase at the time of the event.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary structured data for this event type.",
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp when the event was created.",
    )

    @property
    def timestamp_ms(self) -> float:
        """Unix timestamp in milliseconds — used as Redis sorted-set score."""
        return self.timestamp.timestamp() * 1000


# ========================================================================== #
# Task lifecycle events
# ========================================================================== #


class TaskCompletedEvent(ForgeEvent):
    """Emitted when a TaskNode transitions to COMPLETED status."""

    event_type: str = Field(default="task_completed", frozen=True)
    output_artifacts: list[str] = Field(
        default_factory=list,
        description="File paths produced by the completing agent.",
    )
    duration_seconds: float | None = Field(
        default=None,
        description="Wall-clock seconds from task start to completion.",
    )
    tokens_used: int | None = Field(
        default=None,
        description="Actual LLM tokens consumed by the agent.",
    )


class TaskFailedEvent(ForgeEvent):
    """Emitted when a TaskNode fails (before or after retries)."""

    event_type: str = Field(default="task_failed", frozen=True)
    error_message: str = Field(..., description="Human-readable error description.")
    error_type: str = Field(
        default="unknown",
        description="Error classification: 'timeout', 'validation', 'agent', 'infra', etc.",
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        description="Number of attempts made before this failure.",
    )
    is_permanent: bool = Field(
        default=False,
        description="True when max retries have been exhausted and the failure is permanent.",
    )


class TaskStartedEvent(ForgeEvent):
    """Emitted when a TaskNode transitions to RUNNING status."""

    event_type: str = Field(default="task_started", frozen=True)
    worker_id: str | None = Field(
        default=None,
        description="Celery worker hostname that picked up this task.",
    )
    queue_name: str | None = Field(
        default=None,
        description="Queue from which the task was consumed.",
    )


# ========================================================================== #
# Deployment events
# ========================================================================== #


class DeploymentFailedEvent(ForgeEvent):
    """Emitted when a deployment step fails."""

    event_type: str = Field(default="deployment_failed", frozen=True)
    target: str = Field(..., description="Deployment target, e.g. 'kubernetes', 'vercel'.")
    step: str = Field(
        default="",
        description="Deployment step that failed: 'build', 'push', 'apply', 'smoke_test'.",
    )
    error_message: str = Field(..., description="Failure details.")


class DeploymentSucceededEvent(ForgeEvent):
    """Emitted when all deployment steps complete successfully."""

    event_type: str = Field(default="deployment_succeeded", frozen=True)
    target: str = Field(..., description="Deployment target.")
    service_urls: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of service name → public URL after deployment.",
    )


# ========================================================================== #
# Verification events
# ========================================================================== #


class TestFailedEvent(ForgeEvent):
    """Emitted when the test suite reports failures."""

    event_type: str = Field(default="test_failed", frozen=True)
    failing_tests: list[str] = Field(
        ...,
        description="List of test identifiers (e.g. pytest node IDs) that failed.",
    )
    total_failures: int = Field(default=0, ge=0)
    coverage_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Test coverage percentage reported by the runner.",
    )


class TestPassedEvent(ForgeEvent):
    """Emitted when the full test suite passes."""

    event_type: str = Field(default="test_passed", frozen=True)
    total_tests: int = Field(default=0, ge=0)
    coverage_pct: float | None = Field(default=None, ge=0.0, le=100.0)


# ========================================================================== #
# Retry events
# ========================================================================== #


class RetryTriggeredEvent(ForgeEvent):
    """Emitted when the RetryManager schedules a task retry."""

    event_type: str = Field(default="retry_triggered", frozen=True)
    attempt: int = Field(..., ge=1, description="Upcoming attempt number (1-based).")
    max_attempts: int = Field(..., ge=1, description="Maximum attempts allowed.")
    delay_seconds: float = Field(
        ...,
        ge=0.0,
        description="Seconds before the retry will be dispatched.",
    )
    error_message: str = Field(
        default="",
        description="Error from the previous attempt that triggered this retry.",
    )


# ========================================================================== #
# Phase transition events
# ========================================================================== #


class PhaseCompletedEvent(ForgeEvent):
    """Emitted when all tasks in a phase are complete and the pipeline advances."""

    event_type: str = Field(default="phase_completed", frozen=True)
    phase: ExecutionPhase = Field(..., description="Phase that just completed.")
    next_phase: ExecutionPhase | None = Field(
        default=None,
        description="The phase the pipeline is advancing to (None if pipeline is done).",
    )
    completed_task_count: int = Field(default=0, ge=0)
    failed_task_count: int = Field(default=0, ge=0)
    duration_minutes: float | None = Field(
        default=None,
        description="Wall-clock minutes the phase took.",
    )


class PipelineCompletedEvent(ForgeEvent):
    """Emitted when the entire FORGE pipeline finishes successfully."""

    event_type: str = Field(default="pipeline_completed", frozen=True)
    total_duration_minutes: float | None = None
    deployed_urls: dict[str, str] = Field(default_factory=dict)


class PipelineAbortedEvent(ForgeEvent):
    """Emitted when the pipeline is manually aborted or encounters a fatal error."""

    event_type: str = Field(default="pipeline_aborted", frozen=True)
    reason: str = Field(..., description="Why the pipeline was aborted.")
    aborted_by: str = Field(
        default="system",
        description="'system' for auto-abort, or a user identifier for manual abort.",
    )


# ========================================================================== #
# Event type registry
# ========================================================================== #

EVENT_TYPE_REGISTRY: dict[str, type] = {
    "task_completed": TaskCompletedEvent,
    "task_failed": TaskFailedEvent,
    "task_started": TaskStartedEvent,
    "deployment_failed": DeploymentFailedEvent,
    "deployment_succeeded": DeploymentSucceededEvent,
    "test_failed": TestFailedEvent,
    "test_passed": TestPassedEvent,
    "retry_triggered": RetryTriggeredEvent,
    "phase_completed": PhaseCompletedEvent,
    "pipeline_completed": PipelineCompletedEvent,
    "pipeline_aborted": PipelineAbortedEvent,
}


def deserialize_event(data: dict[str, Any]) -> ForgeEvent:
    """Deserialize a raw dict into the most specific ForgeEvent subclass.

    Falls back to the base ForgeEvent if the event_type is not recognised.
    """
    event_type = data.get("event_type", "")
    cls = EVENT_TYPE_REGISTRY.get(event_type, ForgeEvent)
    return cls.model_validate(data)
