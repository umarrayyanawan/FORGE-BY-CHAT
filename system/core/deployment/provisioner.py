"""Infrastructure provisioner — spins up resources for each DeployTarget."""

from __future__ import annotations

from typing import Any
import uuid

from system.core.deployment.schemas import DeploymentConfig
from system.observability.logging.logger import get_logger
from system.shared.exceptions import DeploymentError
from system.shared.models import DeployTarget

logger = get_logger(__name__)


class InfraProvisioner:
    """Provisions (and deprovisions) infrastructure for a given DeployTarget.

    Optional collaborators are injected at construction time so that the class
    remains fully testable without live infra:
    - ``terminal_executor``  — executes shell commands (``run(cmd, **kw)`` coroutine).
    - ``k8s_manager``        — thin wrapper around the Kubernetes Python client.
    - ``docker_manager``     — thin wrapper around the Docker SDK.
    """

    def __init__(
        self,
        terminal_executor: Any | None = None,
        k8s_manager: Any | None = None,
        docker_manager: Any | None = None,
    ) -> None:
        self.terminal = terminal_executor
        self.k8s = k8s_manager
        self.docker = docker_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def provision(self, config: DeploymentConfig) -> str:
        """Provision infrastructure for *config* and return the resource identifier."""
        logger.info(
            "Provisioning infrastructure",
            target=config.target,
            project_id=config.project_id,
            environment=config.environment,
        )
        dispatch = {
            DeployTarget.DOCKER: self._provision_docker,
            DeployTarget.KUBERNETES: self._provision_kubernetes,
            DeployTarget.VERCEL: self._provision_vercel,
            DeployTarget.RAILWAY: self._provision_railway,
            DeployTarget.AWS: self._provision_aws,
            DeployTarget.GCP: self._provision_gcp,
        }
        handler = dispatch.get(config.target)
        if handler is None:
            raise DeploymentError(
                f"Unsupported deploy target: {config.target}",
                "UNSUPPORTED_TARGET",
            )
        resource_id = await handler(config)
        logger.info(
            "Provisioning complete",
            resource_id=resource_id,
            target=config.target,
        )
        return resource_id

    async def deprovision(self, deployment_id: str) -> None:
        """Tear down resources associated with *deployment_id*."""
        logger.info("Deprovisioning resources", deployment_id=deployment_id)
        # Production: look up target from DB and invoke the appropriate teardown.

    async def update_env_vars(self, deployment_id: str, env_vars: dict[str, str]) -> None:
        """Push updated environment variables to a running deployment."""
        logger.info(
            "Updating environment variables",
            deployment_id=deployment_id,
            var_count=len(env_vars),
        )
        # Production: patch the running container / K8s ConfigMap.

    async def scale(self, deployment_id: str, replicas: int) -> None:
        """Scale the deployment to *replicas* instances."""
        logger.info("Scaling deployment", deployment_id=deployment_id, replicas=replicas)
        # Production: update K8s Deployment or docker-compose scale.

    # ------------------------------------------------------------------
    # Target-specific provisioners
    # ------------------------------------------------------------------

    async def _provision_docker(self, config: DeploymentConfig) -> str:
        resource_id = f"docker-{uuid.uuid4().hex[:8]}"
        if self.terminal:
            result = await self.terminal.run(
                "docker-compose up -d --build",
                timeout=300,
            )
            if result.exit_code != 0:
                raise DeploymentError(
                    f"docker-compose failed: {result.stderr}",
                    "DOCKER_FAILED",
                )
            logger.info("Docker Compose deployment successful", resource_id=resource_id)
        else:
            logger.info(
                "Docker provisioner running without terminal — dry-run",
                resource_id=resource_id,
            )
        return resource_id

    async def _provision_kubernetes(self, config: DeploymentConfig) -> str:
        resource_id = f"k8s-{uuid.uuid4().hex[:8]}"
        manifest = self._generate_k8s_manifest(config)
        if self.k8s:
            self.k8s.apply_manifest(manifest)
            logger.info("K8s manifest applied", resource_id=resource_id)
        else:
            logger.info(
                "K8s provisioner running without k8s_manager — dry-run",
                resource_id=resource_id,
            )
        return resource_id

    async def _provision_vercel(self, config: DeploymentConfig) -> str:
        resource_id = f"vercel-{uuid.uuid4().hex[:8]}"
        if self.terminal:
            result = await self.terminal.run(
                "npx vercel --prod --yes",
                cwd="frontend",
                timeout=300,
            )
            if result.exit_code != 0:
                raise DeploymentError(
                    f"Vercel deploy failed: {result.stderr}",
                    "VERCEL_FAILED",
                )
            logger.info("Vercel deployment successful", resource_id=resource_id)
        else:
            logger.info(
                "Vercel provisioner running without terminal — dry-run",
                resource_id=resource_id,
            )
        return resource_id

    async def _provision_railway(self, config: DeploymentConfig) -> str:
        resource_id = f"railway-{uuid.uuid4().hex[:8]}"
        if self.terminal:
            result = await self.terminal.run("railway up", timeout=300)
            if result.exit_code != 0:
                raise DeploymentError(
                    f"Railway deploy failed: {result.stderr}",
                    "RAILWAY_FAILED",
                )
            logger.info("Railway deployment successful", resource_id=resource_id)
        else:
            logger.info(
                "Railway provisioner running without terminal — dry-run",
                resource_id=resource_id,
            )
        return resource_id

    async def _provision_aws(self, config: DeploymentConfig) -> str:
        resource_id = f"aws-{uuid.uuid4().hex[:8]}"
        if self.terminal:
            # ECS / App Runner via AWS CLI
            cluster = f"forge-{config.environment}"
            service = f"{config.project_id}-{config.environment}"
            result = await self.terminal.run(
                f"aws ecs update-service --cluster {cluster} "
                f"--service {service} --force-new-deployment",
                timeout=120,
            )
            if result.exit_code != 0:
                raise DeploymentError(
                    f"AWS ECS deploy failed: {result.stderr}",
                    "AWS_FAILED",
                )
        logger.info("AWS deployment initiated", resource_id=resource_id)
        return resource_id

    async def _provision_gcp(self, config: DeploymentConfig) -> str:
        resource_id = f"gcp-{uuid.uuid4().hex[:8]}"
        if self.terminal:
            result = await self.terminal.run(
                f"gcloud run deploy {config.project_id} "
                f"--image gcr.io/forge/{config.project_id}:{config.image_tag} "
                f"--platform managed --allow-unauthenticated",
                timeout=300,
            )
            if result.exit_code != 0:
                raise DeploymentError(
                    f"GCP Cloud Run deploy failed: {result.stderr}",
                    "GCP_FAILED",
                )
        logger.info("GCP Cloud Run deployment initiated", resource_id=resource_id)
        return resource_id

    # ------------------------------------------------------------------
    # Manifest generators
    # ------------------------------------------------------------------

    def _generate_k8s_manifest(self, config: DeploymentConfig) -> str:
        """Generate a production-ready Kubernetes Deployment + Service manifest."""
        env_block = "\n".join(
            f'        - name: {k}\n          value: "{v}"' for k, v in config.env_vars.items()
        )
        secret_env_block = "\n".join(
            f"        - name: {ref.upper()}\n"
            f"          valueFrom:\n"
            f"            secretKeyRef:\n"
            f"              name: {config.project_id}-secrets\n"
            f"              key: {ref}"
            for ref in config.secret_refs
        )
        return f"""---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {config.project_id}-{config.environment}
  namespace: forge-system
  labels:
    app: {config.project_id}
    env: {config.environment}
    managed-by: forge
spec:
  replicas: {config.replicas}
  strategy:
    type: {"RollingUpdate" if config.rollout_strategy == "rolling" else "Recreate"}
    {"rollingUpdate:" if config.rollout_strategy == "rolling" else ""}
      {"maxSurge: 1" if config.rollout_strategy == "rolling" else ""}
      {"maxUnavailable: 0" if config.rollout_strategy == "rolling" else ""}
  selector:
    matchLabels:
      app: {config.project_id}
      env: {config.environment}
  template:
    metadata:
      labels:
        app: {config.project_id}
        env: {config.environment}
    spec:
      terminationGracePeriodSeconds: 30
      containers:
      - name: app
        image: {config.project_id}:{config.image_tag}
        imagePullPolicy: Always
        ports:
        - containerPort: {config.port}
          protocol: TCP
        resources:
          requests:
            cpu: "100m"
            memory: "256Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
        livenessProbe:
          httpGet:
            path: {config.health_check_path}
            port: {config.port}
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: {config.health_check_path}
            port: {config.port}
          initialDelaySeconds: 10
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 3
        env:
{env_block}
{secret_env_block}
---
apiVersion: v1
kind: Service
metadata:
  name: {config.project_id}-{config.environment}
  namespace: forge-system
spec:
  selector:
    app: {config.project_id}
    env: {config.environment}
  ports:
  - protocol: TCP
    port: 80
    targetPort: {config.port}
  type: ClusterIP
"""
