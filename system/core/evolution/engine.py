"""Evolution Engine — orchestrates the full software evolution lifecycle."""

from __future__ import annotations

from typing import Any

from system.core.evolution.diff_analyzer import DiffAnalyzer
from system.core.evolution.inspector import RepoInspector
from system.core.evolution.patch_planner import PatchPlanner
from system.core.evolution.regression_guard import RegressionGuard
from system.core.evolution.schemas import (
    ChangeRequest,
    EvolutionRecord,
)
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class EvolutionEngine:
    def __init__(
        self,
        inspector: RepoInspector | None = None,
        diff_analyzer: DiffAnalyzer | None = None,
        patch_planner: PatchPlanner | None = None,
        regression_guard: RegressionGuard | None = None,
        db: Any = None,
    ) -> None:
        self.inspector = inspector or RepoInspector()
        self.diff_analyzer = diff_analyzer or DiffAnalyzer()
        self.patch_planner = patch_planner or PatchPlanner()
        self.regression_guard = regression_guard or RegressionGuard()
        self.db = db
        self._records: dict[str, EvolutionRecord] = {}

    async def plan_evolution(
        self, change_request: ChangeRequest, project_path: str = "."
    ) -> EvolutionRecord:
        logger.info("Planning evolution", project_id=change_request.project_id)

        inspection = await self.inspector.inspect(change_request.project_id, project_path)
        logger.info(
            "Inspection complete", **{k: v for k, v in inspection.items() if k != "project_id"}
        )

        diff = await self.diff_analyzer.analyze(change_request, project_path)
        plan = await self.patch_planner.create_plan(change_request, diff)

        record = EvolutionRecord(
            project_id=change_request.project_id,
            change_request=change_request,
            patch_plan=plan,
            status="planned",
        )
        self._records[record.evolution_id] = record
        return record

    async def apply_evolution(self, evolution_id: str, project_path: str = ".") -> EvolutionRecord:
        record = self._records.get(evolution_id)
        if not record:
            raise ValueError(f"Evolution record not found: {evolution_id}")
        if not record.patch_plan or not record.patch_plan.safe_to_apply:
            record.status = "blocked"
            return record

        record.status = "applying"
        self._records[evolution_id] = record

        regression = await self.regression_guard.run_regression_suite(
            record.patch_plan, project_path
        )
        record.regression_passed = regression["passed"]
        record.status = "completed" if regression["passed"] else "regression_failed"
        self._records[evolution_id] = record
        return record

    async def get_record(self, evolution_id: str) -> EvolutionRecord | None:
        return self._records.get(evolution_id)

    async def list_records(self, project_id: str) -> list[EvolutionRecord]:
        return [r for r in self._records.values() if r.project_id == project_id]

    async def find_improvement_opportunities(self, project_id: str) -> list[str]:
        return await self.inspector.find_improvement_opportunities(project_id)
