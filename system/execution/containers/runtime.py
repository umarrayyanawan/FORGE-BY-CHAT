"""Container runtime — manages Docker containers for isolated agent execution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ContainerSpec:
    image: str
    name: str
    command: str = ""
    environment: Dict[str, str] = field(default_factory=dict)
    volumes: Dict[str, str] = field(default_factory=dict)
    memory_limit: str = "512m"
    cpu_limit: str = "0.5"
    network: str = "bridge"
    remove_on_exit: bool = True


@dataclass
class ContainerResult:
    container_id: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class ContainerRuntime:
    def __init__(self, docker_socket: str = "/var/run/docker.sock") -> None:
        self.docker_socket = docker_socket

    async def run(self, spec: ContainerSpec, timeout: int = 300) -> ContainerResult:
        cmd = self._build_docker_cmd(spec)
        logger.info("Running container", image=spec.image, name=spec.name)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return ContainerResult(
                container_id="",
                exit_code=124,
                stdout="",
                stderr=f"Container timed out after {timeout}s",
            )
        return ContainerResult(
            container_id=spec.name,
            exit_code=proc.returncode or 0,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
        )

    def _build_docker_cmd(self, spec: ContainerSpec) -> str:
        parts = ["docker", "run"]
        if spec.remove_on_exit:
            parts.append("--rm")
        parts.extend(["--name", spec.name])
        parts.extend(["--memory", spec.memory_limit])
        parts.extend(["--cpus", spec.cpu_limit])
        parts.extend(["--network", spec.network])
        for k, v in spec.environment.items():
            parts.extend(["-e", f"{k}={v}"])
        for host_path, container_path in spec.volumes.items():
            parts.extend(["-v", f"{host_path}:{container_path}"])
        parts.append(spec.image)
        if spec.command:
            parts.extend(["sh", "-c", spec.command])
        return " ".join(parts)

    async def pull_image(self, image: str) -> bool:
        proc = await asyncio.create_subprocess_shell(
            f"docker pull {image}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await proc.communicate()
        return proc.returncode == 0

    async def remove_container(self, name: str, force: bool = True) -> bool:
        flag = "-f" if force else ""
        proc = await asyncio.create_subprocess_shell(
            f"docker rm {flag} {name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await proc.communicate()
        return proc.returncode == 0
