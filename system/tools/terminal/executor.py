"""Terminal command executor for the FORGE platform.

Provides a sandboxed async wrapper over ``asyncio.create_subprocess_shell``
with command allow-listing, output size capping, and structured result types.
Designed for use by FORGE agents that need to run shell commands, tests,
linters, and build tools inside project workspaces.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from system.observability.logging.logger import get_logger
from system.shared.exceptions import ToolError

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Security policy
# --------------------------------------------------------------------------- #

BLOCKED_COMMANDS: List[str] = [
    "rm -rf /",
    "sudo rm -rf",
    ":(){:|:&};:",  # fork bomb
    "mkfs",
    "dd if=",
    "chmod 777 /",
    "chown root",
    "> /dev/sda",
    "format c:",
    "del /f /s /q c:\\",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "fdisk",
    "parted",
    "shred",
    "wipe",
]

ALLOWED_COMMANDS: List[str] = [
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "ruff",
    "mypy",
    "black",
    "isort",
    "alembic",
    "npm",
    "npx",
    "node",
    "yarn",
    "pnpm",
    "docker",
    "docker-compose",
    "kubectl",
    "helm",
    "terraform",
    "git",
    "make",
    "uvicorn",
    "gunicorn",
    "celery",
    "echo",
    "cat",
    "ls",
    "find",
    "grep",
    "curl",
    "wget",
    "cp",
    "mv",
    "mkdir",
    "touch",
    "chmod",
    "chown",
    "env",
    "printenv",
    "which",
    "wc",
    "head",
    "tail",
    "sort",
    "uniq",
    "diff",
    "patch",
    "tar",
    "zip",
    "unzip",
    "gzip",
    "gunzip",
    "jq",
    "sed",
    "awk",
    "xargs",
    "true",
    "false",
    "test",
    "openssl",
    "ssh",
    "scp",
    "rsync",
    "pg_dump",
    "psql",
    "redis-cli",
]


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class ExecutionOutput:
    """Structured result of a command execution."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    command: str
    truncated: bool = False
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        """True if the command exited with code 0."""
        return self.exit_code == 0

    @property
    def combined_output(self) -> str:
        """Concatenation of stdout and stderr for convenient inspection."""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(self.stderr)
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


