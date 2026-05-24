"""Infra Agent — Docker / Kubernetes / Terraform / GitHub Actions IaC generation.

Writes complete, production-grade infrastructure-as-code: multi-stage
Dockerfiles, Kubernetes manifests with security contexts, Terraform HCL
modules, and GitHub Actions CI/CD pipeline definitions.  All output follows
CIS Benchmarks for Docker and Kubernetes and enforces least-privilege security
defaults.
"""

from __future__ import annotations

from typing import Any, Optional

from system.agents.base import AgentContract, AgentContext, AgentResult, BaseAgent
from system.agents.prompts import (
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    INFRA_SYSTEM_PROMPT_TEMPLATE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class InfraAgent(BaseAgent):
    """Specialist agent for infrastructure-as-code generation.

    Produces complete, production-grade IaC for Docker, Kubernetes, Terraform,
    and GitHub Actions:

    - Multi-stage Dockerfiles with non-root users and pinned base images.
    - Kubernetes Deployments, Services, Ingresses, HPAs, PDBs, and NetworkPolicies
      with full SecurityContexts (runAsNonRoot, readOnlyRootFilesystem, capability drops).
    - Terraform HCL modules with remote state, tagged resources, and no hardcoded values.
    - GitHub Actions workflows with SHA-pinned actions, separate lint/test/build/deploy
      jobs, and production approval gates.

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the InfraAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.INFRA, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: Optional[ProjectSpec],
        arch: Optional[ArchitecturePlan],
    ) -> AgentContract:
        """Build a scoped AgentContract for an infrastructure task.

        Parameters
        ----------
        task:
            The TaskNode carrying the infrastructure implementation objective.
        spec:
            Project specification (tech stack, deployment target).
        arch:
            Architecture plan (service topology, scaling profile).

        Returns
        -------
        AgentContract
            Contract scoped to infrastructure and CI/CD files.
        """
        return AgentContract(
            identity="infra_agent",
            objective=task.description,
            allowed_files=[
                "infra/**",
                "docker-compose.yml",
                "docker-compose.*.yml",
                ".github/workflows/**",
                "Makefile",
                "Dockerfile",
                "Dockerfile.*",
                ".dockerignore",
                "helm/**",
            ],
            constraints=[
                "NEVER use 'latest' image tags in any Dockerfile or Kubernetes manifest — always pin to a specific digest or semver tag.",
                "ALWAYS run containers as non-root users — define a non-root USER in Dockerfiles and set runAsNonRoot: true in SecurityContext.",
                "ALWAYS set resource requests AND limits (cpu and memory) for every Kubernetes container.",
                "NEVER bake secrets, credentials, or environment variable values into Dockerfiles or docker-compose files.",
                "ALWAYS include liveness and readiness probes for every Kubernetes Deployment container.",
                "ALWAYS use multi-stage Docker builds — a builder stage and a minimal final runtime stage.",
                "NEVER use 'privileged: true' or 'allowPrivilegeEscalation: true' in any SecurityContext.",
                "ALWAYS drop ALL Linux capabilities in SecurityContext and add back only what is strictly required.",
                "ALWAYS pin GitHub Actions to a full commit SHA — never use a mutable tag like @v3.",
                "NEVER store secrets in ConfigMaps — use Kubernetes Secrets or an external secrets operator.",
            ],
            validation_rules=[
                "All Kubernetes Deployments have resource requests and limits defined.",
                "All Kubernetes Deployments have liveness and readiness probes defined.",
                "All containers run as non-root (runAsNonRoot: true or explicit numeric UID).",
                "No 'latest' image tag appears in any manifest or Dockerfile.",
                "All GitHub Actions steps reference actions at a full commit SHA.",
                "Terraform resources have Name, Environment, and ManagedBy tags.",
                "No secrets or credentials appear in any output file.",
            ],
            success_criteria=[
                "Dockerfile(s) written with multi-stage builds and non-root runtime user.",
                "Kubernetes manifests created: Deployment, Service, HPA, PDB, NetworkPolicy, ServiceAccount.",
                "Terraform modules written with remote state, variables, and outputs documented.",
                "GitHub Actions workflow created with separate lint, test, build, and deploy jobs.",
                "docker-compose.yml updated with all required services, health checks, and volumes.",
                "Makefile targets added for common developer workflows (build, test, up, down, deploy).",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.1,
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Infra Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the infra-specific IaC
        standards from the template, the current task contract details,
        and canonical code examples for Dockerfiles, Kubernetes manifests,
        and GitHub Actions.

        Parameters
        ----------
        contract:
            The AgentContract produced by ``build_contract()``.

        Returns
        -------
        str
            Complete system prompt string ready for the LLM.
        """
        constraints_text = "\n".join(f"  - {c}" for c in contract.constraints)
        validation_text = "\n".join(f"  - {v}" for v in contract.validation_rules)
        success_text = "\n".join(f"  - {s}" for s in contract.success_criteria)

        return f"""{FORGE_AGENT_PREAMBLE}

{INFRA_SYSTEM_PROMPT_TEMPLATE}

═══════════════════════════════════════════════════════════════════════════════
CURRENT TASK CONTRACT
═══════════════════════════════════════════════════════════════════════════════

### Objective
{contract.objective}

### Hard Constraints (NEVER violate these)
{constraints_text}

### Validation Rules (your output MUST satisfy ALL of these)
{validation_text}

### Success Criteria (define "done" for this task)
{success_text}

═══════════════════════════════════════════════════════════════════════════════
CANONICAL PATTERNS — FOLLOW THESE EXACTLY
═══════════════════════════════════════════════════════════════════════════════

#### 1. Multi-stage Dockerfile (correct pattern)

```dockerfile
# syntax=docker/dockerfile:1.6
# ── Stage 1: dependency installation ───────────────────────────────────────
FROM python:3.12.4-slim-bookworm AS builder

WORKDIR /build
RUN pip install --upgrade pip==24.1.2
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[prod]"

# ── Stage 2: minimal runtime image ─────────────────────────────────────────
FROM python:3.12.4-slim-bookworm AS runtime

# Non-root user for security
RUN groupadd --gid 1001 appgroup && \\
    useradd  --uid 1001 --gid appgroup --shell /bin/bash appuser

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --chown=appuser:appgroup . .

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "system.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### 2. Kubernetes Deployment (correct pattern)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-server
  namespace: forge
  labels:
    app: api-server
    version: "1.0.0"
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api-server
  template:
    metadata:
      labels:
        app: api-server
    spec:
      serviceAccountName: api-server-sa
      securityContext:
        runAsNonRoot: true
        runAsUser: 1001
        fsGroup: 1001
      containers:
        - name: api-server
          image: ghcr.io/org/api-server:1.0.0  # pinned — never 'latest'
          ports:
            - containerPort: 8000
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 30
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          envFrom:
            - secretRef:
                name: api-server-secrets
```

#### 3. GitHub Actions workflow (correct pattern — SHA-pinned actions)

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.12"
      - run: pip install ruff mypy
      - run: ruff check . && mypy system/

  test:
    needs: lint
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.12"
      - run: pip install -e ".[test]"
      - run: pytest --cov=system --cov-report=xml -q
```

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the infra agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated IaC files, reasoning, and
            any errors.
        """
        logger.info(
            "infra_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
