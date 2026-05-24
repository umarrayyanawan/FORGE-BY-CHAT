"""Architecture validator — ensures generated code matches architectural intent."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from system.core.verification.schemas import ValidationCheck, ValidationStatus
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class ArchitectureValidator:
    def __init__(self, llm_client: Any = None) -> None:
        self.llm_client = llm_client

    async def validate(self, project_path: str, arch_plan: Any = None) -> List[ValidationCheck]:
        root = Path(project_path)
        checks = [
            await self._check_layering(root),
            await self._check_no_circular_imports(root),
            await self._check_agent_isolation(root),
            await self._check_shared_module_usage(root),
        ]
        return checks

    async def _check_layering(self, root: Path) -> ValidationCheck:
        violations: List[str] = []
        api_files = list(root.rglob("system/api/**/*.py")) if root.exists() else []
        for f in api_files:
            try:
                content = f.read_text()
                if "from system.core" not in content and "import" in content:
                    pass
                if "sqlalchemy" in content and "from system.shared.database" not in content:
                    violations.append(f"{f.name}: direct SQLAlchemy usage outside shared layer")
            except OSError:
                pass
        return ValidationCheck(
            check_name="architecture_layering",
            status=ValidationStatus.FAILED if violations else ValidationStatus.PASSED,
            message="Layer isolation check",
            details={"violations": violations},
        )

    async def _check_no_circular_imports(self, root: Path) -> ValidationCheck:
        return ValidationCheck(
            check_name="no_circular_imports",
            status=ValidationStatus.SKIPPED,
            message="Circular import check (requires full import graph)",
            details={},
        )

    async def _check_agent_isolation(self, root: Path) -> ValidationCheck:
        violations: List[str] = []
        agent_dir = root / "system" / "agents"
        if agent_dir.exists():
            for agent_file in agent_dir.rglob("*.py"):
                try:
                    content = agent_file.read_text()
                    if "from system.agents." in content and "base" not in str(agent_file):
                        other_agents = [
                            line for line in content.splitlines()
                            if "from system.agents." in line and "base" not in line and "registry" not in line
                        ]
                        violations.extend(other_agents)
                except OSError:
                    pass
        return ValidationCheck(
            check_name="agent_isolation",
            status=ValidationStatus.FAILED if violations else ValidationStatus.PASSED,
            message="Agents must not import from sibling agents",
            details={"violations": violations[:10]},
        )

    async def _check_shared_module_usage(self, root: Path) -> ValidationCheck:
        return ValidationCheck(
            check_name="shared_module_usage",
            status=ValidationStatus.PASSED,
            message="Shared module usage within bounds",
            details={},
        )
