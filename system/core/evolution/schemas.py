"""Evolution engine schemas."""

from __future__ import annotations

from typing import Any
import uuid

from pydantic import Field

from system.shared.models import BaseForgeModel, TimestampedModel


class ChangeRequest(BaseForgeModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    description: str
    scope: list[str] = []
    breaking: bool = False
    requested_by: str = "user"


class DiffAnalysis(BaseForgeModel):
    analysis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    changed_files: list[str] = []
    added_files: list[str] = []
    deleted_files: list[str] = []
    impact_set: list[str] = []
    breaking_changes: list[str] = []
    migration_required: bool = False
    estimated_effort: str = "minor"


class PatchPlan(BaseForgeModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    change_request: ChangeRequest
    diff_analysis: DiffAnalysis
    tasks: list[dict[str, Any]] = []
    rollback_plan: str = ""
    regression_test_plan: str = ""
    safe_to_apply: bool = True
    risk_level: str = "low"


class EvolutionRecord(TimestampedModel):
    evolution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    change_request: ChangeRequest
    patch_plan: PatchPlan | None = None
    status: str = "planned"
    regression_passed: bool = False
    deployment_id: str | None = None
