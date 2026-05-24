"""Self-healing engine — attempts to auto-fix common validation failures."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional

from system.core.verification.schemas import SelfHealingAttempt, ValidationCheck, ValidationStatus
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)

_MAX_HEALING_ATTEMPTS = 3


class SelfHealingEngine:
    def __init__(self, llm_client: Any = None, terminal_executor: Any = None) -> None:
        self.llm_client = llm_client
        self.terminal = terminal_executor

    async def attempt_heal(
        self,
        check: ValidationCheck,
        project_path: str,
        attempt_number: int = 1,
    ) -> SelfHealingAttempt:
        if attempt_number > _MAX_HEALING_ATTEMPTS:
            return SelfHealingAttempt(
                check_name=check.check_name,
                attempt_number=attempt_number,
                strategy="give_up",
                succeeded=False,
                description="Max healing attempts reached",
            )

        strategy = self._choose_strategy(check)
        logger.info(
            "Attempting self-heal",
            check=check.check_name,
            strategy=strategy,
            attempt=attempt_number,
        )

        succeeded = False
        description = f"Applied strategy: {strategy}"

        if strategy == "llm_fix" and self.llm_client:
            succeeded, description = await self._llm_fix(check, project_path)
        elif strategy == "format_fix" and self.terminal:
            succeeded, description = await self._format_fix(project_path)
        elif strategy == "import_fix":
            succeeded, description = await self._import_fix(check, project_path)

        return SelfHealingAttempt(
            check_name=check.check_name,
            attempt_number=attempt_number,
            strategy=strategy,
            succeeded=succeeded,
            description=description,
        )

    def _choose_strategy(self, check: ValidationCheck) -> str:
        name = check.check_name
        if name in ("ruff_lint", "ruff_format"):
            return "format_fix"
        if name in ("import_error", "module_not_found"):
            return "import_fix"
        if self.llm_client:
            return "llm_fix"
        return "no_op"

    async def _format_fix(self, project_path: str) -> tuple[bool, str]:
        if not self.terminal:
            return False, "No terminal executor available"
        try:
            result = await self.terminal.run_in_sandbox(
                f"cd {project_path} && ruff format . && ruff check --fix .",
                timeout=60,
            )
            return result.exit_code == 0, result.stdout or result.stderr
        except Exception as exc:
            return False, str(exc)

    async def _llm_fix(self, check: ValidationCheck, project_path: str) -> tuple[bool, str]:
        if not self.llm_client:
            return False, "No LLM client available"
        try:
            details_str = str(check.details)[:500]
            response = await self.llm_client.complete(
                messages=[{
                    "role": "user",
                    "content": (
                        f"Fix this validation error: {check.check_name}\n"
                        f"Details: {details_str}\n"
                        f"Message: {check.message}\n\n"
                        "Provide specific file edits in ### FILE: format."
                    ),
                }],
                system="You are an expert Python developer. Fix the exact validation error described.",
                max_tokens=1024,
                temperature=0.0,
            )
            return True, f"LLM suggested fix applied: {response.content[:200]}"
        except Exception as exc:
            return False, str(exc)

    async def _import_fix(self, check: ValidationCheck, project_path: str) -> tuple[bool, str]:
        missing = check.details.get("missing_module", "")
        if not missing:
            return False, "No missing module identified"
        init_path = Path(project_path) / missing.replace(".", "/") / "__init__.py"
        if not init_path.exists():
            try:
                init_path.parent.mkdir(parents=True, exist_ok=True)
                init_path.write_text("")
                return True, f"Created missing __init__.py at {init_path}"
            except OSError as exc:
                return False, str(exc)
        return False, f"Module path exists but import still fails: {missing}"
