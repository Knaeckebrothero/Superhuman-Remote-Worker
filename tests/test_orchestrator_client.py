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

    def test_returns_client_with_default_url(self):
        """Test returns client with default URL when ORCHESTRATOR_URL not set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ORCHESTRATOR_URL", None)
            client = create_orchestrator_client_from_env("creator")
            assert client is not None
            assert client.orchestrator_url == "http://localhost:8085"

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


class TestTriggerSubjobMerge:
    """Tests for trigger_subjob_merge method."""

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
    async def test_trigger_subjob_merge_success(self, client):
        """Test successful subjob merge trigger."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "merged", "pr_number": 42}

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await client.trigger_subjob_merge("subjob-uuid")

            assert result is True
            mock_client.post.assert_called_once_with(
                "http://localhost:8085/api/jobs/subjob-uuid/subjob-merge"
            )

    @pytest.mark.asyncio
    async def test_trigger_subjob_merge_failure(self, client):
        """Test merge trigger returns False on non-200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await client.trigger_subjob_merge("subjob-uuid")

            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_subjob_merge_connection_error(self, client):
        """Test merge trigger handles connection errors."""
        import httpx

        with patch.object(client, "_client", AsyncMock()) as mock_client:
            mock_client.post = AsyncMock(
                side_effect=httpx.RequestError("Connection refused")
            )

            result = await client.trigger_subjob_merge("subjob-uuid")

            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_subjob_merge_connects_if_needed(self, client):
        """Test that trigger_subjob_merge auto-connects when _client is None."""
        client._client = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "merged"}

        with patch.object(client, "connect", AsyncMock()) as mock_connect:
            # After connect, _client should be set
            async def set_client():
                mock_http = AsyncMock()
                mock_http.post = AsyncMock(return_value=mock_response)
                client._client = mock_http

            mock_connect.side_effect = set_client

            result = await client.trigger_subjob_merge("subjob-uuid")

            assert result is True
            mock_connect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Section 4: New persistent-thread methods
# ---------------------------------------------------------------------------


class TestCreateThread:
    """Tests for create_thread method."""

    @pytest.fixture
    def client(self):
        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.5",
            pod_port=8001,
            hostname="test-agent",
            config_name="persistent_defaults",
            pid=12345,
        )

    @pytest.mark.asyncio
    async def test_posts_correct_url_and_payload(self, client):
        """POSTs to /api/agents/threads with config_name, permission_mode, title."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"thread_id": "tid-1"}

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.create_thread("persistent_defaults", "supervised", "My Session")

            mock_http.post.assert_called_once_with(
                "http://localhost:8085/api/agents/threads",
                json={
                    "config_name": "persistent_defaults",
                    "permission_mode": "supervised",
                    "title": "My Session",
                },
            )

    @pytest.mark.asyncio
    async def test_returns_thread_id_on_200(self, client):
        """Returns thread_id string from response JSON on 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"thread_id": "abc-123"}

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            result = await client.create_thread()
            assert result == "abc-123"

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self, client):
        """Returns None on non-200 status."""
        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = "Validation error"

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            result = await client.create_thread()
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, client):
        """Returns None on request exception."""
        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(side_effect=Exception("network down"))
            result = await client.create_thread()
            assert result is None

    @pytest.mark.asyncio
    async def test_auto_connects_if_client_none(self, client):
        """Calls connect() if _client not initialized."""
        client._client = None
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"thread_id": "tid-2"}

        with patch.object(client, "connect", AsyncMock()) as mock_connect:

            async def set_client():
                mock_http = AsyncMock()
                mock_http.post = AsyncMock(return_value=mock_response)
                client._client = mock_http

            mock_connect.side_effect = set_client
            result = await client.create_thread()
            assert result == "tid-2"
            mock_connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_default_args(self, client):
        """Default args: config_name='persistent_defaults', permission_mode='supervised', title='Local Session'."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"thread_id": "tid-3"}

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.create_thread()

            call_payload = mock_http.post.call_args[1]["json"]
            assert call_payload["config_name"] == "persistent_defaults"
            assert call_payload["permission_mode"] == "supervised"
            assert call_payload["title"] == "Local Session"


class TestSaveThreadMessage:
    """Tests for save_thread_message method."""

    @pytest.fixture
    def client(self):
        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.5",
            pod_port=8001,
            hostname="test-agent",
            config_name="persistent_defaults",
            pid=12345,
        )

    @pytest.mark.asyncio
    async def test_posts_correct_url_and_payload(self, client):
        """POSTs to /api/agents/threads/{thread_id}/messages with correct payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.save_thread_message(
                "tid-1", "assistant", "hello", [{"name": "t"}], 3
            )

            mock_http.post.assert_called_once_with(
                "http://localhost:8085/api/agents/threads/tid-1/messages",
                json={
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": [{"name": "t"}],
                    "turn_number": 3,
                    "metrics": None,
                },
            )

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self, client):
        """Returns True on 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            result = await client.save_thread_message("tid-1", "user", "hi")
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200(self, client):
        """Returns False on non-200 status."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            result = await client.save_thread_message("tid-1", "user", "hi")
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_client_none(self, client):
        """Returns False when _client is None — does NOT auto-connect."""
        client._client = None
        result = await client.save_thread_message("tid-1", "user", "hi")
        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_auto_connect(self, client):
        """Does NOT call connect() — fire-and-forget safe means no lazy init."""
        client._client = None
        with patch.object(client, "connect", AsyncMock()) as mock_connect:
            await client.save_thread_message("tid-1", "user", "hi")
            mock_connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, client):
        """Exception returns False — fire-and-forget safe."""
        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(side_effect=RuntimeError("boom"))
            result = await client.save_thread_message("tid-1", "user", "hi")
            assert result is False

    @pytest.mark.asyncio
    async def test_optional_fields_sent_as_none(self, client):
        """Optional content/tool_calls/turn_number default to None in payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.save_thread_message("tid-1", "assistant")

            call_payload = mock_http.post.call_args[1]["json"]
            assert call_payload["content"] is None
            assert call_payload["tool_calls"] is None
            assert call_payload["turn_number"] is None


