"""Main IntentEngine orchestrator for the FORGE Intent Engine.

The engine is the single entry point for all intent-processing logic.  It
wires together the parser, clarification engine, validator, and persistence
layer, and exposes a clean async API consumed by the FastAPI router.
"""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from system.observability.logging.logger import get_logger
from system.shared.constants import MAX_INTENT_CLARIFICATION_ROUNDS
from system.shared.exceptions import IntentError, NotFoundError

from .clarification import ClarificationEngine
from .parser import IntentParser
from .persistence import IntentPersistence
from .schemas import (
    ClarificationRequest,
    ClarificationResponse,
    IntentParseRequest,
    IntentParseResponse,
    IntentSession,
    IntentStatus,
    ProjectIntent,
)
from .validator import IntentValidator

logger = get_logger(__name__)

# Minimum confidence before we consider the intent complete enough to validate.
_CONFIDENCE_THRESHOLD = 0.7


class IntentEngine:
    """Orchestrates intent parsing, clarification, validation, and persistence.

    Typical call flow
    -----------------
    1. Client calls :meth:`process` with a raw prompt.
    2. :class:`IntentParser` extracts a :class:`ProjectIntent`.
    3. If ``confidence_score < 0.7`` and rounds remain, :class:`ClarificationEngine`
       generates questions and the response has ``clarification_needed=True``.
    4. Client submits answers via :meth:`clarify`.
    5. Steps 2–4 repeat until confidence is sufficient or max rounds reached.
    6. :class:`IntentValidator` validates the final intent.
    7. :class:`IntentPersistence` durably stores the session and intent.

    Parameters
    ----------
    llm_client:
        Pre-initialised LLM client from :func:`~system.shared.llm_client.get_llm_client`.
    redis:
        Async Redis client from :func:`~system.shared.redis_client.get_redis`.
    db:
        Async SQLAlchemy session from :func:`~system.shared.database.get_db`.
    """

    def __init__(self, llm_client: Any, redis: Any, db: AsyncSession) -> None:
        self._llm = llm_client
        self._redis = redis
        self._db = db

        self._parser = IntentParser(llm_client=llm_client)
        self._clarifier = ClarificationEngine(llm_client=llm_client)
        self._validator = IntentValidator(llm_client=llm_client)
        self._persistence = IntentPersistence(redis=redis, db_session=db)

        self._log = logger

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def process(self, request: IntentParseRequest) -> IntentParseResponse:
        """Parse a prompt and return either a complete intent or clarification questions.

        Parameters
        ----------
        request:
            Contains the raw prompt and optional session/project identifiers.

        Returns
        -------
        IntentParseResponse
            Always returned — either with ``is_complete=True`` (validated intent)
            or ``clarification_needed=True`` (questions to present to the user).
        """
        self._log.info(
            "intent_engine_process",
            prompt_length=len(request.prompt),
            session_id=request.session_id,
            project_id=request.project_id,
        )

        # Resolve or create session
        session = await self._get_or_create_session(request)

        # Parse intent from the prompt
        parsed_intent = await self._parser.parse(request.prompt)

        # Merge with any existing intent data (preserves previously clarified fields)
        merged_intent = self._merge_intents(session.intent, parsed_intent, request.prompt)

        # Update session with latest intent
        session = session.model_copy(
            update={
                "intent": merged_intent,
                "raw_prompt": request.prompt,
            }
        )

        return await self._decide_and_respond(session)

    async def clarify(self, response: ClarificationResponse) -> IntentParseResponse:
        """Apply user answers and re-process the intent.

        Parameters
        ----------
        response:
            Contains the session_id and a dict of field → answer.

        Returns
        -------
        IntentParseResponse
            Updated response after applying the answers.

        Raises
        ------
        NotFoundError
            When the referenced session does not exist.
        """
        self._log.info(
            "intent_engine_clarify",
            session_id=response.session_id,
            answer_fields=list(response.answers.keys()),
        )

        session = await self._persistence.load_session(response.session_id)
        if session is None:
            raise NotFoundError(
                f"Session '{response.session_id}' not found.",
                details={"session_id": response.session_id},
            )

        # Apply answers to the existing intent
        updated_intent = await self._clarifier.apply_answers(
            intent=session.intent,
            answers=response.answers,
        )

        # Record the exchange in history
        user_answer_text = "; ".join(
            f"{field}: {answer}" for field, answer in response.answers.items()
        )
        updated_history = session.clarification_history + [
            {"role": "user", "content": user_answer_text}
        ]

        # Increment clarification round
        updated_session = session.model_copy(
            update={
                "intent": updated_intent,
                "clarification_round": session.clarification_round + 1,
                "clarification_history": updated_history,
            }
        )

        return await self._decide_and_respond(updated_session)

    async def get_session(self, session_id: str) -> IntentSession | None:
        """Retrieve a session by its identifier.

        Parameters
        ----------
        session_id:
            The UUID of the session.

        Returns
        -------
        Optional[IntentSession]
            The session or None.
        """
        return await self._persistence.load_session(session_id)

    async def get_intent(self, project_id: str) -> ProjectIntent | None:
        """Retrieve the validated project intent.

        Parameters
        ----------
        project_id:
            The FORGE project identifier.

        Returns
        -------
        Optional[ProjectIntent]
            The validated intent, or None if not yet available.
        """
        return await self._persistence.load_intent(project_id)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session from all stores.

        Parameters
        ----------
        session_id:
            The session to remove.

        Returns
        -------
        bool
            True if the session was found and deleted.
        """
        return await self._persistence.delete_session(session_id)

    # ---------------------------------------------------------------------- #
    # Core decision logic
    # ---------------------------------------------------------------------- #

    async def _decide_and_respond(self, session: IntentSession) -> IntentParseResponse:
        """Determine whether to ask for clarification or complete the intent.

        Decision tree:
        - confidence < threshold AND rounds < max → generate questions
        - confidence >= threshold OR max rounds reached → validate and finalise
        """
        intent = session.intent
        round_num = session.clarification_round
        confidence = intent.confidence_score

        needs_clarification = (
            confidence < _CONFIDENCE_THRESHOLD
            and round_num < MAX_INTENT_CLARIFICATION_ROUNDS
            and bool(intent.missing_fields)
        )

        if needs_clarification:
            return await self._build_clarification_response(session)
        else:
            return await self._build_complete_response(session)

    async def _build_clarification_response(self, session: IntentSession) -> IntentParseResponse:
        """Generate clarification questions and persist the session state."""
        questions = await self._clarifier.generate_questions(
            intent=session.intent,
            round_num=session.clarification_round,
        )

        # Format the clarification request for the API response
        clarification_request = ClarificationRequest(
            session_id=session.session_id,
            questions=questions,
            context=(
                f"To build a great {session.intent.product_type or 'product'} for you, "
                f"I need a few more details (round {session.clarification_round + 1} of "
                f"{MAX_INTENT_CLARIFICATION_ROUNDS})."
            ),
        )

        # Record the assistant's questions in history
        question_text = "\n".join(f"Q: {q.question}" for q in questions)
        updated_history = session.clarification_history + [
            {"role": "assistant", "content": question_text}
        ]

        updated_session = session.model_copy(
            update={
                "status": IntentStatus.CLARIFYING,
                "clarification_history": updated_history,
            }
        )

        # Persist the in-progress session
        await self._persistence.save_session(updated_session)

        self._log.info(
            "clarification_requested",
            session_id=session.session_id,
            round=session.clarification_round,
            question_count=len(questions),
            confidence=session.intent.confidence_score,
        )

        return IntentParseResponse(
            session_id=session.session_id,
            project_id=session.project_id,
            intent=session.intent,
            clarification_needed=True,
            clarification_request=clarification_request,
            is_complete=False,
        )

    async def _build_complete_response(self, session: IntentSession) -> IntentParseResponse:
        """Validate and persist the final intent, then return the complete response."""
        self._log.info(
            "finalising_intent",
            session_id=session.session_id,
            confidence=session.intent.confidence_score,
        )

        # Run validation (raises IntentError on critical failures)
        try:
            validation_result = await self._validator.validate(session.intent)
        except IntentError:
            # Validation raised — if there are still missing fields, attempt one
            # more clarification round before propagating the error.
            if (
                session.intent.missing_fields
                and session.clarification_round < MAX_INTENT_CLARIFICATION_ROUNDS
            ):
                return await self._build_clarification_response(session)
            raise

        # Mark intent as validated
        validated_intent = session.intent.model_copy(update={"status": IntentStatus.VALIDATED})
        validated_session = session.model_copy(
            update={
                "intent": validated_intent,
                "status": IntentStatus.VALIDATED,
            }
        )

        # Persist session and standalone intent
        await self._persistence.save_session(validated_session)
        await self._persistence.save_intent(session.project_id, validated_intent)

        self._log.info(
            "intent_complete",
            session_id=session.session_id,
            project_id=session.project_id,
            confidence=validated_intent.confidence_score,
            warnings=validation_result.warnings,
        )

        return IntentParseResponse(
            session_id=session.session_id,
            project_id=session.project_id,
            intent=validated_intent,
            clarification_needed=False,
            clarification_request=None,
            is_complete=True,
        )

    # ---------------------------------------------------------------------- #
    # Session management helpers
    # ---------------------------------------------------------------------- #

    async def _get_or_create_session(self, request: IntentParseRequest) -> IntentSession:
        """Load an existing session or create a new one."""
        if request.session_id:
            session = await self._persistence.load_session(request.session_id)
            if session is not None:
                self._log.debug("session_resumed", session_id=request.session_id)
                return session
            self._log.warning(
                "session_not_found_creating_new",
                requested_session_id=request.session_id,
            )

        session_id = str(uuid.uuid4())
        project_id = request.project_id or str(uuid.uuid4())

        # Minimal placeholder intent so the session can be constructed
        placeholder_intent = ProjectIntent(
            raw_prompt=request.prompt,
            status=IntentStatus.DRAFT,
        )

        session = IntentSession(
            session_id=session_id,
            project_id=project_id,
            raw_prompt=request.prompt,
            intent=placeholder_intent,
            clarification_round=0,
            clarification_history=[],
            status=IntentStatus.DRAFT,
        )

        self._log.info(
            "session_created",
            session_id=session_id,
            project_id=project_id,
        )
        return session

    def _merge_intents(
        self,
        existing: ProjectIntent,
        parsed: ProjectIntent,
        raw_prompt: str,
    ) -> ProjectIntent:
        """Merge a freshly-parsed intent with existing session intent data.

        The parsed intent takes precedence for non-empty fields.  The existing
        intent fills in any fields the new parse left empty.  This preserves
        answers from previous clarification rounds.
        """
        existing_data = existing.model_dump()
        parsed_data = parsed.model_dump()

        merged: dict = {}

        list_fields = {"core_features", "constraints", "integrations", "security_requirements"}
        dict_fields = {"tech_preferences"}
        skip_fields = {"raw_prompt", "status", "confidence_score", "missing_fields"}

        for key in existing_data:
            if key in skip_fields:
                continue

            parsed_val = parsed_data.get(key)
            existing_val = existing_data.get(key)

            if key in list_fields:
                parsed_list = parsed_val if isinstance(parsed_val, list) else []
                existing_list = existing_val if isinstance(existing_val, list) else []
                # Union: prefer parsed items, backfill with any extras from existing
                combined = list(parsed_list)
                for item in existing_list:
                    if item not in combined:
                        combined.append(item)
                merged[key] = combined
            elif key in dict_fields:
                existing_dict = existing_val if isinstance(existing_val, dict) else {}
                parsed_dict = parsed_val if isinstance(parsed_val, dict) else {}
                merged_dict = {**existing_dict, **parsed_dict}  # parsed wins
                merged[key] = merged_dict
            else:
                # Scalar: use parsed value if non-empty, else fall back to existing
                if parsed_val and str(parsed_val).strip():
                    merged[key] = parsed_val
                else:
                    merged[key] = existing_val

        merged["raw_prompt"] = raw_prompt
        merged["status"] = str(parsed_data.get("status", IntentStatus.DRAFT))

        try:
            intent = ProjectIntent.model_validate(merged)
        except Exception:
            intent = parsed  # Fallback: use parse result as-is

        # Re-enrich after merge
        return self._parser._enrich(intent)  # noqa: SLF001
