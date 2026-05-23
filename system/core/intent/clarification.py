"""Clarification engine for the FORGE Intent Engine.

Generates targeted follow-up questions when an initial intent parse yields
low confidence, then applies user answers to update and re-score the intent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, DEFAULT_LLM_TEMPERATURE
from system.shared.exceptions import IntentError
from system.shared.llm_client import LLMMessage, LLMResponse, get_llm_client

from .schemas import ClarificationQuestion, IntentStatus, ProjectIntent

logger = get_logger(__name__)

# =========================================================================== #
# Human-readable field labels and priority ordering.
# =========================================================================== #

CLARIFICATION_FIELD_MAP: Dict[str, str] = {
    "industry": "What industry or business domain does this product serve?",
    "product_type": "What type of software product is this (e.g. CRM, SaaS platform, mobile app)?",
    "core_features": "What are the 3–5 most important features this product must have?",
    "target_users": "Who are the primary users of this system and what are their main tasks?",
    "platform": "Which platform should this run on — web, mobile, desktop, CLI, or API-only?",
    "deployment_target": (
        "Where should this be deployed — Docker/self-hosted, Kubernetes, Vercel, Railway, AWS, or GCP?"
    ),
    "scale_requirements": (
        "What are the expected scale requirements (e.g. concurrent users, data volume, latency targets)?"
    ),
    "security_requirements": (
        "Are there specific security or compliance requirements (e.g. MFA, GDPR, RBAC, SOC 2)?"
    ),
    "integrations": "Which third-party services or APIs should this integrate with?",
    "tech_preferences": "Do you have preferences for specific technologies (backend, frontend, database)?",
    "timeline": "What is the desired delivery timeline or deadline?",
    "budget_range": "What is the budget range or commercial context for this project?",
    "constraints": "Are there any hard constraints (e.g. must run offline, no specific framework)?",
}

# Priority ordering — fields asked first when multiple are missing.
_PRIORITY_ORDER: List[str] = [
    "industry",
    "product_type",
    "core_features",
    "target_users",
    "platform",
    "scale_requirements",
    "security_requirements",
    "deployment_target",
    "integrations",
    "tech_preferences",
    "timeline",
    "budget_range",
    "constraints",
]

# Suggested multiple-choice options for fields where they make sense.
_FIELD_OPTIONS: Dict[str, List[str]] = {
    "platform": ["Web (browser-based)", "Mobile (iOS/Android)", "Desktop (Electron/native)", "CLI (command-line)", "API-only (headless service)"],
    "deployment_target": ["Docker / self-hosted", "Kubernetes (k8s)", "Vercel (serverless)", "Railway", "AWS", "GCP"],
    "tech_preferences": [],  # too open-ended for canned options
}


class ClarificationEngine:
    """Drives the interactive clarification loop.

    Generates smart follow-up questions from a partial :class:`ProjectIntent`
    and merges user answers back into the intent object.

    Parameters
    ----------
    llm_client:
        Pre-initialised LLM client obtained from :func:`get_llm_client`.
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client
        self._log = logger

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def generate_questions(
        self,
        intent: ProjectIntent,
        round_num: int,
    ) -> List[ClarificationQuestion]:
        """Generate up to 3 clarifying questions for the highest-priority gaps.

        First attempts LLM-driven question generation for richer, context-aware
        questions.  Falls back to deterministic rule-based questions if the LLM
        call fails.

        Parameters
        ----------
        intent:
            Current (partial) intent object.  ``missing_fields`` must be
            populated before calling this method.
        round_num:
            Current clarification round (0-indexed).  Used to vary phrasing
            across rounds and avoid repetition.

        Returns
        -------
        List[ClarificationQuestion]
            1–3 questions, ordered by importance.
        """
        self._log.info(
            "generating_clarification_questions",
            round=round_num,
            missing_count=len(intent.missing_fields),
        )

        if not intent.missing_fields:
            return []

        # Prioritise by the canonical ordering
        ordered_missing = self._prioritise(intent.missing_fields)
        # Max 3 per round
        to_ask = ordered_missing[:3]

        try:
            questions = await self._llm_generate(intent, to_ask, round_num)
        except Exception as exc:
            self._log.warning("llm_question_gen_failed", error=str(exc))
            questions = self._rule_based_questions(intent, to_ask)

        self._log.info("questions_generated", count=len(questions))
        return questions[:3]

    async def apply_answers(
        self,
        intent: ProjectIntent,
        answers: Dict[str, str],
    ) -> ProjectIntent:
        """Merge user answers into the intent and recompute confidence.

        Parameters
        ----------
        intent:
            The current intent that is being enriched.
        answers:
            Mapping of field_name → user's answer string.

        Returns
        -------
        ProjectIntent
            Updated intent with ``confidence_score`` and ``missing_fields``
            recomputed.
        """
        self._log.info("applying_answers", fields=list(answers.keys()))

        updated_data = intent.model_dump()

        for field, answer in answers.items():
            answer = answer.strip()
            if not answer or field not in CLARIFICATION_FIELD_MAP:
                continue

            field_type = self._resolve_field_type(field)

            if field_type == "list":
                existing: List[str] = updated_data.get(field, []) or []
                new_items = self._split_list_answer(answer)
                # Merge without duplicates, preserving order
                merged = list(existing)
                for item in new_items:
                    if item and item not in merged:
                        merged.append(item)
                updated_data[field] = merged

            elif field_type == "dict":
                existing_dict: Dict[str, str] = updated_data.get(field, {}) or {}
                parsed = self._parse_tech_preferences(answer)
                existing_dict.update(parsed)
                updated_data[field] = existing_dict

            else:
                # Plain string or enum — direct assignment
                updated_data[field] = answer

        try:
            new_intent = ProjectIntent.model_validate(updated_data)
        except Exception as exc:
            self._log.error("answer_apply_failed", error=str(exc))
            raise IntentError(
                f"Failed to apply clarification answers: {exc}",
                details={"fields": list(answers.keys())},
            ) from exc

        # Re-score
        from .parser import IntentParser

        # Lightweight re-enrichment without a new LLM call
        parser = IntentParser(llm_client=self._llm)
        return parser._enrich(new_intent)  # noqa: SLF001  (internal cross-module call)

    # ---------------------------------------------------------------------- #
    # LLM-based question generation
    # ---------------------------------------------------------------------- #

    async def _llm_generate(
        self,
        intent: ProjectIntent,
        fields_to_ask: List[str],
        round_num: int,
    ) -> List[ClarificationQuestion]:
        """Call the LLM to produce contextually-aware clarification questions."""
        system_prompt = self._build_clarification_prompt(intent, fields_to_ask, round_num)
        messages: List[LLMMessage] = [
            LLMMessage(
                role="user",
                content=(
                    "Generate clarification questions for the following missing intent fields: "
                    + ", ".join(fields_to_ask)
                ),
            )
        ]
        response: LLMResponse = await self._llm.complete(
            messages=messages,
            system=system_prompt,
            model=DEFAULT_LLM_MODEL,
            temperature=0.3,  # Slightly higher for varied phrasing
            max_tokens=1024,
        )
        raw = response.content if hasattr(response, "content") else str(response)
        return self._parse_questions_response(raw, fields_to_ask)

    def _build_clarification_prompt(
        self,
        intent: ProjectIntent,
        missing: List[str],
        round_num: int,
    ) -> str:
        """Construct the system prompt for LLM-driven question generation."""
        context_parts: List[str] = []
        if intent.industry:
            context_parts.append(f"Industry: {intent.industry}")
        if intent.product_type:
            context_parts.append(f"Product type: {intent.product_type}")
        if intent.core_features:
            context_parts.append(f"Known features: {', '.join(intent.core_features[:3])}")
        if intent.target_users:
            context_parts.append(f"Target users: {intent.target_users}")

        context_summary = "\n".join(context_parts) if context_parts else "Minimal context available."

        field_descriptions = "\n".join(
            f"- {field}: {CLARIFICATION_FIELD_MAP.get(field, field)}"
            for field in missing
        )

        options_hints: List[str] = []
        for field in missing:
            opts = _FIELD_OPTIONS.get(field, [])
            if opts:
                options_hints.append(
                    f"For '{field}', suggest these options: {', '.join(opts)}"
                )
        options_text = "\n".join(options_hints) if options_hints else ""

        return f"""You are an expert requirements analyst for FORGE, an autonomous software production system.

## Context
A user has submitted a project request.  You have partially extracted their intent.

## What is already known
{context_summary}

## Original prompt
"{intent.raw_prompt}"

## Missing information (clarification round {round_num + 1})
You need to ask about these fields:
{field_descriptions}

{options_text}

## Your task
Generate EXACTLY {len(missing)} clarification questions — one per missing field listed above.

Respond with a JSON array.  Each element must have:
{{
  "question": "The question text, personalised to the context above",
  "field": "the exact field name from the missing list",
  "options": ["option1", "option2"] or null if free-text is better,
  "required": true or false
}}

Rules:
- Make questions specific and contextual — reference the industry or product type when helpful.
- Keep each question short (one sentence).
- Only provide options when the field has a finite set of sensible choices.
- Do NOT ask about fields that are already populated.
- Respond ONLY with the JSON array — no prose, no code fences.
"""

    def _parse_questions_response(
        self,
        raw: str,
        expected_fields: List[str],
    ) -> List[ClarificationQuestion]:
        """Parse the LLM JSON array into ClarificationQuestion objects."""
        # Strip code fences if present
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        # Find the array
        arr_match = re.search(r"\[[\s\S]*\]", cleaned)
        if not arr_match:
            self._log.warning("question_parse_no_array", raw=raw[:200])
            return self._rule_based_questions(None, expected_fields)

        try:
            items: List[Dict[str, Any]] = json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            self._log.warning("question_parse_json_error", raw=raw[:200])
            return self._rule_based_questions(None, expected_fields)

        questions: List[ClarificationQuestion] = []
        seen_fields: set[str] = set()

        for item in items:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field", "")).strip()
            question_text = str(item.get("question", "")).strip()
            if not field or not question_text:
                continue
            if field in seen_fields:
                continue
            seen_fields.add(field)

            options = item.get("options")
            if options is not None and not isinstance(options, list):
                options = None

            questions.append(
                ClarificationQuestion(
                    question=question_text,
                    field=field,
                    options=options if options else None,
                    required=bool(item.get("required", True)),
                )
            )

        if not questions:
            return self._rule_based_questions(None, expected_fields)

        return questions

    # ---------------------------------------------------------------------- #
    # Rule-based fallback
    # ---------------------------------------------------------------------- #

    def _rule_based_questions(
        self,
        intent: Optional[ProjectIntent],
        fields: List[str],
    ) -> List[ClarificationQuestion]:
        """Build deterministic questions from CLARIFICATION_FIELD_MAP."""
        questions: List[ClarificationQuestion] = []
        for field in fields[:3]:
            question_text = CLARIFICATION_FIELD_MAP.get(
                field, f"Please provide more detail about: {field}"
            )
            options = _FIELD_OPTIONS.get(field) or None
            questions.append(
                ClarificationQuestion(
                    question=question_text,
                    field=field,
                    options=options if options else None,
                    required=True,
                )
            )
        return questions

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    def _prioritise(self, missing: List[str]) -> List[str]:
        """Sort missing fields by priority order."""
        priority_index = {f: i for i, f in enumerate(_PRIORITY_ORDER)}
        return sorted(
            missing,
            key=lambda f: priority_index.get(f, len(_PRIORITY_ORDER)),
        )

    def _resolve_field_type(self, field: str) -> str:
        """Return 'list', 'dict', or 'str' for a given field name."""
        list_fields = {
            "core_features", "constraints", "integrations", "security_requirements", "missing_fields"
        }
        dict_fields = {"tech_preferences"}
        if field in list_fields:
            return "list"
        if field in dict_fields:
            return "dict"
        return "str"

    def _split_list_answer(self, answer: str) -> List[str]:
        """Convert a free-text answer into a list of items."""
        # Try JSON first
        try:
            parsed = json.loads(answer)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x]
        except json.JSONDecodeError:
            pass

        # Split on common delimiters: newlines, semicolons, commas
        for sep in ["\n", ";", ","]:
            if sep in answer:
                return [s.strip() for s in answer.split(sep) if s.strip()]

        # Single item
        return [answer.strip()] if answer.strip() else []

    def _parse_tech_preferences(self, answer: str) -> Dict[str, str]:
        """Parse a tech preferences answer into {role: technology} dict."""
        # Try JSON object
        try:
            parsed = json.loads(answer)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass

        # Heuristic: "FastAPI for backend, React for frontend, PostgreSQL for database"
        prefs: Dict[str, str] = {}
        pattern = re.compile(
            r"(\w[\w\s\.\-]+?)\s+(?:for|as|:)\s+(backend|frontend|database|cache|queue|mobile)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(answer):
            tech = match.group(1).strip()
            role = match.group(2).lower()
            prefs[role] = tech

        if not prefs:
            # Fall back: treat the whole answer as "backend" preference
            prefs["backend"] = answer.strip()

        return prefs
