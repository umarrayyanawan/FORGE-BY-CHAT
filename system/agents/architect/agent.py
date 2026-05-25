"""Architect Agent — CTO-level technical decisions for the FORGE system.

Produces Architecture Decision Records, service topologies, infrastructure
scaffolding, and planning schemas.  This agent is intentionally constrained
to a single concurrent instance so that architectural decisions are serialised
and internally consistent.
"""

from __future__ import annotations

from typing import Any

from system.agents.base import AgentContext, AgentContract, AgentResult, BaseAgent
from system.agents.prompts import (
    ARCHITECT_SYSTEM_PROMPT_TEMPLATE,
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


class ArchitectAgent(BaseAgent):
    """Principal architect agent that makes high-level technical decisions.

    This agent operates at CTO level: it designs service topologies, defines
    database schemas, establishes API contracts, selects infrastructure
    patterns, and documents architectural decisions as ADRs.

    It intentionally does NOT write application code — that is the
    responsibility of the Backend, Frontend, and Infra agents.

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the ArchitectAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.ARCHITECT, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: ProjectSpec | None,
        arch: ArchitecturePlan | None,
    ) -> AgentContract:
        """Build a scoped AgentContract for an architecture task.

        Parameters
        ----------
        task:
            The TaskNode carrying the architecture objective.
        spec:
            Project specification (used for tech stack context).
        arch:
            Existing architecture plan (used as context; may be None for
            the initial architecture design task).

        Returns
        -------
        AgentContract
            Contract scoped to architecture and planning artefacts.
        """
        return AgentContract(
            identity="architect_agent",
            objective=task.description,
            allowed_files=[
                "system/core/planning/**/*.py",
                "system/core/specification/**/*.py",
                "docs/architecture/**/*.md",
                "docs/adr/**/*.md",
                "infra/**/*.tf",
                "infra/**/*.yaml",
                "infra/**/*.yml",
                "infra/helm/**/*.yaml",
            ],
            constraints=[
                "NEVER modify existing database migrations — Alembic owns schema history.",
                "NEVER change API contracts without creating a new versioned path (/api/v2/...).",
                "NEVER remove existing public endpoints — deprecate then remove in a future ADR.",
                "ALWAYS validate scalability of proposed architecture (state horizontal scaling plan).",
                "ALWAYS use connection pooling for all database connections (SQLAlchemy pool).",
                "ALWAYS document trade-offs for every technology selection.",
                "ALWAYS specify resource limits (CPU, memory) for every service you define.",
                "NEVER recommend disabling TLS, authentication, or rate limiting.",
            ],
            validation_rules=[
                "No circular service dependencies in the proposed topology.",
                "All services must have a health-check endpoint (/health or /readyz) defined.",
                "Resource limits (cpu, memory requests AND limits) defined for every K8s workload.",
                "Every ADR must contain: Context, Decision, Consequences, Alternatives Considered.",
                "No secrets or credentials appear in any architecture or configuration file.",
            ],
            success_criteria=[
                "Architecture plan updated and persisted to docs/architecture/.",
                "Service topology defined with all required services and their dependencies.",
                "Scalability profile generated (min/max replicas, CPU/memory targets).",
                "Architecture Decision Records created for all major technology choices.",
                "Infrastructure scaffolding templates produced for all defined services.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.1,
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Architect Agent's system prompt from the contract.

        Composes the universal FORGE preamble, role-specific architecture
        standards from the template, and the full contract details including
        objective, constraints, validation rules, and success criteria.

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

{ARCHITECT_SYSTEM_PROMPT_TEMPLATE}

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

### Additional Architecture Decision Record (ADR) Format
Every ADR file you produce must follow this exact structure:

  # ADR-NNN: <title>

  ## Status
  Accepted | Proposed | Deprecated | Superseded by ADR-NNN

  ## Context
  <Why does this decision need to be made? What problem are we solving?>

  ## Decision
  <What did we decide? Be specific — name the technology, pattern, or approach.>

  ## Consequences
  ### Positive
  - <benefit 1>
  ### Negative / Trade-offs
  - <trade-off 1>
  ### Neutral
  - <neutral consequence>

  ## Alternatives Considered
  | Alternative | Reason Rejected |
  |-------------|-----------------|
  | Option A    | ...             |

  ## References
  - <link or citation>

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the architect agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated architecture artefacts,
            ADRs, reasoning, and any errors.
        """
        logger.info(
            "architect_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
