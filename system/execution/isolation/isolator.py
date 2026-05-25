"""Isolator — enforces resource and filesystem boundaries for agent processes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import resource

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_FILES = 50
_DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB


@dataclass
class IsolationPolicy:
    allowed_directories: list[str]
    blocked_commands: list[str]
    max_file_count: int = _DEFAULT_MAX_FILES
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES
    allow_network: bool = False
    max_cpu_seconds: int = 120
    max_memory_mb: int = 512


class Isolator:
    def __init__(self, policy: IsolationPolicy | None = None) -> None:
        self.policy = policy or IsolationPolicy(
            allowed_directories=["/tmp"],
            blocked_commands=["rm -rf /", "sudo", "su", "chmod 777", "curl", "wget"],
        )

    def validate_command(self, command: str) -> bool:
        for blocked in self.policy.blocked_commands:
            if blocked in command:
                logger.warning("Blocked command rejected", command=command[:100])
                return False
        return True

    def validate_file_access(self, file_path: str) -> bool:
        path = Path(file_path).resolve()
        for allowed in self.policy.allowed_directories:
            if str(path).startswith(str(Path(allowed).resolve())):
                return True
        logger.warning("File access outside allowed directories", path=file_path)
        return False

    def validate_output_size(self, output: str) -> bool:
        size = len(output.encode("utf-8"))
        if size > self.policy.max_output_bytes:
            logger.warning("Output exceeds size limit", size_bytes=size)
            return False
        return True

    def apply_resource_limits(self) -> None:
        try:
            cpu_limit = self.policy.max_cpu_seconds
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            mem_bytes = self.policy.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (OSError, ValueError) as exc:
            logger.warning("Could not set resource limits", error=str(exc))
