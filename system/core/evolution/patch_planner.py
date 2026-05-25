"""Patch planner — creates safe, step-by-step patch plans for applying changes."""

from __future__ import annotations

from typing import Any

from system.core.evolution.schemas import ChangeRequest, DiffAnalysis, PatchPlan
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class PatchPlanner:
    def __init__(self, llm_client: Any = None) -> None:
        self.llm_client = llm_client

    async def create_plan(self, change_request: ChangeRequest, diff: DiffAnalysis) -> PatchPlan:
        tasks = await self._generate_tasks(change_request, diff)
        rollback = self._generate_rollback_plan(diff)
        regression = self._generate_regression_plan(diff)
        risk = self._assess_risk(diff)
        safe = risk != "critical"
        return PatchPlan(
            change_request=change_request,
            diff_analysis=diff,
            tasks=tasks,
            rollback_plan=rollback,
            regression_test_plan=regression,
            safe_to_apply=safe,
            risk_level=risk,
        )

    async def _generate_tasks(self, req: ChangeRequest, diff: DiffAnalysis) -> list[dict[str, Any]]:
        if self.llm_client:
            try:
                prompt = (
                    f"Change: {req.description}\n"
                    f"Files: {', '.join(diff.changed_files[:10])}\n"
                    f"Effort: {diff.estimated_effort}\n\n"
                    "Generate 3-5 implementation tasks as JSON list with fields: "
                    "id, title, description, agent_type, priority"
                )
                response = await self.llm_client.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a software project planner. Output only valid JSON.",
                    max_tokens=512,
                    temperature=0.1,
                )
                import json

                content = response.content.strip()
                start = content.find("[")
                end = content.rfind("]") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
            except Exception as exc:
                logger.warning("LLM task generation failed", error=str(exc))
        return self._default_tasks(req, diff)

    def _default_tasks(self, req: ChangeRequest, diff: DiffAnalysis) -> list[dict[str, Any]]:
        return [
            {
                "id": "t1",
                "title": "Implement changes",
                "description": req.description,
                "agent_type": "backend",
                "priority": "high",
            },
            {
                "id": "t2",
                "title": "Update tests",
                "description": "Add/update tests for changed code",
                "agent_type": "qa",
                "priority": "high",
            },
            {
                "id": "t3",
                "title": "Review security impact",
                "description": "Ensure no security regressions",
                "agent_type": "security",
                "priority": "medium",
            },
        ]

    def _generate_rollback_plan(self, diff: DiffAnalysis) -> str:
        steps = ["1. Stop affected services", "2. Revert code changes via git revert"]
        if diff.migration_required:
            steps.append("3. Run alembic downgrade -1 to revert DB migration")
        steps.append(f"{len(steps) + 1}. Restart services and verify health checks")
        return "\n".join(steps)

    def _generate_regression_plan(self, diff: DiffAnalysis) -> str:
        return (
            "1. Run full pytest suite\n"
            "2. Check API contract compliance\n"
            "3. Verify no performance regressions\n"
            "4. Smoke test critical user paths"
        )

    def _assess_risk(self, diff: DiffAnalysis) -> str:
        if diff.breaking_changes and diff.estimated_effort == "major":
            return "high"
        if diff.breaking_changes or diff.migration_required:
            return "medium"
        return "low"
