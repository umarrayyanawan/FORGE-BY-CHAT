"""FORGE Intent Engine — Phase 2 of the Autonomous Software Production System.

Converts vague user prompts into structured, validated :class:`ProjectIntent`
objects through an iterative LLM-powered extraction and clarification loop.

Public exports
--------------
IntentEngine
    Main orchestrator.  Wire it up via FastAPI DI or call directly.
IntentSchema (alias for ProjectIntent)
    The structured output schema representing a fully-validated project intent.
ClarificationEngine
    Generates targeted clarification questions and applies user answers.

Typical usage
~~~~~~~~~~~~~
::

    from system.core.intent import IntentEngine, IntentSchema, ClarificationEngine

    engine = IntentEngine(llm_client=llm, redis=redis, db=db_session)
    response = await engine.process(IntentParseRequest(prompt="Build CRM for marble suppliers"))

    if response.clarification_needed:
        # Present response.clarification_request.questions to the user
        ...
    else:
        intent: IntentSchema = response.intent
        # Intent is complete — hand off to the Specification phase
        ...
"""

from __future__ import annotations

from .clarification import ClarificationEngine
from .engine import IntentEngine
from .schemas import (
    ClarificationQuestion,
    ClarificationRequest,
    ClarificationResponse,
    IntentParseRequest,
    IntentParseResponse,
    IntentSession,
    IntentStatus,
    ProjectIntent,
)

# Public alias for the canonical intent output type.
IntentSchema = ProjectIntent

__all__ = [
    # Core classes
    "IntentEngine",
    "ClarificationEngine",
    # Schema alias (as specified in the task)
    "IntentSchema",
    # Schemas
    "ProjectIntent",
    "IntentSession",
    "IntentStatus",
    "ClarificationQuestion",
    "ClarificationRequest",
    "ClarificationResponse",
    "IntentParseRequest",
    "IntentParseResponse",
]
