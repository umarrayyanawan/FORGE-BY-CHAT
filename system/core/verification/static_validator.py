"""Static Validation for the FORGE Verification Engine (Phase 10).

Runs ruff lint, mypy type-checking, ruff format, import cycle detection,
and py_compile syntax checks against a project directory.
"""

from __future__ import annotations

import json
from pathlib import Path
import py_compile
import time
from typing import Any
import uuid

from system.observability.logging.logger import get_logger
from system.shared.models import ValidationStatus

from .schemas import ValidationCheck, VerificationReport

logger = get_logger(__name__)


class StaticValidator:
    """Runs all static analysis checks on a project directory.

    Checks performed:
    - ruff lint   (style, unused imports, anti-patterns)
    - mypy        (type errors)
    - ruff format (formatting consistency)
    - import scan (circular import detection)
    - py_compile  (syntax errors in every .py file)
    """

    def __init__(self, terminal_executor: Any) -> None:
        """
        Args:
            terminal_executor: Object exposing ``async run(cmd, cwd) -> (stdout, stderr, returncode)``.
        """
        self._exec = terminal_executor

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def validate(
        self,
        project_path: str,
        project_id: str,
        task_id: str | None = None,
    ) -> VerificationReport:
        """Run all static checks and return a consolidated VerificationReport."""
        t_start = time.monotonic()
        logger.info("Starting static validation for project %s at %s", project_id, project_path)

        all_checks: list[ValidationCheck] = []

        try:
            ruff_checks = await self._ruff_check(project_path)
            all_checks.extend(ruff_checks)
        except Exception as exc:
            logger.warning("ruff check failed unexpectedly: %s", exc)
            all_checks.append(self._error_check("lint", "ruff lint", str(exc)))

        try:
            mypy_checks = await self._mypy_check(project_path)
            all_checks.extend(mypy_checks)
        except Exception as exc:
            logger.warning("mypy check failed unexpectedly: %s", exc)
            all_checks.append(self._error_check("type_check", "mypy", str(exc)))

        try:
            fmt_checks = await self._format_check(project_path)
            all_checks.extend(fmt_checks)
        except Exception as exc:
            logger.warning("format check failed unexpectedly: %s", exc)
            all_checks.append(self._error_check("lint", "ruff format", str(exc)))

        try:
            import_checks = await self._import_check(project_path)
            all_checks.extend(import_checks)
        except Exception as exc:
            logger.warning("import check failed unexpectedly: %s", exc)
            all_checks.append(self._error_check("lint", "import check", str(exc)))

        try:
            syntax_checks = await self._syntax_check(project_path)
            all_checks.extend(syntax_checks)
        except Exception as exc:
            logger.warning("syntax check failed unexpectedly: %s", exc)
            all_checks.append(self._error_check("lint", "syntax check", str(exc)))

        fix_suggestions = self._generate_fix_suggestions(all_checks)
        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        report = VerificationReport.from_checks(
            report_id=str(uuid.uuid4()),
            project_id=project_id,
            phase="static",
            checks=all_checks,
            task_id=task_id,
            fix_suggestions=fix_suggestions,
        )
        logger.info(
            "Static validation complete: %s (%d/%d passed, %dms)",
            report.overall_status,
            report.passed,
            report.total_checks,
            elapsed_ms,
        )
        return report

    # ---------------------------------------------------------------------- #
    # Individual checks
    # ---------------------------------------------------------------------- #

    async def _ruff_check(self, project_path: str) -> list[ValidationCheck]:
        """Run ruff lint and return per-finding ValidationChecks."""
        t0 = time.monotonic()
        stdout, stderr, rc = await self._exec.run(
            ["ruff", "check", ".", "--output-format", "json"],
            cwd=project_path,
        )
        elapsed = int((time.monotonic() - t0) * 1000)

        if not stdout.strip():
            # No findings → clean
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name="ruff lint",
                    status=ValidationStatus.PASSED,
                    message="No lint issues found.",
                    duration_ms=elapsed,
                )
            ]

        checks = self._parse_ruff_output(stdout)
        for c in checks:
            c.duration_ms = elapsed
        return (
            checks
            if checks
            else [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name="ruff lint",
                    status=ValidationStatus.PASSED,
                    message="No lint issues found.",
                    duration_ms=elapsed,
                )
            ]
        )

    async def _mypy_check(self, project_path: str) -> list[ValidationCheck]:
        """Run mypy type-checking and return per-finding ValidationChecks."""
        t0 = time.monotonic()
        stdout, stderr, rc = await self._exec.run(
            [
                "mypy",
                ".",
                "--ignore-missing-imports",
                "--show-error-codes",
                "--no-error-summary",
                "--output=json",
            ],
            cwd=project_path,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        combined = stdout + stderr
        checks = self._parse_mypy_output(combined)
        for c in checks:
            c.duration_ms = elapsed
        if not checks:
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="type_check",
                    name="mypy",
                    status=ValidationStatus.PASSED,
                    message="No type errors found.",
                    duration_ms=elapsed,
                )
            ]
        return checks

    async def _format_check(self, project_path: str) -> list[ValidationCheck]:
        """Run ruff format --check to verify code formatting."""
        t0 = time.monotonic()
        stdout, stderr, rc = await self._exec.run(
            ["ruff", "format", "--check", "."],
            cwd=project_path,
        )
        elapsed = int((time.monotonic() - t0) * 1000)

        if rc == 0:
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name="ruff format",
                    status=ValidationStatus.PASSED,
                    message="All files are properly formatted.",
                    duration_ms=elapsed,
                )
            ]

        # Parse files mentioned in stderr output
        unformatted: list[str] = []
        combined = (stdout + stderr).strip()
        for line in combined.splitlines():
            line = line.strip()
            if line.startswith("Would reformat"):
                parts = line.split()
                if len(parts) >= 3:
                    unformatted.append(parts[2])

        checks: list[ValidationCheck] = []
        if unformatted:
            for fp in unformatted:
                checks.append(
                    ValidationCheck(
                        check_id=str(uuid.uuid4()),
                        check_type="lint",
                        name="ruff format",
                        status=ValidationStatus.WARNING,
                        message=f"File requires reformatting: {fp}",
                        details={"file": fp},
                        duration_ms=elapsed,
                    )
                )
        else:
            checks.append(
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name="ruff format",
                    status=ValidationStatus.WARNING,
                    message="Some files are not properly formatted.",
                    duration_ms=elapsed,
                )
            )
        return checks

    async def _import_check(self, project_path: str) -> list[ValidationCheck]:
        """Detect circular imports by scanning .py files for import relationships."""
        t0 = time.monotonic()
        path_obj = Path(project_path)
        py_files = list(path_obj.rglob("*.py"))

        # Build adjacency map: module → set of imported modules
        imports_map: dict[str, set] = {}
        for py_file in py_files:
            rel = py_file.relative_to(path_obj)
            module_name = str(rel).replace("/", ".").replace("\\", ".").removesuffix(".py")
            imports_map[module_name] = set()
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                for line in source.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("from ") or stripped.startswith("import "):
                        tokens = stripped.split()
                        if tokens[0] == "import":
                            imports_map[module_name].add(tokens[1].split(".")[0])
                        elif tokens[0] == "from" and len(tokens) >= 2:
                            imports_map[module_name].add(tokens[1].split(".")[0])
            except Exception:
                pass

        cycles = self._detect_cycles(imports_map)
        elapsed = int((time.monotonic() - t0) * 1000)

        if not cycles:
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name="import check",
                    status=ValidationStatus.PASSED,
                    message="No circular imports detected.",
                    duration_ms=elapsed,
                )
            ]

        return [
            ValidationCheck(
                check_id=str(uuid.uuid4()),
                check_type="lint",
                name="import check",
                status=ValidationStatus.FAILED,
                message=f"Circular import detected: {' → '.join(cycle)}",
                details={"cycle": cycle},
                duration_ms=elapsed,
            )
            for cycle in cycles
        ]

    async def _syntax_check(self, project_path: str) -> list[ValidationCheck]:
        """Run py_compile on every .py file to catch syntax errors."""
        t0 = time.monotonic()
        path_obj = Path(project_path)
        py_files = list(path_obj.rglob("*.py"))

        syntax_errors: list[ValidationCheck] = []
        for py_file in py_files:
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as exc:
                syntax_errors.append(
                    ValidationCheck(
                        check_id=str(uuid.uuid4()),
                        check_type="lint",
                        name="syntax check",
                        status=ValidationStatus.FAILED,
                        message=str(exc),
                        details={"file": str(py_file)},
                    )
                )
            except Exception as exc:
                syntax_errors.append(
                    ValidationCheck(
                        check_id=str(uuid.uuid4()),
                        check_type="lint",
                        name="syntax check",
                        status=ValidationStatus.WARNING,
                        message=f"Could not compile {py_file}: {exc}",
                        details={"file": str(py_file)},
                    )
                )

        elapsed = int((time.monotonic() - t0) * 1000)

        if not syntax_errors:
            return [
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name="syntax check",
                    status=ValidationStatus.PASSED,
                    message=f"All {len(py_files)} Python files are syntactically valid.",
                    duration_ms=elapsed,
                )
            ]

        for c in syntax_errors:
            c.duration_ms = elapsed
        return syntax_errors

    # ---------------------------------------------------------------------- #
    # Parsers
    # ---------------------------------------------------------------------- #

    def _parse_ruff_output(self, output: str) -> list[ValidationCheck]:
        """Parse ruff JSON output into ValidationCheck list."""
        checks: list[ValidationCheck] = []
        try:
            findings = json.loads(output)
        except json.JSONDecodeError:
            # Fall back to line-by-line parsing
            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                checks.append(
                    ValidationCheck(
                        check_id=str(uuid.uuid4()),
                        check_type="lint",
                        name="ruff lint",
                        status=ValidationStatus.FAILED,
                        message=line,
                    )
                )
            return checks

        for finding in findings:
            # ruff JSON: {filename, row, col, code, message, url, fix}
            filename = finding.get("filename", "")
            row = finding.get("location", {}).get("row", 0)
            col = finding.get("location", {}).get("column", 0)
            code = finding.get("code", "")
            msg = finding.get("message", "")

            # Treat fixable issues as warnings, non-fixable as errors
            fix = finding.get("fix")
            status = ValidationStatus.WARNING if fix else ValidationStatus.FAILED

            checks.append(
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="lint",
                    name=f"ruff:{code}",
                    status=status,
                    message=f"{filename}:{row}:{col}: {code} {msg}",
                    details={
                        "file": filename,
                        "row": row,
                        "col": col,
                        "rule": code,
                        "fixable": fix is not None,
                    },
                )
            )
        return checks

    def _parse_mypy_output(self, output: str) -> list[ValidationCheck]:
        """Parse mypy output (line-by-line text or JSON) into ValidationChecks."""
        checks: list[ValidationCheck] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # Try JSON line
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    severity = obj.get("severity", "error")
                    status = (
                        ValidationStatus.FAILED if severity == "error" else ValidationStatus.WARNING
                    )
                    checks.append(
                        ValidationCheck(
                            check_id=str(uuid.uuid4()),
                            check_type="type_check",
                            name=f"mypy:{obj.get('code', 'error')}",
                            status=status,
                            message=obj.get("message", line),
                            details={
                                "file": obj.get("file", ""),
                                "line": obj.get("line", 0),
                                "col": obj.get("column", 0),
                                "code": obj.get("code", ""),
                            },
                        )
                    )
                    continue
                except json.JSONDecodeError:
                    pass
            # Plain text: path:line: error: message  [code]
            if ": error:" in line:
                status = ValidationStatus.FAILED
            elif ": warning:" in line or ": note:" in line:
                status = ValidationStatus.WARNING
            else:
                continue
            checks.append(
                ValidationCheck(
                    check_id=str(uuid.uuid4()),
                    check_type="type_check",
                    name="mypy",
                    status=status,
                    message=line,
                    details=self._parse_mypy_line(line),
                )
            )
        return checks

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _parse_mypy_line(line: str) -> dict[str, Any]:
        """Extract file/line/col from a mypy text output line."""
        parts = line.split(":")
        result: dict[str, Any] = {"raw": line}
        try:
            result["file"] = parts[0].strip()
            result["line"] = int(parts[1].strip()) if len(parts) > 1 else 0
            result["col"] = int(parts[2].strip()) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            pass
        return result

    @staticmethod
    def _detect_cycles(graph: dict[str, set]) -> list[list[str]]:
        """Detect cycles in an import adjacency map using DFS."""
        visited: set = set()
        rec_stack: set = set()
        cycles: list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            for neighbor in graph.get(node, set()):
                if neighbor not in graph:
                    continue
                if neighbor not in visited:
                    dfs(neighbor, path[:])
                elif neighbor in rec_stack:
                    # Found a cycle — extract it
                    idx = path.index(neighbor)
                    cycle = path[idx:] + [neighbor]
                    if cycle not in cycles:
                        cycles.append(cycle)
            rec_stack.discard(node)

        for node in list(graph.keys()):
            if node not in visited:
                dfs(node, [])

        return cycles[:10]  # Cap at 10 cycles for readability

    def _generate_fix_suggestions(self, checks: list[ValidationCheck]) -> list[str]:
        """Generate actionable fix suggestions for failed/warning checks."""
        suggestions: list[str] = []
        seen_rules: set = set()

        for check in checks:
            if check.status not in (ValidationStatus.FAILED, ValidationStatus.WARNING):
                continue

            rule = check.details.get("rule", "")
            if rule and rule in seen_rules:
                continue
            if rule:
                seen_rules.add(rule)

            name_lower = check.name.lower()

            if "ruff:f401" in name_lower or rule == "F401":
                suggestions.append("Remove unused imports or add `# noqa: F401` if intentional.")
            elif "ruff:e501" in name_lower or rule == "E501":
                suggestions.append(
                    "Wrap long lines to <= 88 characters (use `ruff format .` to auto-fix)."
                )
            elif "ruff" in name_lower and check.details.get("fixable"):
                suggestions.append(f"Run `ruff check --fix .` to auto-fix {rule} violations.")
            elif "mypy" in name_lower or check.check_type == "type_check":
                suggestions.append(
                    f"Fix type error: {check.message[:100]}. "
                    "Add type annotations or use `# type: ignore` with explanation."
                )
            elif "format" in name_lower:
                suggestions.append("Run `ruff format .` to automatically reformat all files.")
            elif "circular" in check.message.lower():
                cycle = check.details.get("cycle", [])
                if cycle:
                    suggestions.append(
                        f"Break circular import: {' → '.join(cycle)}. "
                        "Move shared types to a common module or use TYPE_CHECKING guard."
                    )
            elif "syntax" in name_lower:
                suggestions.append(
                    f"Fix syntax error in {check.details.get('file', 'unknown file')}: {check.message[:100]}"
                )
            else:
                suggestions.append(f"Fix {check.check_type} issue: {check.message[:120]}")

        return suggestions[:20]  # Cap at 20 suggestions

    @staticmethod
    def _error_check(check_type: str, name: str, message: str) -> ValidationCheck:
        return ValidationCheck(
            check_id=str(uuid.uuid4()),
            check_type=check_type,
            name=name,
            status=ValidationStatus.FAILED,
            message=f"Check execution failed: {message}",
        )
