"""Diff analyzer — analyzes changes between project versions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List

from system.core.evolution.schemas import ChangeRequest, DiffAnalysis
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class DiffAnalyzer:
    def __init__(self, llm_client: Any = None) -> None:
        self.llm_client = llm_client

    async def analyze(self, change_request: ChangeRequest, project_path: str) -> DiffAnalysis:
        root = Path(project_path)
        changed_files = self._scan_modified_files(root)
        breaking = self._detect_breaking_changes(changed_files, change_request)
        effort = self._estimate_effort(changed_files)
        return DiffAnalysis(
            changed_files=changed_files,
            added_files=[],
            deleted_files=[],
            impact_set=self._compute_impact_set(changed_files),
            breaking_changes=breaking,
            migration_required=any("model" in f or "schema" in f or "migration" in f for f in changed_files),
            estimated_effort=effort,
        )

    def _scan_modified_files(self, root: Path) -> List[str]:
        if not root.exists():
            return []
        py_files = [str(f.relative_to(root)) for f in root.rglob("*.py")]
        ts_files = [str(f.relative_to(root)) for f in root.rglob("*.ts")]
        return (py_files + ts_files)[:50]

    def _detect_breaking_changes(self, files: List[str], req: ChangeRequest) -> List[str]:
        breaking = []
        if req.breaking:
            breaking.append("Explicitly marked as breaking change")
        for f in files:
            if "api" in f and ("router" in f or "endpoint" in f):
                breaking.append(f"API changes in {f} may break existing clients")
                break
        return breaking

    def _compute_impact_set(self, changed_files: List[str]) -> List[str]:
        impact = set(changed_files)
        for f in changed_files:
            if "model" in f:
                impact.add("alembic/versions/")
            if "schema" in f:
                impact.add("tests/")
        return list(impact)

    def _estimate_effort(self, files: List[str]) -> str:
        n = len(files)
        if n > 20:
            return "major"
        if n > 5:
            return "moderate"
        return "minor"
