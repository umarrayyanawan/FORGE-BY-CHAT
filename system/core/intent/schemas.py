"""Pydantic schemas for the FORGE Intent Engine.

These schemas define the full data contract for converting raw user prompts
into structured, validated project intent objects.
"""

from __future__ import annotations

from enum import StrEnum
import json
from typing import Any

from pydantic import Field, field_validator, model_validator

from system.shared.models import BaseForgeModel, DeployTarget, Platform, TimestampedModel

# ========================================================================== #
# Status enum
# ========================================================================== #


class IntentStatus(StrEnum):
    """Lifecycle states for an intent session."""

    DRAFT = "draft"
    CLARIFYING = "clarifying"
    COMPLETE = "complete"
    VALIDATED = "validated"


# ========================================================================== #
# Core intent model
# ========================================================================== #


class ProjectIntent(BaseForgeModel):
    """Structured representation of a user's project intent.

    Extracted from a raw free-text prompt via LLM analysis.  Fields are
    progressively populated through clarification rounds until the intent
    reaches sufficient completeness (confidence_score >= 0.7).
    """

    raw_prompt: str = Field(..., description="The original, unmodified user prompt.")

    # Product classification
    industry: str = Field(
        default="",
        description=(
            "Business domain / industry vertical, e.g. 'real estate', 'healthcare', 'e-commerce'."
        ),
    )
    product_type: str = Field(
        default="",
        description=(
            "Category of software being built, e.g. 'CRM', 'SaaS dashboard', "
            "'mobile app', 'REST API', 'CLI tool'."
        ),
    )

    # Platform & deployment
    platform: Platform = Field(
        default=Platform.WEB,
        description="Primary deployment surface: web, mobile, desktop, cli, api.",
    )
    deployment_target: DeployTarget = Field(
        default=DeployTarget.DOCKER,
        description="Target infrastructure for the produced artefacts.",
    )

    # Functional requirements
    core_features: list[str] = Field(
        default_factory=list,
        description="Ordered list of the most important features the system must have.",
    )
    integrations: list[str] = Field(
        default_factory=list,
        description=(
            "External services / APIs to integrate with, e.g. 'Stripe', 'Twilio', 'Google OAuth'."
        ),
    )

    # Non-functional requirements
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Hard constraints, e.g. 'must run offline', 'GDPR compliant', "
            "'no JavaScript frameworks'."
        ),
    )
    security_requirements: list[str] = Field(
        default_factory=list,
        description="Security controls required, e.g. 'MFA', 'row-level security', 'SOC 2'.",
    )
    scale_requirements: str = Field(
        default="",
        description=(
            "Expected load / scale, e.g. '10,000 concurrent users', "
            "'1 million records', 'sub-100 ms latency'."
        ),
    )

    # Users & stakeholders
    target_users: str = Field(
        default="",
        description=(
            "Who will use the system, e.g. 'marble suppliers and their sales teams', "
            "'internal ops team of ~50 people'."
        ),
    )

    # Technical preferences
    tech_preferences: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Preferred technologies keyed by role, e.g. "
            "{'backend': 'FastAPI', 'frontend': 'React', 'database': 'PostgreSQL'}."
        ),
    )

    # Planning / commercial
    timeline: str = Field(
        default="",
        description="Desired delivery timeline, e.g. '3 months', 'MVP in 6 weeks'.",
    )
    budget_range: str = Field(
        default="",
        description="Budget envelope, e.g. '$50k-$100k', 'bootstrap / zero cost'.",
    )

    # Meta
    status: IntentStatus = Field(
        default=IntentStatus.DRAFT,
        description="Lifecycle status of this intent object.",
    )
    confidence_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Completeness score 0–1 computed from how many required fields are "
            "populated.  >= 0.7 is considered sufficient to proceed."
        ),
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="Field names that are empty and considered important.",
    )

    @field_validator("confidence_score", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        """Ensure the confidence score stays within [0, 1]."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @field_validator(
        "core_features", "constraints", "integrations", "security_requirements", mode="before"
    )
    @classmethod
    def ensure_string_list(cls, v: Any) -> list[str]:
        """Coerce JSON arrays and CSV strings into a clean list of strings."""
        if v is None:
            return []
        if isinstance(v, str):
            # Try JSON first
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if item]
            except json.JSONDecodeError:
                pass
            # Fall back to comma-separated
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, list):
            return [str(item).strip() for item in v if item]
        return []

    def schema_json_for_prompt(self) -> str:
        """Return a compact JSON schema description for embedding in LLM prompts."""
        return json.dumps(
            {
                "raw_prompt": "string (the original prompt, pass through unchanged)",
                "industry": "string",
                "product_type": "string",
                "platform": ["web", "mobile", "desktop", "cli", "api"],
                "deployment_target": ["docker", "kubernetes", "vercel", "railway", "aws", "gcp"],
                "core_features": ["string", "..."],
                "integrations": ["string", "..."],
                "constraints": ["string", "..."],
                "security_requirements": ["string", "..."],
                "scale_requirements": "string",
                "target_users": "string",
                "tech_preferences": {"role": "technology"},
                "timeline": "string",
                "budget_range": "string",
            },
            indent=2,
        )


# ========================================================================== #
# Session model
# ========================================================================== #


class IntentSession(TimestampedModel):
    """Full state of one clarification session for a given project.

    Persisted in Redis (hot path) and Postgres (durable store).
    """

    session_id: str = Field(..., description="Unique session identifier (UUID).")
    project_id: str = Field(..., description="Associated FORGE project identifier.")
    raw_prompt: str = Field(..., description="Original prompt that started this session.")
    intent: ProjectIntent = Field(..., description="Current best-effort structured intent.")
    clarification_round: int = Field(
        default=0,
        ge=0,
        description="How many clarification rounds have been completed.",
    )
    clarification_history: list[dict[str, str]] = Field(
        default_factory=list,
        description=("Ordered conversation log [{role: 'user'|'assistant', content: str}]."),
    )
    status: IntentStatus = Field(
        default=IntentStatus.DRAFT,
        description="Mirror of intent.status for quick session-level queries.",
    )


# ========================================================================== #
# Clarification schemas
# ========================================================================== #


class ClarificationQuestion(BaseForgeModel):
    """A single targeted question to resolve an ambiguous intent field."""

    question: str = Field(..., description="Human-readable question text.")
    field: str = Field(
        ...,
        description="Name of the ProjectIntent field this question clarifies.",
    )
    options: list[str] | None = Field(
        default=None,
        description="Optional multiple-choice options to present to the user.",
    )
    required: bool = Field(
        default=True,
        description="If True, skipping this question lowers completeness significantly.",
    )

    @field_validator("options", mode="before")
    @classmethod
    def filter_empty_options(cls, v: Any) -> list[str] | None:
        if v is None:
            return None
        filtered = [str(o).strip() for o in v if str(o).strip()]
        return filtered if filtered else None


class ClarificationRequest(BaseForgeModel):
    """Outbound payload sent to the user when more information is needed."""

    session_id: str = Field(..., description="Session this clarification belongs to.")
    questions: list[ClarificationQuestion] = Field(
        ...,
        min_length=1,
        description="Ordered list of questions (max 3 per round).",
    )
    context: str = Field(
        default="",
        description="Optional framing text shown above the questions.",
    )

    @model_validator(mode="after")
    def cap_questions(self) -> ClarificationRequest:
        """Enforce the 3-question-per-round limit at the schema level."""
        if len(self.questions) > 3:
            object.__setattr__(self, "questions", self.questions[:3])
        return self


class ClarificationResponse(BaseForgeModel):
    """Inbound payload submitted by the user with their answers."""

    session_id: str = Field(..., description="Session this response belongs to.")
    answers: dict[str, str] = Field(
        ...,
        description="Mapping of field name → user's answer string.",
    )

    @field_validator("answers", mode="before")
    @classmethod
    def strip_answers(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k).strip(): str(val).strip() for k, val in v.items() if val}


# ========================================================================== #
# Request / Response schemas for the API
# ========================================================================== #


class IntentParseRequest(BaseForgeModel):
    """Request body for the POST /intent/parse endpoint."""

    prompt: str = Field(..., min_length=5, description="Raw user prompt to analyse.")
    project_id: str | None = Field(
        default=None,
        description="If provided, links this session to an existing FORGE project.",
    )
    session_id: str | None = Field(
        default=None,
        description="If provided, resumes an existing session.",
    )


class IntentParseResponse(BaseForgeModel):
    """Response body returned by /intent/parse and /intent/clarify."""

    session_id: str = Field(..., description="Session identifier for subsequent calls.")
    project_id: str = Field(..., description="Associated project identifier.")
    intent: ProjectIntent = Field(..., description="Current structured intent.")
    clarification_needed: bool = Field(
        ...,
        description="True when the intent is incomplete and questions have been generated.",
    )
    clarification_request: ClarificationRequest | None = Field(
        default=None,
        description="Questions to present to the user (populated when clarification_needed=True).",
    )
    is_complete: bool = Field(
        default=False,
        description=(
            "True when the intent passes validation and is ready for the Specification phase."
        ),
    )
