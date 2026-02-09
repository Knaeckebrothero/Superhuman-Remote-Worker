"""Tests for orchestrator integration endpoints in the agent API."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient



class TestJobStartEndpoint:
    """Tests for POST /job/start endpoint."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock agent."""
        agent = MagicMock()
        agent.config.agent_id = "test-agent"
        agent.config.polling.enabled = False
        agent.get_status.return_value = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "initialized": True,
            "jobs_processed": 0,
            "uptime_seconds": 100,
            "connections": {"postgres": True, "neo4j": False},
            "config": {},
        }
        agent.initialize = AsyncMock()
        agent.shutdown = AsyncMock()
        agent.process_job = AsyncMock(return_value={"should_stop": True})
        return agent

    @pytest.fixture
    def test_client(self, mock_agent):
        """Create a test client with mocked agent."""
        import src.api.app as app_module

        # Save original state
        original_agent = app_module._agent
        original_job_id = app_module._current_job_id
        original_orchestrator = app_module._orchestrator_client

        # Set up mocks
        app_module._agent = mock_agent
        app_module._current_job_id = None
        app_module._orchestrator_client = None

        # Create app without lifespan (we're mocking the agent)
        from src.api.app import create_app

        # We need to create a fresh app instance
        test_app = create_app()

        # Override the agent in the app's state
        app_module._agent = mock_agent

        client = TestClient(test_app, raise_server_exceptions=False)

        yield client

        # Restore original state
        app_module._agent = original_agent
        app_module._current_job_id = original_job_id
        app_module._orchestrator_client = original_orchestrator

    def test_job_start_accepts_and_returns_202(self, test_client, mock_agent):
        """Test that /job/start accepts a job and returns 202."""
        import src.api.app as app_module

        # Ensure no job is running
        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/start",
            json={
                "job_id": "test-job-123",
                "description": "Test task",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "test-job-123"
        assert data["status"] == "accepted"

    def test_job_start_rejects_when_busy(self, test_client, mock_agent):
        """Test that /job/start returns 409 when agent is busy."""
        import src.api.app as app_module

        # Simulate a job already running
        app_module._current_job_id = "existing-job-456"
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/start",
            json={
                "job_id": "new-job-789",
                "description": "Another task",
            },
        )

        assert response.status_code == 409
        assert "busy" in response.json()["detail"].lower()

        # Reset
        app_module._current_job_id = None


class TestJobCancelEndpoint:
    """Tests for POST /job/cancel endpoint."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock agent."""
        agent = MagicMock()
        agent.config.agent_id = "test-agent"
        agent.config.polling.enabled = False
        agent.get_status.return_value = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "initialized": True,
            "jobs_processed": 0,
            "uptime_seconds": 100,
            "connections": {"postgres": True, "neo4j": False},
            "config": {},
        }
        agent.initialize = AsyncMock()
        agent.shutdown = AsyncMock()
        return agent

    @pytest.fixture
    def test_client(self, mock_agent):
        """Create a test client with mocked agent."""
        import src.api.app as app_module

        original_agent = app_module._agent
        original_job_id = app_module._current_job_id

        app_module._agent = mock_agent
        app_module._current_job_id = None

        test_app = create_app_for_testing()

        app_module._agent = mock_agent

        client = TestClient(test_app, raise_server_exceptions=False)

        yield client

        app_module._agent = original_agent
        app_module._current_job_id = original_job_id

    def test_job_cancel_no_job_returns_404(self, test_client, mock_agent):
        """Test that /job/cancel returns 404 when no job is running."""
        import src.api.app as app_module

        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/cancel",
            json={"reason": "Test cancellation"},
        )

        assert response.status_code == 404


class TestJobResumeEndpoint:
    """Tests for POST /job/resume endpoint."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock agent."""
        agent = MagicMock()
        agent.config.agent_id = "test-agent"
        agent.config.polling.enabled = False
        agent.get_status.return_value = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "initialized": True,
            "jobs_processed": 0,
            "uptime_seconds": 100,
            "connections": {"postgres": True, "neo4j": False},
            "config": {},
        }
        agent.initialize = AsyncMock()
        agent.shutdown = AsyncMock()
        agent.process_job = AsyncMock(return_value={"should_stop": True})
        return agent

    @pytest.fixture
    def test_client(self, mock_agent):
        """Create a test client with mocked agent."""
        import src.api.app as app_module

        original_agent = app_module._agent
        original_job_id = app_module._current_job_id

        app_module._agent = mock_agent
        app_module._current_job_id = None

        test_app = create_app_for_testing()

        app_module._agent = mock_agent

        client = TestClient(test_app, raise_server_exceptions=False)

        yield client

        app_module._agent = original_agent
        app_module._current_job_id = original_job_id

    def test_job_resume_works(self, test_client, mock_agent):
        """Test that /job/resume accepts a resume request."""
        import src.api.app as app_module

        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/resume",
            json={
                "job_id": "resume-job-123",
                "feedback": "Please continue with step 2",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "resume-job-123"
        assert data["status"] == "accepted"


class TestGetCurrentJobEndpoint:
    """Tests for GET /job/current endpoint."""

    @pytest.fixture
    def mock_agent(self):
        """Create a mock agent."""
        agent = MagicMock()
        agent.config.agent_id = "test-agent"
        agent.config.polling.enabled = False
        agent.get_status.return_value = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "initialized": True,
            "jobs_processed": 0,
            "uptime_seconds": 100,
            "connections": {"postgres": True, "neo4j": False},
            "config": {},
        }
        agent.initialize = AsyncMock()
        agent.shutdown = AsyncMock()
        return agent

    @pytest.fixture
    def test_client(self, mock_agent):
        """Create a test client with mocked agent."""
        import src.api.app as app_module

        original_agent = app_module._agent
        original_job_id = app_module._current_job_id

        app_module._agent = mock_agent

        test_app = create_app_for_testing()

        client = TestClient(test_app, raise_server_exceptions=False)

        yield client

        app_module._agent = original_agent
        app_module._current_job_id = original_job_id

    def test_get_current_job_when_idle(self, test_client, mock_agent):
        """Test /job/current returns no job when idle."""
        import src.api.app as app_module

        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.get("/job/current")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] is None
        assert data["is_busy"] is False

    def test_get_current_job_when_busy(self, test_client, mock_agent):
        """Test /job/current returns job info when busy."""
        import src.api.app as app_module

        app_module._current_job_id = "active-job-123"
        app_module._agent = mock_agent

        response = test_client.get("/job/current")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "active-job-123"
        assert data["is_busy"] is True

        # Reset
        app_module._current_job_id = None


def create_app_for_testing():
    """Create app instance for testing without lifespan management."""
    from src.api.app import create_app

    return create_app()
