"""Agent Registry for the FORGE Agent Runtime.

The registry is a singleton that maps ``AgentType`` enum values to their
concrete agent classes.  It provides:
  - Registration of agent classes.
  - Instantiation of agents with a shared LLM client.
  - Capability metadata for each agent type.
  - Smart task-to-agent dispatch via ``get_agent_for_task()``.

Usage::

    from system.agents.registry import default_registry, get_agent_for_task

    agent = default_registry.get(AgentType.BACKEND)
    result = await agent.execute(context)

    # OR smart dispatch:
    agent = get_agent_for_task(task_node)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type

from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL
from system.shared.exceptions import AgentError
from system.shared.models import AgentType, ExecutionPhase
from system.shared.llm_client import get_llm_client
from system.core.orchestration.task_schemas import TaskNode
from system.agents.base import BaseAgent

logger = get_logger("agent.registry")


# ============================================================================ #
# Agent Capabilities
# ============================================================================ #


@dataclass
class AgentCapabilities:
    """Metadata describing what a specialist agent can do.

    Used by the registry's API endpoint and the smart dispatcher to determine
    which agent is most appropriate for a given task.

    Attributes
    ----------
    agent_type:
        The enum value this capability record describes.
    description:
        One-sentence description of the agent's role.
    allowed_phases:
        Pipeline phases during which this agent may be invoked.
    typical_tasks:
        Sample task descriptions this agent handles (used for keyword matching
        in the smart dispatcher).
    primary_language:
        Primary programming language or file type this agent produces.
    can_read_only:
        If True, this agent can be used in read-only review mode.
    max_concurrent:
        Maximum number of instances that can run concurrently.
    """

    agent_type: AgentType
    description: str
    allowed_phases: List[ExecutionPhase]
    typical_tasks: List[str]
    primary_language: str = "python"
    can_read_only: bool = False
    max_concurrent: int = 3


# ============================================================================ #
# Capability Catalogue
# ============================================================================ #

AGENT_CAPABILITIES: Dict[AgentType, AgentCapabilities] = {
    AgentType.ARCHITECT: AgentCapabilities(
        agent_type=AgentType.ARCHITECT,
        description=(
            "Makes high-level technical decisions: service topology, "
            "database schema, API contracts, infrastructure patterns, and ADRs."
        ),
        allowed_phases=[
            ExecutionPhase.ARCHITECTURE,
            ExecutionPhase.SPECIFICATION,
            ExecutionPhase.PLANNING if hasattr(ExecutionPhase, "PLANNING") else ExecutionPhase.TASK_GRAPH,
        ],
        typical_tasks=[
            "design service topology",
            "plan database schema",
            "create architecture decision record",
            "define API contract",
            "infrastructure planning",
            "service decomposition",
            "scalability analysis",
            "technology selection",
        ],
        primary_language="yaml",
        can_read_only=False,
        max_concurrent=1,  # architecture decisions should be serialised
    ),
    AgentType.BACKEND: AgentCapabilities(
        agent_type=AgentType.BACKEND,
        description=(
            "Writes Python backend code: FastAPI routers, SQLAlchemy models, "
            "Pydantic schemas, service layer, Alembic migrations, and unit tests."
        ),
        allowed_phases=[
            ExecutionPhase.EXECUTION,
            ExecutionPhase.VERIFICATION,
            ExecutionPhase.ITERATION,
        ],
        typical_tasks=[
            "create sqlalchemy model",
            "implement fastapi router",
            "write service layer",
            "create pydantic schema",
            "implement authentication",
            "create alembic migration",
            "implement crud operations",
            "write backend tests",
            "implement api endpoint",
            "add database model",
            "implement business logic",
            "create repository layer",
        ],
        primary_language="python",
        can_read_only=False,
        max_concurrent=4,
    ),
    AgentType.FRONTEND: AgentCapabilities(
        agent_type=AgentType.FRONTEND,
        description=(
            "Writes Next.js 15 / TypeScript / Tailwind frontend code: "
            "React Server Components, client components, hooks, pages, and API clients."
        ),
        allowed_phases=[
            ExecutionPhase.EXECUTION,
            ExecutionPhase.VERIFICATION,
            ExecutionPhase.ITERATION,
        ],
        typical_tasks=[
            "create react component",
            "implement page",
            "write typescript hook",
            "create form",
            "implement dashboard",
            "add tailwind styling",
            "create api client",
            "implement table",
            "add modal",
            "create layout",
            "implement navigation",
            "write frontend tests",
        ],
        primary_language="typescript",
        can_read_only=False,
        max_concurrent=4,
    ),
    AgentType.INFRA: AgentCapabilities(
        agent_type=AgentType.INFRA,
        description=(
            "Writes Docker, Kubernetes, Terraform, and GitHub Actions CI/CD "
            "infrastructure-as-code."
        ),
        allowed_phases=[
            ExecutionPhase.EXECUTION,
            ExecutionPhase.DEPLOYMENT,
            ExecutionPhase.ITERATION,
        ],
        typical_tasks=[
            "write dockerfile",
            "create kubernetes deployment",
            "write terraform module",
            "configure github actions",
            "set up ci/cd pipeline",
            "create docker-compose",
            "configure helm chart",
            "write k8s service",
            "create ingress",
            "configure autoscaling",
            "write network policy",
            "set up monitoring",
        ],
        primary_language="yaml",
        can_read_only=False,
        max_concurrent=2,
    ),
    AgentType.QA: AgentCapabilities(
        agent_type=AgentType.QA,
        description=(
            "Writes comprehensive pytest test suites with ≥80% coverage: "
            "unit tests, integration tests, fixtures, and mock configurations."
        ),
        allowed_phases=[
            ExecutionPhase.EXECUTION,
            ExecutionPhase.VERIFICATION,
            ExecutionPhase.ITERATION,
        ],
        typical_tasks=[
            "write unit tests",
            "write integration tests",
            "create test fixtures",
            "add test coverage",
            "write api tests",
            "mock external services",
            "test authentication flow",
            "write e2e tests",
            "add regression tests",
            "test error handling",
            "write performance tests",
        ],
        primary_language="python",
        can_read_only=True,  # reads source files, writes test files
        max_concurrent=4,
    ),
    AgentType.SECURITY: AgentCapabilities(
        agent_type=AgentType.SECURITY,
        description=(
            "Reviews code for security vulnerabilities (OWASP Top 10, CWE/SANS Top 25) "
            "and produces remediated files plus a security assessment report."
        ),
        allowed_phases=[
            ExecutionPhase.VERIFICATION,
            ExecutionPhase.EXECUTION,
            ExecutionPhase.DEPLOYMENT,
        ],
        typical_tasks=[
            "security review",
            "audit authentication",
            "check for sql injection",
            "review access control",
            "audit secrets handling",
            "owasp review",
            "penetration test review",
            "check input validation",
            "review cors configuration",
            "audit jwt implementation",
        ],
        primary_language="python",
        can_read_only=True,
        max_concurrent=2,
    ),
    AgentType.DOCS: AgentCapabilities(
        agent_type=AgentType.DOCS,
        description=(
            "Generates technical documentation: README, API reference, "
            "architecture diagrams, runbooks, and contributing guides."
        ),
        allowed_phases=[
            ExecutionPhase.EXECUTION,
            ExecutionPhase.VERIFICATION,
            ExecutionPhase.DEPLOYMENT,
            ExecutionPhase.ITERATION,
        ],
        typical_tasks=[
            "write readme",
            "generate api documentation",
            "create runbook",
            "write architecture docs",
            "document api endpoints",
            "create developer guide",
            "write deployment guide",
            "generate changelog",
            "document environment variables",
            "write contributing guide",
        ],
        primary_language="markdown",
        can_read_only=True,
        max_concurrent=3,
    ),
    AgentType.REFACTOR: AgentCapabilities(
        agent_type=AgentType.REFACTOR,
        description=(
            "Improves code quality without changing behaviour: extracts methods, "
            "adds type hints, eliminates duplication, replaces magic values."
        ),
        allowed_phases=[
            ExecutionPhase.ITERATION,
            ExecutionPhase.VERIFICATION,
        ],
        typical_tasks=[
            "refactor code",
            "improve code quality",
            "add type hints",
            "remove duplication",
            "improve error handling",
            "extract helper functions",
            "simplify conditionals",
            "rename variables",
            "add docstrings",
            "improve code coverage",
            "reduce complexity",
            "performance optimisation",
        ],
        primary_language="python",
        can_read_only=False,
        max_concurrent=3,
    ),
}


# ============================================================================ #
# AgentRegistry
# ============================================================================ #


class AgentRegistry:
    """Singleton registry mapping AgentType to agent class implementations.

    The registry is initialised once at module load time with all 8 built-in
    agent types registered.  Additional agents can be registered at runtime
    via ``register()``.

    Thread safety: registrations happen at import time; concurrent reads are
    safe.  Concurrent writes (``register()`` calls) should be avoided in
    production — do all registrations before serving requests.
    """

    _instance: Optional["AgentRegistry"] = None

    def __new__(cls) -> "AgentRegistry":
        """Enforce singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialised = False
        return cls._instance

    def __init__(self) -> None:
        """Initialise the registry (called once due to singleton pattern)."""
        if getattr(self, "_initialised", False):
            return
        self._registry: Dict[AgentType, Type[BaseAgent]] = {}
        self._capabilities: Dict[AgentType, AgentCapabilities] = dict(AGENT_CAPABILITIES)
        self._llm_client: Optional[object] = None
        self._initialised = True
        logger.info("agent_registry_initialised")

    # ---------------------------------------------------------------------- #
    # Registration
    # ---------------------------------------------------------------------- #

    def register(
        self,
        agent_type: AgentType,
        agent_class: Type[BaseAgent],
        capabilities: Optional[AgentCapabilities] = None,
    ) -> None:
        """Register an agent class for a given agent type.

        Parameters
        ----------
        agent_type:
            The ``AgentType`` this class handles.
        agent_class:
            A concrete subclass of ``BaseAgent``.
        capabilities:
            Optional capability override.  If None, uses the built-in
            capability catalogue entry for this agent type.

        Raises
        ------
        TypeError
            If ``agent_class`` is not a subclass of ``BaseAgent``.
        """
        if not (isinstance(agent_class, type) and issubclass(agent_class, BaseAgent)):
            raise TypeError(
                f"agent_class must be a subclass of BaseAgent, got {agent_class!r}"
            )
        self._registry[agent_type] = agent_class
        if capabilities is not None:
            self._capabilities[agent_type] = capabilities
        logger.info(
            "agent_registered",
            agent_type=agent_type.value,
            agent_class=agent_class.__name__,
        )

    # ---------------------------------------------------------------------- #
    # Retrieval
    # ---------------------------------------------------------------------- #

    def get(self, agent_type: AgentType) -> BaseAgent:
        """Instantiate and return an agent for the given type.

        The LLM client is created lazily on the first call and reused for
        all subsequent agent instantiations (the client itself manages
        connection pooling and rate limiting).

        Parameters
        ----------
        agent_type:
            The ``AgentType`` to instantiate.

        Returns
        -------
        BaseAgent
            A fully initialised agent ready for ``execute()`` calls.

        Raises
        ------
        AgentError
            If no agent class is registered for ``agent_type``.
        """
        if agent_type not in self._registry:
            raise AgentError(
                message=f"No agent registered for type: {agent_type.value}",
                details={
                    "agent_type": agent_type.value,
                    "registered_types": [t.value for t in self._registry],
                },
            )

        agent_class = self._registry[agent_type]
        llm_client = self._get_llm_client()

        agent = agent_class(llm_client=llm_client)
        logger.debug(
            "agent_instantiated",
            agent_type=agent_type.value,
            class_name=agent_class.__name__,
        )
        return agent

    def _get_llm_client(self) -> object:
        """Return the shared LLM client, creating it on the first call.

        Returns
        -------
        object
            An initialised async LLM client.
        """
        if self._llm_client is None:
            self._llm_client = get_llm_client()
            logger.info("llm_client_created", model=DEFAULT_LLM_MODEL)
        return self._llm_client

    # ---------------------------------------------------------------------- #
    # Introspection
    # ---------------------------------------------------------------------- #

    def list_agents(self) -> List[AgentType]:
        """Return all registered agent types.

        Returns
        -------
        List[AgentType]
            Sorted list of registered agent types.
        """
        return sorted(self._registry.keys(), key=lambda t: t.value)

    def get_capabilities(self, agent_type: AgentType) -> Optional[AgentCapabilities]:
        """Return capability metadata for an agent type.

        Parameters
        ----------
        agent_type:
            The agent type to look up.

        Returns
        -------
        Optional[AgentCapabilities]
            Capability record, or None if not found.
        """
        return self._capabilities.get(agent_type)

    def all_capabilities(self) -> List[AgentCapabilities]:
        """Return capability records for all registered agent types.

        Returns
        -------
        List[AgentCapabilities]
            List sorted by agent type value.
        """
        return [
            caps
            for agent_type in sorted(self._registry.keys(), key=lambda t: t.value)
            if (caps := self._capabilities.get(agent_type)) is not None
        ]

    def is_registered(self, agent_type: AgentType) -> bool:
        """Check whether an agent type is registered.

        Parameters
        ----------
        agent_type:
            The agent type to check.

        Returns
        -------
        bool
            True if registered.
        """
        return agent_type in self._registry


