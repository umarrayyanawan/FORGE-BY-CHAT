"""UI Structure Mapper — Phase 3: Specification Engine.

Converts a ProjectIntent and APIContract into a complete UIStructure describing
all pages, routes, components, and navigation for the frontend application.
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from system.core.intent.schemas import ProjectIntent
from system.core.specification.schemas import APIContract, UIPage, UIStructure
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import LLMMessage, get_llm_client
from system.shared.models import Platform

logger = get_logger(__name__)

# ---------------------------------------------------------------------- #
# Baseline pages present in every web application
# ---------------------------------------------------------------------- #


def _baseline_pages() -> list[UIPage]:
    return [
        UIPage(
            name="Login",
            route="/login",
            description="Authentication page with email/password login form and link to registration.",
            components=["LoginForm", "SocialLoginButtons", "ForgotPasswordLink"],
            data_requirements=["POST /api/v1/auth/login"],
            auth_required=False,
        ),
        UIPage(
            name="Register",
            route="/register",
            description="New user registration form.",
            components=["RegisterForm", "TermsCheckbox", "SocialLoginButtons"],
            data_requirements=["POST /api/v1/auth/register"],
            auth_required=False,
        ),
        UIPage(
            name="Forgot Password",
            route="/forgot-password",
            description="Email-based password reset flow.",
            components=["ForgotPasswordForm", "SuccessMessage"],
            data_requirements=["POST /api/v1/auth/password/reset-request"],
            auth_required=False,
        ),
        UIPage(
            name="Profile",
            route="/profile",
            description="User profile view and edit — change name, avatar, and password.",
            components=["ProfileCard", "EditProfileForm", "ChangePasswordForm", "DangerZone"],
            data_requirements=[
                "GET /api/v1/auth/me",
                "PUT /api/v1/users/{user_id}",
                "POST /api/v1/auth/password/change",
            ],
            auth_required=True,
        ),
        UIPage(
            name="Not Found",
            route="/404",
            description="404 error page.",
            components=["ErrorIllustration", "BackToHomeButton"],
            data_requirements=[],
            auth_required=False,
        ),
    ]


_BASELINE_GLOBAL_COMPONENTS = [
    "Navbar",
    "Sidebar",
    "Footer",
    "LoadingSpinner",
    "ErrorBoundary",
    "ToastNotification",
    "ConfirmationModal",
    "DataTable",
    "Pagination",
    "SearchBar",
    "EmptyState",
    "PageHeader",
]


class UIMapper:
    """Maps a ProjectIntent + APIContract into a complete UIStructure.

    Strategy:
    1.  Inject mandatory baseline pages (login, register, profile, 404).
    2.  Ask the LLM to generate domain-specific pages as JSON.
    3.  Parse and merge.
    """

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client or get_llm_client()

    async def map(self, intent: ProjectIntent, api_contract: APIContract) -> UIStructure:
        """Generate a UIStructure from an intent and API contract.

        Args:
            intent:       Validated project intent.
            api_contract: Generated API contract.

        Returns:
            A complete UIStructure.

        Raises:
            SpecificationError: On LLM failure or unparseable response.
        """
        # API-only projects don't have a UI
        if intent.platform == Platform.API:
            logger.info("skipping_ui_for_api_only_project")
            return UIStructure(
                pages=[],
                global_components=[],
                navigation=[],
                state_management="N/A (API-only project)",
            )

        logger.info(
            "mapping_ui_structure",
            platform=intent.platform,
            endpoint_count=api_contract.endpoint_count(),
        )

        prompt = self._build_ui_prompt(intent, api_contract)
        messages = [LLMMessage(role="user", content=prompt)]

        try:
            response = await self._llm.complete(
                messages=messages,
                model=DEFAULT_LLM_MODEL,
                max_tokens=5000,
                temperature=0.2,
                system=self._system_prompt(),
            )
        except Exception as exc:
            raise SpecificationError(
                f"LLM call failed during UI mapping: {exc}",
            ) from exc

        raw = response.content.strip()
        if not raw:
            raise SpecificationError("UI mapping returned empty content")

        try:
            domain_structure = self._parse_ui_response(raw)
        except Exception as exc:
            raise SpecificationError(
                f"Failed to parse UI response: {exc}",
                details={"raw_preview": raw[:500]},
            ) from exc

        # Merge baseline + domain pages, deduplicating by route
        baseline = _baseline_pages()
        baseline_routes = {p.route for p in baseline}
        unique_domain_pages = [p for p in domain_structure.pages if p.route not in baseline_routes]
        all_pages = baseline + unique_domain_pages

        # Merge global components
        combined_globals = list(
            dict.fromkeys(_BASELINE_GLOBAL_COMPONENTS + domain_structure.global_components)
        )

        # Build navigation from non-auth pages
        navigation = self._build_navigation(all_pages)

        final = UIStructure(
            pages=all_pages,
            global_components=combined_globals,
            theme=domain_structure.theme or self._default_theme(),
            navigation=navigation,
            state_management=domain_structure.state_management,
        )

        logger.info(
            "ui_structure_mapped",
            page_count=len(final.pages),
            component_count=len(final.global_components),
        )

        return final

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _system_prompt() -> str:
        return textwrap.dedent("""\
            You are a senior frontend architect designing modern React applications.
            You follow these patterns:
            - Next.js App Router for routing
            - Tailwind CSS + shadcn/ui for components
            - React Query (TanStack Query) for server state
            - Zustand for client state
            - TypeScript for type safety

            Output ONLY valid JSON — no markdown fences, no explanations.
        """)

    def _build_ui_prompt(self, intent: ProjectIntent, api_contract: APIContract) -> str:
        """Construct the LLM prompt for domain UI page generation."""
        features = "\n".join(f"- {f}" for f in intent.core_features)

        # Summarise non-baseline endpoints
        domain_endpoints = [
            f"  {ep.method} {ep.path}"
            for ep in api_contract.endpoints
            if not ep.path.startswith("/auth")
            and not ep.path.startswith("/health")
            and ep.path not in ("/users", "/users/{user_id}")
        ][:40]
        endpoints_text = "\n".join(domain_endpoints) or "  (none beyond baseline)"

        return textwrap.dedent(f"""\
            Design DOMAIN-SPECIFIC UI pages for this project.

            DO NOT include: /login, /register, /forgot-password, /profile, /404
            Those pages are already included automatically.

            PROJECT CONTEXT
            ---------------
            Product type : {intent.product_type}
            Industry     : {intent.industry}
            Platform     : {intent.platform}
            Target users : {intent.target_users}

            Core Features:
            {features or "- Not specified"}

            Domain API Endpoints (sample):
            {endpoints_text}

            OUTPUT FORMAT
            -------------
            Return a single JSON object:
            {{
              "pages": [
                {{
                  "name": "Dashboard",
                  "route": "/dashboard",
                  "description": "Main overview page showing key metrics and recent activity",
                  "components": ["StatsGrid", "RecentOrdersTable", "ActivityFeed", "QuickActions"],
                  "data_requirements": ["GET /api/v1/orders", "GET /api/v1/stats/summary"],
                  "auth_required": true,
                  "roles": []
                }}
              ],
              "global_components": ["CustomChart", "NotificationBell", "UserAvatar"],
              "theme": {{
                "primary": "#3B82F6",
                "secondary": "#8B5CF6",
                "background": "#F9FAFB",
                "font": "Inter"
              }},
              "state_management": "React Query + Zustand"
            }}

            Rules:
            - Include a Dashboard page as the first page (unless API-only)
            - Cover all core features with dedicated pages
            - Include admin pages if the project has admin functionality
            - Route paths must start with /
            - components are React component names in PascalCase
            - data_requirements are full API paths
            - Map every core feature to at least one page
            - Include list, detail, and create/edit pages for primary entities
        """)

    def _parse_ui_response(self, response: str) -> UIStructure:
        """Parse LLM JSON into a UIStructure."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in UI response")

        data = json.loads(match.group(0))

        pages: list[UIPage] = []
        for p_data in data.get("pages", []):
            if not isinstance(p_data, dict):
                continue
            try:
                pages.append(UIPage(**p_data))
            except Exception as exc:
                logger.warning(
                    "skipping_invalid_page",
                    route=p_data.get("route", "?"),
                    error=str(exc),
                )

        global_components = data.get("global_components", [])
        theme = data.get("theme", {})
        navigation = data.get("navigation", [])
        state_management = data.get("state_management", "React Query + Zustand")

        return UIStructure(
            pages=pages,
            global_components=global_components,
            theme=theme,
            navigation=navigation,
            state_management=state_management,
        )

    @staticmethod
    def _default_theme() -> dict[str, str]:
        return {
            "primary": "#3B82F6",
            "secondary": "#8B5CF6",
            "accent": "#10B981",
            "background": "#F9FAFB",
            "surface": "#FFFFFF",
            "text": "#111827",
            "border": "#E5E7EB",
            "font": "Inter",
            "radius": "8px",
        }

    @staticmethod
    def _build_navigation(pages: list[UIPage]) -> list[dict[str, str]]:
        """Build a navigation list from authenticated pages."""
        nav_items = []
        icon_map = {
            "dashboard": "LayoutDashboard",
            "home": "Home",
            "order": "ShoppingCart",
            "product": "Package",
            "user": "Users",
            "report": "BarChart",
            "setting": "Settings",
            "admin": "Shield",
            "invoice": "FileText",
            "payment": "CreditCard",
            "message": "MessageSquare",
            "notification": "Bell",
            "profile": "User",
            "analytic": "TrendingUp",
            "task": "CheckSquare",
            "project": "FolderOpen",
        }

        for page in pages:
            if not page.auth_required or page.route in (
                "/login",
                "/register",
                "/forgot-password",
                "/404",
            ):
                continue

            # Determine icon
            icon = "Circle"
            for keyword, icon_name in icon_map.items():
                if keyword in page.name.lower() or keyword in page.route.lower():
                    icon = icon_name
                    break

            nav_items.append(
                {
                    "label": page.name,
                    "route": page.route,
                    "icon": icon,
                    "role_required": page.roles[0] if page.roles else "",
                }
            )

        return nav_items
