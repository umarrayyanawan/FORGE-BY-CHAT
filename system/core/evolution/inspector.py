"""Repo Inspector — analyzes current project state for evolution planning."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class RepoInspector:
    def __init__(self, project_path: str = ".") -> None:
        self.project_path = Path(project_path)

    async def inspect(self, project_id: str, project_path: str) -> Dict[str, Any]:
        root = Path(project_path)
        py_files = list(root.rglob("*.py")) if root.exists() else []
        ts_files = list(root.rglob("*.ts")) + list(root.rglob("*.tsx")) if root.exists() else []
        return {
            "project_id": project_id,
            "python_files": len(py_files),
            "typescript_files": len(ts_files),
            "total_files": len(py_files) + len(ts_files),
            "has_tests": any("test" in str(f) for f in py_files),
        }

    async def find_improvement_opportunities(self, project_id: str) -> List[str]:
        return [
            "Add more unit test coverage",
            "Consider caching for expensive database queries",
            "Review error handling in API endpoints",
            "Add input validation to all user-facing endpoints",
        ]
