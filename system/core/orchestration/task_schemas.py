"""Task graph schemas for the FORGE orchestration layer.

Defines the full data contract for task nodes, task graphs, and graph updates
that drive the autonomous software-production pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from system.shared.models import (
    AgentType,
    BaseForgeModel,
    ExecutionPhase,
    Priority,
    TaskStatus,
    TimestampedModel,
)

# ========================================================================== #
# Validation Rule
# ========================================================================== #


class ValidationRule(BaseForgeModel):
    """A single automated check that must pass before a task is considered done.

    rule_type determines how the check is executed:
    - ``file_exists``  – verify the target path exists in the workspace.
    - ``tests_pass``   – run the test suite at the target path and expect 0 failures.
    - ``lint_clean``   – run a linter and expect no errors.
    - ``type_check``   – run mypy / pyright and expect no type errors.
    - ``custom``       – arbitrary shell command supplied in ``config["command"]``.
    """

    rule_type: str = Field(
        ...,
        description=(
            "Kind of check: 'file_exists' | 'tests_pass' | 'lint_clean' | 'type_check' | 'custom'."
        ),
    )
    target: str = Field(
        ...,
        description="File path, test path, or free-form description for custom rules.",
    )
    severity: str = Field(
        default="error",
        description="'error' causes task failure; 'warning' is logged but non-blocking.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Rule-specific configuration (e.g. command, args, env).",
    )

    @model_validator(mode="after")
    def validate_severity(self) -> ValidationRule:
        if self.severity not in {"error", "warning"}:
            raise ValueError(f"severity must be 'error' or 'warning', got {self.severity!r}")
        return self

    @model_validator(mode="after")
    def validate_rule_type(self) -> ValidationRule:
        allowed = {"file_exists", "tests_pass", "lint_clean", "type_check", "custom"}
        if self.rule_type not in allowed:
            raise ValueError(f"rule_type must be one of {allowed}, got {self.rule_type!r}")
        return self


# ========================================================================== #
# Task Node
# ========================================================================== #


class TaskNode(TimestampedModel):
    """A single unit of work in the FORGE task graph (a DAG vertex).

    TaskNodes are the atoms of the pipeline.  Each node targets one specialist
    agent (backend, frontend, infra, …) and carries the context that agent
    needs to complete its work, plus the list of other tasks it depends on.
    """

    task_id: str = Field(
        ...,
        description="Unique identifier for this task within its graph.",
    )
    name: str = Field(..., description="Human-readable task name (e.g. 'generate_user_model').")
    description: str = Field(
        ..., description="Detailed description of what this task must produce."
    )
    agent_type: AgentType = Field(..., description="Specialist agent responsible for this task.")
    priority: Priority = Field(default=Priority.MEDIUM, description="Scheduling priority.")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Current lifecycle state.")

    # DAG relationships
    dependencies: list[str] = Field(
        default_factory=list,
        description="task_ids that must be COMPLETED before this task can start.",
    )
    blocking: list[str] = Field(
        default_factory=list,
        description="task_ids that cannot start until this task completes.",
    )

    # Validation
    validation_rules: list[ValidationRule] = Field(
        default_factory=list,
        description="Automated checks run after the agent completes the task.",
    )

    # Agent context
    input_context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Scoped context delivered to the agent: relevant spec sections, "
            "existing file contents, interface contracts, etc."
        ),
    )
    output_artifacts: list[str] = Field(
        default_factory=list,
        description="Expected output file paths the agent must create or modify.",
    )

    # Retry / timeout
    retry_count: int = Field(default=0, ge=0, description="Number of attempts made so far.")
    max_retries: int = Field(
        default=3, ge=0, description="Maximum retry attempts before permanent failure."
    )
    timeout_seconds: int = Field(
        default=3600, gt=0, description="Wall-clock deadline for a single attempt."
    )

    # Resource estimate
    estimated_tokens: int = Field(
        default=4096,
        gt=0,
        description="Estimated LLM tokens consumed — used for scheduling and cost tracking.",
    )

    # Ownership
    project_id: str = Field(..., description="FORGE project this task belongs to.")
    phase: ExecutionPhase = Field(..., description="Pipeline phase this task belongs to.")

    # Error tracking
    error_message: str | None = Field(
        default=None,
        description="Last error encountered (populated on failure).",
    )

    # Timing
    started_at: datetime | None = Field(default=None, description="When execution began.")
    completed_at: datetime | None = Field(
        default=None, description="When execution finished (pass or fail)."
    )

    @property
    def is_terminal(self) -> bool:
        """True when the task has reached a final state (completed, failed, blocked)."""
        return self.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}

    @property
    def duration_seconds(self) -> float | None:
        """Elapsed execution time, or None if not yet started / completed."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    def can_start(self, completed_task_ids: set[str]) -> bool:
        """Return True when all declared dependencies are in *completed_task_ids*."""
        return all(dep in completed_task_ids for dep in self.dependencies)