class TerminalExecutor:
    """Async subprocess executor with sandbox enforcement.

    Args:
        workspace_root: All relative ``cwd`` values are resolved against
                        this directory.  Defaults to the current working
                        directory.
    """

    MAX_OUTPUT_SIZE: int = 1024 * 1024  # 1 MiB per stream

    def __init__(self, workspace_root: str = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()

    # ------------------------------------------------------------------ #
    # Safety
    # ------------------------------------------------------------------ #

    def _is_safe_command(self, command: str) -> bool:
        """Return True if the command passes the sandbox policy.

        Checks:
        1. No blocked pattern appears in the lower-cased command string.
        2. The base command name is in the allow-list.
        """
        lower = command.lower().strip()
        for blocked in BLOCKED_COMMANDS:
            if blocked in lower:
                logger.warning("Blocked command matched security pattern", pattern=blocked)
                return False

        try:
            cmd_parts = shlex.split(command)
        except ValueError:
            # Malformed command string — reject it.
            return False

        if not cmd_parts:
            return False

        base_cmd = Path(cmd_parts[0]).name
        if base_cmd not in ALLOWED_COMMANDS:
            logger.warning("Command not in sandbox allowlist", command=base_cmd)
            return False

        return True

    def _resolve_cwd(self, cwd: Optional[str]) -> str:
        """Resolve a working directory relative to the workspace root."""
        if cwd is None:
            return str(self.workspace_root)
        p = Path(cwd)
        if p.is_absolute():
            return str(p)
        return str(self.workspace_root / cwd)

    # ------------------------------------------------------------------ #
    # Core execution
    # ------------------------------------------------------------------ #

    async def run(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        env: Optional[Dict[str, str]] = None,
        inherit_env: bool = True,
    ) -> ExecutionOutput:
        """Execute a shell command asynchronously.

        Args:
            command: Shell command string to execute.
            cwd: Working directory (relative to workspace_root or absolute).
            timeout: Maximum wall-clock time in seconds before kill.
            env: Additional environment variables to set.
            inherit_env: If True, merge ``env`` on top of ``os.environ``.
                         If False, use ``env`` exclusively (risky).

        Returns:
            :class:`ExecutionOutput` with all captured streams and metadata.

        Raises:
            ToolError: If the command times out.
        """
        start = time.monotonic()
        work_dir = self._resolve_cwd(cwd)

        # Build environment
        exec_env: Optional[Dict[str, str]] = None
        if env:
            if inherit_env:
                exec_env = {**os.environ, **env}
            else:
                exec_env = env
        elif not inherit_env:
            exec_env = {}

        logger.debug("Executing command", command=command[:200], cwd=work_dir)

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=exec_env,
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout)
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
                await proc.communicate()
            except ProcessLookupError:
                pass
            raise ToolError(
                f"Command timed out after {timeout}s: {command[:200]}",
                "TIMEOUT",
                {"command": command, "timeout": timeout},
            )

        duration_ms = int((time.monotonic() - start) * 1000)

        stdout_str = stdout_bytes.decode(errors="replace")
        stderr_str = stderr_bytes.decode(errors="replace")
        truncated = False

        if len(stdout_str) > self.MAX_OUTPUT_SIZE:
            stdout_str = stdout_str[: self.MAX_OUTPUT_SIZE] + "\n[OUTPUT TRUNCATED]"
            truncated = True
        if len(stderr_str) > self.MAX_OUTPUT_SIZE:
            stderr_str = stderr_str[: self.MAX_OUTPUT_SIZE] + "\n[STDERR TRUNCATED]"
            truncated = True

        exit_code = proc.returncode if proc.returncode is not None else -1

        logger.debug(
            "Command completed",
            exit_code=exit_code,
            duration_ms=duration_ms,
            truncated=truncated,
        )

        return ExecutionOutput(
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=exit_code,
            duration_ms=duration_ms,
            command=command,
            truncated=truncated,
            timed_out=timed_out,
        )

    async def run_in_sandbox(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        env: Optional[Dict[str, str]] = None,
    ) -> ExecutionOutput:
        """Run a command only if it passes the sandbox allow-list policy.

        Args:
            command: Shell command to execute.
            cwd: Working directory.
            timeout: Execution timeout in seconds.
            env: Additional environment variables.

        Raises:
            ToolError: If the command is blocked by the sandbox policy.
        """
        if not self._is_safe_command(command):
            raise ToolError(
                f"Command blocked by sandbox policy: {command[:200]}",
                "SANDBOX_BLOCKED",
                {"command": command},
            )
        return await self.run(command, cwd=cwd, timeout=timeout, env=env)

    # ------------------------------------------------------------------ #
    # Convenience wrappers for common development tasks
    # ------------------------------------------------------------------ #

    async def run_tests(
        self,
        cwd: str = ".",
        test_path: str = "tests/",
        extra_args: str = "",
        timeout: int = 600,
    ) -> ExecutionOutput:
        """Run the pytest test suite with JSON report output.

        Args:
            cwd: Project directory (relative to workspace_root or absolute).
            test_path: Path to the tests directory or specific test file.
            extra_args: Additional pytest arguments, e.g. ``"-x -k test_foo"``.
            timeout: Execution timeout in seconds.

        Returns:
            :class:`ExecutionOutput` with combined pytest output.
        """
        cmd = (
            f"python -m pytest {test_path} -v --tb=short "
            "--json-report --json-report-file=/tmp/forge_test_results.json "
            f"{extra_args}"
        ).strip()
        return await self.run(cmd, cwd=cwd, timeout=timeout)

    async def run_lint(
        self,
        cwd: str = ".",
        format_output: str = "json",
    ) -> ExecutionOutput:
        """Run ruff lint against the project directory.

        Args:
            cwd: Project directory.
            format_output: ruff output format, e.g. ``"json"`` or ``"text"``.
        """
        return await self.run(
            f"ruff check . --output-format {format_output}",
            cwd=cwd,
        )

    async def run_format_check(self, cwd: str = ".") -> ExecutionOutput:
        """Check code formatting with ruff format (non-destructive).

        Returns:
            :class:`ExecutionOutput` — exit_code 0 means all files are formatted.
        """
        return await self.run("ruff format --check .", cwd=cwd)

    async def run_format(self, cwd: str = ".") -> ExecutionOutput:
        """Auto-format all Python files with ruff format.

        Returns:
            :class:`ExecutionOutput` with a list of files reformatted.
        """
        return await self.run("ruff format .", cwd=cwd)

    async def run_type_check(
        self,
        cwd: str = ".",
        target: str = ".",
        extra_args: str = "--ignore-missing-imports",
    ) -> ExecutionOutput:
        """Run mypy type checking.

        Args:
            cwd: Project directory.
            target: Path/module to check.
            extra_args: Additional mypy flags.
        """
        return await self.run(f"mypy {target} {extra_args}", cwd=cwd)

    async def run_migrations(
        self,
        cwd: str = ".",
        target: str = "head",
        timeout: int = 120,
    ) -> ExecutionOutput:
        """Run Alembic database migrations.

        Args:
            cwd: Project directory containing ``alembic.ini``.
            target: Migration target, defaults to ``"head"``.
            timeout: Execution timeout in seconds.
        """
        return await self.run(f"alembic upgrade {target}", cwd=cwd, timeout=timeout)

    async def npm_install(
        self,
        cwd: str = "frontend",
        timeout: int = 300,
        ci: bool = False,
    ) -> ExecutionOutput:
        """Install Node.js dependencies via npm.

        Args:
            cwd: Directory containing ``package.json``.
            timeout: Execution timeout in seconds.
            ci: Use ``npm ci`` (clean install) instead of ``npm install``.
        """
        cmd = "npm ci" if ci else "npm install"
        return await self.run(cmd, cwd=cwd, timeout=timeout)

    async def build_frontend(
        self,
        cwd: str = "frontend",
        timeout: int = 300,
        script: str = "build",
    ) -> ExecutionOutput:
        """Build the frontend project.

        Args:
            cwd: Directory containing ``package.json``.
            timeout: Execution timeout in seconds.
            script: npm script to invoke (e.g. ``"build"`` or ``"build:prod"``).
        """
        return await self.run(f"npm run {script}", cwd=cwd, timeout=timeout)

    async def pip_install(
        self,
        requirements_file: str = "requirements.txt",
        cwd: str = ".",
        timeout: int = 300,
        extra_args: str = "",
    ) -> ExecutionOutput:
        """Install Python dependencies from a requirements file.

        Args:
            requirements_file: Path to the requirements file.
            cwd: Project directory.
            timeout: Execution timeout in seconds.
            extra_args: Additional pip install flags.
        """
        return await self.run(
            f"pip install -r {requirements_file} {extra_args}".strip(),
            cwd=cwd,
            timeout=timeout,
        )

    async def git_clone(
        self,
        url: str,
        dest: str,
        branch: Optional[str] = None,
        depth: Optional[int] = None,
        timeout: int = 300,
    ) -> ExecutionOutput:
        """Clone a git repository.

        Args:
            url: Repository URL.
            dest: Destination directory path.
            branch: Branch to clone (omit for default branch).
            depth: Shallow clone depth (omit for full history).
            timeout: Execution timeout in seconds.
        """
        cmd = "git clone"
        if branch:
            cmd += f" --branch {branch}"
        if depth:
            cmd += f" --depth {depth}"
        cmd += f" {url} {dest}"
        return await self.run(cmd, timeout=timeout)

    async def git_status(self, cwd: str = ".") -> ExecutionOutput:
        """Run ``git status`` in porcelain format."""
        return await self.run("git status --porcelain", cwd=cwd)

    async def git_add_commit_push(
        self,
        message: str,
        cwd: str = ".",
        remote: str = "origin",
        branch: str = "main",
        timeout: int = 120,
    ) -> ExecutionOutput:
        """Stage all changes, create a commit, and push to origin.

        Args:
            message: Commit message.
            cwd: Repository root.
            remote: Remote name.
            branch: Branch to push to.
            timeout: Per-command timeout.
        """
        await self.run("git add -A", cwd=cwd)
        await self.run(f'git commit -m "{message}"', cwd=cwd)
        return await self.run(f"git push {remote} {branch}", cwd=cwd, timeout=timeout)

    async def docker_compose_up(
        self,
        compose_file: str = "docker-compose.yml",
        services: Optional[List[str]] = None,
        env_file: str = ".env",
        cwd: str = ".",
        timeout: int = 300,
    ) -> ExecutionOutput:
        """Start services with docker-compose.

        Args:
            compose_file: Path to the compose file.
            services: List of specific services to start (all if empty).
            env_file: Path to the .env file.
            cwd: Working directory.
            timeout: Execution timeout in seconds.
        """
        svc_list = " ".join(services) if services else ""
        cmd = f"docker-compose -f {compose_file} --env-file {env_file} up -d {svc_list}".strip()
        return await self.run(cmd, cwd=cwd, timeout=timeout)

    async def docker_compose_down(
        self,
        compose_file: str = "docker-compose.yml",
        volumes: bool = False,
        cwd: str = ".",
    ) -> ExecutionOutput:
        """Stop and remove services with docker-compose.

        Args:
            compose_file: Path to the compose file.
            volumes: If True, also remove named volumes.
            cwd: Working directory.
        """
        vol_flag = "-v" if volumes else ""
        return await self.run(
            f"docker-compose -f {compose_file} down {vol_flag}".strip(),
            cwd=cwd,
        )

    async def make(
        self,
        target: str,
        cwd: str = ".",
        timeout: int = 600,
    ) -> ExecutionOutput:
        """Run a Makefile target.

        Args:
            target: Make target name.
            cwd: Directory containing ``Makefile``.
            timeout: Execution timeout in seconds.
        """
        return await self.run(f"make {target}", cwd=cwd, timeout=timeout)

    async def check_command_available(self, command: str) -> bool:
        """Return True if a command is available on the PATH.

        Args:
            command: Command name (not a full shell string).
        """
        try:
            result = await self.run(f"which {command}", timeout=10)
            return result.exit_code == 0
        except ToolError:
            return False
