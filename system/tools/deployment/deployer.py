"""Deployment tool for the FORGE platform.

Provides adapters for three deployment targets commonly used with
FORGE-generated projects:

- **Docker Compose** — local / VPS self-hosted deployments
- **Vercel** — frontend and serverless deployments
- **Railway** — full-stack PaaS deployments

All adapters share a common :class:`DeploymentResult` return type and
delegate actual subprocess execution to :class:`TerminalExecutor`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from system.observability.logging.logger import get_logger
from system.tools.terminal.executor import ExecutionOutput, TerminalExecutor

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Result & config types
# --------------------------------------------------------------------------- #


@dataclass
class DeploymentResult:
    """Outcome of a deployment operation."""

    deployment_id: str
    url: Optional[str]
    status: str  # "running" | "success" | "failed" | "partial"
    health_url: Optional[str]
    rollback_available: bool = True
    logs: str = ""
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status in ("running", "success")


@dataclass
class DockerDeployConfig:
    """Configuration for a Docker Compose deployment."""

    compose_file: str = "docker-compose.yml"
    services: List[str] = field(default_factory=list)
    env_file: str = ".env"
    project_dir: str = "."
    build: bool = False
    pull: bool = False
    force_recreate: bool = False
    remove_orphans: bool = True
    timeout: int = 300


@dataclass
class VercelDeployConfig:
    """Configuration for a Vercel deployment."""

    project_dir: str = "frontend"
    token: str = ""
    org_id: str = ""
    project_id: str = ""
    production: bool = False
    env_vars: Dict[str, str] = field(default_factory=dict)
    timeout: int = 300


@dataclass
class RailwayDeployConfig:
    """Configuration for a Railway deployment."""

    service: str = ""
    environment: str = "production"
    project_dir: str = "."
    detach: bool = True
    timeout: int = 300


@dataclass
class KubernetesDeployConfig:
    """Configuration for a Kubernetes deployment via kubectl."""

    namespace: str = "default"
    manifest_path: str = "k8s/"
    context: Optional[str] = None
    timeout: int = 300


# --------------------------------------------------------------------------- #
# Deployer
# --------------------------------------------------------------------------- #


class DeploymentTool:
    """Orchestrate deployments across multiple infrastructure targets.

    Args:
        executor: :class:`TerminalExecutor` instance for subprocess calls.
                  A default instance is created if not provided.
    """

    def __init__(self, executor: Optional[TerminalExecutor] = None) -> None:
        self.executor = executor or TerminalExecutor()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _generate_deployment_id(prefix: str) -> str:
        """Generate a unique deployment ID with a readable prefix."""
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def _extract_url_from_output(self, output: str, prefix: str = "https://") -> Optional[str]:
        """Scan command output for the first HTTPS URL."""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped
        return None

    # ------------------------------------------------------------------ #
    # Docker Compose
    # ------------------------------------------------------------------ #

    async def deploy_docker(self, config: DockerDeployConfig) -> DeploymentResult:
        """Deploy services via Docker Compose.

        Runs ``docker-compose up -d`` with optional build, pull, and
        force-recreate flags.

        Args:
            config: :class:`DockerDeployConfig` with deployment parameters.

        Returns:
            :class:`DeploymentResult` with deployment ID and status.
        """
        deployment_id = self._generate_deployment_id("docker")

        # Build the docker-compose command
        cmd_parts = [
            "docker-compose",
            "-f", config.compose_file,
            "--env-file", config.env_file,
        ]

        cmd_parts.append("up")
        cmd_parts.append("-d")

        if config.build:
            cmd_parts.append("--build")
        if config.pull:
            cmd_parts.append("--pull")
        if config.force_recreate:
            cmd_parts.append("--force-recreate")
        if config.remove_orphans:
            cmd_parts.append("--remove-orphans")

        if config.services:
            cmd_parts.extend(config.services)

        cmd = " ".join(cmd_parts)

        logger.info(
            "Deploying via Docker Compose",
            deployment_id=deployment_id,
            compose_file=config.compose_file,
            services=config.services or "all",
        )

        try:
            result: ExecutionOutput = await self.executor.run(
                cmd,
                cwd=config.project_dir,
                timeout=config.timeout,
            )

            status = "running" if result.exit_code == 0 else "failed"
            error: Optional[str] = None
            if result.exit_code != 0:
                error = result.stderr[:500] or result.stdout[:500]

            health_url = "http://localhost:8000/health"  # Default; override in config if needed.

            logger.info(
                "Docker Compose deployment completed",
                deployment_id=deployment_id,
                status=status,
                exit_code=result.exit_code,
            )

            return DeploymentResult(
                deployment_id=deployment_id,
                url=None,
                status=status,
                health_url=health_url,
                rollback_available=True,
                logs=result.combined_output,
                error=error,
            )
        except Exception as exc:
            logger.error("Docker Compose deployment failed", deployment_id=deployment_id, error=str(exc))
            return DeploymentResult(
                deployment_id=deployment_id,
                url=None,
                status="failed",
                health_url=None,
                rollback_available=False,
                logs="",
                error=str(exc),
            )

    async def teardown_docker(self, config: DockerDeployConfig) -> DeploymentResult:
        """Tear down Docker Compose services.

        Args:
            config: :class:`DockerDeployConfig` with project details.

        Returns:
            :class:`DeploymentResult` indicating the teardown outcome.
        """
        deployment_id = self._generate_deployment_id("docker-down")
        cmd = f"docker-compose -f {config.compose_file} down --remove-orphans"
        result = await self.executor.run(cmd, cwd=config.project_dir)
        return DeploymentResult(
            deployment_id=deployment_id,
            url=None,
            status="success" if result.exit_code == 0 else "failed",
            health_url=None,
            rollback_available=False,
            logs=result.combined_output,
        )

    # ------------------------------------------------------------------ #
    # Vercel
    # ------------------------------------------------------------------ #

    async def deploy_vercel(self, config: VercelDeployConfig) -> DeploymentResult:
        """Deploy a project to Vercel.

        Uses the Vercel CLI (``npx vercel``) to push the project directory.
        The deployment URL is extracted from CLI stdout.

        Args:
            config: :class:`VercelDeployConfig` with deployment parameters.

        Returns:
            :class:`DeploymentResult` with the deployed URL and status.
        """
        deployment_id = self._generate_deployment_id("vercel")

        # Build vercel command
        cmd_parts = ["npx", "vercel"]
        if config.production:
            cmd_parts.append("--prod")
        if config.token:
            cmd_parts += ["--token", config.token]
        if config.org_id:
            cmd_parts += ["--scope", config.org_id]
        cmd_parts.append("--yes")  # Non-interactive mode

        # Set environment variables via --env flags
        for key, value in config.env_vars.items():
            cmd_parts += ["--env", f"{key}={value}"]

        cmd = " ".join(cmd_parts)

        logger.info(
            "Deploying to Vercel",
            deployment_id=deployment_id,
            project_dir=config.project_dir,
            production=config.production,
        )

        try:
            result = await self.executor.run(
                cmd,
                cwd=config.project_dir,
                timeout=config.timeout,
            )

            # Extract the deployment URL from stdout
            url = self._extract_url_from_output(result.stdout)

            status = "success" if result.exit_code == 0 else "failed"
            error: Optional[str] = None
            if result.exit_code != 0:
                error = result.stderr[:500] or result.stdout[:500]

            logger.info(
                "Vercel deployment completed",
                deployment_id=deployment_id,
                url=url,
                status=status,
            )

            return DeploymentResult(
                deployment_id=deployment_id,
                url=url,
                status=status,
                health_url=url,
                rollback_available=True,  # Vercel supports instant rollback
                logs=result.combined_output,
                error=error,
            )
        except Exception as exc:
            logger.error("Vercel deployment failed", deployment_id=deployment_id, error=str(exc))
            return DeploymentResult(
                deployment_id=deployment_id,
                url=None,
                status="failed",
                health_url=None,
                rollback_available=False,
                logs="",
                error=str(exc),
            )

    async def promote_vercel_to_production(
        self,
        deployment_url: str,
        token: str = "",
    ) -> DeploymentResult:
        """Promote an existing Vercel preview deployment to production.

        Args:
            deployment_url: Preview deployment URL to promote.
            token: Vercel authentication token.

        Returns:
            :class:`DeploymentResult` with production URL.
        """
        deployment_id = self._generate_deployment_id("vercel-promote")
        token_flag = f"--token {token}" if token else ""
        cmd = f"npx vercel promote {deployment_url} {token_flag} --yes".strip()

        result = await self.executor.run(cmd, timeout=180)
        url = self._extract_url_from_output(result.stdout)

        return DeploymentResult(
            deployment_id=deployment_id,
            url=url,
            status="success" if result.exit_code == 0 else "failed",
            health_url=url,
            logs=result.combined_output,
        )

    # ------------------------------------------------------------------ #
    # Railway
    # ------------------------------------------------------------------ #

    async def deploy_railway(self, config: RailwayDeployConfig) -> DeploymentResult:
        """Deploy to Railway.

        Uses the Railway CLI (``railway up``) to deploy the current project.

        Args:
            config: :class:`RailwayDeployConfig` with deployment parameters.

        Returns:
            :class:`DeploymentResult` with deployment status.
        """
        deployment_id = self._generate_deployment_id("railway")

        cmd_parts = ["railway", "up"]
        cmd_parts += ["--environment", config.environment]
        if config.service:
            cmd_parts += ["--service", config.service]
        if config.detach:
            cmd_parts.append("--detach")

        cmd = " ".join(cmd_parts)

        logger.info(
            "Deploying to Railway",
            deployment_id=deployment_id,
            environment=config.environment,
            service=config.service or "default",
        )

        try:
            result = await self.executor.run(
                cmd,
                cwd=config.project_dir,
                timeout=config.timeout,
            )

            # Railway CLI outputs the deployment URL on a line starting with https://
            url = self._extract_url_from_output(result.stdout)
            status = "success" if result.exit_code == 0 else "failed"
            error: Optional[str] = None
            if result.exit_code != 0:
                error = result.stderr[:500] or result.stdout[:500]

            logger.info(
                "Railway deployment completed",
                deployment_id=deployment_id,
                url=url,
                status=status,
            )

            return DeploymentResult(
                deployment_id=deployment_id,
                url=url,
                status=status,
                health_url=url,
                rollback_available=True,
                logs=result.combined_output,
                error=error,
            )
        except Exception as exc:
            logger.error("Railway deployment failed", deployment_id=deployment_id, error=str(exc))
            return DeploymentResult(
                deployment_id=deployment_id,
                url=None,
                status="failed",
                health_url=None,
                rollback_available=False,
                logs="",
                error=str(exc),
            )

    async def rollback_railway(
        self,
        deployment_id: str,
        environment: str = "production",
    ) -> DeploymentResult:
        """Roll back a Railway deployment.

        Args:
            deployment_id: Railway deployment ID to roll back to.
            environment: Target environment.

        Returns:
            :class:`DeploymentResult` with rollback status.
        """
        new_id = self._generate_deployment_id("railway-rollback")
        cmd = f"railway rollback {deployment_id} --environment {environment}"
        result = await self.executor.run(cmd, timeout=120)

        return DeploymentResult(
            deployment_id=new_id,
            url=None,
            status="success" if result.exit_code == 0 else "failed",
            health_url=None,
            rollback_available=False,
            logs=result.combined_output,
        )

    # ------------------------------------------------------------------ #
    # Kubernetes (kubectl)
    # ------------------------------------------------------------------ #

    async def deploy_kubernetes(self, config: KubernetesDeployConfig) -> DeploymentResult:
        """Apply Kubernetes manifests via kubectl.

        Args:
            config: :class:`KubernetesDeployConfig` with deployment parameters.

        Returns:
            :class:`DeploymentResult` with deployment status.
        """
        deployment_id = self._generate_deployment_id("k8s")

        context_flag = f"--context {config.context}" if config.context else ""
        cmd = (
            f"kubectl apply -f {config.manifest_path} "
            f"--namespace {config.namespace} {context_flag}"
        ).strip()

        logger.info(
            "Applying Kubernetes manifests",
            deployment_id=deployment_id,
            manifest_path=config.manifest_path,
            namespace=config.namespace,
        )

        try:
            result = await self.executor.run(cmd, timeout=config.timeout)
            status = "success" if result.exit_code == 0 else "failed"

            return DeploymentResult(
                deployment_id=deployment_id,
                url=None,
                status=status,
                health_url=None,
                rollback_available=True,
                logs=result.combined_output,
                error=result.stderr[:300] if result.exit_code != 0 else None,
            )
        except Exception as exc:
            return DeploymentResult(
                deployment_id=deployment_id,
                url=None,
                status="failed",
                health_url=None,
                rollback_available=False,
                logs="",
                error=str(exc),
            )

    async def wait_for_kubernetes_rollout(
        self,
        deployment_name: str,
        namespace: str = "default",
        timeout: int = 300,
    ) -> bool:
        """Wait for a Kubernetes deployment rollout to complete.

        Args:
            deployment_name: Name of the Kubernetes Deployment resource.
            namespace: Kubernetes namespace.
            timeout: Maximum wait time in seconds.

        Returns:
            True if the rollout succeeded within the timeout, False otherwise.
        """
        cmd = (
            f"kubectl rollout status deployment/{deployment_name} "
            f"--namespace {namespace} --timeout={timeout}s"
        )
        result = await self.executor.run(cmd, timeout=timeout + 10)
        return result.exit_code == 0

    async def kubernetes_rollback(
        self,
        deployment_name: str,
        namespace: str = "default",
    ) -> DeploymentResult:
        """Roll back a Kubernetes deployment to the previous revision.

        Args:
            deployment_name: Kubernetes Deployment resource name.
            namespace: Kubernetes namespace.

        Returns:
            :class:`DeploymentResult` with rollback status.
        """
        deployment_id = self._generate_deployment_id("k8s-rollback")
        cmd = f"kubectl rollout undo deployment/{deployment_name} --namespace {namespace}"
        result = await self.executor.run(cmd, timeout=60)

        return DeploymentResult(
            deployment_id=deployment_id,
            url=None,
            status="success" if result.exit_code == 0 else "failed",
            health_url=None,
            rollback_available=False,
            logs=result.combined_output,
        )

    # ------------------------------------------------------------------ #
    # Health check
    # ------------------------------------------------------------------ #

    async def health_check(self, url: str, retries: int = 5, delay: int = 5) -> bool:
        """Poll a health endpoint until it returns 200 or retries are exhausted.

        Args:
            url: Health check URL to poll.
            retries: Maximum number of retry attempts.
            delay: Seconds to wait between retries.

        Returns:
            True if the health check succeeded, False otherwise.
        """
        import asyncio

        import httpx

        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(url)
                    if response.status_code == 200:
                        logger.info("Health check passed", url=url, attempt=attempt)
                        return True
                    logger.warning(
                        "Health check returned non-200",
                        url=url,
                        status_code=response.status_code,
                        attempt=attempt,
                    )
            except Exception as exc:
                logger.warning(
                    "Health check attempt failed",
                    url=url,
                    attempt=attempt,
                    error=str(exc),
                )
            if attempt < retries:
                await asyncio.sleep(delay)

        logger.error("Health check failed after all retries", url=url, retries=retries)
        return False