# ============================================================================ #
# Smart dispatcher
# ============================================================================ #


def get_agent_for_task(
    task: TaskNode,
    registry: Optional[AgentRegistry] = None,
) -> BaseAgent:
    """Select and instantiate the most appropriate agent for a task.

    Dispatching strategy:
    1. Use ``task.agent_type`` directly — this is the primary dispatch
       signal set by the TaskGraphEngine during planning.
    2. Verify the agent's ``allowed_phases`` include the task's phase.
    3. Log a warning if the task phase is not in the agent's allowed phases,
       but still return the agent (the contract enforces scope at runtime).

    Parameters
    ----------
    task:
        The TaskNode to dispatch.
    registry:
        Optional registry to use.  Defaults to the module-level singleton.

    Returns
    -------
    BaseAgent
        A ready-to-execute agent instance.

    Raises
    ------
    AgentError
        If no agent is registered for the task's agent_type.
    """
    reg = registry or default_registry
    agent = reg.get(task.agent_type)

    # Validate phase compatibility (advisory — not a hard block)
    caps = reg.get_capabilities(task.agent_type)
    if caps is not None and task.phase not in caps.allowed_phases:
        logger.warning(
            "agent_phase_mismatch",
            agent_type=task.agent_type.value,
            task_phase=task.phase,
            allowed_phases=[p.value for p in caps.allowed_phases],
            task_id=task.task_id,
        )

    logger.info(
        "agent_dispatched",
        agent_type=task.agent_type.value,
        task_id=task.task_id,
        task_name=task.name,
    )
    return agent


