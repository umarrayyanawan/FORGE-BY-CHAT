"""Runtime Validation for the FORGE Verification Engine (Phase 10).

Runs pytest unit/integration tests, coverage analysis, and API smoke tests.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any
import uuid

import httpx

from system.observability.logging.logger import get_logger
from system.shared.models import ValidationStatus

from .schemas import ValidationCheck, VerificationReport

logger = get_logger(__name__)

# Coverage threshold below which we mark the check as failed
DEFAULT_COVERAGE_THRESHOLD: float = 80.0


class RuntimeValidator:
    """Runs pytest test suites and coverage checks against a project directory."""

    def __init__(self, terminal_executor: Any) -> None:
        """
        Args:
            terminal_executor: Object exposing
                ``async run(cmd, cwd) -> (stdout, stderr, returncode)``.
        """
        self._exec = terminal_executor

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def validate(
        self,
        project_path: str,
        project_id: str,
    ) -> VerificationReport:
        """Run all runtime checks and return a consolidated VerificationReport."""
        t_start = time.monotonic()
        logger.info("Starting runtime validation for project %s", project_id)

        all_checks: list[ValidationCheck] = []
        coverage_percent: float | None = None

        try:
            unit_checks = await self._run_unit_tests(project_path)
            all_checks.extend(unit_checks)
        except Exception as exc:
            logger.warning("Unit test run failed: %s", exc)
            all_checks.append(self._error_check("test", "unit tests", str(exc)))

        try:
            int_checks = await self._run_integration_tests(project_path)
            all_checks.extend(int_checks)
        except Exception as exc:
            logger.warning("Integration test run failed: %s", exc)
            all_checks.append(self._error_check("test", "integration tests", str(exc)))

        try:
            cov_check, cov_pct = await self._check_coverage(project_path)
            all_checks.append(cov_check)
            coverage_percent = cov_pct
        except Exception as exc:
            logger.warning("Coverage check failed: %s", exc)
            all_checks.append(self._error_check("coverage", "coverage check", str(exc)))

        try:
            api_checks = await self._run_api_tests(project_path)
            all_checks.extend(api_checks)
        except Exception as exc:
            logger.warning("API test run failed (non-fatal): %s", exc)
            all_checks.append(
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name="api tests",
                    status=ValidationStatus.SKIPPED,
                    message=f"API tests skipped: {exc}",
                )
            )

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        report = VerificationReport.from_checks(
            report_id=str(uuid.uuid4()),
            project_id=project_id,
            phase="runtime",
            checks=all_checks,
            coverage_percent=coverage_percent,
        )
        logger.info(
            "Runtime validation complete: %s (%d/%d passed, cov=%.1f%%, %dms)",
            report.overall_status,
            report.passed,
            report.total_checks,
            coverage_percent or 0.0,
            elapsed_ms,
        )
        return report

    # ---------------------------------------------------------------------- #
    # Individual checks
    # ---------------------------------------------------------------------- #

    async def _run_unit_tests(self, project_path: str) -> list[ValidationCheck]:
        """Run pytest over tests/unit/ with JSON report output."""
        report_path = "/tmp/forge_unit_results.json"
        tests_dir = Path(project_path) / "tests" / "unit"

        if not tests_dir.exists():
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name="unit tests",
                    status=ValidationStatus.SKIPPED,
                    message="tests/unit/ directory not found.",
                )
            ]

        t0 = time.monotonic()
        stdout, stderr, rc = await self._exec.run(
            [
                "pytest",
                "tests/unit/",
                "--json-report",
                f"--json-report-file={report_path}",
                "-q",
                "--tb=short",
            ],
            cwd=project_path,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        return self._parse_pytest_json(report_path, "unit tests", elapsed)

    async def _run_integration_tests(self, project_path: str) -> list[ValidationCheck]:
        """Run pytest over tests/integration/ with JSON report output."""
        report_path = "/tmp/forge_integration_results.json"
        tests_dir = Path(project_path) / "tests" / "integration"

        if not tests_dir.exists():
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name="integration tests",
                    status=ValidationStatus.SKIPPED,
                    message="tests/integration/ directory not found.",
                )
            ]

        t0 = time.monotonic()
        stdout, stderr, rc = await self._exec.run(
            [
                "pytest",
                "tests/integration/",
                "--json-report",
                f"--json-report-file={report_path}",
                "-q",
                "--tb=short",
                "-x",  # Stop on first failure for integration tests
            ],
            cwd=project_path,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        return self._parse_pytest_json(report_path, "integration tests", elapsed)

    async def _check_coverage(
        self, project_path: str, threshold: float = DEFAULT_COVERAGE_THRESHOLD
    ) -> tuple[ValidationCheck, float]:
        """Run pytest-cov and parse coverage.json to check overall coverage."""
        t0 = time.monotonic()
        stdout, stderr, rc = await self._exec.run(
            [
                "pytest",
                "--cov=.",
                "--cov-report=json:/tmp/forge_coverage.json",
                "--cov-report=term-missing",
                "-q",
                "--tb=no",
                "--no-header",
            ],
            cwd=project_path,
        )
        elapsed = int((time.monotonic() - t0) * 1000)

        coverage_data: dict[str, Any] = {}
        try:
            with open("/tmp/forge_coverage.json") as f:
                coverage_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # Try parsing from stdout
            for line in (stdout + stderr).splitlines():
                if "TOTAL" in line and "%" in line:
                    parts = line.split()
                    for part in reversed(parts):
                        if part.endswith("%"):
                            try:
                                pct = float(part.rstrip("%"))
                                check = self._check_coverage_threshold(
                                    {"totals": {"percent_covered": pct}}, threshold
                                )
                                check.duration_ms = elapsed
                                return check, pct
                            except ValueError:
                                pass

            return (
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="coverage",
                    name="coverage check",
                    status=ValidationStatus.SKIPPED,
                    message="Could not read coverage report.",
                    duration_ms=elapsed,
                ),
                0.0,
            )

        check = self._check_coverage_threshold(coverage_data, threshold)
        check.duration_ms = elapsed
        pct = coverage_data.get("totals", {}).get("percent_covered", 0.0)
        return check, float(pct)

    async def _run_api_tests(self, project_path: str) -> list[ValidationCheck]:
        """Run API/e2e tests if they exist under tests/api/ or tests/e2e/."""
        for tests_subdir in ("api", "e2e"):
            tests_dir = Path(project_path) / "tests" / tests_subdir
            if tests_dir.exists():
                report_path = f"/tmp/forge_{tests_subdir}_results.json"
                t0 = time.monotonic()
                stdout, stderr, rc = await self._exec.run(
                    [
                        "pytest",
                        f"tests/{tests_subdir}/",
                        "--json-report",
                        f"--json-report-file={report_path}",
                        "-q",
                        "--tb=short",
                    ],
                    cwd=project_path,
                )
                elapsed = int((time.monotonic() - t0) * 1000)
                return self._parse_pytest_json(report_path, f"{tests_subdir} tests", elapsed)

        return [
            ValidationCheck(
                check_id=str(uuid.uuid4()),
                check_type="test",
                name="api tests",
                status=ValidationStatus.SKIPPED,
                message="No tests/api/ or tests/e2e/ directory found.",
            )
        ]

    # ---------------------------------------------------------------------- #
    # Parsers
    # ---------------------------------------------------------------------- #

    def _parse_pytest_json(
        self,
        report_path: str,
        suite_name: str,
        elapsed_ms: int = 0,
    ) -> list[ValidationCheck]:
        """Parse a pytest-json-report file into ValidationCheck list."""
        try:
            with open(report_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name=suite_name,
                    status=ValidationStatus.FAILED,
                    message=f"Could not read pytest JSON report: {exc}",
                    duration_ms=elapsed_ms,
                )
            ]

        summary = data.get("summary", {})
        total = summary.get("total", 0)
        passed_count = summary.get("passed", 0)
        failed_count = summary.get("failed", 0)
        error_count = summary.get("error", 0)
        skipped_count = summary.get("skipped", 0)

        checks: list[ValidationCheck] = []

        # Summary check
        if failed_count == 0 and error_count == 0:
            checks.append(
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name=suite_name,
                    status=ValidationStatus.PASSED,
                    message=(f"{passed_count}/{total} tests passed, {skipped_count} skipped."),
                    details={"summary": summary},
                    duration_ms=elapsed_ms,
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name=suite_name,
                    status=ValidationStatus.FAILED,
                    message=(
                        f"{failed_count + error_count} failures out of {total} tests. "
                        f"Passed: {passed_count}, Failed: {failed_count}, "
                        f"Errors: {error_count}, Skipped: {skipped_count}."
                    ),
                    details={"summary": summary},
                    duration_ms=elapsed_ms,
                )
            )

        # Individual failed tests
        for test in data.get("tests", []):
            outcome = test.get("outcome", "")
            if outcome in ("failed", "error"):
                call = test.get("call", {})
                longrepr = call.get("longrepr", "")
                if isinstance(longrepr, dict):
                    longrepr = longrepr.get("reprcrash", {}).get("message", str(longrepr))
                checks.append(
                    ValidationCheck(
                        check_id=str(uuid.uuid4()),
                        check_type="test",
                        name=f"{suite_name}::{test.get('nodeid', 'unknown')}",
                        status=ValidationStatus.FAILED,
                        message=str(longrepr)[:500],
                        details={
                            "nodeid": test.get("nodeid"),
                            "duration": test.get("call", {}).get("duration", 0),
                        },
                        duration_ms=int(test.get("call", {}).get("duration", 0) * 1000),
                    )
                )

        return checks

    def _check_coverage_threshold(
        self,
        coverage_data: dict[str, Any],
        threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    ) -> ValidationCheck:
        """Return a ValidationCheck evaluating overall coverage vs threshold."""
        totals = coverage_data.get("totals", {})
        pct = float(totals.get("percent_covered", 0.0))
        num_statements = totals.get("num_statements", 0)
        missing_lines = totals.get("missing_lines", 0)

        if pct >= threshold:
            return ValidationCheck(
                check_id=str(uuid.uuid4()),
                check_type="coverage",
                name="coverage check",
                status=ValidationStatus.PASSED,
                message=(
                    f"Coverage {pct:.1f}% meets threshold {threshold:.0f}%. "
                    f"({num_statements - missing_lines}/{num_statements} lines covered)"
                ),
                details={"percent": pct, "threshold": threshold, "totals": totals},
            )
        else:
            return ValidationCheck(
                check_id=str(uuid.uuid4()),
                check_type="coverage",
                name="coverage check",
                status=ValidationStatus.FAILED,
                message=(
                    f"Coverage {pct:.1f}% is below threshold {threshold:.0f}%. "
                    f"{missing_lines} lines not covered."
                ),
                details={"percent": pct, "threshold": threshold, "totals": totals},
            )

    async def run_smoke_test(self, api_url: str) -> ValidationCheck:
        """Hit the /health endpoint and return a ValidationCheck."""
        t0 = time.monotonic()
        health_url = api_url.rstrip("/") + "/health"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(health_url)
            elapsed = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                return ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name="smoke test",
                    status=ValidationStatus.PASSED,
                    message=f"Health check passed: HTTP {resp.status_code} in {elapsed}ms.",
                    details={"url": health_url, "status_code": resp.status_code},
                    duration_ms=elapsed,
                )
            else:
                return ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="test",
                    name="smoke test",
                    status=ValidationStatus.FAILED,
                    message=f"Health check returned HTTP {resp.status_code}.",
                    details={"url": health_url, "status_code": resp.status_code},
                    duration_ms=elapsed,
                )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return ValidationCheck(
                check_id=str(uuid.uuid4()),
                check_type="test",
                name="smoke test",
                status=ValidationStatus.FAILED,
                message=f"Health check failed: {exc}",
                details={"url": health_url},
                duration_ms=elapsed,
            )

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _error_check(check_type: str, name: str, message: str) -> ValidationCheck:
        return ValidationCheck(
            check_id=str(uuid.uuid4()),
            check_type=check_type,
            name=name,
            status=ValidationStatus.FAILED,
            message=f"Check execution failed: {message}",
        )
