"""Pydantic schemas for the FORGE Verification Engine (Phase 10).

Defines ValidationCheck, VerificationReport, and SelfHealingAttempt.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from system.shared.models import BaseForgeModel, TimestampedModel, ValidationStatus


class ValidationCheck(BaseForgeModel):
    """A single atomic validation check result."""

    check_id: str = Field(..., description="Unique identifier for this check instance.")
    check_type: str = Field(
        ...,
        description=(
            "Category of check: 'lint' | 'type_check' | 'test' | 'coverage' | 'security' | 'arch'"
        ),
    )
    name: str = Field(..., description="Human-readable check name, e.g. 'ruff lint'.")
    status: ValidationStatus = Field(..., description="Pass/fail/warn/skip result.")
    message: str = Field(default="", description="Human-readable result message.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured details: file path, line number, rule ID, etc.",
    )
    duration_ms: int = Field(
        default=0,
        ge=0,
        description="Wall-clock time taken to run this check in milliseconds.",
    )


class VerificationReport(TimestampedModel):
    """Comprehensive result of a full verification run across one or more phases."""

    report_id: str = Field(..., description="Unique report identifier.")
    project_id: str = Field(..., description="FORGE project this report belongs to.")
    task_id: str | None = Field(
        default=None,
        description="Task that triggered this verification (if any).",
    )
    phase: str = Field(
        ...,
        description="Verification phase: 'static' | 'runtime' | 'architecture' | 'full'.",
    )
    overall_status: ValidationStatus = Field(
        ...,
        description="Aggregate pass/fail derived from all individual checks.",
    )
    checks: list[ValidationCheck] = Field(
        default_factory=list,
        description="All individual check results.",
    )
    total_checks: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    coverage_percent: float | None = Field(
        default=None,
        description="Code coverage percentage (populated by runtime validation).",
    )
    error_summary: list[str] = Field(
        default_factory=list,
        description="Condensed list of error messages for quick triage.",
    )
    fix_suggestions: list[str] = Field(
        default_factory=list,
        description="Actionable fix suggestions generated from failed checks.",
    )

    @classmethod
    def from_checks(
        cls,
        report_id: str,
        project_id: str,
        phase: str,
        checks: list[ValidationCheck],
        task_id: str | None = None,
        coverage_percent: float | None = None,
        fix_suggestions: list[str] | None = None,
    ) -> VerificationReport:
        """Build a VerificationReport from a list of checks, computing aggregates."""
        passed = sum(1 for c in checks if c.status == ValidationStatus.PASSED)
        failed = sum(1 for c in checks if c.status == ValidationStatus.FAILED)
        warnings = sum(1 for c in checks if c.status == ValidationStatus.WARNING)
        skipped = sum(1 for c in checks if c.status == ValidationStatus.SKIPPED)

        if failed > 0:
            overall = ValidationStatus.FAILED
        elif warnings > 0:
            overall = ValidationStatus.WARNING
        elif passed > 0:
            overall = ValidationStatus.PASSED
        else:
            overall = ValidationStatus.SKIPPED

        error_summary = [
            f"[{c.check_type}] {c.name}: {c.message}"
            for c in checks
            if c.status == ValidationStatus.FAILED
        ]

        return cls(
            report_id=report_id,
            project_id=project_id,
            task_id=task_id,
            phase=phase,
            overall_status=overall,
            checks=checks,
            total_checks=len(checks),
            passed=passed,
            failed=failed,
            warnings=warnings,
            skipped=skipped,
            coverage_percent=coverage_percent,
            error_summary=error_summary,
            fix_suggestions=fix_suggestions or [],
        )


class SelfHealingAttempt(TimestampedModel):
    """Records a single self-healing attempt made against a failing project."""

    attempt_id: str = Field(..., description="Unique identifier for this healing attempt.")
    task_id: str = Field(..., description="Task that owns this healing loop.")
    error_description: str = Field(..., description="Summary of errors being fixed.")
    fix_applied: str = Field(..., description="Description of the fix applied by the LLM.")
    success: bool = Field(..., description="Whether the fix resolved all failures.")
    attempt_number: int = Field(
        ...,
        ge=1,
        description="Which attempt number this is (1-indexed).",
    )
    files_modified: list[str] = Field(
        default_factory=list,
        description="Paths of files rewritten during this attempt.",
    )
    project_id: str = Field(default="", description="FORGE project this attempt targets.")
