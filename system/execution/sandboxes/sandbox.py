"""Sandbox — isolated execution environment for a single agent task."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class SandboxResult:
    def __init__(
        self,
        exit_code: int,
        stdout: str,
        stderr: str,
        files_written: list[str],
        sandbox_path: str,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.files_written = files_written
        self.sandbox_path = sandbox_path

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class Sandbox:
    """Ephemeral filesystem sandbox for a single agent execution."""

    def __init__(self, task_id: str, base_path: str | None = None) -> None:
        self.task_id = task_id
        self._base = (
            Path(base_path) if base_path else Path(tempfile.mkdtemp(prefix=f"forge_{task_id}_"))
        )
        self._active = True

    @property
    def path(self) -> Path:
        return self._base

    async def write_file(self, relative_path: str, content: str) -> Path:
        target = self._base / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.debug("Sandbox file written", path=str(target))
        return target

    async def read_file(self, relative_path: str) -> str:
        target = self._base / relative_path
        return target.read_text(encoding="utf-8")

    async def run_command(
        self,
        command: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        if not self._active:
            raise RuntimeError("Sandbox is closed")

        merged_env = {**os.environ, **(env or {})}
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._base),
            env=merged_env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return SandboxResult(
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                files_written=[],
                sandbox_path=str(self._base),
            )

        files = [str(f.relative_to(self._base)) for f in self._base.rglob("*") if f.is_file()]
        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            files_written=files,
            sandbox_path=str(self._base),
        )

    def copy_from(self, source_path: str, relative_target: str = "") -> None:
        src = Path(source_path)
        dst = self._base / relative_target if relative_target else self._base
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def destroy(self) -> None:
        if self._active and self._base.exists():
            shutil.rmtree(self._base, ignore_errors=True)
            self._active = False
            logger.debug("Sandbox destroyed", task_id=self.task_id)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *args: Any) -> None:
        self.destroy()
