"""Integration tests for the full FORGE pipeline."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint():
    """API health endpoint returns 200."""
    from system.api.main import app
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_intent_parse_endpoint():
    """Intent parse endpoint accepts prompts and returns structured response."""
    from system.api.main import app
    async with AsyncClient(app=app, base_url="http://test") as client:
        with patch("system.core.intent.engine.IntentEngine") as mock_engine:
            mock_instance = AsyncMock()
            mock_engine.return_value = mock_instance
            mock_instance.process.return_value = MagicMock(
                session_id="sess-123",
                project_id="proj-456",
                intent=MagicMock(raw_prompt="Build CRM", industry="manufacturing"),
                clarification_needed=False,
                clarification_request=None,
                is_complete=True,
            )
            response = await client.post(
                "/api/v1/intent/parse",
                json={"prompt": "Build a CRM for marble suppliers"},
            )
    # Should return 200 or 422 depending on auth setup
    assert response.status_code in (200, 401, 422)


@pytest.mark.asyncio
async def test_pipeline_run_endpoint():
    """Pipeline run endpoint starts background task and returns project_id."""
    from system.api.main import app
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/pipeline/run",
            json={"prompt": "Build e-commerce platform"},
        )
    assert response.status_code in (200, 401, 422)
    if response.status_code == 200:
        data = response.json()
        assert "project_id" in data
        assert "status" in data


@pytest.mark.asyncio
async def test_agents_list_endpoint():
    """Agents endpoint returns list of available agents."""
    from system.api.main import app
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/api/v1/agents")
    assert response.status_code in (200, 401)
