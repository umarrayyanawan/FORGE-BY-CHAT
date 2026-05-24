"""Verification Engine — orchestrates all validation steps for generated projects."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from system.core.verification.schemas import (
    ValidationCheck,
    ValidationStatus,
    VerificationReport,
    SelfHealingAttempt,
)
from system.core.verification.static_validator import StaticValidator
from system.core.verification.runtime_validator import RuntimeValidator
from system.core.verification.architecture_validator import ArchitectureValidator
from system.core.verification.self_healing import SelfHealingEngine
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class VerificationEngine:
    def __init__(
        self,
        static: Optional[StaticValidator] = None,
        runtime: Optional[RuntimeValidator] = None,
        architecture: Optional[ArchitectureValidator] = None,
        healer: Optional[SelfHealingEngine] = None,
        db: Any = None,
    ) -> None:
        self.static = static or StaticValidator()
        self.runtime = runtime or RuntimeValidator()
        self.architecture = architecture or ArchitectureValidator()
        self.healer = healer or SelfHealingEngine()
        self.db = db
        self._reports: Dict[str, VerificationReport] = {}

    async def verify(
        self,
        project_id: str,
        project_path: str,
        auto_heal: bool = True,
    ) -> VerificationReport:
        logger.info("Starting verification", project_id=project_id)

        static_checks, arch_checks = await asyncio.gather(
            self.static.validate(project_path),
            self.architecture.validate(project_path),
        )
        runtime_checks = await self.runtime.validate(project_path)

        all_checks = static_checks + arch_checks + runtime_checks
        healing_log: List[SelfHealingAttempt] = []

        if auto_heal:
            failed = [c for c in all_checks if c.status == ValidationStatus.FAILED]
            for check in failed[:5]:
                attempt = await self.healer.attempt_heal(check, project_path)
                healing_log.append(attempt)
                if attempt.succeeded:
                    check.status = ValidationStatus.HEALED

        passed = sum(1 for c in all_checks if c.status in (ValidationStatus.PASSED, ValidationStatus.HEALED))
        failed_count = sum(1 for c in all_checks if c.status == ValidationStatus.FAILED)
        overall = ValidationStatus.PASSED if failed_count == 0 else ValidationStatus.FAILED

        report = VerificationReport(
            project_id=project_id,
            project_path=project_path,
            checks=all_checks,
            overall_status=overall,
            passed_count=passed,
            failed_count=failed_count,
            healing_attempts=healing_log,
        )
        self._reports[report.report_id] = report
        logger.info(
            "Verification complete",
            project_id=project_id,
            status=overall,
            passed=passed,
            failed=failed_count,
        )
        return report

    async def get_report(self, report_id: str) -> Optional[VerificationReport]:
        return self._reports.get(report_id)

    async def get_project_reports(self, project_id: str) -> List[VerificationReport]:
        return [r for r in self._reports.values() if r.project_id == project_id]
