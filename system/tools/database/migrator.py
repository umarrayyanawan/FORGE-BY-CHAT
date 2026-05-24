"""Database migration tool for the FORGE platform.

Wraps Alembic via subprocess to provide programmatic migration management
from within the agent pipeline.  All Alembic operations are executed as
subprocess calls to avoid interference with the running application's
SQLAlchemy engine state.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class MigrationResult:
    """Outcome of an Alembic migration operation."""

    success: bool
    current_revision: str
    migrations_applied: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""

    @property
    def error_summary(self) -> str:
        """Single-line summary of any errors."""
        return "; ".join(self.errors) if self.errors else ""


# --------------------------------------------------------------------------- #
# Migration tool
# --------------------------------------------------------------------------- #


class MigrationTool:
    """Programmatic Alembic migration management.

    Args:
        database_url: Full SQLAlchemy connection string used to set the
                      ``DATABASE_URL`` environment variable when invoking
                      Alembic.  If not provided, Alembic reads its own
                      ``alembic.ini`` configuration.
        project_dir: Directory containing ``alembic.ini``.  Defaults to
                     the current working directory.
        alembic_executable: Path to the alembic binary.
    """

    def __init__(
        self,
        database_url: str = "",
        project_dir: str = ".",
        alembic_executable: str = "alembic",
    ) -> None:
        self.database_url = database_url
        self.project_dir = str(Path(project_dir).resolve())
        self.alembic_executable = alembic_executable

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_env(self) -> Optional[dict]:
        """Build an environment dict for the Alembic subprocess."""
        if not self.database_url:
            return None
        import os

        env = os.environ.copy()
        env["DATABASE_URL"] = self.database_url
        return env

    def _run_alembic(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run an Alembic command and return the CompletedProcess result.

        Args:
            *args: Alembic subcommand and arguments,
                   e.g. ``("upgrade", "head")`` or ``("current",)``.
            timeout: Maximum execution time in seconds.

        Returns:
            :class:`subprocess.CompletedProcess` with stdout/stderr captured.
        """
        cmd = [self.alembic_executable, *args]
        env = self._build_env()
        logger.debug("Running Alembic", cmd=" ".join(cmd), cwd=self.project_dir)
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.project_dir,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            # Return a synthetic failed result so callers get a consistent type.
            result = subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout="",
                stderr=f"Alembic command timed out after {timeout}s: {' '.join(cmd)}",
            )
            return result
        except FileNotFoundError as exc:
            result = subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout="",
                stderr=f"Alembic executable not found: {self.alembic_executable}",
            )
            return result

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run_migrations(self, target: str = "head", timeout: int = 120) -> MigrationResult:
        """Apply pending migrations up to *target*.

        Args:
            target: Migration target: ``"head"``, a specific revision ID,
                    or a relative offset like ``"+2"``.
            timeout: Execution timeout in seconds.

        Returns:
            :class:`MigrationResult` describing the outcome.
        """
        result = self._run_alembic("upgrade", target, timeout=timeout)
        success = result.returncode == 0
        current = self.get_current_revision()

        # Parse applied revision IDs from Alembic's output
        applied: List[str] = []
        for line in result.stdout.splitlines():
            if "Running upgrade" in line or "Upgraded to" in line:
                parts = line.split()
                for part in parts:
                    if len(part) == 12 and all(c in "0123456789abcdef" for c in part):
                        applied.append(part)

        errors: List[str] = []
        if not success and result.stderr.strip():
            errors = [result.stderr.strip()]
        if not success and result.stdout.strip() and not errors:
            errors = [result.stdout.strip()]

        if success:
            logger.info(
                "Migrations applied",
                target=target,
                current_revision=current,
                applied_count=len(applied),
            )
        else:
            logger.error(
                "Migration failed",
                target=target,
                returncode=result.returncode,
                stderr=result.stderr[:300],
            )

        return MigrationResult(
            success=success,
            current_revision=current,
            migrations_applied=applied,
            errors=errors,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def create_migration(
        self,
        message: str,
        autogenerate: bool = True,
        timeout: int = 60,
    ) -> str:
        """Generate a new Alembic migration file.

        Args:
            message: Migration message / description.
            autogenerate: If True, detect schema changes automatically via
                          ``--autogenerate``.
            timeout: Execution timeout in seconds.

        Returns:
            Path to the newly created migration file.

        Raises:
            RuntimeError: If the migration generation fails.
        """
        args = ["revision", f"--message={message}"]
        if autogenerate:
            args.append("--autogenerate")

        result = self._run_alembic(*args, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create migration '{message}': {result.stderr or result.stdout}"
            )

        # Extract the generated file path from stdout
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if "Generating" in stripped:
                parts = stripped.split()
                if parts:
                    file_path = parts[-1].rstrip(".")
                    logger.info("Migration file created", message=message, path=file_path)
                    return file_path

        # Fallback: return a marker string
        logger.warning("Could not parse migration file path from output", output=result.stdout[:200])
        return f"<migration: {message}>"

    def get_current_revision(self) -> str:
        """Return the current database revision identifier.

        Returns:
            Revision ID string, ``"base"`` if no migrations have been
            applied, or ``"unknown"`` if the revision cannot be determined.
        """
        result = self._run_alembic("current", timeout=30)
        if result.returncode != 0:
            logger.warning("Failed to get current revision", stderr=result.stderr[:200])
            return "unknown"

        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Alembic outputs: "<revision_id> (head)" or just "<revision_id>"
            parts = line.split()
            if parts:
                return parts[0]

        return "base"

    def get_history(self, verbose: bool = False) -> List[str]:
        """Return the migration history as a list of revision strings.

        Args:
            verbose: Include full migration details if True.

        Returns:
            List of revision description strings, most recent first.
        """
        args = ["history", "--verbose"] if verbose else ["history"]
        result = self._run_alembic(*args, timeout=30)
        if result.returncode != 0:
            logger.warning("Failed to get migration history")
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def rollback_migration(self, steps: int = 1, timeout: int = 120) -> MigrationResult:
        """Roll back the most recent migration(s).

        Args:
            steps: Number of migrations to roll back.
            timeout: Execution timeout in seconds.

        Returns:
            :class:`MigrationResult` describing the outcome.
        """
        target = f"-{steps}"
        result = self._run_alembic("downgrade", target, timeout=timeout)
        success = result.returncode == 0
        current = self.get_current_revision()

        errors: List[str] = []
        if not success:
            errors = [result.stderr.strip() or result.stdout.strip()]

        if success:
            logger.info("Migration rolled back", steps=steps, current_revision=current)
        else:
            logger.error("Rollback failed", steps=steps, returncode=result.returncode)

        return MigrationResult(
            success=success,
            current_revision=current,
            migrations_applied=[],
            errors=errors,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def rollback_to(self, revision: str, timeout: int = 120) -> MigrationResult:
        """Roll back to a specific revision.

        Args:
            revision: Target revision ID or ``"base"`` to roll back all migrations.
            timeout: Execution timeout in seconds.

        Returns:
            :class:`MigrationResult` describing the outcome.
        """
        result = self._run_alembic("downgrade", revision, timeout=timeout)
        success = result.returncode == 0
        current = self.get_current_revision()

        errors: List[str] = []
        if not success:
            errors = [result.stderr.strip() or result.stdout.strip()]

        return MigrationResult(
            success=success,
            current_revision=current,
            migrations_applied=[],
            errors=errors,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def stamp_revision(self, revision: str = "head", timeout: int = 30) -> MigrationResult:
        """Stamp the database with a revision without running migrations.

        Useful when manually synchronising the database schema with an
        existing migration state.

        Args:
            revision: Revision to stamp (``"head"``, ``"base"``, or a specific ID).
            timeout: Execution timeout in seconds.
        """
        result = self._run_alembic("stamp", revision, timeout=timeout)
        success = result.returncode == 0
        current = self.get_current_revision()

        errors = [result.stderr.strip()] if not success and result.stderr.strip() else []
        if success:
            logger.info("Database stamped", revision=revision)

        return MigrationResult(
            success=success,
            current_revision=current,
            errors=errors,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def check_pending(self) -> bool:
        """Return True if there are pending migrations to apply.

        Compares the current revision against ``head``.  Returns False if
        the revision cannot be determined.
        """
        current = self.get_current_revision()
        if current in ("unknown", "base"):
            # Assume there are pending migrations if we can't determine state.
            return True

        # Run "alembic check" if available (Alembic >= 1.9.0)
        result = self._run_alembic("check", timeout=30)
        if result.returncode == 0:
            return False  # No pending migrations

        # Alembic < 1.9 — compare heads
        heads_result = self._run_alembic("heads", timeout=30)
        if heads_result.returncode != 0:
            return False

        heads = []
        for line in heads_result.stdout.splitlines():
            parts = line.strip().split()
            if parts:
                heads.append(parts[0])

        return current not in heads
