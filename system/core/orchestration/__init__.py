"""Orchestration sub-package for the FORGE platform.

Exports the three primary engine classes used throughout the system:

- TaskGraphEngine  – builds and manages the DAG of work items.
- OrchestrationEngine – alias exposed for backward compatibility; the
  canonical implementation lives in workflow_engine as WorkflowEngine.
- WorkflowEngine   – the main orchestration loop that drives agents.
"""

from __future__ import annotations

from system.core.orchestration.task_graph import TaskGraphEngine
from system.core.orchestration.workflow_engine import WorkflowEngine

# Alias so callers can use `OrchestrationEngine` without knowing the internal name.
OrchestrationEngine = WorkflowEngine

__all__ = [
    "TaskGraphEngine",
    "OrchestrationEngine",
    "WorkflowEngine",
]
