"""FastAPI router for the Architecture Planning Engine (Phase 4).

Exposes endpoints to trigger planning, retrieve plans, and fetch specific
plan artefacts (Mermaid diagram, ADR list).

Prefix: /architecture
Tags:   architecture
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from system.core.planning.engine import ArchitectureEngine
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.database import get_db
from system.shared.llm_client import get_llm_client
from system.shared.models import BaseForgeModel

logger = get_logger(__name__)

router = APIRouter(prefix="/architecture", tags=["architecture"])


# ========================================================================== #
# Request / Response schemas
# ========================================================================== #


class PlanRequest(BaseForgeModel):
    """Request body for POST /architecture/plan."""

    project_id: str = Field(..., description="FORGE project identifier.")
    spec: ProjectSpec = Field(..., description="Finalised ProjectSpec to plan from.")


class PlanResponse(BaseForgeModel):
    """Response body returned after planning completes."""

    project_id: str
    plan_id: str
    architecture_pattern: str
    service_count: int
    infra_component_count: int
    deployment_target: str
    estimated_monthly_cost_usd: Optional[float] = None
    plan: ArchitecturePlan


class DiagramResponse(BaseForgeModel):
    """Response body for the diagram endpoint."""

    project_id: str
    plan_id: str
    diagram: str  # raw Mermaid.js source


class ADRResponse(BaseForgeModel):
    """Response body for the ADR endpoint."""

    project_id: str
    plan_id: str
    adrs: List[str]  # adr_notes strings from the plan


# ========================================================================== #
# Dependency helpers
# ========================================================================== #


async def get_engine(
    db: AsyncSession = Depends(get_db),
) -> ArchitectureEngine:
    """Create an ArchitectureEngine for each request."""
    return ArchitectureEngine(llm_client=get_llm_client(), db=db)


# ========================================================================== #
# Endpoints
# ========================================================================== #


@router.post(
    "/plan",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate architecture plan",
    description=(
        "Run the full architecture planning pipeline for a project. "
        "Returns the generated ArchitecturePlan and persists it to the database."
    ),
)
async def create_plan(
    body: PlanRequest,
    engine: ArchitectureEngine = Depends(get_engine),
) -> PlanResponse:
    """Trigger architecture planning from a ProjectSpec."""
    logger.info("POST /architecture/plan", project_id=body.project_id)
    try:
        plan = await engine.plan(project_id=body.project_id, spec=body.spec)
    except Exception as exc:
        logger.error(
            "Architecture planning failed",
            project_id=body.project_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Architecture planning failed: {exc}",
        )

    return PlanResponse(
        project_id=plan.project_id,
        plan_id=plan.plan_id,
        architecture_pattern=plan.architecture_pattern,
        service_count=len(plan.services),
        infra_component_count=len(plan.infra_components),
        deployment_target=str(plan.deployment_target),
        plan=plan,
    )


@router.get(
    "/{project_id}",
    response_model=PlanResponse,
    summary="Get architecture plan",
    description="Retrieve the current architecture plan for a project.",
)
async def get_plan(
    project_id: str,
    engine: ArchitectureEngine = Depends(get_engine),
) -> PlanResponse:
    """Load the architecture plan for *project_id* from the database."""
    logger.info("GET /architecture/{project_id}", project_id=project_id)
    plan = await engine.get_plan(project_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No architecture plan found for project '{project_id}'.",
        )
    return PlanResponse(
        project_id=plan.project_id,
        plan_id=plan.plan_id,
        architecture_pattern=plan.architecture_pattern,
        service_count=len(plan.services),
        infra_component_count=len(plan.infra_components),
        deployment_target=str(plan.deployment_target),
        plan=plan,
    )


@router.get(
    "/{project_id}/diagram",
    response_model=DiagramResponse,
    summary="Get Mermaid architecture diagram",
    description=(
        "Return the Mermaid.js graph TD source string for the project's "
        "architecture diagram. Embed directly in Markdown or a diagram renderer."
    ),
)
async def get_diagram(
    project_id: str,
    engine: ArchitectureEngine = Depends(get_engine),
) -> DiagramResponse:
    """Return the Mermaid diagram for *project_id*."""
    logger.info("GET /architecture/{project_id}/diagram", project_id=project_id)
    plan = await engine.get_plan(project_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No architecture plan found for project '{project_id}'.",
        )
    if not plan.architecture_diagram:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Architecture diagram not yet generated for this plan.",
        )
    return DiagramResponse(
        project_id=plan.project_id,
        plan_id=plan.plan_id,
        diagram=plan.architecture_diagram,
    )


@router.get(
    "/{project_id}/adr",
    response_model=ADRResponse,
    summary="Get Architecture Decision Records",
    description=(
        "Return the list of Architecture Decision Records (ADRs) captured "
        "during architecture planning."
    ),
)
async def get_adrs(
    project_id: str,
    engine: ArchitectureEngine = Depends(get_engine),
) -> ADRResponse:
    """Return ADR notes from the architecture plan for *project_id*."""
    logger.info("GET /architecture/{project_id}/adr", project_id=project_id)
    plan = await engine.get_plan(project_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No architecture plan found for project '{project_id}'.",
        )
    return ADRResponse(
        project_id=plan.project_id,
        plan_id=plan.plan_id,
        adrs=plan.adr_notes,
    )
