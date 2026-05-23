"""Task Graph Engine — Phase 5 of the FORGE pipeline.

Builds a complete directed-acyclic graph (DAG) of TaskNodes from a
ProjectSpec and ArchitecturePlan, computes topological execution order
(Kahn's algorithm), finds the critical path, and persists / loads graphs
to/from PostgreSQL via SQLAlchemy.

Usage::

    engine = TaskGraphEngine()
    graph  = await engine.generate(project_id, spec, arch)
    engine.validate_graph(graph)                  # raises on cycles
    await engine.persist_graph(graph)
    graph2 = await engine.load_graph(graph.graph_id)
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from system.core.orchestration.task_schemas import (
    GenerateGraphRequest,
    GraphStatusSummary,
    TaskGraph,
    TaskGraphUpdate,
    TaskNode,
    ValidationRule,
)
from system.core.planning.schemas import ArchitecturePlan, ServiceDefinition
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.database import get_db, AsyncSessionLocal
from system.shared.exceptions import OrchestrationError, ValidationError
from system.shared.models import AgentType, ExecutionPhase, Priority, TaskStatus

logger = get_logger(__name__)


# ========================================================================== #
# Helpers — token and duration estimation
# ========================================================================== #

_AGENT_TOKEN_ESTIMATES: Dict[str, int] = {
    AgentType.ARCHITECT: 6144,
    AgentType.BACKEND: 8192,
    AgentType.FRONTEND: 6144,
    AgentType.INFRA: 4096,
    AgentType.QA: 4096,
    AgentType.SECURITY: 3072,
    AgentType.DOCS: 2048,
    AgentType.REFACTOR: 4096,
}

# Rough tokens-per-minute throughput at 1 concurrent worker
_TOKENS_PER_MINUTE: int = 8000


def _estimate_task_minutes(task: TaskNode) -> int:
    """Estimate minutes a task takes based on token budget."""
    tokens = task.estimated_tokens
    return max(1, round(tokens / _TOKENS_PER_MINUTE))


# ========================================================================== #
# Task factory helpers
# ========================================================================== #


def _make_task_id(phase: str, role: str, suffix: str) -> str:
    """Create a deterministic, readable task_id."""
    return f"{phase}:{role}:{suffix}"


def _validation_rules_for_agent(agent_type: AgentType, output_artifacts: List[str]) -> List[ValidationRule]:
    """Build a sensible set of validation rules for a given agent type."""
    rules: List[ValidationRule] = []

    # File-exists checks for every expected output
    for artifact in output_artifacts:
        rules.append(ValidationRule(rule_type="file_exists", target=artifact, severity="error"))

    # Agent-specific extra checks
    if agent_type == AgentType.BACKEND:
        rules.append(ValidationRule(rule_type="lint_clean", target="backend/", severity="warning"))
        rules.append(ValidationRule(rule_type="type_check", target="backend/", severity="warning"))
    elif agent_type == AgentType.FRONTEND:
        rules.append(ValidationRule(rule_type="lint_clean", target="frontend/", severity="warning"))
    elif agent_type == AgentType.QA:
        rules.append(ValidationRule(rule_type="tests_pass", target="tests/", severity="error"))

    return rules


# ========================================================================== #
# Task Graph Engine
# ========================================================================== #


class TaskGraphEngine:
    """Builds, validates, persists, and loads FORGE task graphs.

    The engine is stateless; all state lives in the TaskGraph / TaskNode
    objects and in PostgreSQL / Redis.
    """

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def generate(
        self,
        project_id: str,
        spec: ProjectSpec,
        arch: ArchitecturePlan,
    ) -> TaskGraph:
        """Generate a complete task graph for *project_id*.

        The graph covers the full pipeline:
        1. SPECIFICATION phase tasks
        2. ARCHITECTURE phase tasks
        3. EXECUTION phase tasks (per service × agent type)
        4. VERIFICATION phase tasks
        5. DEPLOYMENT phase tasks

        Args:
            project_id: FORGE project identifier.
            spec:        Finalized ProjectSpec.
            arch:        Finalized ArchitecturePlan.

        Returns:
            A fully populated and validated TaskGraph.
        """
        graph_id = str(uuid.uuid4())
        logger.info("generating_task_graph", project_id=project_id, graph_id=graph_id)

        all_tasks: List[TaskNode] = []

        # 1 – Specification phase tasks
        spec_tasks = self._build_specification_tasks(project_id, spec)
        all_tasks.extend(spec_tasks)

        # 2 – Architecture phase tasks
        arch_tasks = self._build_architecture_tasks(project_id, spec, arch)
        all_tasks.extend(arch_tasks)

        # 3 – Execution phase tasks (main coding work)
        exec_tasks = self._build_execution_tasks(project_id, spec, arch)
        all_tasks.extend(exec_tasks)

        # 4 – Verification phase tasks
        verif_tasks = self._build_verification_tasks(project_id, spec, arch)
        all_tasks.extend(verif_tasks)

        # 5 – Deployment phase tasks
        deploy_tasks = self._build_deployment_tasks(project_id, spec, arch)
        all_tasks.extend(deploy_tasks)

        # Assign dependency edges
        all_tasks = self._calculate_dependencies(all_tasks)

        # Topological sort → parallelizable execution levels
        execution_order = self._topological_sort(all_tasks)

        # Critical path (longest dependency chain)
        critical_path = self._find_critical_path(all_tasks)

        # Duration estimate
        estimated_duration = self._estimate_duration(all_tasks)

        graph = TaskGraph(
            graph_id=graph_id,
            project_id=project_id,
            tasks=all_tasks,
            phase=ExecutionPhase.EXECUTION,
            total_tasks=len(all_tasks),
            completed_tasks=0,
            failed_tasks=0,
            execution_order=execution_order,
            critical_path=critical_path,
            estimated_duration_minutes=estimated_duration,
        )

        self.validate_graph(graph)

        logger.info(
            "task_graph_generated",
            graph_id=graph_id,
            total_tasks=len(all_tasks),
            levels=len(execution_order),
            critical_path_len=len(critical_path),
            estimated_minutes=estimated_duration,
        )
        return graph

    # ------------------------------------------------------------------ #
    # Phase task builders
    # ------------------------------------------------------------------ #

    def _build_specification_tasks(
        self, project_id: str, spec: ProjectSpec
    ) -> List[TaskNode]:
        """One task per major specification component."""
        tasks: List[TaskNode] = []

        components = [
            ("prd", "Write Product Requirements Document", AgentType.ARCHITECT, ["docs/prd.md"]),
            ("db_schema", "Design normalized database schema", AgentType.ARCHITECT, ["docs/schema.md"]),
            ("api_contract", "Define REST API contract and OpenAPI spec", AgentType.ARCHITECT, ["docs/api.yaml"]),
            ("ui_structure", "Define UI page and component structure", AgentType.ARCHITECT, ["docs/ui.md"]),
            ("permissions", "Design RBAC permissions matrix", AgentType.ARCHITECT, ["docs/permissions.md"]),
        ]

        for suffix, description, agent, artifacts in components:
            task_id = _make_task_id("spec", agent.value, suffix)
            rules = _validation_rules_for_agent(agent, artifacts)
            task = TaskNode(
                task_id=task_id,
                name=f"spec:{suffix}",
                description=description,
                agent_type=agent,
                priority=Priority.HIGH,
                status=TaskStatus.PENDING,
                dependencies=[],
                blocking=[],
                validation_rules=rules,
                input_context={
                    "spec_id": spec.id,
                    "project_id": project_id,
                    "component": suffix,
                },
                output_artifacts=artifacts,
                max_retries=2,
                timeout_seconds=1800,
                estimated_tokens=_AGENT_TOKEN_ESTIMATES[agent],
                project_id=project_id,
                phase=ExecutionPhase.SPECIFICATION,
            )
            tasks.append(task)

        return tasks

    def _build_architecture_tasks(
        self,
        project_id: str,
        spec: ProjectSpec,
        arch: ArchitecturePlan,
    ) -> List[TaskNode]:
        """Architecture planning tasks — one per planning concern."""
        tasks: List[TaskNode] = []

        arch_items = [
            ("service_topology", "Define service topology and inter-service contracts", ["docs/architecture.md"]),
            ("data_flow", "Map data flow and event streams between services", ["docs/data_flow.md"]),
            ("infra_plan", "Plan infrastructure components and cloud resources", ["infra/plan.md"]),
            ("security_arch", "Define security architecture and threat model", ["docs/security.md"]),
        ]

        for suffix, description, artifacts in arch_items:
            task_id = _make_task_id("arch", "architect", suffix)
            task = TaskNode(
                task_id=task_id,
                name=f"arch:{suffix}",
                description=description,
                agent_type=AgentType.ARCHITECT,
                priority=Priority.HIGH,
                status=TaskStatus.PENDING,
                dependencies=[],
                blocking=[],
                validation_rules=_validation_rules_for_agent(AgentType.ARCHITECT, artifacts),
                input_context={
                    "arch_id": arch.plan_id,
                    "project_id": project_id,
                    "concern": suffix,
                },
                output_artifacts=artifacts,
                max_retries=2,
                timeout_seconds=1800,
                estimated_tokens=_AGENT_TOKEN_ESTIMATES[AgentType.ARCHITECT],
                project_id=project_id,
                phase=ExecutionPhase.ARCHITECTURE,
            )
            tasks.append(task)

        return tasks

    def _build_execution_tasks(
        self,
        project_id: str,
        spec: ProjectSpec,
        arch: ArchitecturePlan,
    ) -> List[TaskNode]:
        """Generate execution-phase tasks for every service in the architecture.

        For each backend service:  models, routes, services, tests
        For each frontend service: pages, components, state management
        For each infra service:    Dockerfile, K8s manifests, Terraform
        """
        tasks: List[TaskNode] = []

        # ---- Backend services ----
        for svc in arch.backend_services():
            svc_name = svc.name
            backend_task_defs = [
                (
                    "models",
                    f"Generate SQLAlchemy ORM models for {svc_name}",
                    [f"backend/{svc_name}/models.py"],
                    Priority.HIGH,
                ),
                (
                    "services",
                    f"Implement business-logic service layer for {svc_name}",
                    [f"backend/{svc_name}/services.py"],
                    Priority.HIGH,
                ),
                (
                    "routes",
                    f"Implement FastAPI routes for {svc_name}",
                    [f"backend/{svc_name}/routes.py"],
                    Priority.MEDIUM,
                ),
                (
                    "schemas",
                    f"Generate Pydantic request/response schemas for {svc_name}",
                    [f"backend/{svc_name}/schemas.py"],
                    Priority.MEDIUM,
                ),
                (
                    "tests",
                    f"Write pytest unit and integration tests for {svc_name}",
                    [f"tests/unit/test_{svc_name}.py", f"tests/integration/test_{svc_name}_api.py"],
                    Priority.MEDIUM,
                ),
            ]

            for suffix, description, artifacts, priority in backend_task_defs:
                task_id = _make_task_id("exec", "backend", f"{svc_name}_{suffix}")
                task = TaskNode(
                    task_id=task_id,
                    name=f"backend:{svc_name}:{suffix}",
                    description=description,
                    agent_type=AgentType.BACKEND,
                    priority=priority,
                    status=TaskStatus.PENDING,
                    dependencies=[],
                    blocking=[],
                    validation_rules=_validation_rules_for_agent(AgentType.BACKEND, artifacts),
                    input_context={
                        "service": svc_name,
                        "technology": svc.technology,
                        "component": suffix,
                        "project_id": project_id,
                        "spec_id": spec.id,
                        "arch_id": arch.plan_id,
                    },
                    output_artifacts=artifacts,
                    max_retries=3,
                    timeout_seconds=3600,
                    estimated_tokens=_AGENT_TOKEN_ESTIMATES[AgentType.BACKEND],
                    project_id=project_id,
                    phase=ExecutionPhase.EXECUTION,
                )
                tasks.append(task)

        # ---- Frontend services ----
        for svc in arch.frontend_services():
            svc_name = svc.name
            frontend_task_defs = [
                (
                    "pages",
                    f"Implement React pages for {svc_name}",
                    [f"frontend/{svc_name}/pages/"],
                    Priority.MEDIUM,
                ),
                (
                    "components",
                    f"Implement reusable React components for {svc_name}",
                    [f"frontend/{svc_name}/components/"],
                    Priority.MEDIUM,
                ),
                (
                    "state",
                    f"Implement Zustand/React Query state management for {svc_name}",
                    [f"frontend/{svc_name}/store/"],
                    Priority.LOW,
                ),
                (
                    "api_client",
                    f"Generate typed API client for {svc_name}",
                    [f"frontend/{svc_name}/api/client.ts"],
                    Priority.HIGH,
                ),
            ]

            for suffix, description, artifacts, priority in frontend_task_defs:
                task_id = _make_task_id("exec", "frontend", f"{svc_name}_{suffix}")
                task = TaskNode(
                    task_id=task_id,
                    name=f"frontend:{svc_name}:{suffix}",
                    description=description,
                    agent_type=AgentType.FRONTEND,
                    priority=priority,
                    status=TaskStatus.PENDING,
                    dependencies=[],
                    blocking=[],
                    validation_rules=_validation_rules_for_agent(AgentType.FRONTEND, artifacts),
                    input_context={
                        "service": svc_name,
                        "technology": svc.technology,
                        "component": suffix,
                        "project_id": project_id,
                        "spec_id": spec.id,
                    },
                    output_artifacts=artifacts,
                    max_retries=3,
                    timeout_seconds=3600,
                    estimated_tokens=_AGENT_TOKEN_ESTIMATES[AgentType.FRONTEND],
                    project_id=project_id,
                    phase=ExecutionPhase.EXECUTION,
                )
                tasks.append(task)

        # ---- Infrastructure ----
        infra_task_defs = [
            (
                "dockerfile",
                "Write production-grade Dockerfiles for all services",
                ["infra/docker/"],
                Priority.HIGH,
            ),
            (
                "docker_compose",
                "Write docker-compose.yml for local development",
                ["docker-compose.yml"],
                Priority.MEDIUM,
            ),
            (
                "k8s_manifests",
                "Generate Kubernetes deployment, service, and ingress manifests",
                ["infra/k8s/"],
                Priority.MEDIUM,
            ),
            (
                "terraform",
                "Write Terraform modules for cloud infrastructure",
                ["infra/terraform/"],
                Priority.MEDIUM,
            ),
            (
                "ci_cd",
                "Configure GitHub Actions CI/CD pipelines",
                [".github/workflows/"],
                Priority.LOW,
            ),
            (
                "env_config",
                "Generate environment variable templates and secrets management",
                [".env.example", "infra/secrets/"],
                Priority.HIGH,
            ),
        ]

        for suffix, description, artifacts, priority in infra_task_defs:
            task_id = _make_task_id("exec", "infra", suffix)
            task = TaskNode(
                task_id=task_id,
                name=f"infra:{suffix}",
                description=description,
                agent_type=AgentType.INFRA,
                priority=priority,
                status=TaskStatus.PENDING,
                dependencies=[],
                blocking=[],
                validation_rules=_validation_rules_for_agent(AgentType.INFRA, artifacts),
                input_context={
                    "deployment_target": arch.deployment_target,
                    "services": [s.name for s in arch.services],
                    "component": suffix,
                    "project_id": project_id,
                    "arch_id": arch.plan_id,
                },
                output_artifacts=artifacts,
                max_retries=2,
                timeout_seconds=2400,
                estimated_tokens=_AGENT_TOKEN_ESTIMATES[AgentType.INFRA],
                project_id=project_id,
                phase=ExecutionPhase.EXECUTION,
            )
            tasks.append(task)

        return tasks

    def _build_verification_tasks(
        self,
        project_id: str,
        spec: ProjectSpec,
        arch: ArchitecturePlan,
    ) -> List[TaskNode]:
        """Verification-phase tasks: test runner, lint, type check, security scan."""
        tasks: List[TaskNode] = []

        verif_defs = [
            (
                "run_tests",
                AgentType.QA,
                "Execute the full test suite and report coverage",
                ["reports/test_results.xml", "reports/coverage.html"],
                Priority.CRITICAL,
            ),
            (
                "lint",
                AgentType.QA,
                "Run linters (ruff, eslint) across the entire codebase",
                ["reports/lint.json"],
                Priority.HIGH,
            ),
            (
                "type_check",
                AgentType.QA,
                "Run mypy and tsc for comprehensive type checking",
                ["reports/type_check.txt"],
                Priority.HIGH,
            ),
            (
                "security_scan",
                AgentType.SECURITY,
                "Run SAST security scan (bandit, semgrep) and dependency audit",
                ["reports/security.json"],
                Priority.HIGH,
            ),
            (
                "performance_test",
                AgentType.QA,
                "Run basic load tests with Locust and record baselines",
                ["reports/performance.html"],
                Priority.MEDIUM,
            ),
        ]

        for suffix, agent, description, artifacts, priority in verif_defs:
            task_id = _make_task_id("verif", agent.value, suffix)
            rules = [
                ValidationRule(rule_type="file_exists", target=a, severity="error")
                for a in artifacts
            ]
            task = TaskNode(
                task_id=task_id,
                name=f"verif:{suffix}",
                description=description,
                agent_type=agent,
                priority=priority,
                status=TaskStatus.PENDING,
                dependencies=[],
                blocking=[],
                validation_rules=rules,
                input_context={"project_id": project_id, "verification_type": suffix},
                output_artifacts=artifacts,
                max_retries=2,
                timeout_seconds=1800,
                estimated_tokens=_AGENT_TOKEN_ESTIMATES[agent],
                project_id=project_id,
                phase=ExecutionPhase.VERIFICATION,
            )
            tasks.append(task)

        return tasks

    def _build_deployment_tasks(
        self,
        project_id: str,
        spec: ProjectSpec,
        arch: ArchitecturePlan,
    ) -> List[TaskNode]:
        """Deployment-phase tasks: build images, push, deploy, smoke test."""
        tasks: List[TaskNode] = []

        deploy_defs = [
            (
                "build_images",
                "Build and tag Docker images for all services",
                ["reports/build_manifest.json"],
                Priority.HIGH,
            ),
            (
                "push_registry",
                "Push images to container registry",
                ["reports/push_manifest.json"],
                Priority.HIGH,
            ),
            (
                "apply_migrations",
                "Apply database migrations in target environment",
                ["reports/migration.log"],
                Priority.CRITICAL,
            ),
            (
                "deploy_services",
                "Deploy all services to target infrastructure",
                ["reports/deploy_manifest.json"],
                Priority.CRITICAL,
            ),
            (
                "smoke_tests",
                "Run smoke tests against the deployed environment",
                ["reports/smoke_test.json"],
                Priority.HIGH,
            ),
        ]

        for suffix, description, artifacts, priority in deploy_defs:
            task_id = _make_task_id("deploy", "infra", suffix)
            task = TaskNode(
                task_id=task_id,
                name=f"deploy:{suffix}",
                description=description,
                agent_type=AgentType.INFRA,
                priority=priority,
                status=TaskStatus.PENDING,
                dependencies=[],
                blocking=[],
                validation_rules=[
                    ValidationRule(rule_type="file_exists", target=a, severity="error")
                    for a in artifacts
                ],
                input_context={
                    "project_id": project_id,
                    "deployment_target": arch.deployment_target,
                    "step": suffix,
                },
                output_artifacts=artifacts,
                max_retries=2,
                timeout_seconds=3600,
                estimated_tokens=_AGENT_TOKEN_ESTIMATES[AgentType.INFRA],
                project_id=project_id,
                phase=ExecutionPhase.DEPLOYMENT,
            )
            tasks.append(task)

        return tasks

    # ------------------------------------------------------------------ #
    # Dependency assignment
    # ------------------------------------------------------------------ #

    def _calculate_dependencies(self, tasks: List[TaskNode]) -> List[TaskNode]:
        """Assign dependency edges between tasks.

        Dependency rules (in order of specificity):
        - All ARCHITECTURE tasks depend on all SPECIFICATION tasks.
        - All EXECUTION tasks depend on all ARCHITECTURE tasks.
        - Within EXECUTION backend tasks:
            routes depends on schemas and services
            services depends on models
            tests depends on routes and services
        - Within EXECUTION frontend tasks:
            pages depends on components and api_client
            state depends on api_client
        - Infra k8s / terraform depend on dockerfile.
        - Infra ci_cd depends on k8s_manifests and terraform.
        - All VERIFICATION tasks depend on all EXECUTION tasks.
        - Deployment push_registry depends on build_images.
        - Deployment apply_migrations depends on push_registry.
        - Deployment deploy_services depends on apply_migrations.
        - Deployment smoke_tests depends on deploy_services.
        - All DEPLOYMENT tasks depend on all VERIFICATION tasks.
        """
        # Index tasks by task_id for O(1) lookup
        task_map: Dict[str, TaskNode] = {t.task_id: t for t in tasks}

        # Group tasks by phase
        by_phase: Dict[str, List[str]] = defaultdict(list)
        for t in tasks:
            by_phase[t.phase].append(t.task_id)

        spec_ids = by_phase[ExecutionPhase.SPECIFICATION]
        arch_ids = by_phase[ExecutionPhase.ARCHITECTURE]
        exec_ids = by_phase[ExecutionPhase.EXECUTION]
        verif_ids = by_phase[ExecutionPhase.VERIFICATION]
        deploy_ids = by_phase[ExecutionPhase.DEPLOYMENT]

        def _add_deps(task_id: str, dep_ids: List[str]) -> None:
            task = task_map.get(task_id)
            if task is None:
                return
            existing = set(task.dependencies)
            for dep in dep_ids:
                if dep != task_id and dep not in existing:
                    task.dependencies.append(dep)
                    existing.add(dep)
            # Update blocking on the dependency side
            for dep_id in dep_ids:
                dep_task = task_map.get(dep_id)
                if dep_task and task_id not in dep_task.blocking:
                    dep_task.blocking.append(task_id)

        # ---- Phase-level cross-dependencies ----
        for arch_tid in arch_ids:
            _add_deps(arch_tid, spec_ids)

        for exec_tid in exec_ids:
            _add_deps(exec_tid, arch_ids)

        for verif_tid in verif_ids:
            _add_deps(verif_tid, exec_ids)

        for deploy_tid in deploy_ids:
            _add_deps(deploy_tid, verif_ids)

        # ---- Intra-backend task ordering ----
        # Find all backend task_ids and group by service
        backend_by_svc: Dict[str, Dict[str, str]] = defaultdict(dict)
        for tid in exec_ids:
            task = task_map[tid]
            if task.agent_type == AgentType.BACKEND:
                # task_id format: "exec:backend:{svc_name}_{suffix}"
                parts = task.task_id.split(":")
                if len(parts) == 3:
                    svc_suffix = parts[2]  # e.g. "api_models"
                    # Extract suffix (last component after final "_")
                    underscore_idx = svc_suffix.rfind("_")
                    if underscore_idx != -1:
                        svc = svc_suffix[:underscore_idx]
                        suffix = svc_suffix[underscore_idx + 1:]
                        backend_by_svc[svc][suffix] = tid

        for svc, suffix_map in backend_by_svc.items():
            # services depends on models
            if "services" in suffix_map and "models" in suffix_map:
                _add_deps(suffix_map["services"], [suffix_map["models"]])
            # schemas is independent
            # routes depends on services and schemas
            if "routes" in suffix_map:
                if "services" in suffix_map:
                    _add_deps(suffix_map["routes"], [suffix_map["services"]])
                if "schemas" in suffix_map:
                    _add_deps(suffix_map["routes"], [suffix_map["schemas"]])
            # tests depends on routes and services
            if "tests" in suffix_map:
                if "routes" in suffix_map:
                    _add_deps(suffix_map["tests"], [suffix_map["routes"]])
                if "services" in suffix_map:
                    _add_deps(suffix_map["tests"], [suffix_map["services"]])

        # ---- Intra-frontend task ordering ----
        frontend_by_svc: Dict[str, Dict[str, str]] = defaultdict(dict)
        for tid in exec_ids:
            task = task_map[tid]
            if task.agent_type == AgentType.FRONTEND:
                parts = task.task_id.split(":")
                if len(parts) == 3:
                    svc_suffix = parts[2]
                    underscore_idx = svc_suffix.rfind("_")
                    if underscore_idx != -1:
                        svc = svc_suffix[:underscore_idx]
                        suffix = svc_suffix[underscore_idx + 1:]
                        frontend_by_svc[svc][suffix] = tid

        for svc, suffix_map in frontend_by_svc.items():
            # pages depends on components and api_client
            if "pages" in suffix_map:
                if "components" in suffix_map:
                    _add_deps(suffix_map["pages"], [suffix_map["components"]])
                if "api_client" in suffix_map:
                    _add_deps(suffix_map["pages"], [suffix_map["api_client"]])
            # state depends on api_client
            if "state" in suffix_map and "api_client" in suffix_map:
                _add_deps(suffix_map["state"], [suffix_map["api_client"]])

        # ---- Intra-infra task ordering ----
        infra_tids: Dict[str, str] = {}
        for tid in exec_ids:
            task = task_map[tid]
            if task.agent_type == AgentType.INFRA:
                parts = task.task_id.split(":")
                if len(parts) == 3:
                    infra_tids[parts[2]] = tid

        if "k8s_manifests" in infra_tids and "dockerfile" in infra_tids:
            _add_deps(infra_tids["k8s_manifests"], [infra_tids["dockerfile"]])
        if "terraform" in infra_tids and "dockerfile" in infra_tids:
            _add_deps(infra_tids["terraform"], [infra_tids["dockerfile"]])
        if "ci_cd" in infra_tids:
            prereqs = []
            for key in ("k8s_manifests", "terraform"):
                if key in infra_tids:
                    prereqs.append(infra_tids[key])
            if prereqs:
                _add_deps(infra_tids["ci_cd"], prereqs)

        # ---- Intra-deployment ordering ----
        deploy_tids: Dict[str, str] = {}
        for tid in deploy_ids:
            parts = tid.split(":")
            if len(parts) == 3:
                deploy_tids[parts[2]] = tid

        ordered_deploy = [
            ("push_registry", "build_images"),
            ("apply_migrations", "push_registry"),
            ("deploy_services", "apply_migrations"),
            ("smoke_tests", "deploy_services"),
        ]
        for target, prereq in ordered_deploy:
            if target in deploy_tids and prereq in deploy_tids:
                _add_deps(deploy_tids[target], [deploy_tids[prereq]])

        return list(task_map.values())

    # ------------------------------------------------------------------ #
    # Graph algorithms
    # ------------------------------------------------------------------ #

    def _topological_sort(self, tasks: List[TaskNode]) -> List[List[str]]:
        """Kahn's algorithm — returns parallelizable execution levels.

        Each inner list is a set of task_ids that can run concurrently
        (all their dependencies are in previous levels).

        Raises:
            OrchestrationError: if a cycle is detected.
        """
        task_map: Dict[str, TaskNode] = {t.task_id: t for t in tasks}

        # in-degree map
        in_degree: Dict[str, int] = {tid: 0 for tid in task_map}
        for task in tasks:
            for dep in task.dependencies:
                if dep in in_degree:
                    in_degree[task.task_id] = in_degree.get(task.task_id, 0) + 1

        # Recompute properly
        in_degree = defaultdict(int)
        for tid in task_map:
            in_degree[tid]  # ensure every id present
        for task in tasks:
            for dep in task.dependencies:
                if dep in task_map:
                    in_degree[task.task_id] += 1

        levels: List[List[str]] = []
        queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
        processed = 0

        while queue:
            level: List[str] = []
            # Drain all nodes that are currently at in-degree 0 (same "wave")
            next_queue: deque[str] = deque()
            while queue:
                tid = queue.popleft()
                level.append(tid)
                processed += 1
                task = task_map[tid]
                for blocked_id in task.blocking:
                    if blocked_id not in in_degree:
                        continue
                    in_degree[blocked_id] -= 1
                    if in_degree[blocked_id] == 0:
                        next_queue.append(blocked_id)
            levels.append(level)
            queue = next_queue

        if processed != len(tasks):
            raise OrchestrationError(
                "Cycle detected in task graph: topological sort failed.",
                details={"unprocessed_count": len(tasks) - processed},
            )

        return levels

    def _find_critical_path(self, tasks: List[TaskNode]) -> List[str]:
        """Find the critical path (longest dependency chain by estimated minutes).

        Uses a forward-pass / longest-path algorithm on the DAG.

        Returns:
            Ordered list of task_ids forming the critical path.
        """
        task_map: Dict[str, TaskNode] = {t.task_id: t for t in tasks}

        # Duration for each task in minutes
        durations: Dict[str, int] = {t.task_id: _estimate_task_minutes(t) for t in tasks}

        # earliest_finish[tid] = earliest time this task can complete
        earliest_finish: Dict[str, float] = {}
        # predecessor on critical path
        predecessor: Dict[str, Optional[str]] = {}

        # Process in topological order
        try:
            topo_levels = self._topological_sort(tasks)
        except OrchestrationError:
            # If there's a cycle we cannot find a critical path
            return []

        for level in topo_levels:
            for tid in level:
                task = task_map[tid]
                if not task.dependencies:
                    earliest_finish[tid] = durations[tid]
                    predecessor[tid] = None
                else:
                    # latest finish of all deps
                    max_dep_finish: float = 0.0
                    best_dep: Optional[str] = None
                    for dep_id in task.dependencies:
                        dep_finish = earliest_finish.get(dep_id, 0.0)
                        if dep_finish > max_dep_finish:
                            max_dep_finish = dep_finish
                            best_dep = dep_id
                    earliest_finish[tid] = max_dep_finish + durations[tid]
                    predecessor[tid] = best_dep

        if not earliest_finish:
            return []

        # Critical path ends at the node with the maximum earliest_finish
        end_tid = max(earliest_finish, key=lambda tid: earliest_finish[tid])

        # Trace back through predecessors
        path: List[str] = []
        current: Optional[str] = end_tid
        while current is not None:
            path.append(current)
            current = predecessor.get(current)

        path.reverse()
        return path

    def _estimate_duration(self, tasks: List[TaskNode]) -> int:
        """Estimate total wall-clock minutes assuming parallelism within each level."""
        try:
            levels = self._topological_sort(tasks)
        except OrchestrationError:
            return sum(_estimate_task_minutes(t) for t in tasks)

        task_map: Dict[str, TaskNode] = {t.task_id: t for t in tasks}
        total = 0
        for level in levels:
            level_max = max(
                (_estimate_task_minutes(task_map[tid]) for tid in level if tid in task_map),
                default=0,
            )
            total += level_max
        return total

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def validate_graph(self, graph: TaskGraph) -> None:
        """Validate the graph for structural correctness.

        Checks:
        1. All dependency task_ids reference existing tasks.
        2. No cycles (runs topological sort).
        3. total_tasks is consistent with len(tasks).

        Raises:
            ValidationError: on any structural problem.
            OrchestrationError: on cycle detection.
        """
        task_ids: Set[str] = {t.task_id for t in graph.tasks}

        for task in graph.tasks:
            for dep_id in task.dependencies:
                if dep_id not in task_ids:
                    raise ValidationError(
                        f"Task '{task.task_id}' has dependency '{dep_id}' which does not exist.",
                        details={"task_id": task.task_id, "missing_dep": dep_id},
                    )
            for blocking_id in task.blocking:
                if blocking_id not in task_ids:
                    raise ValidationError(
                        f"Task '{task.task_id}' blocks '{blocking_id}' which does not exist.",
                        details={"task_id": task.task_id, "missing_blocking": blocking_id},
                    )

        # Cycle check via topological sort
        self._topological_sort(graph.tasks)

        logger.debug("task_graph_validated", graph_id=graph.graph_id, task_count=len(graph.tasks))

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def serialize(self, graph: TaskGraph) -> str:
        """Serialize a TaskGraph to a JSON string."""
        return graph.model_dump_json()

    def deserialize(self, data: str) -> TaskGraph:
        """Deserialize a TaskGraph from a JSON string.

        Raises:
            ValidationError: if the JSON is malformed or fails schema validation.
        """
        try:
            return TaskGraph.model_validate_json(data)
        except Exception as exc:
            raise ValidationError(
                f"Failed to deserialize TaskGraph: {exc}",
                details={"raw_data_length": len(data)},
            ) from exc

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    async def persist_graph(self, graph: TaskGraph) -> None:
        """Persist the task graph to PostgreSQL as a JSON blob.

        Uses a raw upsert so we don't depend on an Alembic migration
        being in place for the orchestration tables.
        """
        serialized = self.serialize(graph)
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO forge_task_graphs (graph_id, project_id, phase, data, created_at, updated_at)
                    VALUES (:graph_id, :project_id, :phase, :data::jsonb, :created_at, :updated_at)
                    ON CONFLICT (graph_id) DO UPDATE
                        SET data       = EXCLUDED.data,
                            updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "graph_id": graph.graph_id,
                    "project_id": graph.project_id,
                    "phase": graph.phase,
                    "data": serialized,
                    "created_at": graph.created_at,
                    "updated_at": datetime.utcnow(),
                },
            )
            await session.commit()
        logger.info("task_graph_persisted", graph_id=graph.graph_id)

    async def load_graph(self, graph_id: str) -> Optional[TaskGraph]:
        """Load a task graph from PostgreSQL by graph_id.

        Returns:
            The deserialized TaskGraph, or None if not found.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT data FROM forge_task_graphs WHERE graph_id = :graph_id"),
                {"graph_id": graph_id},
            )
            row = result.fetchone()
            if row is None:
                logger.warning("task_graph_not_found", graph_id=graph_id)
                return None
            data = row[0]
            if isinstance(data, dict):
                return TaskGraph.model_validate(data)
            return self.deserialize(data)

    async def update_task_status(
        self,
        graph_id: str,
        task_id: str,
        update: TaskGraphUpdate,
    ) -> None:
        """Atomically update one task's status inside a persisted graph.

        Loads the graph, mutates the relevant TaskNode, and re-persists.
        Raises OrchestrationError if graph or task is not found.
        """
        graph = await self.load_graph(graph_id)
        if graph is None:
            raise OrchestrationError(
                f"Task graph '{graph_id}' not found.",
                details={"graph_id": graph_id},
            )

        task = graph.task_by_id(task_id)
        if task is None:
            raise OrchestrationError(
                f"Task '{task_id}' not found in graph '{graph_id}'.",
                details={"graph_id": graph_id, "task_id": task_id},
            )

        # Apply update
        task.status = update.status
        if update.error_message is not None:
            task.error_message = update.error_message
        if update.output_artifacts is not None:
            task.output_artifacts = update.output_artifacts

        now = datetime.utcnow()
        if update.status == TaskStatus.RUNNING and task.started_at is None:
            task.started_at = now
        if update.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            task.completed_at = now

        # Recompute aggregates
        graph.completed_tasks = sum(1 for t in graph.tasks if t.status == TaskStatus.COMPLETED)
        graph.failed_tasks = sum(1 for t in graph.tasks if t.status == TaskStatus.FAILED)

        await self.persist_graph(graph)

        logger.info(
            "task_status_updated",
            graph_id=graph_id,
            task_id=task_id,
            new_status=update.status,
        )

    # ------------------------------------------------------------------ #
    # Ready-task query
    # ------------------------------------------------------------------ #

    def get_ready_tasks(
        self,
        graph: TaskGraph,
        completed_task_ids: Optional[Set[str]] = None,
    ) -> List[TaskNode]:
        """Return tasks that are PENDING and have all dependencies satisfied.

        Args:
            graph:              The current task graph.
            completed_task_ids: Override set of completed ids (defaults to
                                all tasks in COMPLETED status within the graph).

        Returns:
            List of TaskNodes ready to execute.
        """
        if completed_task_ids is None:
            completed_task_ids = {
                t.task_id for t in graph.tasks if t.status == TaskStatus.COMPLETED
            }

        return [
            t
            for t in graph.tasks
            if t.status == TaskStatus.PENDING and t.can_start(completed_task_ids)
        ]
