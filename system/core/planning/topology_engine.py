"""Topology Engine — Phase 4 of the FORGE planning pipeline.

Generates the repository topology (monorepo vs polyrepo) and the full set of
ServiceDefinitions that define the system's deployable units.

Usage::

    engine = TopologyEngine(llm_client=get_llm_client())
    topology = await engine.generate(spec, stack)
    dir_tree = engine.generate_directory_structure(topology)
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from system.core.planning.schemas import (
    RepoTopology,
    ServiceDefinition,
)
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.llm_client import LLMMessage

logger = get_logger(__name__)


class TopologyEngine:
    """Generates repo topology and service definitions from a ProjectSpec + stack.

    Deterministic service creation:
    - Always creates: api_service, worker_service, frontend_service.
    - Adds additional services based on spec feature flags (real-time,
      auth, analytics, etc.).
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self, spec: ProjectSpec, stack: Dict[str, str]
    ) -> RepoTopology:
        """Generate and return the complete RepoTopology for the project."""
        services = self._create_service_definitions(spec, stack)
        service_count = len(services)
        repo_type = self._determine_type(service_count)

        topology = RepoTopology(
            topology_id=str(uuid.uuid4()),
            project_id=spec.project_id,
            repo_type=repo_type,
            services=services,
            monorepo_root="." if repo_type == "monorepo" else None,
        )

        logger.info(
            "Topology generated",
            project_id=spec.project_id,
            repo_type=repo_type,
            service_count=service_count,
        )
        return topology

    # ------------------------------------------------------------------
    # Repo type determination
    # ------------------------------------------------------------------

    @staticmethod
    def _determine_type(service_count: int) -> str:
        """Return repo organisation strategy based on service count.

        Rules:
        - < 5 services  → monorepo  (lower overhead, easier initial dev)
        - 5 – 10        → monorepo  (still manageable)
        - > 10          → polyrepo  (CI isolation, independent deployments)
        """
        if service_count > 10:
            return "polyrepo"
        return "monorepo"

    # ------------------------------------------------------------------
    # Service definitions
    # ------------------------------------------------------------------

    def _create_service_definitions(
        self, spec: ProjectSpec, stack: Dict[str, str]
    ) -> List[ServiceDefinition]:
        """Build the full list of ServiceDefinitions for this project.

        Always creates: api_service, worker_service, frontend_service.
        Adds extra services based on spec.intent flags.
        """
        services: List[ServiceDefinition] = []

        backend_tech = stack.get("backend", "FastAPI")
        frontend_tech = stack.get("frontend", "Next.js")
        db_tech = stack.get("database", "PostgreSQL")
        cache_tech = stack.get("cache", "Redis")
        queue_tech = stack.get("queue", "Celery + Redis")
        auth_tech = stack.get("auth", "JWT")

        # ---- 1. API Service (always) ------------------------------------
        api_env: Dict[str, str] = {
            "DATABASE_URL": f"postgresql+asyncpg://user:pass@postgres:5432/{spec.project_id}",
            "REDIS_URL": "redis://redis:6379/0",
            "SECRET_KEY": "REPLACE_WITH_SECURE_KEY",
            "LOG_LEVEL": "INFO",
        }
        if "oauth" in auth_tech.lower() or "jwt" in auth_tech.lower():
            api_env["AUTH_SECRET"] = "REPLACE_WITH_AUTH_SECRET"

        services.append(
            ServiceDefinition(
                name="api_service",
                service_type="backend",
                technology=backend_tech,
                language="Python",
                port=8000,
                dependencies=["database_service", "cache_service"],
                environment_variables=api_env,
                scaling={"min_replicas": 2, "max_replicas": 10, "cpu_threshold": 70},
                health_check_path="/health",
                description="Core REST API — handles all business logic and data persistence.",
            )
        )

        # ---- 2. Worker Service (always) ----------------------------------
        services.append(
            ServiceDefinition(
                name="worker_service",
                service_type="worker",
                technology=queue_tech,
                language="Python",
                port=0,  # no HTTP port — queue consumer
                dependencies=["api_service", "cache_service", "queue_service"],
                environment_variables={
                    "CELERY_BROKER_URL": "redis://redis:6379/1",
                    "CELERY_RESULT_BACKEND": "redis://redis:6379/2",
                    "DATABASE_URL": f"postgresql+asyncpg://user:pass@postgres:5432/{spec.project_id}",
                },
                scaling={"min_replicas": 1, "max_replicas": 5, "cpu_threshold": 80},
                health_check_path="",
                description="Async task worker — handles background jobs and event processing.",
            )
        )

        # ---- 3. Frontend Service (always) --------------------------------
        frontend_env: Dict[str, str] = {
            "NEXT_PUBLIC_API_URL": "http://api_service:8000",
        }
        if "none" not in frontend_tech.lower():
            services.append(
                ServiceDefinition(
                    name="frontend_service",
                    service_type="frontend",
                    technology=frontend_tech,
                    language="TypeScript",
                    port=3000,
                    dependencies=["api_service"],
                    environment_variables=frontend_env,
                    scaling={"min_replicas": 1, "max_replicas": 4, "cpu_threshold": 75},
                    health_check_path="/",
                    description="Frontend application — user-facing web interface.",
                )
            )

        # ---- 4. Database Service (always) --------------------------------
        services.append(
            ServiceDefinition(
                name="database_service",
                service_type="database",
                technology=db_tech.split("(")[0].strip(),  # strip "(+ read replicas)" suffix
                language="",
                port=5432,
                dependencies=[],
                environment_variables={
                    "POSTGRES_USER": "forge",
                    "POSTGRES_PASSWORD": "REPLACE_WITH_DB_PASSWORD",
                    "POSTGRES_DB": spec.project_id,
                },
                scaling={"min_replicas": 1, "max_replicas": 1},
                health_check_path="",
                description="Primary relational database.",
            )
        )

        # ---- 5. Cache Service (always) -----------------------------------
        services.append(
            ServiceDefinition(
                name="cache_service",
                service_type="cache",
                technology=cache_tech.split("(")[0].strip(),
                language="",
                port=6379,
                dependencies=[],
                environment_variables={},
                scaling={"min_replicas": 1, "max_replicas": 3},
                health_check_path="",
                description="In-memory cache and message broker.",
            )
        )

        # ---- 6. Queue Service (if separate from cache) -------------------
        if "rabbitmq" in queue_tech.lower() or "kafka" in queue_tech.lower():
            services.append(
                ServiceDefinition(
                    name="queue_service",
                    service_type="worker",
                    technology=queue_tech.split("+")[0].strip(),
                    language="",
                    port=5672 if "rabbitmq" in queue_tech.lower() else 9092,
                    dependencies=[],
                    environment_variables={},
                    scaling={"min_replicas": 1, "max_replicas": 3},
                    health_check_path="",
                    description="Dedicated message queue broker.",
                )
            )

        # ---- 7. Real-time / WebSocket Gateway ---------------------------
        features_text = " ".join(spec.intent.core_features).lower()
        intent_text = spec.intent.raw_prompt.lower()

        has_realtime = any(
            kw in features_text or kw in intent_text
            for kw in ["real-time", "realtime", "websocket", "chat", "live", "notification"]
        )
        if has_realtime:
            services.append(
                ServiceDefinition(
                    name="realtime_gateway",
                    service_type="gateway",
                    technology="FastAPI + WebSocket",
                    language="Python",
                    port=8001,
                    dependencies=["api_service", "cache_service"],
                    environment_variables={
                        "REDIS_URL": "redis://redis:6379/0",
                        "API_URL": "http://api_service:8000",
                    },
                    scaling={"min_replicas": 2, "max_replicas": 8, "cpu_threshold": 60},
                    health_check_path="/health",
                    description="WebSocket gateway for real-time event delivery.",
                )
            )

        # ---- 8. Auth Service (if OAuth / SSO required) ------------------
        has_oauth = any(
            kw in " ".join(spec.intent.security_requirements).lower() or kw in features_text
            for kw in ["oauth", "sso", "saml", "openid"]
        )
        if has_oauth:
            services.append(
                ServiceDefinition(
                    name="auth_service",
                    service_type="backend",
                    technology="FastAPI + OAuth2",
                    language="Python",
                    port=8002,
                    dependencies=["database_service", "cache_service"],
                    environment_variables={
                        "OAUTH_CLIENT_ID": "REPLACE_WITH_OAUTH_CLIENT_ID",
                        "OAUTH_CLIENT_SECRET": "REPLACE_WITH_OAUTH_CLIENT_SECRET",
                        "JWT_SECRET": "REPLACE_WITH_JWT_SECRET",
                    },
                    scaling={"min_replicas": 2, "max_replicas": 4},
                    health_check_path="/health",
                    description="OAuth2 / OpenID Connect authentication service.",
                )
            )

        # ---- 9. API Gateway (if microservices or multiple backends) ------
        if len([s for s in services if s.service_type == "backend"]) >= 3:
            services.append(
                ServiceDefinition(
                    name="api_gateway",
                    service_type="gateway",
                    technology="NGINX / Traefik",
                    language="",
                    port=80,
                    dependencies=[
                        s.name for s in services if s.service_type in {"backend", "frontend"}
                    ],
                    environment_variables={},
                    scaling={"min_replicas": 2, "max_replicas": 4},
                    health_check_path="/healthz",
                    description="Reverse proxy / API gateway — routes requests to upstream services.",
                )
            )

        return services

    # ------------------------------------------------------------------
    # Directory structure generation
    # ------------------------------------------------------------------

    def generate_directory_structure(self, topology: "RepoTopology") -> Dict[str, Any]:
        """Return a nested dict representing the project file tree.

        Each value is either a nested dict (directory) or ``None`` (file).
        """
        if topology.repo_type == "monorepo":
            return self._monorepo_structure(topology)
        return self._polyrepo_structure(topology)

    # ------------------------------------------------------------------

    def _monorepo_structure(self, topology: "RepoTopology") -> Dict[str, Any]:
        """Monorepo layout — all services under one root."""
        tree: Dict[str, Any] = {
            ".github": {
                "workflows": {
                    "ci.yml": None,
                    "cd.yml": None,
                    "security.yml": None,
                }
            },
            "apps": {},
            "packages": {
                "shared": {
                    "__init__.py": None,
                    "models.py": None,
                    "exceptions.py": None,
                    "utils.py": None,
                },
                "config": {
                    "settings.py": None,
                    "__init__.py": None,
                },
            },
            "infra": {
                "docker": {
                    "docker-compose.yml": None,
                    "docker-compose.prod.yml": None,
                },
                "kubernetes": {
                    "namespace.yaml": None,
                    "ingress.yaml": None,
                },
                "terraform": {
                    "main.tf": None,
                    "variables.tf": None,
                    "outputs.tf": None,
                },
            },
            "docs": {
                "architecture.md": None,
                "api.md": None,
                "adr": {},
            },
            "scripts": {
                "setup.sh": None,
                "migrate.sh": None,
                "deploy.sh": None,
            },
            "Makefile": None,
            "README.md": None,
            ".env.example": None,
            "pyproject.toml": None,
        }

        for svc in topology.services:
            svc_tree = self._service_tree(svc)
            tree["apps"][svc.name] = svc_tree

        return tree

    def _polyrepo_structure(self, topology: "RepoTopology") -> Dict[str, Any]:
        """Polyrepo layout — one top-level entry per service repo."""
        tree: Dict[str, Any] = {}
        for svc in topology.services:
            tree[svc.name] = {
                **self._service_tree(svc),
                ".github": {
                    "workflows": {
                        "ci.yml": None,
                        "cd.yml": None,
                    }
                },
                "README.md": None,
            }
        return tree

    def _service_tree(self, svc: ServiceDefinition) -> Dict[str, Any]:
        """Return the internal directory tree for a single service."""
        if svc.service_type == "frontend":
            return {
                "src": {
                    "app": {
                        "page.tsx": None,
                        "layout.tsx": None,
                    },
                    "components": {},
                    "hooks": {},
                    "lib": {
                        "api.ts": None,
                        "auth.ts": None,
                    },
                    "styles": {
                        "globals.css": None,
                    },
                    "types": {
                        "index.ts": None,
                    },
                },
                "public": {},
                "package.json": None,
                "tsconfig.json": None,
                "next.config.js": None,
                ".env.local.example": None,
                "Dockerfile": None,
            }
        elif svc.service_type in {"backend", "worker"}:
            return {
                "app": {
                    "__init__.py": None,
                    "main.py": None,
                    "api": {
                        "__init__.py": None,
                        "routes.py": None,
                        "dependencies.py": None,
                    },
                    "models": {
                        "__init__.py": None,
                    },
                    "services": {
                        "__init__.py": None,
                    },
                    "repositories": {
                        "__init__.py": None,
                    },
                    "schemas": {
                        "__init__.py": None,
                    },
                    "core": {
                        "config.py": None,
                        "security.py": None,
                        "__init__.py": None,
                    },
                },
                "tests": {
                    "__init__.py": None,
                    "conftest.py": None,
                    "unit": {"__init__.py": None},
                    "integration": {"__init__.py": None},
                },
                "alembic": {
                    "env.py": None,
                    "versions": {},
                },
                "Dockerfile": None,
                "pyproject.toml": None,
                "requirements.txt": None,
                ".env.example": None,
            }
        elif svc.service_type == "database":
            return {
                "migrations": {},
                "init": {
                    "01_schema.sql": None,
                    "02_seed.sql": None,
                },
                "Dockerfile": None,
            }
        elif svc.service_type in {"cache", "gateway"}:
            return {
                "config": {
                    f"{svc.technology.lower().split('/')[0].strip()}.conf": None,
                },
                "Dockerfile": None,
            }
        else:
            return {"Dockerfile": None, "README.md": None}
