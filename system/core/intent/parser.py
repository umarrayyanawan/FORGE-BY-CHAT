"""LLM-powered intent parser for the FORGE Intent Engine.

Converts a raw free-text user prompt into a fully-structured
:class:`ProjectIntent` by calling the configured LLM with a structured
extraction prompt and then scoring the result for completeness.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, DEFAULT_LLM_TEMPERATURE
from system.shared.exceptions import IntentError
from system.shared.llm_client import LLMMessage, LLMResponse, get_llm_client

from .schemas import IntentStatus, ProjectIntent

logger = get_logger(__name__)

# =========================================================================== #
# Required-field weights used to compute the confidence score.
# Each entry is (field_name, weight).  Weights should sum to ≤ 1.0 so the
# confidence score is naturally bounded; anything un-weighted contributes a
# small bonus.
# =========================================================================== #

_FIELD_WEIGHTS: List[Tuple[str, float]] = [
    ("industry", 0.15),
    ("product_type", 0.15),
    ("core_features", 0.20),
    ("target_users", 0.10),
    ("platform", 0.08),
    ("deployment_target", 0.05),
    ("scale_requirements", 0.07),
    ("security_requirements", 0.05),
    ("integrations", 0.05),
    ("tech_preferences", 0.05),
    ("timeline", 0.03),
    ("budget_range", 0.02),
]

# Platform & DeployTarget default values — if the model returns these unchanged
# it doesn't necessarily mean they were *specified*, but we still give partial
# credit because we have a working default.
_LIST_FIELDS = {"core_features", "constraints", "integrations", "security_requirements"}
_DICT_FIELDS = {"tech_preferences"}


class IntentParser:
    """Extracts :class:`ProjectIntent` fields from a natural-language prompt.

    Parameters
    ----------
    llm_client:
        Pre-initialised LLM client obtained from :func:`get_llm_client`.
        The parser owns none of the client's lifecycle.
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client
        self._log = logger

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def parse(self, prompt: str) -> ProjectIntent:
        """Parse a raw user prompt into a structured :class:`ProjectIntent`.

        The method calls the LLM with a highly-specific extraction system
        prompt, parses the JSON response, then enriches it with a confidence
        score and a list of missing fields.

        Parameters
        ----------
        prompt:
            The raw, unmodified user prompt.

        Returns
        -------
        ProjectIntent
            Populated intent object.  ``confidence_score`` and
            ``missing_fields`` are always set on return.

        Raises
        ------
        IntentError
            When the LLM call fails or returns unparseable output.
        """
        self._log.info("parsing_intent", prompt_length=len(prompt))

        system_prompt = self._build_parse_prompt(prompt)

        try:
            messages: List[LLMMessage] = [
                LLMMessage(role="user", content=prompt),
            ]
            response: LLMResponse = await self._llm.complete(
                messages=messages,
                system=system_prompt,
                model=DEFAULT_LLM_MODEL,
                temperature=DEFAULT_LLM_TEMPERATURE,
                max_tokens=2048,
            )
        except Exception as exc:
            self._log.error("llm_parse_failed", error=str(exc))
            raise IntentError(
                f"LLM intent extraction failed: {exc}",
                details={"prompt_length": len(prompt)},
            ) from exc

        raw_text = response.content if hasattr(response, "content") else str(response)
        intent = self._parse_llm_response(raw_text, prompt)
        intent = self._enrich(intent)

        self._log.info(
            "intent_parsed",
            confidence=intent.confidence_score,
            missing=intent.missing_fields,
        )
        return intent

    # ---------------------------------------------------------------------- #
    # Prompt construction
    # ---------------------------------------------------------------------- #

    def _build_parse_prompt(self, prompt: str) -> str:
        """Construct the highly-detailed system prompt for intent extraction."""
        # Build a compact inline schema description from the model itself so it
        # stays in sync with the actual Pydantic schema.
        sample = ProjectIntent(raw_prompt="__placeholder__")
        schema_desc = sample.schema_json_for_prompt()

        return f"""You are an expert software requirements analyst for FORGE, an autonomous software production system.

Your task is to extract structured project intent from the user's free-text description.

## Output format
Respond ONLY with a single, valid JSON object — no markdown, no code fences, no explanatory text.
The JSON must match this exact schema:

{schema_desc}

## Extraction rules

1. **raw_prompt** — copy the user's prompt verbatim, do not modify it.

2. **industry** — identify the business domain (e.g. "construction materials", "healthcare",
   "fintech", "e-commerce", "logistics"). Use specific sub-domains when possible. If unclear,
   infer from context.

3. **product_type** — classify the type of software being built.
   Examples: "CRM", "ERP", "SaaS platform", "mobile app", "REST API", "CLI tool",
   "internal dashboard", "marketplace", "e-commerce store", "analytics platform".

4. **platform** — one of: "web", "mobile", "desktop", "cli", "api".
   Default to "web" when unclear.

5. **deployment_target** — one of: "docker", "kubernetes", "vercel", "railway", "aws", "gcp".
   Choose based on context clues; default to "docker" when unspecified.

6. **core_features** — extract a list of the 3–8 most important features.
   Each item should be a concise action phrase, e.g.:
   "Contact and lead management", "Quote generation and approval workflow",
   "Inventory tracking", "Role-based access control".
   Infer reasonable features if the prompt is vague (e.g. a CRM always needs contact management).

7. **integrations** — list third-party services/APIs mentioned or strongly implied.
   Examples: "Stripe", "QuickBooks", "Google OAuth", "Twilio SMS", "Slack notifications".
   Leave empty array if none implied.

8. **constraints** — hard constraints on the system.
   Examples: "must run on-premises", "no cloud storage", "must comply with GDPR",
   "must support Arabic RTL", "single-tenant only".

9. **security_requirements** — explicit or implied security controls.
   Examples: "Multi-factor authentication", "Row-level security per tenant",
   "Data encryption at rest", "Audit logging for all mutations", "RBAC with admin/user roles".

10. **scale_requirements** — expected load, data volume, latency targets.
    Examples: "Up to 500 concurrent users", "10 million product records",
    "Sub-200ms API response time", "Handle 1000 orders/day".
    Leave empty string if unspecified.

11. **target_users** — who will use the system and in what capacity.
    Examples: "Marble suppliers and their inside sales teams",
    "Small business owners managing up to 5 staff",
    "Healthcare administrators and nursing staff at mid-size clinics".

12. **tech_preferences** — any technology preferences explicitly stated.
    Keys: "backend", "frontend", "database", "cache", "queue", "mobile".
    Only populate keys that the user explicitly mentioned; leave object empty otherwise.

13. **timeline** — desired delivery window.
    Examples: "MVP in 6 weeks", "Full launch in 3 months", "ASAP".
    Leave empty string if unspecified.

14. **budget_range** — budget envelope if mentioned.
    Examples: "$20k–$50k", "bootstrapped / zero cost", "enterprise budget > $500k".
    Leave empty string if unspecified.

## Quality standards
- Be specific, not generic.  "Manage contacts" is better than "data management".
- Infer sensible defaults where context makes them obvious.
- Do NOT invent features or constraints that have no basis in the prompt.
- Return every key in the schema even when the value is an empty string or empty list.

## User prompt to analyse
\"\"\"{prompt}\"\"\"
"""

    # ---------------------------------------------------------------------- #
    # Response parsing
    # ---------------------------------------------------------------------- #

    def _parse_llm_response(self, raw: str, original_prompt: str) -> ProjectIntent:
        """Extract and validate the JSON from the LLM's raw response string.

        Handles:
        - Pure JSON responses
        - JSON wrapped in markdown code fences
        - Responses with leading/trailing prose
        """
        json_str = self._extract_json(raw)

        try:
            data: Dict[str, Any] = json.loads(json_str)
        except json.JSONDecodeError as exc:
            self._log.warning(
                "json_parse_failed",
                error=str(exc),
                raw_snippet=raw[:200],
            )
            # Return a minimal intent so the clarification engine can fill gaps
            return ProjectIntent(
                raw_prompt=original_prompt,
                status=IntentStatus.DRAFT,
            )

        # Ensure raw_prompt is always preserved
        data["raw_prompt"] = original_prompt

        try:
            intent = ProjectIntent.model_validate(data)
        except Exception as exc:
            self._log.warning("intent_validation_failed", error=str(exc))
            # Attempt a lenient partial parse
            intent = self._lenient_parse(data, original_prompt)

        return intent

    def _extract_json(self, text: str) -> str:
        """Pull the first JSON object out of an arbitrary string."""
        # Try code fence extraction first (```json ... ```)
        fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
        fence_match = fence_pattern.search(text)
        if fence_match:
            return fence_match.group(1).strip()

        # Try to find the outermost {...} block
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            return brace_match.group(0).strip()

        return text.strip()

    def _lenient_parse(self, data: Dict[str, Any], prompt: str) -> ProjectIntent:
        """Build a ProjectIntent from partially valid data, skipping bad fields."""
        safe: Dict[str, Any] = {"raw_prompt": prompt}
        string_fields = [
            "industry", "product_type", "scale_requirements",
            "target_users", "timeline", "budget_range",
        ]
        for field in string_fields:
            val = data.get(field)
            if isinstance(val, str):
                safe[field] = val

        enum_fields = {"platform": "web", "deployment_target": "docker"}
        for field, default in enum_fields.items():
            val = data.get(field)
            if isinstance(val, str) and val:
                safe[field] = val
            else:
                safe[field] = default

        for field in _LIST_FIELDS:
            val = data.get(field)
            if isinstance(val, list):
                safe[field] = [str(v) for v in val if v]

        tech = data.get("tech_preferences")
        if isinstance(tech, dict):
            safe["tech_preferences"] = {str(k): str(v) for k, v in tech.items()}

        return ProjectIntent.model_validate(safe)

    # ---------------------------------------------------------------------- #
    # Confidence scoring & missing-field detection
    # ---------------------------------------------------------------------- #

    def _calculate_confidence(self, intent: ProjectIntent) -> float:
        """Return a 0–1 completeness score based on populated fields.

        Each field carries a weight.  The score is the sum of weights for
        non-empty fields, capped at 1.0.
        """
        score = 0.0
        for field_name, weight in _FIELD_WEIGHTS:
            val = getattr(intent, field_name, None)
            if self._field_is_populated(field_name, val):
                score += weight
        return round(min(score, 1.0), 4)

    def _identify_missing_fields(self, intent: ProjectIntent) -> List[str]:
        """Return names of important fields that are empty or default."""
        missing: List[str] = []
        for field_name, _ in _FIELD_WEIGHTS:
            val = getattr(intent, field_name, None)
            if not self._field_is_populated(field_name, val):
                missing.append(field_name)
        return missing

    def _field_is_populated(self, field_name: str, value: Any) -> bool:
        """Return True when the field has a meaningful non-default value."""
        if value is None:
            return False
        if field_name in _LIST_FIELDS:
            return isinstance(value, list) and len(value) > 0
        if field_name in _DICT_FIELDS:
            return isinstance(value, dict) and len(value) > 0
        if isinstance(value, str):
            return bool(value.strip())
        # Enum values always have a string representation — treat any non-empty
        # string as populated.
        return bool(str(value).strip())

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _enrich(self, intent: ProjectIntent) -> ProjectIntent:
        """Attach confidence_score and missing_fields to an intent in place."""
        confidence = self._calculate_confidence(intent)
        missing = self._identify_missing_fields(intent)

        status = IntentStatus.DRAFT
        if confidence >= 0.7:
            status = IntentStatus.COMPLETE
        elif confidence > 0.0:
            status = IntentStatus.CLARIFYING

        return intent.model_copy(
            update={
                "confidence_score": confidence,
                "missing_fields": missing,
                "status": status,
            }
        )
