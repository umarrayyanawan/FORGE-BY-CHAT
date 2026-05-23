"""Pydantic schemas for the FORGE Specification Engine (Phase 3).

These schemas represent the full data contract for a ProjectSpec — the
authoritative blueprint generated from a ProjectIntent before architecture
planning begins.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from system.shared.models import BaseForgeModel, TimestampedModel
from system.core.intent.schemas import ProjectIntent


# ========================================================================== #
# Database Schema
# ========================================================================== #


class DBField(BaseForgeModel):
    """Single column definition within a database table."""

    name: str = Field(description="Column name in snake_case")
    type: str = Field(
        description=(
            "Logical type: 'string', 'integer', 'boolean', 'datetime', "
            "'uuid', 'json', 'array', 'decimal', 'text', 'float'"
        )
    )
    nullable: bool = Field(default=True, description="Whether the column allows NULL")
    unique: bool = Field(default=False, description="Whether a UNIQUE constraint is applied")
    indexed: bool = Field(default=False, description="Whether a single-column index is created")
    foreign_key: Optional[str] = Field(
        default=None,
        description="Foreign key reference in 'table.column' form, e.g. 'users.id'",
    )
    default: Optional[Any] = Field(
        default=None,
        description="Default column value expressed as a Python literal or SQL expression string",
    )
    description: str = Field(default="", description="Human-readable column description")

    @field_validator("name")
    @classmethod
    def name_must_be_snake_case(cls, v: str) -> str:
        if not v.islower() and "_" not in v:
            pass  # allow mixed naming from LLM, just strip whitespace
        return v.strip()

    @field_validator("type")
    @classmethod
    def type_must_be_known(cls, v: str) -> str:
        known = {
            "string", "integer", "boolean", "datetime", "uuid",
            "json", "array", "decimal", "text", "float", "bigint",
            "smallint", "bytea", "timestamp", "date", "time",
        }
        lower = v.lower()
        if lower not in known:
            # Accept unknown types from LLM without crashing — validation
            # layer will flag them as warnings.
            return v
        return lower


class DBTable(BaseForgeModel):
    """Full table definition including columns and metadata."""

    name: str = Field(description="Table name in snake_case (plural preferred)")
    fields: List[DBField] = Field(description="Ordered list of column definitions")
    description: str = Field(default="", description="Purpose of this table")
    indexes: List[str] = Field(
        default_factory=list,
        description=(
            "Additional multi-column or partial index definitions in SQL notation, "
            "e.g. 'CREATE INDEX idx_orders_user_status ON orders(user_id, status)'"
        ),
    )

    @field_validator("fields")
    @classmethod
    def must_have_id_field(cls, v: List[DBField]) -> List[DBField]:
        names = [f.name for f in v]
        if "id" not in names:
            # Inject a UUID PK at the front
            pk = DBField(
                name="id",
                type="uuid",
                nullable=False,
                unique=True,
                indexed=True,
                description="Primary key (UUID v4)",
            )
            v = [pk] + list(v)
        return v


class DBSchema(BaseForgeModel):
    """Complete normalised database schema for the project."""

    tables: List[DBTable] = Field(description="All table definitions")
    relationships: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Entity-relationship descriptors: "
            "[{from: 'table', to: 'table', type: 'one-to-many|many-to-many|one-to-one'}]"
        ),
    )

    def get_table(self, name: str) -> Optional[DBTable]:
        """Return a table by name, or None if not found."""
        for t in self.tables:
            if t.name == name:
                return t
        return None

    def table_names(self) -> List[str]:
        return [t.name for t in self.tables]


# ========================================================================== #
# API Contract
# ========================================================================== #


class APIEndpoint(BaseForgeModel):
    """Single REST API endpoint specification."""

    path: str = Field(description="URL path, e.g. '/users/{user_id}'")
    method: str = Field(description="HTTP method: GET, POST, PUT, DELETE, PATCH")
    description: str = Field(description="What this endpoint does")
    request_body: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema of the request body (for POST/PUT/PATCH)",
    )
    response_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema of the successful response payload",
    )
    auth_required: bool = Field(
        default=True,
        description="Whether the endpoint requires a valid JWT",
    )
    roles: List[str] = Field(
        default_factory=list,
        description="Role names allowed to call this endpoint (empty = all authenticated)",
    )
    rate_limit: Optional[str] = Field(
        default=None,
        description="Rate limit expression, e.g. '100/minute', '1000/hour'",
    )
    query_params: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Supported query parameters [{name, type, required, description}]",
    )

    @field_validator("method")
    @classmethod
    def method_must_be_valid(cls, v: str) -> str:
        valid = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"Invalid HTTP method: {v}")
        return upper


class APIContract(BaseForgeModel):
    """Complete REST API contract for the project."""

    version: str = Field(default="v1", description="API version string")
    base_path: str = Field(default="/api/v1", description="Common path prefix")
    endpoints: List[APIEndpoint] = Field(description="All API endpoints")
    auth_scheme: str = Field(
        default="JWT",
        description="Authentication scheme: JWT, OAuth2, API_KEY, etc.",
    )
    global_headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Headers required on all requests, e.g. {'X-Request-ID': 'string'}",
    )

    def get_endpoints_for_path(self, path: str) -> List[APIEndpoint]:
        return [e for e in self.endpoints if e.path == path]

    def endpoint_count(self) -> int:
        return len(self.endpoints)


# ========================================================================== #
# UI Structure
# ========================================================================== #


class UIPage(BaseForgeModel):
    """Single page or view in the application's UI."""

    name: str = Field(description="Human-readable page name, e.g. 'Dashboard'")
    route: str = Field(description="Client-side route path, e.g. '/dashboard'")
    description: str = Field(description="What the user can do on this page")
    components: List[str] = Field(
        description="UI component names rendered on this page, e.g. ['DataTable', 'StatsCard']"
    )
    data_requirements: List[str] = Field(
        description=(
            "API endpoints or data sources this page depends on, "
            "e.g. ['GET /api/v1/orders', 'WebSocket /ws/notifications']"
        )
    )
    auth_required: bool = Field(
        default=True,
        description="Whether the user must be logged in to access this page",
    )
    roles: List[str] = Field(
        default_factory=list,
        description="Roles that can see this page (empty = all authenticated)",
    )


