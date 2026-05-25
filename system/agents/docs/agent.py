"""Docs Agent — professional technical documentation generation.

Produces complete, developer-facing technical documentation: project README,
API reference in Markdown, C4 architecture diagrams, operator runbooks,
and contributing guides.  Reads Python source files for context but never
modifies them — all output is in the ``docs/`` directory.
"""

from __future__ import annotations

from typing import Any

from system.agents.base import AgentContext, AgentContract, AgentResult, BaseAgent
from system.agents.prompts import (
    DOCS_SYSTEM_PROMPT_TEMPLATE,
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class DocsAgent(BaseAgent):
    """Specialist agent for professional technical documentation generation.

    Produces complete, developer-facing documentation:

    - ``README.md`` with badges, quickstart, architecture overview (Mermaid),
      environment variable reference, and contributing section.
    - API reference Markdown supplement with request/response schemas, curl
      examples, and error code tables.
    - Architecture documentation with C4 Context and Container diagrams.
    - Operator runbooks with step-by-step commands, expected outputs, and
      rollback instructions.
    - Contributing guide with development environment setup and PR process.

    The agent reads Python source files for docstring and type annotation
    context but NEVER modifies any source file — all output is written
    exclusively to the ``docs/`` directory or ``README.md``.

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the DocsAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.DOCS, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: ProjectSpec | None,
        arch: ArchitecturePlan | None,
    ) -> AgentContract:
        """Build a scoped AgentContract for a documentation task.

        The Docs agent reads source files from ``system/`` for context
        (docstrings, type hints, schemas) but writes only to ``docs/``
        and ``README.md``.

        Parameters
        ----------
        task:
            The TaskNode carrying the documentation objective.
        spec:
            Project specification (API contract, tech stack).
        arch:
            Architecture plan (service topology for architecture docs).

        Returns
        -------
        AgentContract
            Contract scoped to documentation output files and source
            read-only context.
        """
        return AgentContract(
            identity="docs_agent",
            objective=task.description,
            allowed_files=[
                "docs/**/*.md",
                "docs/architecture/**/*.md",
                "docs/api/**/*.md",
                "docs/runbooks/**/*.md",
                "docs/adr/**/*.md",
                "docs/security/**/*.md",
                "README.md",
                "CONTRIBUTING.md",
                "CHANGELOG.md",
                # Read-only source files for context
                "system/**/*.py",
            ],
            constraints=[
                "NEVER modify any Python source file (.py) — the Docs agent is READ-ONLY for source code.",
                "ALWAYS generate documentation in Markdown format with proper heading hierarchy.",
                "ALWAYS include working code examples (curl, Python, TypeScript) in API documentation.",
                "ALWAYS document every public function, class, and module found in the source files.",
                "NEVER write documentation that contradicts the actual code behaviour observed in source files.",
                "ALWAYS use active voice: 'Run this command' not 'This command should be run'.",
                "ALWAYS include a Mermaid diagram in architecture documentation sections.",
                "NEVER include placeholder values like 'YOUR_VALUE_HERE' — use realistic example values.",
                "ALWAYS include a rollback procedure for every deployment runbook step.",
                "ALWAYS document all environment variables with their type, description, default, and whether they are required.",
            ],
            validation_rules=[
                "No Python source files (.py) appear in generated_files or modifications output.",
                "README.md contains: project description, quickstart commands, environment variable table, and architecture overview.",
                "API documentation includes request schema, response schema, example curl, and error codes for every endpoint.",
                "All Mermaid diagrams use valid Mermaid syntax (graph TD, sequenceDiagram, or C4Context).",
                "Runbooks contain step-by-step commands with expected output and rollback instructions.",
                "No placeholder values (YOUR_*, <INSERT_*, REPLACE_*) appear in any output.",
            ],
            success_criteria=[
                "README.md written with badges, quickstart, env var reference, architecture diagram, and links to full docs.",
                "API reference documentation written for all endpoints with examples and error codes.",
                "Architecture documentation written with C4 Context and Container diagrams in Mermaid.",
                "At least one runbook written (deployment, debugging, or common operation).",
                "All public Python functions and classes documented based on source file inspection.",
                "Contributing guide written with development setup, test commands, and PR workflow.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.2,  # Slightly higher for natural prose
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Docs Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the docs-specific writing
        standards from the template, the current task contract details, and
        canonical examples for README structure, API documentation blocks,
        and Mermaid diagram syntax.

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

{DOCS_SYSTEM_PROMPT_TEMPLATE}

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
CANONICAL DOCUMENTATION PATTERNS — FOLLOW THESE EXACTLY
═══════════════════════════════════════════════════════════════════════════════

#### 1. README.md structure (correct pattern)

```markdown
# Project Name

> One-sentence description of what this project does.

![Build](https://github.com/org/repo/actions/workflows/ci.yml/badge.svg)
![Coverage](https://codecov.io/gh/org/repo/branch/main/graph/badge.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Prerequisites
- Python 3.12+
- Docker 24+
- PostgreSQL 16+

## Quick Start
```bash
git clone https://github.com/org/repo.git && cd repo
cp .env.example .env
docker compose up -d postgres redis
pip install -e ".[dev]"
alembic upgrade head
uvicorn system.main:app --reload
```

Open http://localhost:8000/docs for the interactive API docs.

## Architecture

```mermaid
graph TD
    Client[Browser / Mobile] --> GW[API Gateway :8000]
    GW --> API[FastAPI Service]
    API --> DB[(PostgreSQL)]
    API --> Cache[(Redis)]
    API --> Queue[Celery Worker]
```

## Environment Variables

| Variable | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| DATABASE_URL | string | yes | — | PostgreSQL connection URL |
| SECRET_KEY | string | yes | — | JWT signing secret (≥256 bits) |
| REDIS_URL | string | yes | — | Redis connection URL |
| DEBUG | bool | no | false | Enable debug mode (never true in prod) |
```

#### 2. API endpoint documentation block (correct pattern)

```markdown
### POST /api/v1/users

Create a new user account.

**Authentication:** Not required (public endpoint)
**Rate limit:** 10 requests per minute per IP

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| email | string | yes | Valid email format | User's email address |
| password | string | yes | Min 8 characters | Plain-text password (hashed server-side) |

#### Example Request

```bash
curl -X POST http://localhost:8000/api/v1/users \\
  -H "Content-Type: application/json" \\
  -d '{{"email": "alice@example.com", "password": "securepassword"}}'
```

#### Success Response (201 Created)

```json
{{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "alice@example.com",
  "is_active": true,
  "created_at": "2025-01-15T10:30:00Z"
}}
```

#### Error Responses

| Status | Condition | Detail |
|--------|-----------|--------|
| 409 | Email already registered | `"A user with email '...' already exists."` |
| 422 | Invalid request body | Pydantic validation error details |
| 429 | Rate limit exceeded | `"Too many requests."` |
```

#### 3. C4 Architecture diagram (Mermaid)

```mermaid
C4Context
  title System Context for FORGE

  Person(dev, "Developer", "Provides a project description in natural language.")
  System(forge, "FORGE", "Autonomous software production system.")
  SystemDb(db, "PostgreSQL", "Persistent project state and generated artefacts.")
  SystemQueue(redis, "Redis", "Task queue and event bus.")
  SystemExt(llm, "Anthropic API", "LLM for code generation (Claude Sonnet).")

  Rel(dev, forge, "Submits project intent", "HTTPS")
  Rel(forge, db, "Reads/writes project state", "TCP/5432")
  Rel(forge, redis, "Publishes tasks and events", "TCP/6379")
  Rel(forge, llm, "Sends agent prompts", "HTTPS")
```

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the docs agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated Markdown documentation files,
            reasoning, and any errors.
        """
        logger.info(
            "docs_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
