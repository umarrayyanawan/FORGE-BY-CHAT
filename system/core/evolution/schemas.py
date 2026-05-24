"""Evolution engine schemas."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import Field

from system.shared.models import BaseForgeModel, TimestampedModel


class ChangeRequest(BaseForgeModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    description: str
    scope: List[str] = []
    breaking: bool = False
    requested_by: str = "user"


class DiffAnalysis(BaseForgeModel):
    analysis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    changed_files: List[str] = []
    added_files: List[str] = []
    deleted_files: List[str] = []
    impact_set: List[str] = []
    breaking_changes: List[str] = []
    migration_required: bool = False
    estimated_effort: str = "minor"


class PatchPlan(BaseForgeModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    change_request: ChangeRequest
    diff_analysis: DiffAnalysis
    tasks: List[Dict[str, Any]] = []
    rollback_plan: str = ""
    regression_test_plan: str = ""
    safe_to_apply: bool = True
    risk_level: str = "low"


class EvolutionRecord(TimestampedModel):
    evolution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    change_request: ChangeRequest
    patch_plan: Optional[PatchPlan] = None
    status: str = "planned"
    regression_passed: bool = False
    deployment_id: Optional[str] = None
