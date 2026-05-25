"""FastAPI router exposing the FORGE Agent Runtime over HTTP.

Provides endpoints to:
  - List all registered agents with their capabilities.
  - Query the operational status of a specific agent type.
  - Manually trigger a single agent for a given task payload.

This router is mounted at ``/agents`` in the main FastAPI application.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/agents", tags=["agents"])


# --------------------------------------------------------------------------- #
# GET /agents/
# --------------------------------------------------------------------------- #


@router.get(
    "/",
    summary="List all available agents",
    status_code=status.HTTP_200_OK,
)
async def list_agents() -> dict[str, Any]:
    """Return all registered agent types with their capabilities.

    Queries the module-level default agent registry and returns a list of
    capability records for every registered specialist agent.

    Returns
    -------
    dict
        ``{"agents": [{"agent_type": ..., "description": ..., ...}]}``

    Raises
    ------
    HTTPException
        500 if the registry cannot be initialised (e.g. missing LLM config).
    """
    from system.agents.registry import default_registry

    capabilities = default_registry.all_capabilities()

    agent_list = [
        {
            "agent_type": cap.agent_type,
            "description": cap.description,
            "primary_language": cap.primary_language,
            "allowed_phases": [p for p in cap.allowed_phases],
            "typical_tasks": cap.typical_tasks,
            "can_read_only": cap.can_read_only,
            "max_concurrent": cap.max_concurrent,
        }
        for cap in capabilities
    ]

    return {"agents": agent_list, "count": len(agent_list)}


# --------------------------------------------------------------------------- #
# GET /agents/{agent_type}/status
# --------------------------------------------------------------------------- #


@router.get(
    "/{agent_type}/status",
    summary="Get agent operational status",
    status_code=status.HTTP_200_OK,
)
async def agent_status(agent_type: str) -> dict[str, Any]:
    """Return the operational status for a specific agent type.

    Verifies that the agent type is registered and returns its capability
    metadata plus current availability.

    Parameters
    ----------
    agent_type:
        The string value of an ``AgentType`` enum (e.g. ``"backend"``).

    Returns
    -------
    dict
        Status record with ``agent_type``, ``status``, ``model``, and
        capability details.

    Raises
    ------
    HTTPException
        404 if the agent_type is not a known registered agent.
    """
    from system.agents.registry import default_registry
    from system.shared.constants import DEFAULT_LLM_MODEL
    from system.shared.models import AgentType

    # Validate the agent_type string against the enum
    try:
        agent_enum = AgentType(agent_type.lower())
    except ValueError:
        valid_types = [t.value for t in AgentType]
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"Unknown agent type: {agent_type!r}. Valid types: {valid_types}"),
        )

    if not default_registry.is_registered(agent_enum):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent type {agent_type!r} is not registered in the registry.",
        )

    capabilities = default_registry.get_capabilities(agent_enum)

    return {
        "agent_type": agent_type,
        "status": "available",
        "model": DEFAULT_LLM_MODEL,
        "description": capabilities.description if capabilities else None,
        "primary_language": capabilities.primary_language if capabilities else None,
        "max_concurrent": capabilities.max_concurrent if capabilities else None,
        "can_read_only": capabilities.can_read_only if capabilities else None,
        "allowed_phases": ([p for p in capabilities.allowed_phases] if capabilities else []),
    }


# --------------------------------------------------------------------------- #
# POST /agents/run
# --------------------------------------------------------------------------- #


@router.post(
    "/run",
    summary="Manually trigger an agent for a specific task",
    status_code=status.HTTP_200_OK,
)
async def run_agent(task_data: dict[str, Any]) -> dict[str, Any]:
    """Manually trigger a specialist agent for a given task payload.

    Constructs a ``TaskNode`` from the raw ``task_data`` dictionary, selects
    the appropriate agent from the registry, runs it via ``AgentRunner``, and
    returns the serialised ``AgentResult``.

    Request Body
    ------------
    The body must be a valid ``TaskNode`` payload.  Required fields:

    - ``task_id`` (str): Unique identifier for this task run.
    - ``name`` (str): Short human-readable task name.
    - ``description`` (str): Full task description (becomes the agent objective).
    - ``agent_type`` (str): One of: architect, backend, frontend, infra, qa,
      security, docs, refactor.
    - ``phase`` (str): Current execution phase (e.g. ``"execution"``).
    - ``priority`` (str): One of: critical, high, medium, low.

    Returns
    -------
    dict
        Serialised ``AgentResult`` with keys: ``success``, ``generated_files``,
        ``modifications``, ``deleted_files``, ``errors``, ``warnings``,
        ``reasoning``, ``tokens_used``, ``duration_ms``.

    Raises
    ------
    HTTPException
        422 if the task_data cannot be parsed as a valid TaskNode.
        500 if the agent raises an unrecoverable error during execution.
    """
    from system.agents.registry import default_registry
    from system.agents.runner import AgentRunner
    from system.core.orchestration.task_schemas import TaskNode
    from system.shared.exceptions import AgentError

    # Parse task_data into a TaskNode — raises ValidationError on bad input
    try:
        task = TaskNode(**task_data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid task payload: {exc}",
        ) from exc

    runner = AgentRunner(registry=default_registry)

    try:
        result = await runner.run_task(task)
    except AgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent execution failed: {exc.message}",
        ) from exc

    # Serialise the dataclass result to a plain dict for JSON response
    return {
        "agent_type": result.agent_type,
        "task_id": result.task_id,
        "success": result.success,
        "generated_files": list(result.generated_files.keys()),
        "modifications": list(result.modifications.keys()),
        "deleted_files": result.deleted_files,
        "validation_passed": result.validation_passed,
        "errors": result.errors,
        "warnings": result.warnings,
        "reasoning": result.reasoning,
        "tokens_used": result.tokens_used,
        "duration_ms": result.duration_ms,
    }
