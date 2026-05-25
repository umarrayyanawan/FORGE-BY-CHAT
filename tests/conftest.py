"""Shared pytest fixtures for FORGE test suite."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Create shared event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_llm_client():
    """Mock LLM client that returns predictable responses."""
    client = AsyncMock()
    client.complete = AsyncMock(
        return_value=MagicMock(
            content='{"industry": "manufacturing", "product_type": "crm", "platform": "web", '
            '"core_features": ["contact management", "deal tracking", "invoicing"], '
            '"deployment_target": "docker", "constraints": [], "integrations": [], '
            '"target_users": "sales teams", "scale_requirements": "small team", '
            '"security_requirements": [], "tech_preferences": {}, "timeline": "", "budget_range": ""}',
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=200,
            stop_reason="end_turn",
        )
    )
    client.stream_complete = AsyncMock(return_value=iter([]))
    return client


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.exists = AsyncMock(return_value=0)
    redis.publish = AsyncMock(return_value=1)
    redis.pipeline = MagicMock(return_value=AsyncMock())
    return redis


@pytest.fixture
def mock_db_session():
    """Mock SQLAlchemy async session."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def sample_project_intent():
    """Sample ProjectIntent for testing."""
    from system.core.intent.schemas import IntentStatus, ProjectIntent
    from system.shared.models import DeployTarget, Platform

    return ProjectIntent(
        raw_prompt="Build a CRM for marble suppliers",
        industry="manufacturing",
        product_type="crm",
        platform=Platform.WEB,
        core_features=[
            "contact management",
            "deal tracking",
            "invoice generation",
            "inventory management",
        ],
        deployment_target=DeployTarget.DOCKER,
        constraints=["must support multi-tenancy"],
        integrations=["stripe", "email"],
        target_users="sales and operations teams",
        scale_requirements="50-100 users",
        security_requirements=["data encryption at rest", "audit logs"],
        tech_preferences={},
        timeline="3 months",
        budget_range="startup",
        status=IntentStatus.VALIDATED,
        confidence_score=0.9,
        missing_fields=[],
    )


@pytest.fixture
def sample_task_node():
    """Sample TaskNode for testing agent execution."""
    from system.core.orchestration.task_schemas import TaskNode
    from system.shared.models import AgentType, ExecutionPhase, Priority, TaskStatus

    return TaskNode(
        task_id="task-test-001",
        name="Generate user authentication models",
        description="Create SQLAlchemy models for User, Session, and RefreshToken tables",
        agent_type=AgentType.BACKEND,
        priority=Priority.HIGH,
        status=TaskStatus.PENDING,
        dependencies=[],
        blocking=["task-test-002"],
        validation_rules=[],
        input_context={"feature": "authentication"},
        output_artifacts=["system/models/auth.py", "tests/unit/test_auth_models.py"],
        project_id="proj-test-001",
        phase=ExecutionPhase.EXECUTION,
    )
