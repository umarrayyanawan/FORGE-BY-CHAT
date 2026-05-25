"""FastAPI router for the FORGE Intent Engine API.

Exposes endpoints for:
  - Parsing a raw prompt into structured intent
  - Submitting clarification answers
  - Retrieving sessions and project intents
  - Deleting sessions
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from system.observability.logging.logger import get_logger
from system.shared.database import get_db
from system.shared.exceptions import ForgeError, IntentError, NotFoundError, ValidationError
from system.shared.llm_client import get_llm_client
from system.shared.redis_client import get_redis

from .engine import IntentEngine
from .persistence import IntentPersistence
from .schemas import (
    ClarificationResponse,
    IntentParseRequest,
    IntentParseResponse,
    IntentSession,
    ProjectIntent,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/intent", tags=["intent"])


# =========================================================================== #
# Dependency injection
# =========================================================================== #


async def get_intent_engine(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AsyncGenerator[IntentEngine, None]:
    """Construct an :class:`IntentEngine` with all dependencies injected."""
    redis = await get_redis()
    llm_client = get_llm_client()
    engine = IntentEngine(llm_client=llm_client, redis=redis, db=db)
    return engine


async def get_intent_persistence(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AsyncGenerator[IntentPersistence, None]:
    """Construct :class:`IntentPersistence` for read-only endpoints."""
    redis = await get_redis()
    persistence = IntentPersistence(redis=redis, db_session=db)
    return persistence


# =========================================================================== #
# Helper — unified error handler
# =========================================================================== #


def _handle_forge_error(exc: ForgeError, default_status: int = 500) -> HTTPException:
    """Convert a :class:`ForgeError` to an :class:`HTTPException`."""
    status_map = {
        "NOT_FOUND_ERROR": status.HTTP_404_NOT_FOUND,
        "VALIDATION_ERROR": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "INTENT_ERROR": status.HTTP_400_BAD_REQUEST,
        "AUTHENTICATION_ERROR": status.HTTP_401_UNAUTHORIZED,
        "AUTHORIZATION_ERROR": status.HTTP_403_FORBIDDEN,
        "CONFLICT_ERROR": status.HTTP_409_CONFLICT,
    }
    http_status = status_map.get(exc.code, default_status)
    return HTTPException(
        status_code=http_status,
        detail={
            "error": exc.message,
            "code": exc.code,
            "details": exc.details,
        },
    )


# =========================================================================== #
# Endpoints
# =========================================================================== #


@router.post(
    "/parse",
    response_model=IntentParseResponse,
    status_code=status.HTTP_200_OK,
    summary="Parse a raw prompt into structured project intent",
    description=(
        "Accepts a free-text user prompt and uses LLM analysis to extract a structured "
        ":class:`ProjectIntent`.  If the intent is incomplete (confidence < 0.7), the "
        "response will include clarification questions.  Call ``/intent/clarify`` to "
        "submit answers and continue the clarification loop."
    ),
)
async def parse_intent(
    request: IntentParseRequest,
    engine: Annotated[IntentEngine, Depends(get_intent_engine)],
) -> IntentParseResponse:
    """Parse a raw prompt and return structured intent or clarification questions."""
    logger.info("api_parse_intent", prompt_length=len(request.prompt))
    try:
        return await engine.process(request)
    except ValidationError as exc:
        raise _handle_forge_error(exc, status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
    except IntentError as exc:
        raise _handle_forge_error(exc, status.HTTP_400_BAD_REQUEST) from exc
    except ForgeError as exc:
        raise _handle_forge_error(exc) from exc
    except Exception as exc:
        logger.error("unexpected_parse_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "Internal server error during intent parsing.",
                "code": "INTERNAL_ERROR",
            },
        ) from exc


@router.post(
    "/clarify",
    response_model=IntentParseResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit clarification answers",
    description=(
        "Submit user answers to the clarification questions returned by ``/intent/parse``. "
        "The engine will update the intent and either return another round of questions or "
        "mark the intent as complete."
    ),
)
async def submit_clarification(
    response: ClarificationResponse,
    engine: Annotated[IntentEngine, Depends(get_intent_engine)],
) -> IntentParseResponse:
    """Apply user answers and re-evaluate the intent."""
    logger.info(
        "api_submit_clarification",
        session_id=response.session_id,
        answer_count=len(response.answers),
    )
    try:
        return await engine.clarify(response)
    except NotFoundError as exc:
        raise _handle_forge_error(exc, status.HTTP_404_NOT_FOUND) from exc
    except IntentError as exc:
        raise _handle_forge_error(exc, status.HTTP_400_BAD_REQUEST) from exc
    except ForgeError as exc:
        raise _handle_forge_error(exc) from exc
    except Exception as exc:
        logger.error("unexpected_clarify_error", error=str(exc), session_id=response.session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "Internal server error during clarification.",
                "code": "INTERNAL_ERROR",
            },
        ) from exc


@router.get(
    "/sessions/{session_id}",
    response_model=IntentSession,
    status_code=status.HTTP_200_OK,
    summary="Get an intent session",
    description="Retrieve the full state of an intent session, including history.",
)
async def get_session(
    session_id: str,
    persistence: Annotated[IntentPersistence, Depends(get_intent_persistence)],
) -> IntentSession:
    """Retrieve a session by ID."""
    logger.debug("api_get_session", session_id=session_id)
    session = await persistence.load_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Session '{session_id}' not found.",
                "code": "NOT_FOUND_ERROR",
                "details": {"session_id": session_id},
            },
        )
    return session


@router.get(
    "/projects/{project_id}/intent",
    response_model=ProjectIntent,
    status_code=status.HTTP_200_OK,
    summary="Get the validated intent for a project",
    description=(
        "Retrieve the final, validated :class:`ProjectIntent` for a FORGE project. "
        "Returns 404 if the intent has not yet been validated and persisted."
    ),
)
async def get_project_intent(
    project_id: str,
    persistence: Annotated[IntentPersistence, Depends(get_intent_persistence)],
) -> ProjectIntent:
    """Retrieve the validated intent for a project."""
    logger.debug("api_get_project_intent", project_id=project_id)
    intent = await persistence.load_intent(project_id)
    if intent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"No validated intent found for project '{project_id}'.",
                "code": "NOT_FOUND_ERROR",
                "details": {"project_id": project_id},
            },
        )
    return intent


@router.get(
    "/projects/{project_id}/sessions",
    response_model=list[IntentSession],
    status_code=status.HTTP_200_OK,
    summary="List all sessions for a project",
    description="Returns all intent sessions associated with a project, newest first.",
)
async def list_project_sessions(
    project_id: str,
    persistence: Annotated[IntentPersistence, Depends(get_intent_persistence)],
) -> list[IntentSession]:
    """List all sessions for a given project."""
    logger.debug("api_list_project_sessions", project_id=project_id)
    sessions = await persistence.list_sessions(project_id)
    return sessions


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an intent session",
    description=(
        "Permanently remove a session from Redis and PostgreSQL. "
        "Returns 204 on success, 404 if the session does not exist."
    ),
)
async def delete_session(
    session_id: str,
    persistence: Annotated[IntentPersistence, Depends(get_intent_persistence)],
) -> None:
    """Delete a session by ID."""
    logger.info("api_delete_session", session_id=session_id)
    deleted = await persistence.delete_session(session_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": f"Session '{session_id}' not found.",
                "code": "NOT_FOUND_ERROR",
                "details": {"session_id": session_id},
            },
        )
    # 204 No Content — return None implicitly
