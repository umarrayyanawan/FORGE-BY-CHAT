"""API Contract Generator — Phase 3: Specification Engine.

Converts a ProjectIntent and DBSchema into a complete RESTful APIContract,
following REST conventions with CRUD for each entity plus business-logic
endpoints.
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from system.core.intent.schemas import ProjectIntent
from system.core.specification.schemas import APIContract, APIEndpoint, DBSchema
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import LLMMessage, get_llm_client

logger = get_logger(__name__)

# ---------------------------------------------------------------------- #
# Endpoints that must always be present in every FORGE project
# ---------------------------------------------------------------------- #


def _baseline_endpoints() -> list[APIEndpoint]:
    """Return the mandatory baseline endpoints for every project."""
    return [
        # ---------- Infrastructure ----------
        APIEndpoint(
            path="/health",
            method="GET",
            description="Liveness/readiness probe for load balancers and Kubernetes.",
            auth_required=False,
            response_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ok", "degraded"]},
                    "version": {"type": "string"},
                    "timestamp": {"type": "string", "format": "date-time"},
                    "components": {"type": "object"},
                },
            },
        ),
        # ---------- Auth ----------
        APIEndpoint(
            path="/auth/register",
            method="POST",
            description="Register a new user account.",
            auth_required=False,
            request_body={
                "type": "object",
                "required": ["email", "password"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "password": {"type": "string", "minLength": 8},
                    "full_name": {"type": "string"},
                },
            },
            response_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "format": "uuid"},
                    "email": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        ),
        APIEndpoint(
            path="/auth/login",
            method="POST",
            description="Authenticate with email + password and receive JWT tokens.",
            auth_required=False,
            request_body={
                "type": "object",
                "required": ["email", "password"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "password": {"type": "string"},
                },
            },
            response_schema={
                "type": "object",
                "properties": {
                    "access_token": {"type": "string"},
                    "refresh_token": {"type": "string"},
                    "token_type": {"type": "string", "enum": ["bearer"]},
                    "expires_in": {"type": "integer"},
                },
            },
            rate_limit="10/minute",
        ),
        APIEndpoint(
            path="/auth/refresh",
            method="POST",
            description="Exchange a refresh token for a new access token.",
            auth_required=False,
            request_body={
                "type": "object",
                "required": ["refresh_token"],
                "properties": {
                    "refresh_token": {"type": "string"},
                },
            },
            response_schema={
                "type": "object",
                "properties": {
                    "access_token": {"type": "string"},
                    "token_type": {"type": "string"},
                    "expires_in": {"type": "integer"},
                },
            },
            rate_limit="30/minute",
        ),
        APIEndpoint(
            path="/auth/logout",
            method="POST",
            description="Revoke the current refresh token and invalidate the session.",
            auth_required=True,
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        ),
        APIEndpoint(
            path="/auth/me",
            method="GET",
            description="Return the currently authenticated user's profile.",
            auth_required=True,
            response_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "email": {"type": "string"},
                    "full_name": {"type": "string"},
                    "role": {"type": "string"},
                    "is_active": {"type": "boolean"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
        ),
        APIEndpoint(
            path="/auth/password/change",
            method="POST",
            description="Change the authenticated user's password.",
            auth_required=True,
            request_body={
                "type": "object",
                "required": ["current_password", "new_password"],
                "properties": {
                    "current_password": {"type": "string"},
                    "new_password": {"type": "string", "minLength": 8},
                },
            },
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        ),
        # ---------- Users ----------
        APIEndpoint(
            path="/users",
            method="GET",
            description="List all users (paginated). Admin only.",
            auth_required=True,
            roles=["admin"],
            query_params=[
                {
                    "name": "page",
                    "type": "integer",
                    "required": "false",
                    "description": "Page number",
                },
                {
                    "name": "page_size",
                    "type": "integer",
                    "required": "false",
                    "description": "Items per page",
                },
                {
                    "name": "search",
                    "type": "string",
                    "required": "false",
                    "description": "Filter by email or name",
                },
                {
                    "name": "role",
                    "type": "string",
                    "required": "false",
                    "description": "Filter by role",
                },
            ],
            response_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "total": {"type": "integer"},
                    "page": {"type": "integer"},
                    "pages": {"type": "integer"},
                },
            },
        ),
        APIEndpoint(
            path="/users/{user_id}",
            method="GET",
            description="Retrieve a single user by ID.",
            auth_required=True,
            roles=["admin"],
            response_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "email": {"type": "string"},
                    "full_name": {"type": "string"},
                    "role": {"type": "string"},
                    "is_active": {"type": "boolean"},
                    "created_at": {"type": "string"},
                },
            },
        ),
        APIEndpoint(
            path="/users/{user_id}",
            method="PUT",
            description="Update a user's profile. Admin or self.",
            auth_required=True,
            request_body={
                "type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "role": {"type": "string"},
                    "is_active": {"type": "boolean"},
                },
            },
            response_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}, "email": {"type": "string"}},
            },
        ),
        APIEndpoint(
            path="/users/{user_id}",
            method="DELETE",
            description="Soft-delete a user account. Admin only.",
            auth_required=True,
            roles=["admin"],
            response_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        ),
    ]


class APIContractGenerator:
    """Generates a complete RESTful API contract from a ProjectIntent and DBSchema.

    Strategy:
    1.  Inject mandatory baseline endpoints (health, auth, users).
    2.  Ask the LLM to generate domain-specific endpoints as JSON.
    3.  Parse, deduplicate, and validate the combined contract.
    """

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client or get_llm_client()

    async def generate(self, intent: ProjectIntent, db_schema: DBSchema) -> APIContract:
        """Generate a complete APIContract.

        Args:
            intent:    Validated project intent.
            db_schema: Generated database schema (after SchemaGenerator).

        Returns:
            A fully-populated APIContract.

        Raises:
            SpecificationError: On LLM failure or unparseable response.
        """
        logger.info(
            "generating_api_contract",
            table_count=len(db_schema.tables),
            product_type=intent.product_type,
        )

        prompt = self._build_api_prompt(intent, db_schema)
        messages = [LLMMessage(role="user", content=prompt)]

        try:
            response = await self._llm.complete(
                messages=messages,
                model=DEFAULT_LLM_MODEL,
                max_tokens=6000,
                temperature=0.1,
                system=self._system_prompt(),
            )
        except Exception as exc:
            raise SpecificationError(
                f"LLM call failed during API contract generation: {exc}",
            ) from exc

        raw = response.content.strip()
        if not raw:
            raise SpecificationError("API contract generation returned empty content")

        try:
            domain_endpoints = self._parse_api_response(raw)
        except Exception as exc:
            raise SpecificationError(
                f"Failed to parse API contract response: {exc}",
                details={"raw_preview": raw[:500]},
            ) from exc

        # Merge baseline + domain, deduplicating on (method, path)
        baseline = _baseline_endpoints()
        baseline_keys = {(e.method, e.path) for e in baseline}
        unique_domain = [ep for ep in domain_endpoints if (ep.method, ep.path) not in baseline_keys]
        all_endpoints = baseline + unique_domain

        contract = APIContract(
            version="v1",
            base_path="/api/v1",
            endpoints=all_endpoints,
            auth_scheme="JWT",
            global_headers={
                "X-Request-ID": "string (UUID for request tracing)",
                "Content-Type": "application/json",
            },
        )

        logger.info(
            "api_contract_generated",
            total_endpoints=len(contract.endpoints),
            domain_endpoints=len(unique_domain),
        )

        return contract

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _system_prompt() -> str:
        return textwrap.dedent("""\
            You are a senior backend architect designing RESTful API contracts.
            Follow these rules strictly:
            - Use resource-oriented URL design: /resources/{id}/sub-resources
            - HTTP methods: GET (read), POST (create), PUT (replace), PATCH (partial update), DELETE (remove)
            - Versioned under /api/v1/
            - Return arrays wrapped in paginated envelopes: {items, total, page, pages}
            - All protected endpoints require JWT bearer authentication
            - Output ONLY valid JSON — no markdown fences, no explanations.
        """)

    def _build_api_prompt(self, intent: ProjectIntent, db_schema: DBSchema) -> str:
        """Build the LLM prompt for domain endpoint generation."""
        # Describe tables concisely to stay within token limits
        tables_summary = []
        for table in db_schema.tables:
            if table.name in ("users", "audit_logs", "refresh_tokens"):
                continue  # baseline tables already have hardcoded endpoints
            field_names = [f.name for f in table.fields[:10]]
            tables_summary.append(f"- {table.name}: {', '.join(field_names)}")

        tables_text = "\n".join(tables_summary) or "- No domain tables"
        features = "\n".join(f"- {f}" for f in intent.core_features)
        integrations = ", ".join(intent.integrations) or "None"

        return textwrap.dedent(f"""\
            Design DOMAIN-SPECIFIC REST API endpoints for this project.

            DO NOT generate endpoints for: /health, /auth/*, /users/*
            Those are already included.

            PROJECT CONTEXT
            ---------------
            Product type : {intent.product_type}
            Industry     : {intent.industry}
            Integrations : {integrations}

            Core Features:
            {features or "- Not specified"}

            Domain Database Tables (excluding baseline):
            {tables_text}

            OUTPUT FORMAT
            -------------
            Return a JSON array of endpoint objects:
            [
              {{
                "path": "/orders",
                "method": "GET",
                "description": "List all orders for the authenticated user",
                "request_body": null,
                "response_schema": {{
                  "type": "object",
                  "properties": {{
                    "items": {{"type": "array"}},
                    "total": {{"type": "integer"}},
                    "page": {{"type": "integer"}},
                    "pages": {{"type": "integer"}}
                  }}
                }},
                "auth_required": true,
                "roles": [],
                "rate_limit": null,
                "query_params": [
                  {{"name": "page", "type": "integer", "required": "false", "description": "Page number"}}
                ]
              }}
            ]

            Rules:
            - Generate full CRUD (GET list, GET by ID, POST, PUT, DELETE) for every domain table
            - Add business-logic endpoints beyond CRUD (e.g. /orders/{{id}}/confirm,
              /products/{{id}}/publish, /reports/generate)
            - Include search/filter query parameters on list endpoints
            - Specify rate_limit for expensive operations (e.g. "100/hour")
            - Specify roles where access is restricted (e.g. ["admin"])
            - Use path params in curly braces: /resources/{{resource_id}}
            - Paths start with / (no /api/v1 prefix — that is added automatically)
        """)

    def _parse_api_response(self, response: str) -> list[APIEndpoint]:
        """Parse LLM JSON array into a list of APIEndpoint models."""
        # Strip markdown code fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        # Extract JSON array
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            # Maybe wrapped in an object
            obj_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if obj_match:
                data = json.loads(obj_match.group(0))
                endpoint_list = data.get("endpoints", list(data.values())[0] if data else [])
            else:
                raise ValueError("No JSON array or object found in API response")
        else:
            endpoint_list = json.loads(match.group(0))

        endpoints: list[APIEndpoint] = []
        for ep_data in endpoint_list:
            if not isinstance(ep_data, dict):
                continue
            try:
                endpoints.append(APIEndpoint(**ep_data))
            except Exception as exc:
                logger.warning(
                    "skipping_invalid_endpoint",
                    path=ep_data.get("path", "?"),
                    error=str(exc),
                )
        return endpoints
