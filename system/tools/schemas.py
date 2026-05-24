"""Pydantic schemas for FORGE tool execution requests and results."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from pydantic import Field

from system.shared.models import BaseForgeModel, TimestampedModel


class ToolExecutionRequest(BaseForgeModel):
    """Request payload for executing a tool action."""

    tool_name: str = Field(..., description="Name of the tool: 'github' | 'terminal' | 'docker' | 'deploy'.")
    action: str = Field(..., description="Specific action within the tool, e.g. 'create_repo', 'run_tests'.")
    params: Dict[str, Any] = Field(default_factory=dict, description="Action-specific parameters.")
    project_id: str = Field(..., description="FORGE project this execution belongs to.")
    task_id: Optional[str] = Field(default=None, description="Task that triggered this execution.")
    dry_run: bool = Field(default=False, description="If True, validate params but do not execute.")


class ToolExecutionResult(TimestampedModel):
    """Result of a completed tool execution."""

    execution_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this execution instance.",
    )
    tool_name: str = Field(..., description="Tool that was executed.")
    action: str = Field(..., description="Action that was executed.")
    success: bool = Field(..., description="True if the action completed without error.")
    output: Any = Field(default=None, description="Structured output from the action.")
    error: Optional[str] = Field(default=None, description="Error message if success=False.")
    duration_ms: int = Field(default=0, ge=0, description="Wall-clock execution time in milliseconds.")
    rollback_id: Optional[str] = Field(
        default=None,
        description="Identifier that can be passed to a rollback action to undo this execution.",
    )
    audit_trail: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured audit record: who, what, when, where.",
    )