# ========================================================================== #
# Task Graph
# ========================================================================== #


class TaskGraph(TimestampedModel):
    """A complete directed-acyclic graph (DAG) of TaskNodes for one project phase.

    The graph stores both the raw node list and pre-computed structural
    metadata (topological levels, critical path, duration estimate) so the
    orchestration engine can make fast scheduling decisions without
    re-running graph algorithms at runtime.
    """

    graph_id: str = Field(..., description="Unique identifier for this task graph.")
    project_id: str = Field(..., description="FORGE project this graph belongs to.")
    tasks: list[TaskNode] = Field(default_factory=list, description="All task nodes in the graph.")
    phase: ExecutionPhase = Field(..., description="Pipeline phase this graph represents.")

    # Aggregate counters (kept in sync by the engine)
    total_tasks: int = Field(default=0, ge=0)
    completed_tasks: int = Field(default=0, ge=0)
    failed_tasks: int = Field(default=0, ge=0)

    # Pre-computed structural data
    execution_order: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Topological levels as computed by Kahn's algorithm.  Tasks within "
            "the same level can be dispatched in parallel."
        ),
    )
    critical_path: list[str] = Field(
        default_factory=list,
        description="Ordered task_ids forming the longest dependency chain.",
    )
    estimated_duration_minutes: int = Field(
        default=0,
        ge=0,
        description="Sum of per-task estimates along the critical path (minutes).",
    )

    def task_by_id(self, task_id: str) -> TaskNode | None:
        """Look up a task by its task_id.  Returns None when not found."""
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        return None

    def tasks_by_phase(self, phase: ExecutionPhase) -> list[TaskNode]:
        """Return all tasks that belong to the given phase."""
        return [t for t in self.tasks if t.phase == phase]

    def tasks_by_status(self, status: TaskStatus) -> list[TaskNode]:
        """Return all tasks in the given status."""
        return [t for t in self.tasks if t.status == status]

    @property
    def pending_tasks(self) -> int:
        """Number of tasks not yet started or retrying."""
        return sum(1 for t in self.tasks if t.status in {TaskStatus.PENDING, TaskStatus.RETRYING})

    @property
    def running_tasks(self) -> int:
        """Number of actively executing tasks."""
        return sum(1 for t in self.tasks if t.status == TaskStatus.RUNNING)

    @property
    def progress_pct(self) -> float:
        """Completion percentage (0–100)."""
        if self.total_tasks == 0:
            return 0.0
        return round((self.completed_tasks / self.total_tasks) * 100, 2)


# ========================================================================== #
# Task Graph Update
# ========================================================================== #


class TaskGraphUpdate(BaseForgeModel):
    """Inbound payload used to update the status of a single task node."""

    task_id: str = Field(..., description="task_id of the node to update.")
    status: TaskStatus = Field(..., description="New lifecycle status.")
    error_message: str | None = Field(
        default=None,
        description="Error details (required when status is FAILED).",
    )
    output_artifacts: list[str] | None = Field(
        default=None,
        description="Paths of files produced by the agent (populated on COMPLETED).",
    )

    @model_validator(mode="after")
    def error_required_on_failure(self) -> TaskGraphUpdate:
        if self.status == TaskStatus.FAILED and not self.error_message:
            raise ValueError("error_message is required when status is 'failed'")
        return self


# ========================================================================== #
# Graph generation request / response (API layer)
# ========================================================================== #


class GenerateGraphRequest(BaseForgeModel):
    """Request body for POST /tasks/generate."""

    project_id: str = Field(..., description="Project to generate a task graph for.")
    spec_id: str = Field(..., description="ID of the finalized ProjectSpec to use.")
    arch_id: str = Field(..., description="ID of the finalized ArchitecturePlan to use.")


class GraphStatusSummary(BaseForgeModel):
    """Lightweight status snapshot returned by GET /tasks/{graph_id}/status."""

    graph_id: str
    project_id: str
    phase: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    running_tasks: int
    pending_tasks: int
    progress_pct: float
    estimated_duration_minutes: int
    critical_path: list[str]
