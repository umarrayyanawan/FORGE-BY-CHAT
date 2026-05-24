"""Backend Agent — production-grade FastAPI/SQLAlchemy/PostgreSQL code generation.

Writes Python backend code following FORGE's strict coding standards:
FastAPI routers, SQLAlchemy 2.0 async models, Pydantic v2 schemas, service
layer, Alembic migrations, and pytest unit tests.
"""

from __future__ import annotations

from typing import Any, Optional

from system.agents.base import AgentContract, AgentContext, AgentResult, BaseAgent
from system.agents.prompts import (
    BACKEND_SYSTEM_PROMPT_TEMPLATE,
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class BackendAgent(BaseAgent):
    """Specialist agent for Python backend code generation.

    Produces complete, production-grade Python modules for a FastAPI
    application: SQLAlchemy 2.0 async ORM models, Pydantic v2 schemas,
    service-layer functions, FastAPI routers with dependency injection,
    Alembic migrations, and pytest test suites.

    All code strictly follows PEP 8, uses full type annotations (PEP 484),
    and is validated with ruff + mypy (strict mode) conventions.

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the BackendAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.BACKEND, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: Optional[ProjectSpec],
        arch: Optional[ArchitecturePlan],
    ) -> AgentContract:
        """Build a scoped AgentContract for a backend implementation task.

        Parameters
        ----------
        task:
            The TaskNode carrying the backend implementation objective.
        spec:
            Project specification (db schema, API contract, tech stack).
        arch:
            Architecture plan (service topology, scaling profile).

        Returns
        -------
        AgentContract
            Contract scoped to Python backend source and test files.
        """
        return AgentContract(
            identity="backend_agent",
            objective=task.description,
            allowed_files=[
                "system/**/*.py",
                "tests/**/*.py",
                "alembic/**/*.py",
                "alembic/versions/**/*.py",
                "alembic/env.py",
                "alembic/script.py.mako",
                "pyproject.toml",
                "alembic.ini",
            ],
            constraints=[
                "ALWAYS add full PEP 484 type hints to every function and method signature.",
                "ALWAYS write Google-style docstrings for all public APIs, classes, and modules.",
                "ALWAYS write at least one success and one failure pytest unit test for every new function.",
                "NEVER use raw SQL string formatting — use SQLAlchemy ORM or parameterised text() queries.",
                "NEVER store passwords in plain text — always use passlib bcrypt with cost factor ≥12.",
                "ALWAYS use async/await for all database operations (SQLAlchemy async session).",
                "ALWAYS validate all user inputs at API boundaries using Pydantic v2 validators.",
                "NEVER put business logic in FastAPI route handlers — delegate to the service layer.",
                "ALWAYS use SQLAlchemy 2.0 mapped_column() and Mapped[] style, not legacy Column().",
                "NEVER expose internal IDs, stack traces, or sensitive fields in API responses.",
            ],
            validation_rules=[
                "All FastAPI route handlers must have response_model= and status_code= defined.",
                "All SQLAlchemy models must have __tablename__: str class attribute.",
                "All SQLAlchemy models must inherit from the project's declarative Base.",
                "Every Alembic migration must implement both upgrade() and downgrade() functions.",
                "No hardcoded credentials, tokens, or secrets — use os.getenv() or settings.",
                "All async functions must use await for every I/O operation.",
                "Pydantic response schemas must exclude password_hash and internal token fields.",
            ],
            success_criteria=[
                "SQLAlchemy ORM models defined with all required columns and relationships.",
                "Pydantic v2 schemas created: Create, Update, and Response variants.",
                "FastAPI router implemented with all CRUD or domain-specific endpoints.",
                "Service layer written with proper session handling and error propagation.",
                "Alembic migration file created for any schema changes.",
                "pytest unit tests written covering happy path and error scenarios.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.1,
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Backend Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the backend-specific
        technology standards from the template, the current task contract
        details, and annotated code examples for correct FastAPI/SQLAlchemy
        patterns.

        Parameters
        ----------
        contract:
            The AgentContract produced by ``build_contract()``.

        Returns
        -------
        str
            Complete system prompt string ready for the LLM.
        """
        constraints_text = "\n".join(f"  - {c}" for c in contract.constraints)
        validation_text = "\n".join(f"  - {v}" for v in contract.validation_rules)
        success_text = "\n".join(f"  - {s}" for s in contract.success_criteria)

        return f"""{FORGE_AGENT_PREAMBLE}

{BACKEND_SYSTEM_PROMPT_TEMPLATE}

═══════════════════════════════════════════════════════════════════════════════
CURRENT TASK CONTRACT
═══════════════════════════════════════════════════════════════════════════════

### Objective
{contract.objective}

### Hard Constraints (NEVER violate these)
{constraints_text}

### Validation Rules (your output MUST satisfy ALL of these)
{validation_text}

### Success Criteria (define "done" for this task)
{success_text}

═══════════════════════════════════════════════════════════════════════════════
CANONICAL CODE PATTERNS — FOLLOW THESE EXACTLY
═══════════════════════════════════════════════════════════════════════════════

#### 1. SQLAlchemy 2.0 Model (correct pattern)

```python
import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class User(Base):
    \"\"\"ORM model for the users table.\"\"\"

    __tablename__: str = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                     default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now(),
                                                  onupdate=func.now())

    def __repr__(self) -> str:
        return f"User(id={{self.id!r}}, email={{self.email!r}})"
```

#### 2. FastAPI Router (correct pattern)

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/users", tags=["users"])

@router.post(
    "/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user account",
)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_auth),
) -> UserResponse:
    \"\"\"Create a new user account.

    Validates the payload, hashes the password, persists the user,
    and returns the sanitised response schema (no password_hash).

    Raises:
        HTTPException: 409 if the email already exists.
        HTTPException: 422 if the payload fails validation.
    \"\"\"
    return await user_service.create_user(db=db, payload=payload)
```

#### 3. Service Layer (correct pattern)

```python
async def create_user(db: AsyncSession, payload: UserCreate) -> UserResponse:
    \"\"\"Create a new user and persist to the database.

    Args:
        db: Async database session from the FastAPI dependency.
        payload: Validated Pydantic schema with user creation fields.

    Returns:
        UserResponse schema with the newly created user data.

    Raises:
        HTTPException: 409 if a user with the given email already exists.
    \"\"\"
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email {{payload.email!r}} already exists.",
        )
    hashed = password_context.hash(payload.password)
    user = User(email=payload.email, password_hash=hashed)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)
```

#### 4. Pydantic v2 Schema (correct pattern)

```python
from pydantic import BaseModel, ConfigDict, EmailStr, Field

class UserCreate(BaseModel):
    \"\"\"Request schema for creating a new user.\"\"\"
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

class UserResponse(BaseModel):
    \"\"\"Public response schema — password_hash is intentionally excluded.\"\"\"
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    is_active: bool
    created_at: datetime
```

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the backend agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated Python modules, migrations,
            tests, reasoning, and any errors.
        """
        logger.info(
            "backend_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
