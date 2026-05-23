"""Intent validator for the FORGE Intent Engine.

Validates a :class:`ProjectIntent` for structural completeness, internal
coherence, and feasibility before handing it off to the Specification phase.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set

from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, DEFAULT_LLM_TEMPERATURE
from system.shared.exceptions import IntentError
from system.shared.llm_client import LLMMessage, LLMResponse, get_llm_client
from system.shared.models import DeployTarget, Platform

from .schemas import ProjectIntent

logger = get_logger(__name__)

# =========================================================================== #
# Validation result
# =========================================================================== #


@dataclass
class ValidationResult:
    """Outcome of running all intent validation checks.

    Attributes
    ----------
    is_valid:
        True when all *critical* checks passed.  Warning-level issues do not
        make is_valid False.
    errors:
        Critical problems that must be resolved before proceeding.
    warnings:
        Non-blocking observations — the pipeline will continue but quality may
        be reduced.
    suggestions:
        Positive improvement suggestions for the user or downstream agents.
    """

    is_valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.is_valid = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_suggestion(self, message: str) -> None:
        self.suggestions.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
        }


# =========================================================================== #
# Constraint conflict pairs
# =========================================================================== #

_CONFLICTING_CONSTRAINTS: List[tuple[str, str, str]] = [
    (
        "offline",
        "real-time",
        "'must run offline' conflicts with real-time sync requirements.",
    ),
    (
        "no cloud",
        "kubernetes",
        "'no cloud storage' conflicts with Kubernetes cloud deployment.",
    ),
    (
        "no javascript",
        "react",
        "'no JavaScript frameworks' conflicts with React (frontend framework).",
    ),
    (
        "no javascript",
        "vue",
        "'no JavaScript frameworks' conflicts with Vue (frontend framework).",
    ),
    (
        "single-tenant",
        "multi-tenant",
        "Single-tenant and multi-tenant requirements conflict.",
    ),
    (
        "gdpr",
        "us only",
        "GDPR compliance with a US-only deployment may conflict with data residency rules.",
    ),
]

# =========================================================================== #
# Platform ↔ DeployTarget compatibility matrix
# =========================================================================== #

_INCOMPATIBLE_DEPLOY: dict[str, Set[str]] = {
    "vercel": {"desktop", "cli"},  # Vercel is web-only
    "railway": {"desktop"},
}


class IntentValidator:
    """Validates a :class:`ProjectIntent` across multiple dimensions.

    Parameters
    ----------
    llm_client:
        Optional LLM client.  When provided, feature-coherence checks use LLM
        analysis.  When None, coherence checks are skipped gracefully.
    """

    def __init__(self, llm_client: Optional[Any] = None) -> None:
        self._llm = llm_client
        self._log = logger

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def validate(self, intent: ProjectIntent) -> ValidationResult:
        """Run all validation checks and return a consolidated result.

        Critical failures are surfaced as errors (``is_valid=False``).
        Non-critical observations become warnings or suggestions.

        Parameters
        ----------
        intent:
            The fully-parsed intent to validate.

        Returns
        -------
        ValidationResult
            Aggregated result with errors, warnings, and suggestions.

        Raises
        ------
        IntentError
            When a critical validation failure is detected that cannot be
            recovered from.
        """
        result = ValidationResult()
        self._log.info("validating_intent", industry=intent.industry, product_type=intent.product_type)

        # --- Structural checks (synchronous) ---
        self._validate_required_fields(intent, result)
        self._validate_deployment_target(intent, result)
        self._validate_constraints_feasibility(intent, result)
        self._validate_feature_list_quality(intent, result)
        self._validate_scale_requirements(intent, result)

        # --- LLM coherence check (async, optional) ---
        if self._llm and intent.core_features and intent.product_type:
            await self._validate_feature_coherence(intent, result)

        # --- Raise on critical failures ---
        if not result.is_valid:
            self._log.warning(
                "intent_validation_failed",
                error_count=len(result.errors),
                errors=result.errors,
            )
            raise IntentError(
                f"Intent validation failed with {len(result.errors)} error(s): "
                + "; ".join(result.errors),
                details=result.to_dict(),
            )

        self._log.info(
            "intent_validation_passed",
            warning_count=len(result.warnings),
            suggestion_count=len(result.suggestions),
        )
        return result

    # ---------------------------------------------------------------------- #
    # Individual checks
    # ---------------------------------------------------------------------- #

    def _validate_required_fields(
        self, intent: ProjectIntent, result: ValidationResult
    ) -> None:
        """Ensure all critical fields are non-empty."""
        required = {
            "industry": "The project industry must be specified.",
            "product_type": "The product type must be specified (e.g. CRM, SaaS, mobile app).",
            "core_features": "At least one core feature must be identified.",
            "platform": "A deployment platform must be specified.",
        }
        for field_name, error_message in required.items():
            val = getattr(intent, field_name, None)
            if not self._is_populated(field_name, val):
                result.add_error(error_message)

        # Warnings for strongly-recommended fields
        recommended = {
            "target_users": "No target users specified — this may lead to poorly-scoped features.",
            "scale_requirements": "Scale requirements not specified — architecture may be over/under-engineered.",
        }
        for field_name, warning_message in recommended.items():
            val = getattr(intent, field_name, None)
            if not self._is_populated(field_name, val):
                result.add_warning(warning_message)

    def _validate_deployment_target(
        self, intent: ProjectIntent, result: ValidationResult
    ) -> None:
        """Check that deployment target is compatible with the platform."""
        deploy = str(intent.deployment_target).lower()
        platform = str(intent.platform).lower()

        incompatible_platforms = _INCOMPATIBLE_DEPLOY.get(deploy, set())
        if platform in incompatible_platforms:
            result.add_error(
                f"Deployment target '{deploy}' is not compatible with platform '{platform}'. "
                f"Vercel and Railway only support web deployments."
            )

        # Advisory: Kubernetes for a simple MVP is likely overkill
        if deploy == "kubernetes" and not intent.scale_requirements:
            result.add_warning(
                "Kubernetes deployment was selected but no scale requirements were specified. "
                "Consider Docker unless you have specific orchestration needs."
            )

        # Advisory: serverless on Vercel with non-web platform
        if deploy == "vercel" and platform == "api":
            result.add_suggestion(
                "Vercel serverless functions work well for API-only deployments. "
                "Consider Railway or AWS if you need persistent background workers."
            )

    def _validate_constraints_feasibility(
        self, intent: ProjectIntent, result: ValidationResult
    ) -> None:
        """Check for contradictory constraints and deployment mismatches."""
        all_text = " ".join(
            [
                " ".join(intent.constraints),
                " ".join(intent.integrations),
                " ".join(intent.security_requirements),
                str(intent.tech_preferences),
                str(intent.deployment_target),
                str(intent.platform),
            ]
        ).lower()

        for keyword_a, keyword_b, message in _CONFLICTING_CONSTRAINTS:
            if keyword_a in all_text and keyword_b in all_text:
                result.add_warning(f"Potential conflict detected: {message}")

        # GDPR with no security requirements is a risk
        gdpr_mentioned = any(
            "gdpr" in c.lower() for c in intent.constraints + intent.security_requirements
        )
        if gdpr_mentioned and not intent.security_requirements:
            result.add_warning(
                "GDPR compliance mentioned in constraints but no security requirements specified. "
                "Add security controls such as encryption at rest, audit logging, and RBAC."
            )

        # Offline constraint with cloud deployment
        offline = any("offline" in c.lower() for c in intent.constraints)
        cloud_targets = {"kubernetes", "vercel", "railway", "aws", "gcp"}
        if offline and str(intent.deployment_target).lower() in cloud_targets:
            result.add_error(
                "Constraint 'must run offline' is incompatible with cloud-only deployment target "
                f"'{intent.deployment_target}'. Use 'docker' for self-hosted deployments."
            )

    def _validate_feature_list_quality(
        self, intent: ProjectIntent, result: ValidationResult
    ) -> None:
        """Check that features are specific enough to be useful."""
        if not intent.core_features:
            return  # Already caught in required fields

        vague_terms = {"system", "app", "application", "platform", "software", "tool", "thing"}
        vague_features = [
            f for f in intent.core_features
            if len(f.split()) <= 2 and any(word.lower() in vague_terms for word in f.split())
        ]
        if vague_features:
            result.add_warning(
                f"Some core features are vague and may hinder code generation: "
                f"{', '.join(vague_features)}. Prefer action-oriented descriptions like "
                f"'Contact management with search and filters'."
            )

        if len(intent.core_features) > 15:
            result.add_warning(
                f"{len(intent.core_features)} core features listed. Consider trimming to the "
                f"10 most important to keep the MVP scope manageable."
            )

        if len(intent.core_features) == 1:
            result.add_suggestion(
                "Only one core feature identified. Consider adding more to ensure the "
                "specification phase can produce a complete architecture."
            )

    def _validate_scale_requirements(
        self, intent: ProjectIntent, result: ValidationResult
    ) -> None:
        """Infer scale-target mismatches from stated requirements."""
        scale = intent.scale_requirements.lower()
        if not scale:
            return

        # Large scale + Docker single-container warning
        large_scale_keywords = ["million", "100k", "1 million", "10 million", "high availability", "ha"]
        is_large_scale = any(kw in scale for kw in large_scale_keywords)
        if is_large_scale and str(intent.deployment_target).lower() == "docker":
            result.add_suggestion(
                "Large-scale requirements detected. Consider Kubernetes or a managed cloud "
                "platform (AWS/GCP) to handle the expected load reliably."
            )

    async def _validate_feature_coherence(
        self, intent: ProjectIntent, result: ValidationResult
    ) -> None:
        """Use LLM to check whether features make sense for the product type."""
        features_text = "\n".join(f"- {f}" for f in intent.core_features[:10])
        prompt = (
            f"You are a senior software architect reviewing project requirements.\n\n"
            f"Product type: {intent.product_type}\n"
            f"Industry: {intent.industry}\n"
            f"Core features:\n{features_text}\n\n"
            f"Respond with a JSON object:\n"
            f'{{"coherent": true/false, "issues": ["issue1", ...], "suggestions": ["sug1", ...]}}\n\n'
            f"Are the listed features appropriate and coherent for a {intent.product_type} "
            f"in the {intent.industry} industry? "
            f"Identify any features that seem out of place or any important missing features. "
            f"Respond ONLY with the JSON object."
        )

        try:
            response: LLMResponse = await self._llm.complete(
                messages=[LLMMessage(role="user", content=prompt)],
                system="You are a software architecture reviewer. Respond only with valid JSON.",
                model=DEFAULT_LLM_MODEL,
                temperature=0.2,
                max_tokens=512,
            )
            raw = response.content if hasattr(response, "content") else str(response)
            self._apply_coherence_result(raw, result)
        except Exception as exc:
            self._log.warning("coherence_check_failed", error=str(exc))
            # Coherence check failure is non-fatal; continue with other results.

    def _apply_coherence_result(self, raw: str, result: ValidationResult) -> None:
        """Parse the LLM coherence response and apply issues/suggestions."""
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if not json_match:
            return

        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return

        coherent = data.get("coherent", True)
        issues: List[str] = data.get("issues", [])
        suggestions: List[str] = data.get("suggestions", [])

        if not coherent and issues:
            for issue in issues[:3]:  # Cap to avoid overly noisy output
                result.add_warning(f"Feature coherence: {issue}")

        for suggestion in suggestions[:2]:
            result.add_suggestion(f"Feature suggestion: {suggestion}")

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    def _is_populated(self, field_name: str, value: Any) -> bool:
        """Return True when the field carries meaningful content."""
        if value is None:
            return False
        if isinstance(value, list):
            return len(value) > 0
        if isinstance(value, dict):
            return len(value) > 0
        return bool(str(value).strip())
