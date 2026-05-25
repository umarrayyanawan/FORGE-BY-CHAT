"""Docker container management for the FORGE platform.

Wraps the official ``docker`` Python SDK to provide container build,
run, inspect, and cleanup operations used by agents and the deployment
pipeline.  Degrades gracefully when Docker is not available in the
execution environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from system.observability.logging.logger import get_logger
from system.shared.exceptions import ToolError

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class ContainerResult:
    """Outcome of a completed container run."""

    container_id: str
    status: str
    output: str
    exit_code: int

    @property
    def succeeded(self) -> bool:
        """True if the container exited with code 0."""
        return self.exit_code == 0


@dataclass
class ImageBuildResult:
    """Outcome of a Docker image build."""

    image_id: str
    tag: str
    build_log: str
    succeeded: bool
    error: str | None = None


@dataclass
class ContainerInfo:
    """Lightweight container metadata."""

    container_id: str
    name: str
    status: str
    image: str
    labels: dict[str, str] = field(default_factory=dict)
    ports: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class DockerManager:
    """Manage Docker containers and images via the Docker Python SDK.

    On initialisation, attempts to connect to the Docker daemon.  If
    Docker is unavailable (e.g. in CI without DinD), the instance is
    created in a degraded state and all methods raise ``ToolError``
    with code ``"DOCKER_UNAVAILABLE"`` until a daemon becomes reachable.
    """

    def __init__(self) -> None:
        try:
            import docker  # type: ignore[import-untyped]

            self.client = docker.from_env()
            # Ping to confirm the daemon is responding.
            self.client.ping()
            self.available = True
            logger.info("Docker daemon connected")
        except Exception as exc:
            logger.warning("Docker not available", error=str(exc))
            self.client = None
            self.available = False

    # ------------------------------------------------------------------ #
    # Guard
    # ------------------------------------------------------------------ #

    def _require_client(self) -> None:
        """Raise ToolError if Docker is not available."""
        if not self.available or self.client is None:
            raise ToolError(
                "Docker daemon not available in this environment.",
                "DOCKER_UNAVAILABLE",
            )

    # ------------------------------------------------------------------ #
    # Image operations
    # ------------------------------------------------------------------ #

    def build_image(
        self,
        dockerfile_path: str,
        tag: str,
        build_args: dict[str, str] | None = None,
        target: str | None = None,
        no_cache: bool = False,
        labels: dict[str, str] | None = None,
    ) -> ImageBuildResult:
        """Build a Docker image from a Dockerfile.

        Args:
            dockerfile_path: Absolute or relative path to the Dockerfile.
            tag: Image tag, e.g. ``"myapp:latest"`` or ``"myapp:1.2.3"``.
            build_args: Optional ``--build-arg`` key/value pairs.
            target: Multi-stage build target name.
            no_cache: Disable layer caching if True.
            labels: Image labels to apply.

        Returns:
            :class:`ImageBuildResult` with image ID and build log.

        Raises:
            ToolError: On Docker build failures or daemon unavailability.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        dockerfile = Path(dockerfile_path)
        build_context = str(dockerfile.parent)
        dockerfile_name = dockerfile.name

        build_log_lines: list[str] = []
        try:
            image, logs = self.client.images.build(
                path=build_context,
                dockerfile=dockerfile_name,
                tag=tag,
                buildargs=build_args or {},
                target=target,
                nocache=no_cache,
                labels=labels or {},
                rm=True,  # Remove intermediate containers
                forcerm=True,
            )
            for log_item in logs:
                if "stream" in log_item:
                    build_log_lines.append(log_item["stream"].rstrip())
                elif "error" in log_item:
                    build_log_lines.append(f"ERROR: {log_item['error']}")

            logger.info("Docker image built", tag=tag, image_id=image.id[:12])
            return ImageBuildResult(
                image_id=image.id,
                tag=tag,
                build_log="\n".join(build_log_lines),
                succeeded=True,
            )
        except docker.errors.BuildError as exc:
            for log_item in exc.build_log:
                if "stream" in log_item:
                    build_log_lines.append(log_item["stream"].rstrip())
                elif "error" in log_item:
                    build_log_lines.append(f"ERROR: {log_item['error']}")
            error_msg = str(exc)
            logger.error("Docker build failed", tag=tag, error=error_msg[:200])
            raise ToolError(
                f"Docker build failed for '{tag}': {error_msg}",
                "BUILD_ERROR",
                {"tag": tag, "log": "\n".join(build_log_lines[-20:])},
            ) from exc

    def pull_image(self, image: str, tag: str = "latest") -> str:
        """Pull an image from a registry.

        Args:
            image: Image name, e.g. ``"python"`` or ``"ghcr.io/org/myapp"``.
            tag: Image tag.

        Returns:
            Full image ID of the pulled image.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            pulled = self.client.images.pull(image, tag=tag)
            logger.info("Docker image pulled", image=image, tag=tag)
            return pulled.id
        except docker.errors.ImageNotFound as exc:
            raise ToolError(f"Image not found: {image}:{tag}", "IMAGE_NOT_FOUND") from exc
        except docker.errors.APIError as exc:
            raise ToolError(f"Docker pull failed: {exc}", "PULL_ERROR") from exc

    def remove_image(self, image: str, force: bool = False) -> None:
        """Remove a local Docker image.

        Args:
            image: Image name or ID.
            force: Force removal even if containers depend on it.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            self.client.images.remove(image, force=force)
            logger.info("Docker image removed", image=image)
        except docker.errors.ImageNotFound:
            pass  # Already gone — treat as success.
        except docker.errors.APIError as exc:
            raise ToolError(f"Failed to remove image '{image}': {exc}", "REMOVE_ERROR") from exc

    def list_images(self, name: str | None = None) -> list[dict[str, Any]]:
        """List local Docker images.

        Args:
            name: Filter by image name.

        Returns:
            List of dicts with ``id``, ``tags``, ``size``.
        """
        self._require_client()
        filters = {"reference": name} if name else {}
        images = self.client.images.list(filters=filters)
        return [
            {
                "id": img.id[:12],
                "tags": img.tags,
                "size": img.attrs.get("Size", 0),
                "created": img.attrs.get("Created", ""),
            }
            for img in images
        ]

    # ------------------------------------------------------------------ #
    # Container run
    # ------------------------------------------------------------------ #

    def run_container(
        self,
        image: str,
        command: str | None = None,
        env: dict[str, str] | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        network: str | None = None,
        user: str | None = None,
        working_dir: str | None = None,
        mem_limit: str | None = None,
        cpu_quota: int | None = None,
        read_only: bool = False,
        labels: dict[str, str] | None = None,
        remove_on_exit: bool = True,
    ) -> ContainerResult:
        """Run a command inside a container and wait for it to finish.

        Args:
            image: Docker image to run.
            command: Command to execute inside the container.
            env: Environment variables to inject.
            volumes: Volume mounts, e.g.
                     ``{"/host/path": {"bind": "/container/path", "mode": "ro"}}``.
            network: Docker network to attach to.
            user: User ID or ``"user:group"`` string to run as.
            working_dir: Container working directory.
            mem_limit: Memory limit, e.g. ``"512m"`` or ``"2g"``.
            cpu_quota: CPU quota in microseconds per 100ms period.
            read_only: Make the container filesystem read-only.
            labels: Container labels.
            remove_on_exit: Remove the container after completion.

        Returns:
            :class:`ContainerResult` with combined output and exit code.

        Raises:
            ToolError: On container or Docker API errors.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        kwargs: dict[str, Any] = {
            "image": image,
            "command": command,
            "environment": env or {},
            "volumes": volumes or {},
            "detach": True,
            "remove": False,  # We remove manually after reading logs.
            "labels": labels or {"managed-by": "forge"},
        }
        if network:
            kwargs["network"] = network
        if user:
            kwargs["user"] = user
        if working_dir:
            kwargs["working_dir"] = working_dir
        if mem_limit:
            kwargs["mem_limit"] = mem_limit
        if cpu_quota:
            kwargs["cpu_quota"] = cpu_quota
        if read_only:
            kwargs["read_only"] = read_only

        try:
            container = self.client.containers.run(**kwargs)
            result = container.wait()
            output = container.logs(stdout=True, stderr=True).decode(errors="replace")
            exit_code = result.get("StatusCode", -1)
            container_id = container.id

            if remove_on_exit:
                try:
                    container.remove()
                except Exception:
                    pass

            logger.info(
                "Container run completed",
                image=image,
                exit_code=exit_code,
                container_id=container_id[:12],
            )
            return ContainerResult(
                container_id=container_id,
                status="exited",
                output=output,
                exit_code=exit_code,
            )
        except docker.errors.ImageNotFound as exc:
            raise ToolError(f"Image not found: {image}", "IMAGE_NOT_FOUND") from exc
        except docker.errors.ContainerError as exc:
            raise ToolError(
                f"Container error for image '{image}': {exc}",
                "CONTAINER_ERROR",
                {"image": image, "command": command},
            ) from exc
        except docker.errors.APIError as exc:
            raise ToolError(
                f"Docker API error: {exc}",
                "DOCKER_API_ERROR",
                {"image": image},
            ) from exc

    def run_detached(
        self,
        image: str,
        command: str | None = None,
        name: str | None = None,
        env: dict[str, str] | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        network: str | None = None,
        ports: dict[str, Any] | None = None,
        restart_policy: dict[str, Any] | None = None,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Start a container in the background and return its ID.

        Args:
            image: Docker image to run.
            command: Optional command override.
            name: Assign a specific name to the container.
            env: Environment variables.
            volumes: Volume mounts.
            network: Docker network.
            ports: Port bindings, e.g. ``{"8000/tcp": 8000}``.
            restart_policy: e.g. ``{"Name": "always"}`` or ``{"Name": "on-failure", "MaximumRetryCount": 3}``.
            labels: Container labels.

        Returns:
            Container ID of the started container.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        kwargs: dict[str, Any] = {
            "image": image,
            "command": command,
            "environment": env or {},
            "volumes": volumes or {},
            "detach": True,
            "remove": False,
            "labels": labels or {"managed-by": "forge"},
        }
        if name:
            kwargs["name"] = name
        if network:
            kwargs["network"] = network
        if ports:
            kwargs["ports"] = ports
        if restart_policy:
            kwargs["restart_policy"] = restart_policy

        try:
            container = self.client.containers.run(**kwargs)
            logger.info("Container started (detached)", image=image, container_id=container.id[:12])
            return container.id
        except docker.errors.APIError as exc:
            raise ToolError(f"Failed to start container: {exc}", "DOCKER_API_ERROR") from exc

    # ------------------------------------------------------------------ #
    # Container management
    # ------------------------------------------------------------------ #

    def list_containers(
        self,
        all_containers: bool = False,
        filter_label: str | None = None,
        filter_name: str | None = None,
    ) -> list[ContainerInfo]:
        """List containers visible to the Docker daemon.

        Args:
            all_containers: Include stopped containers if True.
            filter_label: Filter by label key (optionally ``"key=value"``).
            filter_name: Filter by container name pattern.

        Returns:
            List of :class:`ContainerInfo` objects.
        """
        self._require_client()
        filters: dict[str, Any] = {}
        if filter_label:
            filters["label"] = filter_label
        if filter_name:
            filters["name"] = filter_name

        containers = self.client.containers.list(all=all_containers, filters=filters)
        return [
            ContainerInfo(
                container_id=c.id,
                name=c.name,
                status=c.status,
                image=c.image.tags[0] if c.image.tags else c.image.id[:12],
                labels=c.labels or {},
                ports=c.ports or {},
            )
            for c in containers
        ]

    def get_container(self, container_id: str) -> ContainerInfo:
        """Fetch details for a specific container.

        Args:
            container_id: Container ID or name.

        Raises:
            ToolError: If the container is not found.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            c = self.client.containers.get(container_id)
            return ContainerInfo(
                container_id=c.id,
                name=c.name,
                status=c.status,
                image=c.image.tags[0] if c.image.tags else c.image.id[:12],
                labels=c.labels or {},
                ports=c.ports or {},
            )
        except docker.errors.NotFound as exc:
            raise ToolError(
                f"Container not found: {container_id}",
                "CONTAINER_NOT_FOUND",
            ) from exc

    def stop_container(self, container_id: str, timeout: int = 10) -> None:
        """Stop a running container gracefully.

        Args:
            container_id: Container ID or name.
            timeout: Seconds to wait before sending SIGKILL.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            self.client.containers.get(container_id).stop(timeout=timeout)
            logger.info("Container stopped", container_id=container_id[:12])
        except docker.errors.NotFound:
            pass  # Already gone — no-op.
        except docker.errors.APIError as exc:
            raise ToolError(f"Failed to stop container: {exc}", "STOP_ERROR") from exc

    def restart_container(self, container_id: str, timeout: int = 10) -> None:
        """Restart a container.

        Args:
            container_id: Container ID or name.
            timeout: Seconds to wait before sending SIGKILL during stop.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            self.client.containers.get(container_id).restart(timeout=timeout)
            logger.info("Container restarted", container_id=container_id[:12])
        except docker.errors.NotFound as exc:
            raise ToolError(f"Container not found: {container_id}", "CONTAINER_NOT_FOUND") from exc

    def remove_container(self, container_id: str, force: bool = False) -> None:
        """Remove a container.

        Args:
            container_id: Container ID or name.
            force: Kill and remove a running container if True.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            self.client.containers.get(container_id).remove(force=force)
            logger.info("Container removed", container_id=container_id[:12])
        except docker.errors.NotFound:
            pass  # Already removed — no-op.
        except docker.errors.APIError as exc:
            raise ToolError(f"Failed to remove container: {exc}", "REMOVE_ERROR") from exc

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 100,
        follow: bool = False,
        since: int | None = None,
        timestamps: bool = False,
    ) -> str:
        """Fetch log output from a container.

        Args:
            container_id: Container ID or name.
            tail: Number of lines to return from the end of the logs.
            follow: Stream live log output (blocks until container stops).
            since: UNIX timestamp; return logs after this point.
            timestamps: Prefix each line with a timestamp.

        Returns:
            Decoded log string.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            container = self.client.containers.get(container_id)
            kwargs: dict[str, Any] = {
                "stdout": True,
                "stderr": True,
                "tail": tail,
                "follow": follow,
                "timestamps": timestamps,
            }
            if since is not None:
                kwargs["since"] = since
            raw_logs = container.logs(**kwargs)
            if isinstance(raw_logs, bytes):
                return raw_logs.decode(errors="replace")
            return "".join(chunk.decode(errors="replace") for chunk in raw_logs)
        except docker.errors.NotFound as exc:
            raise ToolError(
                f"Container not found: {container_id}",
                "CONTAINER_NOT_FOUND",
            ) from exc

    def exec_in_container(
        self,
        container_id: str,
        command: str,
        user: str | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ContainerResult:
        """Execute a command inside a running container.

        Args:
            container_id: Container ID or name.
            command: Shell command to run inside the container.
            user: Optional user to run as.
            workdir: Optional working directory inside the container.
            env: Optional environment variables.

        Returns:
            :class:`ContainerResult` with output and exit code.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            container = self.client.containers.get(container_id)
            kwargs: dict[str, Any] = {"stdout": True, "stderr": True, "demux": False}
            if user:
                kwargs["user"] = user
            if workdir:
                kwargs["workdir"] = workdir
            if env:
                kwargs["environment"] = env

            exit_code, output = container.exec_run(command, **kwargs)
            decoded = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
            return ContainerResult(
                container_id=container_id,
                status="exec_complete",
                output=decoded,
                exit_code=exit_code if exit_code is not None else -1,
            )
        except docker.errors.NotFound as exc:
            raise ToolError(
                f"Container not found: {container_id}",
                "CONTAINER_NOT_FOUND",
            ) from exc
        except docker.errors.APIError as exc:
            raise ToolError(f"exec failed: {exc}", "EXEC_ERROR") from exc

    # ------------------------------------------------------------------ #
    # Docker Compose
    # ------------------------------------------------------------------ #

    def compose_up(
        self,
        project_dir: str,
        compose_file: str = "docker-compose.yml",
        services: list[str] | None = None,
        env_file: str | None = None,
        build: bool = False,
        detach: bool = True,
    ) -> ContainerResult:
        """Start services defined in a docker-compose file.

        Args:
            project_dir: Directory containing the compose file.
            compose_file: Compose file name.
            services: Specific services to start (all if empty).
            env_file: Path to env file.
            build: Build images before starting.
            detach: Run in detached mode.

        Returns:
            :class:`ContainerResult` with compose up output.

        Note:
            This uses subprocess, not the Docker SDK directly, because
            docker-compose is a separate CLI tool.
        """
        import subprocess

        cmd = ["docker-compose", "-f", compose_file]
        if env_file:
            cmd += ["--env-file", env_file]
        cmd.append("up")
        if detach:
            cmd.append("-d")
        if build:
            cmd.append("--build")
        if services:
            cmd.extend(services)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=project_dir,
                timeout=300,
            )
            return ContainerResult(
                container_id="compose",
                status="up" if result.returncode == 0 else "failed",
                output=result.stdout + result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("docker-compose up timed out", "COMPOSE_TIMEOUT") from exc
        except FileNotFoundError as exc:
            raise ToolError("docker-compose CLI not found", "DOCKER_COMPOSE_NOT_FOUND") from exc

    # ------------------------------------------------------------------ #
    # Network management
    # ------------------------------------------------------------------ #

    def create_network(
        self,
        name: str,
        driver: str = "bridge",
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a Docker network.

        Args:
            name: Network name.
            driver: Network driver (``"bridge"`` | ``"overlay"`` | ``"host"``).
            labels: Network labels.

        Returns:
            Network ID.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            net = self.client.networks.create(
                name=name,
                driver=driver,
                labels=labels or {"managed-by": "forge"},
            )
            logger.info("Docker network created", name=name, network_id=net.id[:12])
            return net.id
        except docker.errors.APIError as exc:
            raise ToolError(f"Failed to create network '{name}': {exc}", "NETWORK_ERROR") from exc

    def remove_network(self, network_id: str) -> None:
        """Remove a Docker network.

        Args:
            network_id: Network ID or name.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            self.client.networks.get(network_id).remove()
        except docker.errors.NotFound:
            pass

    # ------------------------------------------------------------------ #
    # Volume management
    # ------------------------------------------------------------------ #

    def create_volume(
        self,
        name: str,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a named Docker volume.

        Args:
            name: Volume name.
            labels: Volume labels.

        Returns:
            Volume name.
        """
        self._require_client()
        vol = self.client.volumes.create(
            name=name,
            labels=labels or {"managed-by": "forge"},
        )
        logger.info("Docker volume created", name=name)
        return vol.name

    def remove_volume(self, name: str, force: bool = False) -> None:
        """Remove a named Docker volume.

        Args:
            name: Volume name.
            force: Force removal even if in use.
        """
        self._require_client()
        import docker  # type: ignore[import-untyped]

        try:
            self.client.volumes.get(name).remove(force=force)
        except docker.errors.NotFound:
            pass

    # ------------------------------------------------------------------ #
    # System
    # ------------------------------------------------------------------ #

    def get_system_info(self) -> dict[str, Any]:
        """Return Docker daemon system information."""
        self._require_client()
        info = self.client.info()
        return {
            "docker_version": info.get("ServerVersion", "unknown"),
            "os": info.get("OperatingSystem", "unknown"),
            "total_memory": info.get("MemTotal", 0),
            "cpus": info.get("NCPU", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
        }

    def prune_stopped_containers(self) -> dict[str, Any]:
        """Remove all stopped containers and return reclaimed space."""
        self._require_client()
        result = self.client.containers.prune()
        logger.info(
            "Pruned stopped containers",
            containers_deleted=len(result.get("ContainersDeleted") or []),
            space_reclaimed=result.get("SpaceReclaimed", 0),
        )
        return result

    def ping(self) -> bool:
        """Return True if the Docker daemon is reachable."""
        if not self.available or self.client is None:
            return False
        try:
            return self.client.ping()
        except Exception:
            return False
