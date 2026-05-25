"""Kubernetes cluster management for the FORGE platform.

Wraps the official ``kubernetes-client`` Python library to provide
Deployment, Service, ConfigMap, Secret, Pod, and Namespace management
operations.  Degrades gracefully when no Kubernetes cluster configuration
is available in the runtime environment.
"""

from __future__ import annotations

from typing import Any

import yaml

from system.observability.logging.logger import get_logger
from system.shared.exceptions import ToolError

logger = get_logger(__name__)


class K8sManager:
    """Manage Kubernetes resources via the official Python client.

    On initialisation, attempts to load cluster configuration from:
    1. A specific kubeconfig file (``kubeconfig_path``).
    2. In-cluster service account (when running inside a Pod).
    3. Default kubeconfig at ``~/.kube/config``.

    If none of the above succeed, the manager enters a degraded state
    and all methods raise ``ToolError`` with code ``"K8S_UNAVAILABLE"``.

    Args:
        kubeconfig_path: Optional explicit path to a kubeconfig file.
        namespace: Default namespace for operations that don't specify one.
    """

    def __init__(
        self,
        kubeconfig_path: str | None = None,
        namespace: str = "default",
    ) -> None:
        self.default_namespace = namespace
        self.available = False
        self.apps_v1 = None
        self.core_v1 = None
        self.batch_v1 = None
        self.networking_v1 = None

        try:
            from kubernetes import client as k8s_client  # type: ignore[import-untyped]
            from kubernetes import config as k8s_config  # type: ignore[import-untyped]

            if kubeconfig_path:
                k8s_config.load_kube_config(config_file=kubeconfig_path)
                logger.info("Kubernetes config loaded from file", path=kubeconfig_path)
            else:
                try:
                    k8s_config.load_incluster_config()
                    logger.info("Kubernetes in-cluster config loaded")
                except k8s_config.ConfigException:
                    k8s_config.load_kube_config()
                    logger.info("Kubernetes kubeconfig loaded from default location")

            self.apps_v1 = k8s_client.AppsV1Api()
            self.core_v1 = k8s_client.CoreV1Api()
            self.batch_v1 = k8s_client.BatchV1Api()
            self.networking_v1 = k8s_client.NetworkingV1Api()
            self.available = True

        except Exception as exc:
            logger.warning("Kubernetes not available", error=str(exc))
            self.available = False

    # ------------------------------------------------------------------ #
    # Guard
    # ------------------------------------------------------------------ #

    def _require_k8s(self) -> None:
        """Raise ToolError if Kubernetes is not configured."""
        if not self.available:
            raise ToolError(
                "Kubernetes is not configured in this environment.",
                "K8S_UNAVAILABLE",
            )

    # ------------------------------------------------------------------ #
    # Generic manifest operations
    # ------------------------------------------------------------------ #

    def apply_manifest(
        self,
        manifest_yaml: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Apply one or more Kubernetes manifests from a YAML string.

        Handles multiple documents in a single YAML stream.  Each document
        is dispatched to the appropriate API based on its ``kind`` field.

        Args:
            manifest_yaml: Raw YAML string containing one or more manifests.
            namespace: Target namespace (overrides manifest metadata if set).

        Returns:
            Dict with ``applied`` list of ``{"kind", "name", "status"}`` records.
        """
        self._require_k8s()

        documents = list(yaml.safe_load_all(manifest_yaml))
        results: list[dict[str, Any]] = []

        for doc in documents:
            if not doc:
                continue
            kind = doc.get("kind", "")
            name = doc.get("metadata", {}).get("name", "")
            ns = namespace or doc.get("metadata", {}).get("namespace", self.default_namespace)

            try:
                status = self._apply_single(doc, kind, name, ns)
                results.append({"kind": kind, "name": name, "namespace": ns, "status": status})
            except Exception as exc:
                logger.error(
                    "Failed to apply manifest",
                    kind=kind,
                    name=name,
                    error=str(exc)[:200],
                )
                results.append(
                    {
                        "kind": kind,
                        "name": name,
                        "namespace": ns,
                        "status": "error",
                        "error": str(exc)[:200],
                    }
                )

        logger.info(
            "Manifests applied", count=len(results), namespace=namespace or self.default_namespace
        )
        return {"applied": results}

    def _apply_single(self, manifest: dict[str, Any], kind: str, name: str, namespace: str) -> str:
        """Dispatch a single manifest to the appropriate Kubernetes API."""
        from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

        def _try_create_or_patch(create_fn: Any, patch_fn: Any, body: Any) -> str:
            try:
                create_fn(namespace=namespace, body=body)
                return "created"
            except ApiException as exc:
                if exc.status == 409:  # Already exists — patch instead.
                    patch_fn(name=name, namespace=namespace, body=body)
                    return "patched"
                raise

        kind_upper = kind.upper()

        if kind_upper == "DEPLOYMENT":
            return _try_create_or_patch(
                self.apps_v1.create_namespaced_deployment,
                self.apps_v1.patch_namespaced_deployment,
                manifest,
            )
        elif kind_upper == "SERVICE":
            return _try_create_or_patch(
                self.core_v1.create_namespaced_service,
                self.core_v1.patch_namespaced_service,
                manifest,
            )
        elif kind_upper == "CONFIGMAP":
            return _try_create_or_patch(
                self.core_v1.create_namespaced_config_map,
                self.core_v1.patch_namespaced_config_map,
                manifest,
            )
        elif kind_upper == "SECRET":
            return _try_create_or_patch(
                self.core_v1.create_namespaced_secret,
                self.core_v1.patch_namespaced_secret,
                manifest,
            )
        elif kind_upper == "JOB":
            return _try_create_or_patch(
                self.batch_v1.create_namespaced_job,
                self.batch_v1.patch_namespaced_job,
                manifest,
            )
        elif kind_upper == "INGRESS":
            return _try_create_or_patch(
                self.networking_v1.create_namespaced_ingress,
                self.networking_v1.patch_namespaced_ingress,
                manifest,
            )
        elif kind_upper == "NAMESPACE":
            try:
                self.core_v1.create_namespace(body=manifest)
                return "created"
            except Exception:
                self.core_v1.patch_namespace(name=name, body=manifest)
                return "patched"
        else:
            logger.warning("Unsupported manifest kind — skipping", kind=kind, name=name)
            return "skipped"

    # ------------------------------------------------------------------ #
    # Deployment operations
    # ------------------------------------------------------------------ #

    def get_deployment_status(
        self,
        name: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Return current status for a Deployment.

        Args:
            name: Deployment name.
            namespace: Kubernetes namespace.

        Returns:
            Dict with replica counts, conditions, and observed generation.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        try:
            dep = self.apps_v1.read_namespaced_deployment(name=name, namespace=ns)
        except Exception as exc:
            raise ToolError(
                f"Deployment '{name}' not found in namespace '{ns}': {exc}",
                "RESOURCE_NOT_FOUND",
            ) from exc

        status = dep.status
        spec = dep.spec
        return {
            "name": dep.metadata.name,
            "namespace": ns,
            "replicas": spec.replicas if spec else 0,
            "ready_replicas": status.ready_replicas or 0 if status else 0,
            "available_replicas": status.available_replicas or 0 if status else 0,
            "updated_replicas": status.updated_replicas or 0 if status else 0,
            "unavailable_replicas": status.unavailable_replicas or 0 if status else 0,
            "observed_generation": status.observed_generation if status else 0,
            "conditions": [
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                }
                for c in (status.conditions or [])
            ]
            if status
            else [],
        }

    def scale_deployment(
        self,
        name: str,
        replicas: int,
        namespace: str | None = None,
    ) -> None:
        """Scale a Deployment to the specified replica count.

        Args:
            name: Deployment name.
            replicas: Target replica count.
            namespace: Kubernetes namespace.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        self.apps_v1.patch_namespaced_deployment_scale(
            name=name,
            namespace=ns,
            body={"spec": {"replicas": replicas}},
        )
        logger.info("Deployment scaled", name=name, namespace=ns, replicas=replicas)

    def list_deployments(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """List all Deployments in a namespace.

        Args:
            namespace: Kubernetes namespace (all namespaces if ``None``).

        Returns:
            List of deployment status dicts.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        deps = self.apps_v1.list_namespaced_deployment(namespace=ns)
        return [
            {
                "name": d.metadata.name,
                "namespace": d.metadata.namespace,
                "replicas": d.spec.replicas if d.spec else 0,
                "ready_replicas": d.status.ready_replicas or 0 if d.status else 0,
                "image": d.spec.template.spec.containers[0].image
                if d.spec and d.spec.template.spec.containers
                else "unknown",
            }
            for d in deps.items
        ]

    def restart_deployment(
        self,
        name: str,
        namespace: str | None = None,
    ) -> None:
        """Trigger a rolling restart of a Deployment by patching its annotations.

        Args:
            name: Deployment name.
            namespace: Kubernetes namespace.
        """
        self._require_k8s()
        from datetime import datetime

        ns = namespace or self.default_namespace
        now = datetime.utcnow().isoformat() + "Z"
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                        }
                    }
                }
            }
        }
        self.apps_v1.patch_namespaced_deployment(name=name, namespace=ns, body=patch)
        logger.info("Deployment restarted", name=name, namespace=ns)

    def update_deployment_image(
        self,
        name: str,
        container_name: str,
        new_image: str,
        namespace: str | None = None,
    ) -> None:
        """Update the container image for a specific container in a Deployment.

        Args:
            name: Deployment name.
            container_name: Name of the container within the Deployment spec.
            new_image: Full image reference including tag.
            namespace: Kubernetes namespace.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        patch = {
            "spec": {
                "template": {"spec": {"containers": [{"name": container_name, "image": new_image}]}}
            }
        }
        self.apps_v1.patch_namespaced_deployment(name=name, namespace=ns, body=patch)
        logger.info(
            "Deployment image updated",
            name=name,
            namespace=ns,
            container=container_name,
            image=new_image,
        )

    # ------------------------------------------------------------------ #
    # Pod operations
    # ------------------------------------------------------------------ #

    def list_pods(
        self,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        """List Pods in a namespace.

        Args:
            namespace: Kubernetes namespace.
            label_selector: Kubernetes label selector string, e.g.
                           ``"app=myapp,version=v1"``.

        Returns:
            List of pod info dicts with name, status, and IP.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        kwargs: dict[str, Any] = {"namespace": ns}
        if label_selector:
            kwargs["label_selector"] = label_selector
        pods = self.core_v1.list_namespaced_pod(**kwargs)
        return [
            {
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "status": p.status.phase if p.status else "Unknown",
                "pod_ip": p.status.pod_ip if p.status else None,
                "node": p.spec.node_name if p.spec else None,
                "ready": all(c.ready for c in (p.status.container_statuses or []))
                if p.status and p.status.container_statuses
                else False,
            }
            for p in pods.items
        ]

    def get_pod_logs(
        self,
        name: str,
        namespace: str | None = None,
        container: str | None = None,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> str:
        """Fetch log output from a Pod (or a specific container within it).

        Args:
            name: Pod name.
            namespace: Kubernetes namespace.
            container: Container name (required for multi-container Pods).
            tail_lines: Number of log lines to return from the end.
            previous: Return logs from the previous (crashed) container instance.

        Returns:
            Log output as a string.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        kwargs: dict[str, Any] = {
            "name": name,
            "namespace": ns,
            "tail_lines": tail_lines,
            "previous": previous,
        }
        if container:
            kwargs["container"] = container
        try:
            return self.core_v1.read_namespaced_pod_log(**kwargs) or ""
        except Exception as exc:
            raise ToolError(
                f"Failed to get logs for pod '{name}': {exc}",
                "LOGS_ERROR",
            ) from exc

    def delete_pod(
        self,
        name: str,
        namespace: str | None = None,
        grace_period_seconds: int = 0,
    ) -> None:
        """Delete a Pod (it will be recreated by the owning controller).

        Args:
            name: Pod name.
            namespace: Kubernetes namespace.
            grace_period_seconds: Termination grace period.
        """
        self._require_k8s()
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]

        ns = namespace or self.default_namespace
        self.core_v1.delete_namespaced_pod(
            name=name,
            namespace=ns,
            body=k8s_client.V1DeleteOptions(grace_period_seconds=grace_period_seconds),
        )
        logger.info("Pod deleted", name=name, namespace=ns)

    # ------------------------------------------------------------------ #
    # ConfigMap & Secret management
    # ------------------------------------------------------------------ #

    def create_or_update_configmap(
        self,
        name: str,
        data: dict[str, str],
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create or update a ConfigMap.

        Args:
            name: ConfigMap name.
            data: Key/value data to store.
            namespace: Kubernetes namespace.
            labels: Optional labels to apply.

        Returns:
            ``"created"`` or ``"updated"``.
        """
        self._require_k8s()
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

        ns = namespace or self.default_namespace
        body = k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=ns,
                labels=labels or {"managed-by": "forge"},
            ),
            data=data,
        )
        try:
            self.core_v1.create_namespaced_config_map(namespace=ns, body=body)
            return "created"
        except ApiException as exc:
            if exc.status == 409:
                self.core_v1.replace_namespaced_config_map(name=name, namespace=ns, body=body)
                return "updated"
            raise ToolError(f"ConfigMap operation failed: {exc}", "CONFIGMAP_ERROR") from exc

    def create_or_update_secret(
        self,
        name: str,
        string_data: dict[str, str],
        namespace: str | None = None,
        secret_type: str = "Opaque",
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create or update a Kubernetes Secret.

        Args:
            name: Secret name.
            string_data: Plain-text key/value pairs (Kubernetes will base64-encode them).
            namespace: Kubernetes namespace.
            secret_type: Secret type (e.g. ``"Opaque"``, ``"kubernetes.io/tls"``).
            labels: Optional labels to apply.

        Returns:
            ``"created"`` or ``"updated"``.
        """
        self._require_k8s()
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

        ns = namespace or self.default_namespace
        body = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=ns,
                labels=labels or {"managed-by": "forge"},
            ),
            string_data=string_data,
            type=secret_type,
        )
        try:
            self.core_v1.create_namespaced_secret(namespace=ns, body=body)
            return "created"
        except ApiException as exc:
            if exc.status == 409:
                self.core_v1.replace_namespaced_secret(name=name, namespace=ns, body=body)
                return "updated"
            raise ToolError(f"Secret operation failed: {exc}", "SECRET_ERROR") from exc

    # ------------------------------------------------------------------ #
    # Namespace management
    # ------------------------------------------------------------------ #

    def create_namespace(
        self,
        name: str,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a Kubernetes namespace.

        Args:
            name: Namespace name.
            labels: Labels to apply to the namespace.

        Returns:
            ``"created"`` or ``"exists"``.
        """
        self._require_k8s()
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

        body = k8s_client.V1Namespace(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                labels=labels or {"managed-by": "forge"},
            )
        )
        try:
            self.core_v1.create_namespace(body=body)
            logger.info("Namespace created", name=name)
            return "created"
        except ApiException as exc:
            if exc.status == 409:
                return "exists"
            raise ToolError(
                f"Failed to create namespace '{name}': {exc}", "NAMESPACE_ERROR"
            ) from exc

    def list_namespaces(self) -> list[str]:
        """Return a list of all namespace names in the cluster."""
        self._require_k8s()
        ns_list = self.core_v1.list_namespace()
        return [ns.metadata.name for ns in ns_list.items]

    # ------------------------------------------------------------------ #
    # Service operations
    # ------------------------------------------------------------------ #

    def get_service(
        self,
        name: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Return details for a Kubernetes Service.

        Args:
            name: Service name.
            namespace: Kubernetes namespace.

        Returns:
            Dict with cluster IP, external IPs, and port mappings.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        try:
            svc = self.core_v1.read_namespaced_service(name=name, namespace=ns)
        except Exception as exc:
            raise ToolError(f"Service '{name}' not found: {exc}", "RESOURCE_NOT_FOUND") from exc

        spec = svc.spec
        return {
            "name": svc.metadata.name,
            "namespace": ns,
            "type": spec.type if spec else "ClusterIP",
            "cluster_ip": spec.cluster_ip if spec else None,
            "external_ips": spec.external_i_ps if spec else [],
            "ports": [
                {
                    "port": p.port,
                    "target_port": str(p.target_port),
                    "protocol": p.protocol,
                    "node_port": p.node_port,
                }
                for p in (spec.ports or [])
            ]
            if spec
            else [],
        }

    def list_services(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """List Services in a namespace.

        Args:
            namespace: Kubernetes namespace.
        """
        self._require_k8s()
        ns = namespace or self.default_namespace
        svcs = self.core_v1.list_namespaced_service(namespace=ns)
        return [
            {
                "name": s.metadata.name,
                "type": s.spec.type if s.spec else "ClusterIP",
                "cluster_ip": s.spec.cluster_ip if s.spec else None,
            }
            for s in svcs.items
        ]

    # ------------------------------------------------------------------ #
    # Health & diagnostics
    # ------------------------------------------------------------------ #

    def is_available(self) -> bool:
        """Return True if the Kubernetes API is reachable."""
        return self.available

    def get_cluster_info(self) -> dict[str, Any]:
        """Return basic cluster-level information.

        Returns:
            Dict with API server version and node count.
        """
        self._require_k8s()
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]

        version_api = k8s_client.VersionApi()
        try:
            version = version_api.get_code()
            server_version = f"{version.major}.{version.minor}"
        except Exception:
            server_version = "unknown"

        try:
            nodes = self.core_v1.list_node()
            node_count = len(nodes.items)
            node_names = [n.metadata.name for n in nodes.items]
        except Exception:
            node_count = 0
            node_names = []

        return {
            "server_version": server_version,
            "node_count": node_count,
            "nodes": node_names,
        }
