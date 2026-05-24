"""QA Agent — comprehensive pytest test suite generation for FORGE outputs.

Writes production-grade pytest suites with ≥80% coverage for code produced
by the Backend, Frontend, and other agents.  Follows strict test-isolation
rules: every test is independently runnable, external APIs are always mocked,
and database state is rolled back after each test using session-scoped fixtures.
"""

from __future__ import annotations

from typing import Any, Optional

from system.agents.base import AgentContract, AgentContext, AgentResult, BaseAgent
from system.agents.prompts import (
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    QA_SYSTEM_PROMPT_TEMPLATE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class QAAgent(BaseAgent):
    """Specialist agent for comprehensive pytest test suite generation.

    Produces complete, isolated test suites for Python backend code:
    unit tests with full mock isolation, integration tests against a test
    database, API endpoint tests using httpx.AsyncClient, and shared
    conftest.py fixtures with proper teardown.

    All tests use pytest-asyncio with ``asyncio_mode = "auto"`` for async
    test functions, mock all external HTTP calls and third-party services,
    and aim for ≥80% line coverage on new code with 100% branch coverage
    on critical paths (authentication, data mutations, payment flows).

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the QAAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.QA, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: Optional[ProjectSpec],
        arch: Optional[ArchitecturePlan],
    ) -> AgentContract:
        """Build a scoped AgentContract for a QA test generation task.

        The QA agent reads source files from ``system/`` and writes test
        files exclusively to the ``tests/`` directory.  It does NOT modify
        any source file.

        Parameters
        ----------
        task:
            The TaskNode carrying the test generation objective.
        spec:
            Project specification (API contract, data models).
        arch:
            Architecture plan (service topology for integration test setup).

        Returns
        -------
        AgentContract
            Contract scoped to test files (write) and source files (read).
        """
        return AgentContract(
            identity="qa_agent",
            objective=task.description,
            allowed_files=[
                "tests/**/*.py",
                "tests/conftest.py",
                "tests/unit/**/*.py",
                "tests/integration/**/*.py",
                "tests/e2e/**/*.py",
                "pytest.ini",
                "pyproject.toml",
                # Read-only source files for context (QA reads but MUST NOT write to system/)
                "system/**/*.py",
            ],
            constraints=[
                "ALWAYS use @pytest.mark.asyncio (or asyncio_mode='auto') for every async test function.",
                "NEVER write a test that depends on the execution order of other tests — every test must be fully isolated.",
                "ALWAYS mock all external APIs, HTTP calls, email services, payment gateways, and third-party SDKs.",
                "ALWAYS aim for ≥80% line coverage on all new code, and 100% branch coverage on auth/payment flows.",
                "NEVER commit tests with hardcoded credentials, tokens, or real API keys.",
                "ALWAYS use pytest fixtures with function scope for database sessions (rollback after each test).",
                "NEVER call asyncio.run() inside an async test or fixture — let pytest-asyncio manage the event loop.",
                "ALWAYS assert on specific values and status codes, not just truthiness.",
                "ALWAYS test both the happy path AND at least one failure/error path for every function.",
                "NEVER write tests that make real network calls to external services.",
            ],
            validation_rules=[
                "All async test functions use @pytest.mark.asyncio or asyncio_mode='auto' is configured.",
                "All test functions have docstrings describing the behaviour under test.",
                "All external service calls are mocked — no real HTTP calls in any test.",
                "Database fixture uses rollback-after-each-test pattern (not truncate).",
                "Every public function in the code under test has at least one test.",
                "Test file names follow the pattern: test_<module_being_tested>.py.",
                "conftest.py provides: db session fixture, app fixture, and AsyncClient fixture.",
            ],
            success_criteria=[
                "conftest.py written with reusable fixtures: async db session, test app, and httpx AsyncClient.",
                "Unit tests written for all service layer functions with full mock isolation.",
                "Integration tests written for all FastAPI endpoints (success and error cases).",
                "Coverage configuration added to pyproject.toml targeting ≥80% line coverage.",
                "All edge cases from the task description covered by explicit test cases.",
                "pytest.ini or pyproject.toml configured with asyncio_mode=auto.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.1,
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the QA Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the QA-specific testing
        standards from the template, the current task contract details,
        and canonical code examples for async fixtures, mocked API tests,
        and parametrised test cases.

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

{QA_SYSTEM_PROMPT_TEMPLATE}

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
CANONICAL TEST PATTERNS — FOLLOW THESE EXACTLY
═══════════════════════════════════════════════════════════════════════════════

#### 1. conftest.py with async fixtures (correct pattern)

```python
# tests/conftest.py
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from system.main import app
from system.db.base import Base
from system.db.session import get_db

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture(scope="session")
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest_asyncio.fixture
async def db(engine) -> AsyncSession:
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        async with session.begin():
            yield session
            await session.rollback()  # Roll back after each test for isolation

@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncClient:
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()
```

#### 2. API endpoint test (correct pattern)

```python
# tests/integration/test_users_router.py
import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio

class TestCreateUser:
    async def test_create_user__returns_201_on_valid_input(
        self, client: AsyncClient
    ) -> None:
        \"\"\"POST /users with valid payload creates a user and returns 201.\"\"\"
        payload = {{"email": "alice@example.com", "password": "securepass123"}}

        response = await client.post("/api/v1/users", json=payload)

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "alice@example.com"
        assert "password_hash" not in body
        assert "id" in body

    async def test_create_user__returns_409_when_email_exists(
        self, client: AsyncClient
    ) -> None:
        \"\"\"POST /users with a duplicate email returns 409 Conflict.\"\"\"
        payload = {{"email": "duplicate@example.com", "password": "securepass123"}}
        await client.post("/api/v1/users", json=payload)  # First creation

        response = await client.post("/api/v1/users", json=payload)  # Duplicate

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"].lower()

    async def test_create_user__returns_422_on_invalid_email(
        self, client: AsyncClient
    ) -> None:
        \"\"\"POST /users with an invalid email returns 422 Unprocessable Entity.\"\"\"
        payload = {{"email": "not-an-email", "password": "securepass123"}}

        response = await client.post("/api/v1/users", json=payload)

        assert response.status_code == 422
```

#### 3. Service unit test with mocks (correct pattern)

```python
# tests/unit/test_user_service.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio

class TestCreateUserService:
    async def test_create_user__success(self, db: AsyncSession) -> None:
        \"\"\"create_user() persists a user and returns a UserResponse schema.\"\"\"
        from system.services.user_service import create_user
        from system.schemas.user_schemas import UserCreate

        payload = UserCreate(email="test@example.com", password="password123")

        result = await create_user(db=db, payload=payload)

        assert result.email == "test@example.com"
        assert result.id is not None

    async def test_create_user__raises_409_on_duplicate(self, db: AsyncSession) -> None:
        \"\"\"create_user() raises HTTPException(409) for duplicate email.\"\"\"
        from fastapi import HTTPException
        from system.services.user_service import create_user
        from system.schemas.user_schemas import UserCreate
        import pytest

        payload = UserCreate(email="dupe@example.com", password="password123")
        await create_user(db=db, payload=payload)

        with pytest.raises(HTTPException) as exc_info:
            await create_user(db=db, payload=payload)

        assert exc_info.value.status_code == 409
```

#### 4. Mocking an external HTTP call (correct pattern)

```python
# tests/unit/test_email_service.py
import pytest
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio

async def test_send_welcome_email__calls_provider_with_correct_payload() -> None:
    \"\"\"send_welcome_email() calls the email provider API with the correct payload.\"\"\"
    from system.services.email_service import send_welcome_email

    with patch("system.services.email_service.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=AsyncMock(status_code=200))
        mock_client_cls.return_value = mock_client

        await send_welcome_email(to="user@example.com", name="Alice")

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["json"]["to"] == "user@example.com"
```

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the QA agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Note: The QA agent may read source files from ``system/`` but must
        only write to ``tests/``.  The base class ``_validate_file_access``
        enforces this via the allowed_files contract.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated pytest test modules,
            conftest.py, reasoning, and any errors.
        """
        logger.info(
            "qa_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
