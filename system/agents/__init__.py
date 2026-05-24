"""FORGE Agent Runtime — specialized autonomous agents with scoped execution."""
from system.agents.base import BaseAgent, AgentContract, AgentContext, AgentResult
from system.agents.registry import AgentRegistry, default_registry
from system.agents.runner import AgentRunner

# Export the module-level default registry under the documented alias
agent_registry = default_registry

__all__ = [
    "BaseAgent",
    "AgentContract",
    "AgentContext",
    "AgentResult",
    "AgentRegistry",
    "agent_registry",
    "AgentRunner",
]
