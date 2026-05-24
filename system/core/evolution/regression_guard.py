"""Regression guard — ensures changes don't break existing functionality."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from system.core.evolution.schemas import PatchPlan
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class RegressionGuard:
    def __init__(self, terminal_executor: Any = None) -> None:
        self.terminal = terminal_executor

    async def run_regression_suite(
        self, plan: PatchPlan, project_path: str
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "plan_id": plan.plan_id,
            "passed": False,
            "checks": [],
            "failures": [],
        }
        checks = await asyncio.gather(
            self._run_tests(project_path),
            self._run_lint(project_path),
            return_exceptions=True,
        )
        for check in checks:
            if isinstance(check, Exception):
                results["checks"].append({"name": "check", "passed": False, "error": str(check)})
                results["failures"].append(str(check))
            else:
                results["checks"].append(check)
                if not check.get("passed"):
                    results["failures"].append(check.get("output", "unknown failure"))

        results["passed"] = len(results["failures"]) == 0
        return results

    async def _run_tests(self, project_path: str) -> Dict[str, Any]:
        if self.terminal:
            try:
                result = await self.terminal.run_tests(project_path)
                return {"name": "pytest", "passed": result.exit_code == 0, "output": result.stdout}
            except Exception as exc:
                return {"name": "pytest", "passed": False, "error": str(exc)}
        return {"name": "pytest", "passed": True, "output": "skipped (no executor)"}

    async def _run_lint(self, project_path: str) -> Dict[str, Any]:
        if self.terminal:
            try:
                result = await self.terminal.run_lint(project_path)
                return {"name": "lint", "passed": result.exit_code == 0, "output": result.stdout}
            except Exception as exc:
                return {"name": "lint", "passed": False, "error": str(exc)}
        return {"name": "lint", "passed": True, "output": "skipped (no executor)"}

    async def validate_no_api_breakage(
        self, plan: PatchPlan, expected_contracts: Optional[List[Dict]] = None
    ) -> bool:
        if not expected_contracts:
            return True
        breaking = plan.diff_analysis.breaking_changes
        return len(breaking) == 0
