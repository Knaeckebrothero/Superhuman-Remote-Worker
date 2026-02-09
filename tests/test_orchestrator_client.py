"""Tests for the orchestrator client module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.orchestrator_client import (
    OrchestratorClient,
    create_orchestrator_client_from_env,
    get_agent_ip,
    get_hostname,
)


class TestGetAgentIp:
    """Tests for get_agent_ip function."""

    def test_get_agent_ip_from_env(self):
        """Test that AGENT_POD_IP env var is used when set."""
        with patch.dict(os.environ, {"AGENT_POD_IP": "192.168.1.100"}):
            ip = get_agent_ip()
            assert ip == "192.168.1.100"

    def test_get_agent_ip_auto_detect(self):
        """Test IP auto-detection when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove AGENT_POD_IP if it exists
            os.environ.pop("AGENT_POD_IP", None)
            ip = get_agent_ip()
            # Should return some valid IP (localhost or detected)
            assert ip is not None
            assert len(ip) > 0


class TestGetHostname:
    """Tests for get_hostname function."""

    def test_get_hostname_from_env(self):
        """Test that AGENT_HOSTNAME env var is used when set."""
        with patch.dict(os.environ, {"AGENT_HOSTNAME": "test-pod-1"}):
            hostname = get_hostname()
            assert hostname == "test-pod-1"

    def test_get_hostname_auto_detect(self):
        """Test hostname auto-detection when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AGENT_HOSTNAME", None)
            hostname = get_hostname()
            # Should return some valid hostname
            assert hostname is not None
            assert len(hostname) > 0


class TestOrchestratorClient:
    """Tests for OrchestratorClient class."""

    @pytest.fixture
    def client(self):
        """Create a test client instance."""
        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.5",
            pod_port=8001,
            hostname="test-agent",
            config_name="creator",
            pid=12345,
        )

    @pytest.mark.asyncio
    async def test_connect(self, client):
        """Test client connection."""
        await client.connect()
        assert client._client is not None
        await client.close()

    @pytest.mark.asyncio
    async def test_close(self, client):
        """Test client close."""
        await client.connect()
        await client.close()
        assert client._client is None

    @pytest.mark.asyncio
    async def test_register_success(self, client):
        """Test successful registration."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "agent_id": "agent-123",
            "heartbeat_interval_seconds": 60,
        }

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await client.register()

            assert result is True
            assert client.agent_id == "agent-123"
            assert client.heartbeat_interval == 60
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_failure(self, client):
        """Test registration failure is handled gracefully."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await client.register()

            assert result is False
            assert client.agent_id is None

    @pytest.mark.asyncio
    async def test_register_connection_error(self, client):
        """Test registration handles connection errors gracefully."""
        import httpx

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(
                side_effect=httpx.RequestError("Connection refused")
            )

            result = await client.register()

            assert result is False

    @pytest.mark.asyncio
    async def test_heartbeat_success(self, client):
        """Test successful heartbeat."""
        client.agent_id = "agent-123"

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await client.heartbeat(
                status="ready",
                job_id=None,
                metrics={"memory_mb": 512, "cpu_percent": 25.5},
            )

            assert result is True
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_without_agent_id(self, client):
        """Test heartbeat fails when agent_id not set."""
        client.agent_id = None

        result = await client.heartbeat(status="ready")

        assert result is False

    @pytest.mark.asyncio
    async def test_deregister_success(self, client):
        """Test successful deregistration."""
        client.agent_id = "agent-123"

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.delete = AsyncMock(return_value=mock_response)

            result = await client.deregister()

            assert result is True
            assert client.agent_id is None

    @pytest.mark.asyncio
    async def test_deregister_without_agent_id(self, client):
        """Test deregister fails when agent_id not set."""
        client.agent_id = None

        result = await client.deregister()

        assert result is False

    @pytest.mark.asyncio
    async def test_stop_heartbeat(self, client):
        """Test stop_heartbeat sets the event."""
        assert not client._stop_heartbeat.is_set()
        client.stop_heartbeat()
        assert client._stop_heartbeat.is_set()


class TestCreateOrchestratorClientFromEnv:
    """Tests for create_orchestrator_client_from_env function."""

    def test_returns_none_without_url(self):
        """Test returns None when ORCHESTRATOR_URL not set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ORCHESTRATOR_URL", None)
            client = create_orchestrator_client_from_env("creator")
            assert client is None

    def test_creates_client_with_url(self):
        """Test creates client when ORCHESTRATOR_URL is set."""
        with patch.dict(
            os.environ,
            {
                "ORCHESTRATOR_URL": "http://localhost:8085",
                "AGENT_POD_IP": "10.0.0.5",
                "AGENT_POD_PORT": "8001",
                "AGENT_HOSTNAME": "test-agent",
            },
        ):
            client = create_orchestrator_client_from_env("creator")

            assert client is not None
            assert client.orchestrator_url == "http://localhost:8085"
            assert client.pod_ip == "10.0.0.5"
            assert client.pod_port == 8001
            assert client.hostname == "test-agent"
            assert client.config_name == "creator"

    def test_uses_defaults_for_optional_vars(self):
        """Test uses defaults when optional env vars not set."""
        with patch.dict(os.environ, {"ORCHESTRATOR_URL": "http://localhost:8085"}):
            os.environ.pop("AGENT_POD_IP", None)
            os.environ.pop("AGENT_POD_PORT", None)
            os.environ.pop("AGENT_HOSTNAME", None)

            client = create_orchestrator_client_from_env("validator")

            assert client is not None
            assert client.pod_port == 8001  # Default
            assert client.pod_ip is not None  # Auto-detected
            assert client.hostname is not None  # Auto-detected
