"""PRD Generator — Phase 3: Specification Engine.

Converts a ProjectIntent into a full, detailed Product Requirements Document
in Markdown format using structured LLM prompting.
"""

from __future__ import annotations

import textwrap
from typing import Any

from system.core.intent.schemas import ProjectIntent
from system.observability.logging.logger import get_logger
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import LLMMessage, get_llm_client
from system.shared.constants import DEFAULT_LLM_MODEL

logger = get_logger(__name__)


class PRDGenerator:
    """Generates a structured Product Requirements Document from a ProjectIntent.

    The PRD is the first artefact produced by the Specification Engine and
    serves as the canonical source of truth for all downstream generators
    (schema, API, UI, etc.).
    """

    # Sections we expect the LLM to produce
    REQUIRED_SECTIONS = [
        "Executive Summary",
        "Problem Statement",
        "User Personas",
        "Core Features",
        "Non-Functional Requirements",
        "Success Metrics",
        "Out of Scope",
        "Risk Assessment",
    ]

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client or get_llm_client()

    async def generate(self, intent: ProjectIntent) -> str:
        """Generate a full Markdown PRD from a validated ProjectIntent.

        Args:
            intent: Fully-populated ProjectIntent (confidence >= 0.7 expected).

        Returns:
            A multi-thousand-word Markdown document with all required sections.

        Raises:
            SpecificationError: If the LLM fails or returns malformed output.
        """
        logger.info(
            "generating_prd",
            product_type=intent.product_type,
            industry=intent.industry,
            feature_count=len(intent.core_features),
        )

        prompt = self._build_prd_prompt(intent)
        messages = [LLMMessage(role="user", content=prompt)]

        try:
            response = await self._llm.complete(
                messages=messages,
                model=DEFAULT_LLM_MODEL,
                max_tokens=8192,
                temperature=0.3,  # slightly higher for creative prose
                system=self._system_prompt(),
            )
        except Exception as exc:
            raise SpecificationError(
                f"LLM call failed during PRD generation: {exc}",
                details={"intent_product_type": intent.product_type},
            ) from exc

        prd_text = response.content.strip()

        if not prd_text:
            raise SpecificationError(
                "PRD generation returned empty content",
                details={"prompt_length": len(prompt)},
            )

        # Validate that all required sections are present
        missing = self._check_sections(prd_text)
        if missing:
            logger.warning(
                "prd_missing_sections",
                missing=missing,
                attempting_repair=True,
            )
            prd_text = await self._repair_missing_sections(intent, prd_text, missing)

        logger.info(
            "prd_generated",
            length_chars=len(prd_text),
        )

        return prd_text

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _system_prompt() -> str:
        return textwrap.dedent("""\
            You are a senior product manager and technical writer at a world-class
            software consultancy. You write thorough, well-structured Product
            Requirements Documents (PRDs) that engineering teams can execute from
            directly. Your PRDs are:

            - Concrete and actionable — no vague language like "the system should
              be fast" without specifying measurable criteria.
            - Comprehensive but focused — every section adds information that
              downstream engineers will use.
            - Written in clear, professional British English.
            - Structured with proper Markdown headings (##, ###), bullet points,
              and tables where appropriate.

            Do NOT include any preamble like "Here is your PRD:" — output ONLY
            the Markdown document itself.
        """)

    def _build_prd_prompt(self, intent: ProjectIntent) -> str:
        """Construct the full LLM prompt for PRD generation."""
        features_md = "\n".join(f"- {f}" for f in intent.core_features)
        integrations_md = "\n".join(f"- {i}" for i in intent.integrations) or "- None specified"
        constraints_md = "\n".join(f"- {c}" for c in intent.constraints) or "- None specified"
        security_md = (
            "\n".join(f"- {s}" for s in intent.security_requirements)
            or "- Standard best practices"
        )
        tech_prefs = (
            "\n".join(f"- {k}: {v}" for k, v in intent.tech_preferences.items())
            or "- No specific preferences"
        )

        return textwrap.dedent(f"""\
            Create a comprehensive Product Requirements Document (PRD) for the following project.

            ## Project Overview
            - **Name**: {intent.product_type or "Software Project"}
            - **Industry**: {intent.industry or "General"}
            - **Platform**: {intent.platform}
            - **Deployment Target**: {intent.deployment_target}
            - **Original Request**: {intent.raw_prompt}

            ## Target Users
            {intent.target_users or "General users"}

            ## Core Features (Must-Have)
            {features_md or "- To be determined"}

            ## Third-Party Integrations
            {integrations_md}

            ## Technical Constraints
            {constraints_md}

            ## Security Requirements
            {security_md}

            ## Scale Requirements
            {intent.scale_requirements or "Standard startup scale (< 10,000 concurrent users)"}

            ## Technical Preferences
            {tech_prefs}

            ## Timeline
            {intent.timeline or "To be determined"}

            ## Budget Range
            {intent.budget_range or "Not specified"}

            ---

            Write a full PRD with EXACTLY these sections (use ## for top-level headings):

            ## 1. Executive Summary
            A 2–3 paragraph executive overview of the product: what it is, who it's for,
            and what business problem it solves. Include the project's vision statement.

            ## 2. Problem Statement
            Describe the problem in depth. Include:
            - Current pain points users experience
            - Business impact of the problem
            - Why existing solutions are inadequate
            - The opportunity this product addresses

            ## 3. User Personas
            Define 2–4 detailed user personas. For each persona include:
            - Name & role
            - Goals and motivations
            - Pain points
            - Technical sophistication level
            - How they will use this product

            ## 4. Core Features
            For each core feature provide:
            - Feature name (###) with priority badge [P0/P1/P2]
            - Detailed description of what the feature does
            - User stories (As a [persona], I want to [action] so that [benefit])
            - Acceptance criteria (bullet list of testable conditions)
            - Dependencies on other features

            ## 5. Non-Functional Requirements
            Cover all of these subsections (###):
            ### 5.1 Performance
            Specific, measurable performance targets (response times, throughput, concurrency).
            ### 5.2 Scalability
            How the system must scale (users, data volume, geography).
            ### 5.3 Reliability & Availability
            SLA targets, acceptable downtime, backup & recovery requirements.
            ### 5.4 Security
            Auth mechanisms, data encryption, compliance requirements, audit logging.
            ### 5.5 Maintainability
            Code quality standards, observability requirements, deployment process.

            ## 6. Success Metrics
            Define 5–8 measurable KPIs that will determine whether the product is
            successful. Include baseline (current state), target, and measurement method.
            Present as a Markdown table.

            ## 7. Out of Scope
            Explicitly list features and capabilities that are NOT included in this
            release, and why. This prevents scope creep.

            ## 8. Risk Assessment
            Identify the top 5 risks to successful delivery. For each risk include:
            - Risk description
            - Probability (Low/Medium/High)
            - Impact (Low/Medium/High)
            - Mitigation strategy

            Present the risk matrix as a Markdown table.
        """)

    def _check_sections(self, prd_text: str) -> list[str]:
        """Return a list of required section headers that are missing."""
        missing = []
        for section in self.REQUIRED_SECTIONS:
            if section.lower() not in prd_text.lower():
                missing.append(section)
        return missing

    async def _repair_missing_sections(
        self,
        intent: ProjectIntent,
        prd_text: str,
        missing_sections: list[str],
    ) -> str:
        """Ask the LLM to append the missing sections to the existing PRD."""
        sections_list = "\n".join(f"- {s}" for s in missing_sections)
        repair_prompt = textwrap.dedent(f"""\
            The following PRD is missing these required sections:
            {sections_list}

            Append the missing sections to the PRD below. Keep the existing content
            intact and only add the missing sections at the end.

            ---EXISTING PRD---
            {prd_text}
            ---END PRD---

            Project context:
            - Product type: {intent.product_type}
            - Industry: {intent.industry}
            - Core features: {", ".join(intent.core_features[:5])}

            Output ONLY the complete PRD (existing + new sections), no preamble.
        """)

        messages = [LLMMessage(role="user", content=repair_prompt)]
        try:
            response = await self._llm.complete(
                messages=messages,
                model=DEFAULT_LLM_MODEL,
                max_tokens=4096,
                temperature=0.2,
                system=self._system_prompt(),
            )
            return response.content.strip() or prd_text
        except Exception as exc:
            logger.warning("prd_repair_failed", error=str(exc))
            # Return original even if imperfect rather than crashing
            return prd_text
