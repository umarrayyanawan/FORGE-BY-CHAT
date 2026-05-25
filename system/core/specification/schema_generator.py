"""Database Schema Generator — Phase 3: Specification Engine.

Converts a ProjectIntent + PRD into a normalised PostgreSQL schema represented
as a DBSchema Pydantic model.
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from system.core.intent.schemas import ProjectIntent
from system.core.specification.schemas import DBField, DBSchema, DBTable
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import LLMMessage, get_llm_client

logger = get_logger(__name__)

# ---------------------------------------------------------------------- #
# Baseline tables that every FORGE project always includes
# ---------------------------------------------------------------------- #

_USERS_TABLE = DBTable(
    name="users",
    description="Application users — one row per registered account.",
    fields=[
        DBField(
            name="id",
            type="uuid",
            nullable=False,
            unique=True,
            indexed=True,
            description="Primary key (UUID v4)",
        ),
        DBField(
            name="email",
            type="string",
            nullable=False,
            unique=True,
            indexed=True,
            description="User's email address (login identifier)",
        ),
        DBField(
            name="hashed_password",
            type="string",
            nullable=False,
            description="Bcrypt-hashed password",
        ),
        DBField(name="full_name", type="string", nullable=True, description="Display name"),
        DBField(
            name="role",
            type="string",
            nullable=False,
            default="user",
            description="RBAC role: admin, user, viewer, etc.",
        ),
        DBField(
            name="is_active",
            type="boolean",
            nullable=False,
            default=True,
            description="Soft-delete / account suspension flag",
        ),
        DBField(
            name="is_verified",
            type="boolean",
            nullable=False,
            default=False,
            description="Email verification status",
        ),
        DBField(
            name="last_login_at",
            type="datetime",
            nullable=True,
            description="Timestamp of most recent successful login",
        ),
        DBField(
            name="created_at",
            type="datetime",
            nullable=False,
            default="now()",
            description="Row creation timestamp",
        ),
        DBField(
            name="updated_at",
            type="datetime",
            nullable=False,
            default="now()",
            description="Row last-update timestamp",
        ),
    ],
    indexes=[
        "CREATE INDEX idx_users_email ON users(email)",
        "CREATE INDEX idx_users_role ON users(role)",
        "CREATE INDEX idx_users_is_active ON users(is_active)",
    ],
)

_AUDIT_LOGS_TABLE = DBTable(
    name="audit_logs",
    description="Immutable audit trail of all significant system events.",
    fields=[
        DBField(
            name="id",
            type="uuid",
            nullable=False,
            unique=True,
            indexed=True,
            description="Primary key (UUID v4)",
        ),
        DBField(
            name="user_id",
            type="uuid",
            nullable=True,
            indexed=True,
            foreign_key="users.id",
            description="User who performed the action (NULL for system events)",
        ),
        DBField(
            name="action",
            type="string",
            nullable=False,
            indexed=True,
            description="Action type, e.g. 'user.login', 'order.created'",
        ),
        DBField(
            name="entity_type",
            type="string",
            nullable=True,
            indexed=True,
            description="Affected entity table name",
        ),
        DBField(
            name="entity_id",
            type="uuid",
            nullable=True,
            indexed=True,
            description="Affected entity primary key",
        ),
        DBField(
            name="old_value",
            type="json",
            nullable=True,
            description="Snapshot of entity state before the action",
        ),
        DBField(
            name="new_value",
            type="json",
            nullable=True,
            description="Snapshot of entity state after the action",
        ),
        DBField(name="ip_address", type="string", nullable=True, description="Client IP address"),
        DBField(
            name="user_agent", type="string", nullable=True, description="Client user-agent string"
        ),
        DBField(
            name="metadata", type="json", nullable=True, description="Arbitrary additional context"
        ),
        DBField(
            name="created_at",
            type="datetime",
            nullable=False,
            default="now()",
            description="When the event occurred",
        ),
    ],
    indexes=[
        "CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id)",
        "CREATE INDEX idx_audit_logs_action ON audit_logs(action)",
        "CREATE INDEX idx_audit_logs_entity ON audit_logs(entity_type, entity_id)",
        "CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at DESC)",
    ],
)

_REFRESH_TOKENS_TABLE = DBTable(
    name="refresh_tokens",
    description="Stores long-lived JWT refresh tokens for session management.",
    fields=[
        DBField(
            name="id",
            type="uuid",
            nullable=False,
            unique=True,
            indexed=True,
            description="Primary key",
        ),
        DBField(
            name="user_id",
            type="uuid",
            nullable=False,
            indexed=True,
            foreign_key="users.id",
            description="Owning user",
        ),
        DBField(
            name="token_hash",
            type="string",
            nullable=False,
            unique=True,
            description="SHA-256 hash of the refresh token value",
        ),
        DBField(
            name="expires_at", type="datetime", nullable=False, description="Token expiry timestamp"
        ),
        DBField(
            name="revoked",
            type="boolean",
            nullable=False,
            default=False,
            description="Whether this token has been revoked",
        ),
        DBField(
            name="created_at",
            type="datetime",
            nullable=False,
            default="now()",
            description="When the token was issued",
        ),
    ],
    indexes=[
        "CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens(user_id)",
        "CREATE INDEX idx_refresh_tokens_token_hash ON refresh_tokens(token_hash)",
    ],
)


class SchemaGenerator:
    """Generates a normalised PostgreSQL DBSchema from a ProjectIntent and PRD.

    Strategy:
    1.  Inject mandatory baseline tables (users, audit_logs, refresh_tokens).
    2.  Ask the LLM to generate domain-specific tables as JSON.
    3.  Parse and validate the LLM response.
    4.  Merge baseline + domain tables and return a DBSchema.
    """

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client or get_llm_client()

    async def generate(self, intent: ProjectIntent, prd: str) -> DBSchema:
        """Generate a complete DBSchema from intent and PRD.

        Args:
            intent: Validated project intent.
            prd:    Generated PRD Markdown.

        Returns:
            A complete, validated DBSchema ready for downstream consumers.

        Raises:
            SpecificationError: On LLM failure or unparseable response.
        """
        logger.info(
            "generating_db_schema",
            product_type=intent.product_type,
            industry=intent.industry,
        )

        prompt = self._build_schema_prompt(intent, prd)
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
                f"LLM call failed during schema generation: {exc}",
            ) from exc

        raw = response.content.strip()
        if not raw:
            raise SpecificationError("Schema generation returned empty content")

        try:
            domain_schema = self._parse_schema_response(raw)
        except Exception as exc:
            raise SpecificationError(
                f"Failed to parse schema LLM response: {exc}",
                details={"raw_preview": raw[:500]},
            ) from exc

        # Merge baseline tables with domain tables (avoid duplication)
        baseline_names = {"users", "audit_logs", "refresh_tokens"}
        domain_tables = [t for t in domain_schema.tables if t.name not in baseline_names]
        all_tables = [_USERS_TABLE, _AUDIT_LOGS_TABLE, _REFRESH_TOKENS_TABLE] + domain_tables

        # Merge relationships
        merged_relationships = list(domain_schema.relationships)

        merged_schema = DBSchema(tables=all_tables, relationships=merged_relationships)

        self._validate_schema(merged_schema)

        logger.info(
            "db_schema_generated",
            table_count=len(merged_schema.tables),
            relationship_count=len(merged_schema.relationships),
        )

        return merged_schema

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _system_prompt() -> str:
        return textwrap.dedent("""\
            You are a senior database architect specialising in PostgreSQL.
            You design normalised, production-grade database schemas following
            these conventions:
            - All table names: plural snake_case (e.g. orders, line_items)
            - All column names: snake_case
            - Primary keys: always "id" of type UUID (uuid_generate_v4())
            - Every table has: id, created_at, updated_at
            - Foreign keys: column named <table_singular>_id referencing <table>.id
            - Soft deletes via deleted_at timestamp (preferred over hard deletes)
            - Use JSONB for flexible metadata fields

            Output ONLY valid JSON — no markdown fences, no explanation.
        """)

    def _build_schema_prompt(self, intent: ProjectIntent, prd: str) -> str:
        """Construct the LLM prompt for domain table generation."""
        features = "\n".join(f"- {f}" for f in intent.core_features)
        integrations = ", ".join(intent.integrations) or "None"

        # Trim PRD to avoid token overflow — take first 3000 chars
        prd_excerpt = prd[:3000] + ("..." if len(prd) > 3000 else "")

        return textwrap.dedent(f"""\
            Design the DOMAIN-SPECIFIC database tables for this project.
            DO NOT include the following tables — they are always added automatically:
            users, audit_logs, refresh_tokens

            PROJECT CONTEXT
            ---------------
            Product type : {intent.product_type}
            Industry     : {intent.industry}
            Platform     : {intent.platform}
            Integrations : {integrations}
            Scale        : {intent.scale_requirements or "Standard startup scale"}

            Core Features:
            {features or "- Not specified"}

            PRD Excerpt:
            {prd_excerpt}

            OUTPUT FORMAT
            -------------
            Return a single JSON object with this exact structure:
            {{
              "tables": [
                {{
                  "name": "table_name",
                  "description": "What this table stores",
                  "fields": [
                    {{
                      "name": "id",
                      "type": "uuid",
                      "nullable": false,
                      "unique": true,
                      "indexed": true,
                      "foreign_key": null,
                      "default": "uuid_generate_v4()",
                      "description": "Primary key"
                    }},
                    {{
                      "name": "created_at",
                      "type": "datetime",
                      "nullable": false,
                      "default": "now()",
                      "description": "Row creation timestamp"
                    }},
                    {{
                      "name": "updated_at",
                      "type": "datetime",
                      "nullable": false,
                      "default": "now()",
                      "description": "Row last-modified timestamp"
                    }}
                  ],
                  "indexes": [
                    "CREATE INDEX idx_<table>_<col> ON <table>(<col>)"
                  ]
                }}
              ],
              "relationships": [
                {{
                  "from": "orders",
                  "to": "users",
                  "type": "one-to-many"
                }}
              ]
            }}

            Rules:
            - Every table must have id (uuid), created_at (datetime), updated_at (datetime)
            - Use foreign keys for all relationships; name them <entity>_id
            - Include appropriate indexes for all FK columns and frequently-queried columns
            - Type choices: uuid, string, text, integer, bigint, boolean, datetime,
              decimal, json, array, float
            - Model at least one junction/pivot table for every many-to-many relationship
            - Think through all the data the features need — be thorough
        """)

    def _parse_schema_response(self, response: str) -> DBSchema:
        """Parse LLM JSON output into a DBSchema model."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        # Find the first { ... } block
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in schema response")

        data = json.loads(match.group(0))

        tables: list[DBTable] = []
        for t_data in data.get("tables", []):
            fields: list[DBField] = []
            for f_data in t_data.get("fields", []):
                fields.append(DBField(**f_data))
            tables.append(
                DBTable(
                    name=t_data["name"],
                    description=t_data.get("description", ""),
                    fields=fields,
                    indexes=t_data.get("indexes", []),
                )
            )

        relationships = data.get("relationships", [])
        return DBSchema(tables=tables, relationships=relationships)

    def _validate_schema(self, schema: DBSchema) -> None:
        """Perform structural validation of the merged schema.

        Raises:
            SpecificationError: If critical validation rules are violated.
        """
        errors: list[str] = []

        # 1. Required baseline tables must be present
        table_names = schema.table_names()
        for required in ["users", "audit_logs", "refresh_tokens"]:
            if required not in table_names:
                errors.append(f"Required table '{required}' is missing")

        # 2. All tables must have id, created_at, updated_at
        for table in schema.tables:
            field_names = [f.name for f in table.fields]
            for required_col in ["id", "created_at", "updated_at"]:
                if required_col not in field_names:
                    errors.append(
                        f"Table '{table.name}' is missing required column '{required_col}'"
                    )

        # 3. Foreign keys must reference existing tables
        for table in schema.tables:
            for field in table.fields:
                if field.foreign_key:
                    ref_table = field.foreign_key.split(".")[0]
                    if ref_table not in table_names:
                        errors.append(
                            f"Table '{table.name}'.{field.name} references "
                            f"non-existent table '{ref_table}'"
                        )

        # 4. Table names must be snake_case
        snake_re = re.compile(r"^[a-z][a-z0-9_]*$")
        for table in schema.tables:
            if not snake_re.match(table.name):
                errors.append(f"Table name '{table.name}' does not follow snake_case convention")

        if errors:
            raise SpecificationError(
                f"Schema validation failed with {len(errors)} error(s)",
                details={"errors": errors},
            )

        logger.debug("schema_validation_passed", table_count=len(schema.tables))
