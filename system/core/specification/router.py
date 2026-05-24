"""FastAPI router for the Specification Engine — Phase 3.

Endpoints:
  POST /spec/generate            → Generate a new ProjectSpec
  GET  /spec/{project_id}        → Retrieve a spec by project ID
  PUT  /spec/{project_id}        → Apply incremental updates
  GET  /spec/{project_id}/export/markdown → Export spec as Markdown
  GET  /spec/{project_id}/export/json     → Export spec as JSON
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from system.core.intent.schemas import ProjectIntent
from system.core.specification.engine import SpecificationEngine
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.database import get_db
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import get_llm_client
from system.shared.models import BaseForgeModel

logger = get_logger(__name__)

router = APIRouter(prefix="/spec", tags=["specification"])


# ========================================================================== #
# Request / Response schemas
# ========================================================================== #


class GenerateSpecRequest(BaseForgeModel):
    """Request body for POST /spec/generate."""

    project_id: str = Field(description="UUID of the project to generate a spec for")
    intent: ProjectIntent = Field(description="Validated ProjectIntent")


class GenerateSpecResponse(BaseForgeModel):
    """Response body for POST /spec/generate."""

    project_id: str
    spec_id: str
    complexity: str
    table_count: int
    endpoint_count: int
    page_count: int
    message: str = "Specification generated successfully"


class UpdateSpecRequest(BaseForgeModel):
    """Request body for PUT /spec/{project_id}."""

    updates: dict[str, Any] = Field(
        description="Key-value pairs of ProjectSpec fields to update"
    )


# ========================================================================== #
# Dependency injection helpers
# ========================================================================== #


def get_spec_engine(
    db: AsyncSession = Depends(get_db),
) -> SpecificationEngine:
    """FastAPI dependency that provides a SpecificationEngine instance."""
    llm_client = get_llm_client()
    return SpecificationEngine(llm_client=llm_client, db=db)


# ========================================================================== #
# Route handlers
# ========================================================================== #


@router.post(
    "/generate",
    response_model=GenerateSpecResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new ProjectSpec",
    description=(
        "Runs the full specification pipeline: PRD → DB schema → API contract → "
        "UI structure → dependency analysis. Returns a summary; "
        "use GET /spec/{project_id} for the full spec."
    ),
)
async def generate_spec(
    request: GenerateSpecRequest,
    engine: SpecificationEngine = Depends(get_spec_engine),
    db: AsyncSession = Depends(get_db),
) -> GenerateSpecResponse:
    """Generate a complete ProjectSpec from a ProjectIntent."""
    logger.info(
        "api_generate_spec",
        project_id=request.project_id,
        product_type=request.intent.product_type,
    )

    try:
        spec = await engine.generate_spec(
            project_id=request.project_id,
            intent=request.intent,
            db=db,
        )
    except SpecificationError as exc:
        logger.error("spec_generation_failed", error=str(exc), details=exc.details)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.to_dict(),
        ) from exc
    except Exception as exc:
        logger.exception("spec_generation_unexpected_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Internal error during spec generation", "code": "INTERNAL_ERROR"},
        ) from exc

    return GenerateSpecResponse(
        project_id=spec.project_id,
        spec_id=spec.id,
        complexity=spec.estimated_complexity,
        table_count=len(spec.db_schema.tables),
        endpoint_count=spec.api_contract.endpoint_count(),
        page_count=len(spec.ui_structure.pages),
    )


@router.get(
    "/{project_id}",
    response_model=ProjectSpec,
    summary="Retrieve a ProjectSpec",
    description="Fetch the full specification for a project by its UUID.",
)
async def get_spec(
    project_id: str,
    engine: SpecificationEngine = Depends(get_spec_engine),
    db: AsyncSession = Depends(get_db),
) -> ProjectSpec:
    """Return the full ProjectSpec for a given project."""
    logger.info("api_get_spec", project_id=project_id)

    try:
        spec = await engine.get_spec(project_id=project_id, db=db)
    except SpecificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exc.to_dict(),
        ) from exc

    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"No spec found for project_id={project_id}",
                "code": "SPECIFICATION_NOT_FOUND",
            },
        )

    return spec


@router.put(
    "/{project_id}",
    response_model=ProjectSpec,
    summary="Update a ProjectSpec",
    description=(
        "Apply incremental updates to an existing spec. Increments the version "
        "number and persists the changes."
    ),
)
async def update_spec(
    project_id: str,
    request: UpdateSpecRequest,
    engine: SpecificationEngine = Depends(get_spec_engine),
    db: AsyncSession = Depends(get_db),
) -> ProjectSpec:
    """Incrementally update fields on an existing ProjectSpec."""
    logger.info(
        "api_update_spec",
        project_id=project_id,
        fields=list(request.updates.keys()),
    )

    try:
        updated_spec = await engine.update_spec(
            project_id=project_id,
            updates=request.updates,
            db=db,
        )
    except SpecificationError as exc:
        if exc.code == "SPECIFICATION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=exc.to_dict(),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.to_dict(),
        ) from exc

    return updated_spec


@router.get(
    "/{project_id}/export/markdown",
    response_class=PlainTextResponse,
    summary="Export spec as Markdown",
    description="Render the full ProjectSpec as a human-readable Markdown document.",
)
async def export_spec_markdown(
    project_id: str,
    engine: SpecificationEngine = Depends(get_spec_engine),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Export a ProjectSpec as a Markdown string."""
    try:
        spec = await engine.get_spec(project_id=project_id, db=db)
    except SpecificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exc.to_dict(),
        ) from exc

    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"No spec found for project_id={project_id}"},
        )

    markdown = spec.to_markdown()
    return PlainTextResponse(
        content=markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="spec-{project_id}.md"'},
    )


@router.get(
    "/{project_id}/export/json",
    summary="Export spec as JSON",
    description="Return the full ProjectSpec serialised to JSON.",
)
async def export_spec_json(
    project_id: str,
    engine: SpecificationEngine = Depends(get_spec_engine),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Export a ProjectSpec as a JSON download."""
    try:
        spec = await engine.get_spec(project_id=project_id, db=db)
    except SpecificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exc.to_dict(),
        ) from exc

    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"No spec found for project_id={project_id}"},
        )

    return JSONResponse(
        content=spec.model_dump(mode="json"),
        headers={
            "Content-Disposition": f'attachment; filename="spec-{project_id}.json"',
        },
    )