class UIStructure(BaseForgeModel):
    """Complete frontend UI architecture."""

    pages: List[UIPage] = Field(description="All pages / views in the application")
    global_components: List[str] = Field(
        description="Components used across multiple pages, e.g. ['Navbar', 'Sidebar', 'Footer']"
    )
    theme: Dict[str, str] = Field(
        default_factory=dict,
        description="Design token overrides, e.g. {'primary': '#3B82F6', 'radius': '8px'}",
    )
    navigation: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Navigation items: [{label, route, icon, role_required}]"
        ),
    )
    state_management: str = Field(
        default="React Query + Zustand",
        description="Client-side state management approach",
    )


# ========================================================================== #
# Permissions Matrix
# ========================================================================== #


class PermissionMatrix(BaseForgeModel):
    """Role-based access control matrix for the project."""

    roles: List[str] = Field(
        description="All role names in the system, e.g. ['admin', 'user', 'viewer']"
    )
    permissions: Dict[str, List[str]] = Field(
        description=(
            "Mapping of role name → list of permission strings, "
            "e.g. {'admin': ['users:read', 'users:write', 'users:delete']}"
        )
    )

    def role_has_permission(self, role: str, permission: str) -> bool:
        return permission in self.permissions.get(role, [])

    def roles_with_permission(self, permission: str) -> List[str]:
        return [r for r, perms in self.permissions.items() if permission in perms]


# ========================================================================== #
# Feature Dependency
# ========================================================================== #


class FeatureDependency(BaseForgeModel):
    """Directed dependency edge between two features."""

    feature: str = Field(description="The feature being described")
    depends_on: List[str] = Field(
        description="Features that must be built before this feature"
    )
    blocking: bool = Field(
        default=False,
        description=(
            "If True, this feature cannot be started until all depends_on are complete. "
            "If False, partial parallelism is allowed."
        ),
    )
    estimated_days: int = Field(
        default=3,
        description="Rough engineering effort estimate in developer-days",
    )


