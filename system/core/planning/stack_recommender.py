"""Stack Recommender — Phase 4 of the FORGE planning pipeline.

Determines the optimal technology stack for a project by applying
deterministic rule-based matching first, then refining with an LLM call
when confidence is insufficient or when the project description is too
nuanced to match a known pattern.

Usage::

    recommender = StackRecommender(llm_client=get_llm_client())
    stack = await recommender.recommend(intent, spec)
    # {'backend': 'FastAPI', 'frontend': 'Next.js', 'database': 'PostgreSQL', ...}
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from system.core.intent.schemas import ProjectIntent
from system.core.planning.schemas import (
    ArchitecturePlan,
    InfraComponent,
    SecurityArchitecture,
    ServiceDefinition,
)
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.llm_client import LLMMessage

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical stack presets keyed by primary use-case
# ---------------------------------------------------------------------------

RULE_BASED_STACK: Dict[str, Dict[str, str]] = {
    "e-commerce": {
        "backend": "FastAPI",
        "frontend": "Next.js",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker / Kubernetes",
        "auth": "JWT + OAuth2",
        "monitoring": "Prometheus + Grafana",
        "payments": "Stripe",
    },
    "mobile": {
        "backend": "FastAPI",
        "frontend": "React Native",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker",
        "auth": "JWT",
        "monitoring": "Sentry + Prometheus",
    },
    "real-time": {
        "backend": "FastAPI",
        "frontend": "Next.js",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Redis Streams",
        "infra": "Docker / Kubernetes",
        "auth": "JWT",
        "monitoring": "Prometheus + Grafana",
        "realtime": "WebSocket / Redis Pub-Sub",
    },
    "ml": {
        "backend": "FastAPI",
        "frontend": "Next.js",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker / Kubernetes",
        "auth": "JWT",
        "monitoring": "MLflow + Prometheus",
        "ml_framework": "PyTorch",
    },
    "cms": {
        "backend": "FastAPI",
        "frontend": "Next.js",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker",
        "auth": "JWT + OAuth2",
        "monitoring": "Prometheus + Grafana",
    },
    "api": {
        "backend": "FastAPI",
        "frontend": "None",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker",
        "auth": "JWT + API Key",
        "monitoring": "Prometheus + Grafana",
    },
    "saas": {
        "backend": "FastAPI",
        "frontend": "Next.js",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker / Kubernetes",
        "auth": "JWT + OAuth2",
        "monitoring": "Prometheus + Grafana",
    },
    "crm": {
        "backend": "FastAPI",
        "frontend": "Next.js",
        "database": "PostgreSQL",
        "cache": "Redis",
        "queue": "Celery + Redis",
        "infra": "Docker",
        "auth": "JWT + RBAC",
        "monitoring": "Prometheus + Grafana",
    },
}

# keyword → preset key mapping
_KEYWORD_MAP: Dict[str, str] = {
    "shop": "e-commerce",
    "store": "e-commerce",
    "commerce": "e-commerce",
    "marketplace": "e-commerce",
    "stripe": "e-commerce",
    "payment": "e-commerce",
    "checkout": "e-commerce",
    "mobile": "mobile",
    "ios": "mobile",
    "android": "mobile",
    "react native": "mobile",
    "chat": "real-time",
    "real-time": "real-time",
    "realtime": "real-time",
    "websocket": "real-time",
    "notification": "real-time",
    "live": "real-time",
    "stream": "real-time",
    "ml": "ml",
    "machine learning": "ml",
    "ai": "ml",
    "model": "ml",
    "prediction": "ml",
    "pytorch": "ml",
    "tensorflow": "ml",
    "inference": "ml",
    "cms": "cms",
    "content management": "cms",
    "blog": "cms",
    "publishing": "cms",
    "headless": "cms",
    "api": "api",
    "rest api": "api",
    "microservice": "api",
    "saas": "saas",
    "dashboard": "saas",
    "subscription": "saas",
    "multi-tenant": "saas",
    "crm": "crm",
    "customer": "crm",
    "lead": "crm",
    "sales": "crm",
    "pipeline": "crm",
}

# High-scale indicators
_HIGH_SCALE_INDICATORS = [
    "million",
    "billion",
    "1m+",
    "10k+",
    "100k+",
    "high traffic",
    "high load",
    "enterprise",
    "global",
    "distributed",
]


class StackRecommender:
    """Recommends a technology stack for a FORGE project.

    Strategy:
    1. ``_apply_rules`` — deterministic keyword matching against known patterns.
    2. ``_adjust_for_scale`` — adds Redis CDN / replicas when scale is high.
    3. ``_refine_with_llm`` — only called when rule confidence is < 0.6 or
       no rule matched.
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def recommend(
        self, intent: ProjectIntent, spec: ProjectSpec
    ) -> Dict[str, str]:
        """Return the recommended technology stack for *intent* and *spec*.

        Returns a dict with keys:
            backend, frontend, database, cache, queue, infra, auth, monitoring
        and any additional keys specific to the matched preset.
        """
        initial_stack, confidence = self._apply_rules(intent)
        stack = self._adjust_for_scale(initial_stack.copy(), intent)

        if confidence < 0.6:
            logger.info(
                "Rule confidence low — refining with LLM",
                confidence=confidence,
                product_type=intent.product_type,
            )
            stack = await self._refine_with_llm(intent, spec, stack)
        else:
            # Honour explicit tech preferences from the intent
            for role, tech in intent.tech_preferences.items():
                if tech:
                    stack[role] = tech

        logger.info(
            "Stack recommendation complete",
            confidence=confidence,
            stack=stack,
        )
        return stack

    # ------------------------------------------------------------------
    # Rule-based matching
    # ------------------------------------------------------------------

    def _apply_rules(self, intent: ProjectIntent) -> tuple[Dict[str, str], float]:
        """Return (matched_stack, confidence) using keyword matching.

        Confidence is 1.0 for an exact preset match, 0.7 for a keyword match,
        and 0.0 when no rule fires.
        """
        # Combine all text signals into one lowercase search string
        signals = " ".join(
            [
                intent.raw_prompt,
                intent.product_type,
                intent.industry,
                " ".join(intent.core_features),
                " ".join(intent.integrations),
            ]
        ).lower()

        # Check preset keys directly (e.g. product_type == "e-commerce")
        product_lower = intent.product_type.lower().strip()
        if product_lower in RULE_BASED_STACK:
            return RULE_BASED_STACK[product_lower].copy(), 1.0

        # Walk keyword map — take highest-weight match
        matched_preset: Optional[str] = None
        for keyword, preset_key in _KEYWORD_MAP.items():
            if keyword in signals:
                matched_preset = preset_key
                break  # first / most specific match wins

        if matched_preset:
            return RULE_BASED_STACK[matched_preset].copy(), 0.7

        # Default fallback — generic web app
        return {
            "backend": "FastAPI",
            "frontend": "Next.js",
            "database": "PostgreSQL",
            "cache": "Redis",
            "queue": "Celery + Redis",
            "infra": "Docker",
            "auth": "JWT",
            "monitoring": "Prometheus + Grafana",
        }, 0.0

    # ------------------------------------------------------------------
    # Scale adjustment
    # ------------------------------------------------------------------

    def _adjust_for_scale(
        self, stack: Dict[str, str], intent: ProjectIntent
    ) -> Dict[str, str]:
        """Upgrade the stack when high-scale indicators are detected."""
        scale_text = (intent.scale_requirements or "").lower()
        is_high_scale = any(ind in scale_text for ind in _HIGH_SCALE_INDICATORS)

        if is_high_scale:
            # Always add Redis cache if not already there
            if "redis" not in stack.get("cache", "").lower():
                stack["cache"] = "Redis (clustered)"

            # Upgrade Kubernetes if only Docker
            if stack.get("infra", "") == "Docker":
                stack["infra"] = "Docker / Kubernetes"

            # Add CDN
            stack["cdn"] = "CloudFront / Cloudflare"

            # Read replicas note
            stack["database"] = stack.get("database", "PostgreSQL") + " (+ read replicas)"

            # Load balancer
            stack["load_balancer"] = "NGINX / AWS ALB"

            logger.info("Adjusted stack for high-scale requirements", scale=scale_text)

        return stack

    # ------------------------------------------------------------------
    # LLM refinement
    # ------------------------------------------------------------------

    async def _refine_with_llm(
        self,
        intent: ProjectIntent,
        spec: ProjectSpec,
        initial_stack: Dict[str, str],
    ) -> Dict[str, str]:
        """Use the LLM to refine or override the initial stack.

        Only called when rule-based matching has low confidence.
        Returns a merged dict of the initial stack plus LLM suggestions.
        """
        system_prompt = (
            "You are a senior software architect specialising in cloud-native systems. "
            "Given a project description, return the optimal technology stack as a JSON "
            "object. Keys: backend, frontend, database, cache, queue, infra, auth, "
            "monitoring. Values: specific technologies (e.g. 'FastAPI', 'Next.js', "
            "'PostgreSQL'). Return ONLY the JSON object, no commentary."
        )

        user_content = (
            f"Project intent:\n"
            f"  Raw prompt: {intent.raw_prompt}\n"
            f"  Product type: {intent.product_type}\n"
            f"  Industry: {intent.industry}\n"
            f"  Core features: {', '.join(intent.core_features)}\n"
            f"  Integrations: {', '.join(intent.integrations)}\n"
            f"  Scale requirements: {intent.scale_requirements}\n"
            f"  Security requirements: {', '.join(intent.security_requirements)}\n"
            f"  Tech preferences: {json.dumps(intent.tech_preferences)}\n\n"
            f"Initial rule-based stack (use as baseline, improve where justified):\n"
            f"{json.dumps(initial_stack, indent=2)}\n\n"
            "Return the refined stack as a JSON object."
        )

        try:
            response = await self._llm.complete(
                messages=[LLMMessage(role="user", content=user_content)],
                system=system_prompt,
                max_tokens=1024,
                temperature=0.1,
            )
            raw = response.content.strip()

            # Extract JSON block if wrapped in markdown
            json_match = re.search(r"\{[\s\S]+\}", raw)
            if json_match:
                refined = json.loads(json_match.group())
                if isinstance(refined, dict):
                    # Merge: LLM output takes precedence, keep initial keys not returned
                    merged = {**initial_stack, **refined}
                    return merged
        except Exception as exc:
            logger.warning(
                "LLM stack refinement failed — using rule-based stack",
                error=str(exc),
            )

        return initial_stack