# ============================================================================ #
# Module-level default registry — all 8 agents pre-registered
# ============================================================================ #

def _build_default_registry() -> AgentRegistry:
    """Build and return the default registry with all agents registered.

    Imports are deferred inside the function to avoid circular imports at
    module load time (each agent module imports from base which imports
    from here).

    Returns
    -------
    AgentRegistry
        Singleton registry with all 8 agents registered.
    """
    reg = AgentRegistry()

    # Deferred imports to break circular dependency
    from system.agents.architect.agent import ArchitectAgent
    from system.agents.backend.agent import BackendAgent
    from system.agents.frontend.agent import FrontendAgent
    from system.agents.infra.agent import InfraAgent
    from system.agents.qa.agent import QAAgent
    from system.agents.security.agent import SecurityAgent
    from system.agents.docs.agent import DocsAgent
    from system.agents.refactor.agent import RefactorAgent

    reg.register(AgentType.ARCHITECT, ArchitectAgent)
    reg.register(AgentType.BACKEND, BackendAgent)
    reg.register(AgentType.FRONTEND, FrontendAgent)
    reg.register(AgentType.INFRA, InfraAgent)
    reg.register(AgentType.QA, QAAgent)
    reg.register(AgentType.SECURITY, SecurityAgent)
    reg.register(AgentType.DOCS, DocsAgent)
    reg.register(AgentType.REFACTOR, RefactorAgent)

    logger.info(
        "default_registry_ready",
        agent_count=len(reg.list_agents()),
        agents=[t.value for t in reg.list_agents()],
    )
    return reg


# Lazily initialised default registry
_default_registry_instance: Optional[AgentRegistry] = None


def _get_default_registry() -> AgentRegistry:
    """Return the default registry, initialising it on first access."""
    global _default_registry_instance
    if _default_registry_instance is None:
        _default_registry_instance = _build_default_registry()
    return _default_registry_instance


class _DefaultRegistryProxy:
    """Proxy object that delegates to the lazily-initialised default registry.

    This allows ``default_registry`` to be used as a module-level constant
    while deferring the actual initialisation (and circular imports) until
    first access.
    """

    def __getattr__(self, name: str) -> object:
        return getattr(_get_default_registry(), name)

    def __repr__(self) -> str:
        return f"<DefaultRegistryProxy for {_get_default_registry()!r}>"


default_registry: AgentRegistry = _DefaultRegistryProxy()  # type: ignore[assignment]