# ========================================================================== #
# Service Topology
# ========================================================================== #


class ServiceTopology(BaseForgeModel):
    """High-level service decomposition of the system."""

    services: List[Dict[str, Any]] = Field(
        description=(
            "Service definitions: "
            "[{name, type, description, port, dependencies, technology}]"
        )
    )
    communication_patterns: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Service-to-service communication: "
            "[{from, to, protocol: 'HTTP'|'gRPC'|'queue'|'event', description}]"
        ),
    )

    def service_names(self) -> List[str]:
        return [s.get("name", "") for s in self.services]


# ========================================================================== #
# Project Spec (top-level)
# ========================================================================== #


class ProjectSpec(TimestampedModel):
    """The complete, authoritative specification for a FORGE project.

    Generated by the SpecificationEngine from a ProjectIntent; consumed by
    the ArchitectureEngine, TaskGraph planner, and all downstream agents.
    """

    project_id: str = Field(description="UUID linking this spec to its project")
    intent: ProjectIntent = Field(description="The intent this spec was derived from")
    prd: str = Field(description="Full Product Requirements Document in Markdown")
    db_schema: DBSchema = Field(description="Normalised PostgreSQL database schema")
    api_contract: APIContract = Field(description="RESTful API contract")
    ui_structure: UIStructure = Field(description="Frontend page and component map")
    service_topology: ServiceTopology = Field(description="Service decomposition")
    permissions_matrix: PermissionMatrix = Field(description="RBAC matrix")
    feature_dependency_map: List[FeatureDependency] = Field(
        description="Directed dependency graph for all features"
    )
    tech_stack: Dict[str, str] = Field(
        description="Layer → technology mapping, e.g. {'backend': 'FastAPI + Python 3.12'}"
    )
    estimated_complexity: str = Field(
        description="Project complexity estimate: 'low', 'medium', 'high', 'enterprise'"
    )
    version: int = Field(default=1, description="Spec revision number (increments on updates)")

    @field_validator("estimated_complexity")
    @classmethod
    def validate_complexity(cls, v: str) -> str:
        valid = {"low", "medium", "high", "enterprise"}
        if v.lower() not in valid:
            return "medium"  # safe default from LLM hallucination
        return v.lower()

    def to_markdown(self) -> str:
        """Render a human-readable Markdown summary of this spec."""
        lines: List[str] = [
            f"# Project Specification: {self.intent.raw_prompt[:80]}",
            "",
            f"**Project ID:** `{self.project_id}`",
            f"**Version:** {self.version}",
            f"**Complexity:** {self.estimated_complexity}",
            f"**Generated:** {self.created_at.isoformat()}",
            "",
            "---",
            "",
            "## Product Requirements Document",
            "",
            self.prd,
            "",
            "---",
            "",
            "## Database Schema",
            "",
        ]

        for table in self.db_schema.tables:
            lines.append(f"### `{table.name}`")
            lines.append(f"{table.description}")
            lines.append("")
            lines.append("| Column | Type | Nullable | Unique | FK |")
            lines.append("|--------|------|----------|--------|----|")
            for field in table.fields:
                fk = field.foreign_key or ""
                lines.append(
                    f"| {field.name} | {field.type} | {field.nullable} "
                    f"| {field.unique} | {fk} |"
                )
            lines.append("")

        lines += [
            "---",
            "",
            "## API Contract",
            "",
            f"**Base Path:** `{self.api_contract.base_path}`  "
            f"**Auth:** {self.api_contract.auth_scheme}",
            "",
            "| Method | Path | Description | Auth |",
            "|--------|------|-------------|------|",
        ]
        for ep in self.api_contract.endpoints:
            lines.append(
                f"| {ep.method} | `{ep.path}` | {ep.description} | {ep.auth_required} |"
            )

        lines += [
            "",
            "---",
            "",
            "## Tech Stack",
            "",
        ]
        for layer, tech in self.tech_stack.items():
            lines.append(f"- **{layer}**: {tech}")

        return "\n".join(lines)
