"""Environment Manager — provisions and tears down agent execution environments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from system.execution.sandboxes.sandbox import Sandbox
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass
class EnvironmentSpec:
    task_id: str
    agent_type: str
    python_version: str = "3.12"
    node_version: str = "20"
    extra_packages: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    base_project_path: str | None = None


class EnvironmentManager:
    def __init__(self, workdir_base: str = "/tmp/forge-envs") -> None:
        self.workdir_base = Path(workdir_base)
        self.workdir_base.mkdir(parents=True, exist_ok=True)
        self._active: dict[str, Sandbox] = {}

    async def provision(self, spec: EnvironmentSpec) -> Sandbox:
        task_dir = self.workdir_base / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        sandbox = Sandbox(spec.task_id, base_path=str(task_dir))

        if spec.base_project_path:
            sandbox.copy_from(spec.base_project_path)

        await sandbox.write_file(
            ".forge_env",
            f"TASK_ID={spec.task_id}\nAGENT_TYPE={spec.agent_type}\n",
        )

        self._active[spec.task_id] = sandbox
        logger.info("Environment provisioned", task_id=spec.task_id, agent=spec.agent_type)
        return sandbox

    async def teardown(self, task_id: str, preserve_output: bool = False) -> None:
        sandbox = self._active.pop(task_id, None)
        if sandbox:
            if not preserve_output:
                sandbox.destroy()
            logger.info("Environment torn down", task_id=task_id)

    def get(self, task_id: str) -> Sandbox | None:
        return self._active.get(task_id)

    async def teardown_all(self) -> None:
        for task_id in list(self._active.keys()):
            await self.teardown(task_id)