class TestRequestThreadVmUpgrade:
    """Tests for request_thread_vm_upgrade method."""

    @pytest.fixture
    def client(self):
        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.5",
            pod_port=8001,
            hostname="test-agent",
            config_name="persistent_defaults",
            pid=12345,
        )

    @pytest.mark.asyncio
    async def test_posts_correct_url_and_payload(self, client):
        """POSTs to /api/agents/threads/{thread_id}/upgrade-to-vm with cpu/memory."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.request_thread_vm_upgrade("tid-1", cpu_cores=4, memory="8Gi")

            mock_http.post.assert_called_once_with(
                "http://localhost:8085/api/agents/threads/tid-1/upgrade-to-vm",
                json={"cpu_cores": 4, "memory": "8Gi"},
            )

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self, client):
        """Returns True on 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            result = await client.request_thread_vm_upgrade("tid-1")
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200(self, client):
        """Returns False on non-200 status."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service Unavailable"

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            result = await client.request_thread_vm_upgrade("tid-1")
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, client):
        """Returns False on request exception."""
        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(side_effect=ConnectionError("refused"))
            result = await client.request_thread_vm_upgrade("tid-1")
            assert result is False

    @pytest.mark.asyncio
    async def test_auto_connects_if_client_none(self, client):
        """Calls connect() if _client not initialized."""
        client._client = None
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "connect", AsyncMock()) as mock_connect:

            async def set_client():
                mock_http = AsyncMock()
                mock_http.post = AsyncMock(return_value=mock_response)
                client._client = mock_http

            mock_connect.side_effect = set_client
            result = await client.request_thread_vm_upgrade("tid-1")
            assert result is True
            mock_connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_cpu_and_memory(self, client):
        """Default: cpu_cores=2, memory='4Gi'."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.post = AsyncMock(return_value=mock_response)
            await client.request_thread_vm_upgrade("tid-1")

            call_payload = mock_http.post.call_args[1]["json"]
            assert call_payload["cpu_cores"] == 2
            assert call_payload["memory"] == "4Gi"


class TestGetThreadWorkspace:
    """Tests for get_thread_workspace method."""

    @pytest.fixture
    def client(self):
        return OrchestratorClient(
            orchestrator_url="http://localhost:8085",
            pod_ip="10.0.0.5",
            pod_port=8001,
            hostname="test-agent",
            config_name="persistent_defaults",
            pid=12345,
        )

    @pytest.mark.asyncio
    async def test_gets_correct_url(self, client):
        """GETs /api/agents/threads/{thread_id}/workspace."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "running"}

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.get = AsyncMock(return_value=mock_response)
            await client.get_thread_workspace("tid-1")

            mock_http.get.assert_called_once_with(
                "http://localhost:8085/api/agents/threads/tid-1/workspace"
            )

    @pytest.mark.asyncio
    async def test_returns_parsed_json_on_200(self, client):
        """Returns parsed JSON dict on 200."""
        workspace_data = {
            "status": "running",
            "pod_ip": "10.0.0.99",
            "pod_name": "ws-pod-1",
            "namespace": "agent-vms",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = workspace_data

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.get = AsyncMock(return_value=mock_response)
            result = await client.get_thread_workspace("tid-1")
            assert result == workspace_data

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self, client):
        """Returns None on non-200 status."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.get = AsyncMock(return_value=mock_response)
            result = await client.get_thread_workspace("tid-1")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, client):
        """Returns None on request exception."""
        with patch.object(client, "_client", AsyncMock()) as mock_http:
            mock_http.get = AsyncMock(side_effect=Exception("timeout"))
            result = await client.get_thread_workspace("tid-1")
            assert result is None

    @pytest.mark.asyncio
    async def test_auto_connects_if_client_none(self, client):
        """Calls connect() if _client not initialized."""
        client._client = None
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "pending"}

        with patch.object(client, "connect", AsyncMock()) as mock_connect:

            async def set_client():
                mock_http = AsyncMock()
                mock_http.get = AsyncMock(return_value=mock_response)
                client._client = mock_http

            mock_connect.side_effect = set_client
            result = await client.get_thread_workspace("tid-1")
            assert result == {"status": "pending"}
            mock_connect.assert_awaited_once()
