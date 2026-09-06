"""Tests for orchestrator integration endpoints in the agent API."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


AGENT_ID = "11111111-1111-4111-8111-111111111111"
PROCESS_GENERATION = "22222222-2222-4222-8222-222222222222"


class _TrackedStream:
    """Async iterator whose explicit close is observable independently."""

    def __init__(self, states, events, *, error=None):
        self._states = list(states)
        self._events = events
        self._error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._states:
            return self._states.pop(0)
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self._events.append("close")


def _recipient(job_id: str) -> dict:
    """A pinned worker mutation names the exact registered process."""

    return {
        "expected_agent_id": AGENT_ID,
        "expected_pod_uid": None,
        "expected_process_generation": PROCESS_GENERATION,
        "expected_job_id": job_id,
    }


def _runtime_client():
    from types import SimpleNamespace

    return SimpleNamespace(
        agent_id=AGENT_ID, dispatch_process_generation=PROCESS_GENERATION
    )


def _workspace_bundle() -> dict:
    return {
        "config_override": {
            "workspace": {
                "backend": "sandbox",
                "remote": {"host": "workspace.internal"},
            }
        },
        "workspace_runtime": {
            "requested_backend": None,
            "assigned_backend": "sandbox",
            "effective_backend": "sandbox",
            "state": "ready",
        },
    }


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
        import agent.api.app as app_module

        # Save original state
        original_agent = app_module._agent
        original_job_id = app_module._current_job_id
        original_orchestrator = app_module._orchestrator_client

        # Set up mocks
        app_module._agent = mock_agent
        app_module._current_job_id = None
        app_module._orchestrator_client = _runtime_client()

        # Create app without lifespan (we're mocking the agent)
        from agent.api.app import create_app

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
        import agent.api.app as app_module

        # Ensure no job is running
        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/start",
            json={
                "job_id": "test-job-123",
                "description": "Test task",
                "recipient": _recipient("test-job-123"),
                **_workspace_bundle(),
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "test-job-123"
        assert data["status"] == "accepted"

    def test_pre_contract_job_start_is_refused(self, test_client, mock_agent):
        import agent.api.app as app_module

        app_module._current_job_id = None
        app_module._agent = mock_agent
        response = test_client.post(
            "/job/start",
            json={
                "job_id": "old-orchestrator",
                "description": "old bundle",
                "recipient": _recipient("old-orchestrator"),
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == (
            "workspace_runtime_authority_missing"
        )

    def test_job_start_rejects_when_busy(self, test_client, mock_agent):
        """Test that /job/start returns 409 when agent is busy."""
        import agent.api.app as app_module

        # Simulate a job already running
        app_module._current_job_id = "existing-job-456"
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/start",
            json={
                "job_id": "new-job-789",
                "description": "Another task",
                "recipient": _recipient("new-job-789"),
                **_workspace_bundle(),
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
        import agent.api.app as app_module

        original_agent = app_module._agent
        original_job_id = app_module._current_job_id
        original_orchestrator = app_module._orchestrator_client

        app_module._agent = mock_agent
        app_module._current_job_id = None
        app_module._orchestrator_client = _runtime_client()

        test_app = create_app_for_testing()

        app_module._agent = mock_agent

        client = TestClient(test_app, raise_server_exceptions=False)

        yield client

        app_module._agent = original_agent
        app_module._current_job_id = original_job_id
        app_module._orchestrator_client = original_orchestrator

    def test_job_cancel_no_job_returns_404(self, test_client, mock_agent):
        """Test that /job/cancel returns 404 when no job is running."""
        import agent.api.app as app_module

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
        import agent.api.app as app_module

        original_agent = app_module._agent
        original_job_id = app_module._current_job_id
        original_orchestrator = app_module._orchestrator_client

        app_module._agent = mock_agent
        app_module._current_job_id = None
        app_module._orchestrator_client = _runtime_client()

        test_app = create_app_for_testing()

        app_module._agent = mock_agent

        client = TestClient(test_app, raise_server_exceptions=False)

        yield client

        app_module._agent = original_agent
        app_module._current_job_id = original_job_id
        app_module._orchestrator_client = original_orchestrator

    def test_job_resume_works(self, test_client, mock_agent):
        """Test that /job/resume accepts a resume request."""
        import agent.api.app as app_module

        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.post(
            "/job/resume",
            json={
                "job_id": "resume-job-123",
                "feedback": "Please continue with step 2",
                "recipient": _recipient("resume-job-123"),
                **_workspace_bundle(),
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
        import agent.api.app as app_module

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
        import agent.api.app as app_module

        app_module._current_job_id = None
        app_module._agent = mock_agent

        response = test_client.get("/job/current")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] is None
        assert data["is_busy"] is False

    def test_get_current_job_when_busy(self, test_client, mock_agent):
        """Test /job/current returns job info when busy."""
        import agent.api.app as app_module

        app_module._current_job_id = "active-job-123"
        app_module._agent = mock_agent

        response = test_client.get("/job/current")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "active-job-123"
        assert data["is_busy"] is True

        # Reset
        app_module._current_job_id = None


class TestPinnedJobStreamClose:
    @pytest.mark.asyncio
    async def test_close_timeout_is_a_typed_lifecycle_failure(self, monkeypatch):
        import asyncio

        import agent.api.app as app_module
        from shared.subagent_lifecycle import SubagentQuiescenceError

        class _HungStream:
            async def aclose(self):
                await asyncio.Future()

        monkeypatch.setattr(app_module, "_JOB_STREAM_CLOSE_TIMEOUT_SECONDS", 0.001)

        with pytest.raises(SubagentQuiescenceError, match="cleanup timed out"):
            await app_module._close_job_stream(_HungStream(), job_id="hung-close")

    @pytest.mark.asyncio
    async def test_fresh_job_closes_before_completion_report_and_cleanup(
        self, monkeypatch
    ):
        import agent.api.app as app_module

        events: list[str] = []
        final = {"should_stop": True, "error": None}

        async def _report(*_args, **_kwargs):
            assert events == ["close"]
            events.append("report")

        client = MagicMock()
        client.agent_id = AGENT_ID
        client.report_completion = AsyncMock(side_effect=_report)
        client.heartbeat = AsyncMock(return_value={})
        agent = MagicMock()
        agent._tool_context = None
        agent.process_job = AsyncMock(return_value=_TrackedStream([final], events))

        monkeypatch.setattr(app_module, "_agent", agent)
        monkeypatch.setattr(app_module, "_orchestrator_client", client)
        monkeypatch.setattr(app_module, "_current_job_id", "fresh-close")
        app_module._clear_stop()
        monkeypatch.setattr(app_module, "_setup_job_file_logging", MagicMock())
        monkeypatch.setattr(
            app_module,
            "_cleanup_job_file_handler",
            MagicMock(side_effect=lambda _job_id: events.append("cleanup")),
        )

        await app_module._process_orchestrator_job("fresh-close", "description")

        assert events == ["close", "report", "cleanup"]

    @pytest.mark.asyncio
    async def test_resumed_job_error_closes_before_error_report_and_cleanup(
        self, monkeypatch
    ):
        import agent.api.app as app_module
        from agent.api.models import JobResumeRequest

        events: list[str] = []

        async def _report(*_args, **_kwargs):
            assert events == ["close"]
            events.append("report")

        client = MagicMock()
        client.agent_id = AGENT_ID
        client.dispatch_process_generation = PROCESS_GENERATION
        client.report_completion = AsyncMock(side_effect=_report)
        client.heartbeat = AsyncMock(return_value={})
        agent = MagicMock()
        agent.config.agent_id = "worker_base"
        agent.process_job = AsyncMock(
            return_value=_TrackedStream(
                [], events, error=RuntimeError("resume stream failed")
            )
        )

        monkeypatch.delenv("POD_UID", raising=False)
        monkeypatch.setattr(app_module, "_agent", agent)
        monkeypatch.setattr(app_module, "_orchestrator_client", client)
        monkeypatch.setattr(app_module, "_current_job_id", None)
        monkeypatch.setattr(app_module, "_current_job_task", None)
        monkeypatch.setattr(app_module, "_shutdown_requested", False)
        app_module._clear_stop()
        monkeypatch.setattr(app_module, "_setup_job_file_logging", MagicMock())
        monkeypatch.setattr(
            app_module,
            "_cleanup_job_file_handler",
            MagicMock(side_effect=lambda _job_id: events.append("cleanup")),
        )

        app = app_module.create_app()
        resume_ep = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/job/resume"
        )
        await resume_ep(
            JobResumeRequest(
                job_id="resume-close",
                recipient=_recipient("resume-close"),
                **_workspace_bundle(),
            ),
            MagicMock(),
        )
        await app_module._current_job_task

        assert events == ["close", "report", "cleanup"]

    @pytest.mark.asyncio
    async def test_fresh_job_lifecycle_failure_drains_without_completion_report(
        self, monkeypatch
    ):
        import agent.api.app as app_module
        from shared.subagent_lifecycle import SubagentQuiescenceError

        events: list[str] = []
        client = MagicMock()
        client.agent_id = AGENT_ID
        client.report_completion = AsyncMock()
        client.heartbeat = AsyncMock(return_value={})
        agent = MagicMock()
        agent._tool_context = None
        agent.abandon_worker_subagents = AsyncMock()
        agent.process_job = AsyncMock(
            return_value=_TrackedStream(
                [], events, error=SubagentQuiescenceError("delivery failed")
            )
        )
        schedule_exit = MagicMock()

        monkeypatch.setattr(app_module, "_agent", agent)
        monkeypatch.setattr(app_module, "_orchestrator_client", client)
        monkeypatch.setattr(app_module, "_current_job_id", "fresh-lifecycle")
        monkeypatch.setattr(app_module, "_shutdown_requested", False)
        monkeypatch.setattr(app_module, "_schedule_fatal_exit", schedule_exit)
        monkeypatch.setattr(app_module, "_setup_job_file_logging", MagicMock())
        monkeypatch.setattr(app_module, "_cleanup_job_file_handler", MagicMock())
        app_module._clear_stop()

        await app_module._process_orchestrator_job("fresh-lifecycle", "description")

        assert events == ["close"]
        client.report_completion.assert_not_awaited()
        agent.abandon_worker_subagents.assert_awaited_once()
        assert client.heartbeat.await_args.kwargs["status"] == "draining"
        assert client.heartbeat.await_args.kwargs["job_id"] == "fresh-lifecycle"
        assert app_module._shutdown_requested is True
        schedule_exit.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_resumed_job_lifecycle_failure_drains_without_completion_report(
        self, monkeypatch
    ):
        import agent.api.app as app_module
        from agent.api.models import JobResumeRequest
        from shared.subagent_lifecycle import SubagentRecoveryError

        events: list[str] = []
        client = MagicMock()
        client.agent_id = AGENT_ID
        client.dispatch_process_generation = PROCESS_GENERATION
        client.report_completion = AsyncMock()
        client.heartbeat = AsyncMock(return_value={})
        agent = MagicMock()
        agent.config.agent_id = "worker_base"
        agent._tool_context = None
        agent.abandon_worker_subagents = AsyncMock()
        agent.process_job = AsyncMock(
            return_value=_TrackedStream(
                [], events, error=SubagentRecoveryError("recovery failed")
            )
        )
        schedule_exit = MagicMock()

        monkeypatch.delenv("POD_UID", raising=False)
        monkeypatch.setattr(app_module, "_agent", agent)
        monkeypatch.setattr(app_module, "_orchestrator_client", client)
        monkeypatch.setattr(app_module, "_current_job_id", None)
        monkeypatch.setattr(app_module, "_current_job_task", None)
        monkeypatch.setattr(app_module, "_shutdown_requested", False)
        monkeypatch.setattr(app_module, "_schedule_fatal_exit", schedule_exit)
        monkeypatch.setattr(app_module, "_setup_job_file_logging", MagicMock())
        monkeypatch.setattr(app_module, "_cleanup_job_file_handler", MagicMock())
        app_module._clear_stop()

        app = app_module.create_app()
        resume_ep = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/job/resume"
        )
        await resume_ep(
            JobResumeRequest(
                job_id="resume-lifecycle",
                recipient=_recipient("resume-lifecycle"),
                **_workspace_bundle(),
            ),
            MagicMock(),
        )
        await app_module._current_job_task

        assert events == ["close"]
        client.report_completion.assert_not_awaited()
        agent.abandon_worker_subagents.assert_awaited_once()
        assert client.heartbeat.await_args.kwargs["status"] == "draining"
        assert client.heartbeat.await_args.kwargs["job_id"] == "resume-lifecycle"
        schedule_exit.assert_called_once_with()


def create_app_for_testing():
    """Create app instance for testing without lifespan management."""
    from agent.api.app import create_app

    return create_app()
