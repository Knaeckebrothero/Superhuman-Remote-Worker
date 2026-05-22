"""Tests for the NATS bridge module (orchestrator/services/nats_bridge.py).

Tests cover:
1. NatsBridge initialization and graceful degradation
2. connect() / disconnect() lifecycle management
3. request_vm_create() — message format, payload, timeout handling
4. request_vm_delete() — message format
5. query_vm_status() — request/reply pattern, timeout
6. send_control() — freeze/resume/terminate actions
7. _on_vm_lifecycle_status() — parsing status messages, context updates
8. _on_daemon_register() — daemon registration with IP/hostname, callback
9. _on_daemon_heartbeat() — heartbeat processing, IDE session tracking
10. _on_daemon_status() — agent process exit reporting
11. NATS connection error/disconnect/reconnect callbacks
12. Edge cases: malformed JSON, missing fields, reconnection, publish errors
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# =============================================================================
# Helpers
# =============================================================================


def make_msg(data: dict) -> MagicMock:
    """Create a mock NATS message with JSON-encoded data."""
    msg = MagicMock()
    msg.data = json.dumps(data).encode()
    msg.subject = "test.subject"
    return msg


def make_msg_raw(raw_bytes: bytes) -> MagicMock:
    """Create a mock NATS message with raw bytes (for malformed JSON tests)."""
    msg = MagicMock()
    msg.data = raw_bytes
    msg.subject = "test.subject"
    return msg


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_nats_module():
    """Patch the nats module globally so NatsBridge thinks nats-py is installed."""
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    with patch.dict("sys.modules", {"nats": mock_nats, "nats.aio.client": MagicMock()}):
        with patch("orchestrator.services.nats_bridge.NATS_AVAILABLE", True):
            with patch("orchestrator.services.nats_bridge.nats", mock_nats):
                yield mock_nats


@pytest.fixture
def mock_nc():
    """Create a mock NATS client with common methods."""
    nc = AsyncMock()
    nc.subscribe = AsyncMock()
    nc.publish = AsyncMock()
    nc.request = AsyncMock()
    nc.drain = AsyncMock()
    return nc


@pytest.fixture
def mock_db():
    """Create a mock database instance."""
    db = AsyncMock()
    db.merge_vm_context = AsyncMock()
    db.merge_ide_session_context = AsyncMock()
    return db


@pytest.fixture
def bridge(mock_nats_module, mock_nc):
    """Create a NatsBridge instance with a connected mock NATS client."""
    from orchestrator.services.nats_bridge import NatsBridge

    b = NatsBridge(url="nats://test:4222")
    b._nc = mock_nc
    b._available = True
    return b


@pytest.fixture
def bridge_with_db(bridge, mock_db):
    """Create a NatsBridge with both mock client and mock database."""
    bridge._db = mock_db
    return bridge


@pytest.fixture
def disconnected_bridge(mock_nats_module):
    """Create a NatsBridge that is not connected (available=False)."""
    from orchestrator.services.nats_bridge import NatsBridge

    b = NatsBridge(url="nats://test:4222")
    b._available = False
    return b


# =============================================================================
# Test: Initialization
# =============================================================================


class TestNatsBridgeInit:
    """Tests for NatsBridge.__init__ and is_available property."""

    def test_init_with_url(self, mock_nats_module):
        from orchestrator.services.nats_bridge import NatsBridge

        bridge = NatsBridge(url="nats://myhost:4222")
        assert bridge._url == "nats://myhost:4222"
        assert bridge._nc is None
        assert bridge._available is False

    def test_init_with_env_var(self, mock_nats_module):
        from orchestrator.services.nats_bridge import NatsBridge

        with patch.dict("os.environ", {"NATS_URL": "nats://env-host:4222"}):
            bridge = NatsBridge()
            assert bridge._url == "nats://env-host:4222"

    def test_init_url_overrides_env(self, mock_nats_module):
        from orchestrator.services.nats_bridge import NatsBridge

        with patch.dict("os.environ", {"NATS_URL": "nats://env-host:4222"}):
            bridge = NatsBridge(url="nats://explicit:4222")
            assert bridge._url == "nats://explicit:4222"

    def test_init_no_url_no_env(self, mock_nats_module):
        from orchestrator.services.nats_bridge import NatsBridge

        with patch.dict("os.environ", {}, clear=True):
            bridge = NatsBridge(url=None)
            assert bridge._url is None

    def test_is_available_false_initially(self, mock_nats_module):
        from orchestrator.services.nats_bridge import NatsBridge

        bridge = NatsBridge(url="nats://test:4222")
        assert bridge.is_available is False

    def test_is_available_true_when_connected(self, bridge):
        assert bridge.is_available is True

    def test_init_without_nats_package(self):
        """When nats-py is not installed, bridge initializes but stays unavailable."""
        with patch("orchestrator.services.nats_bridge.NATS_AVAILABLE", False):
            from orchestrator.services.nats_bridge import NatsBridge

            bridge = NatsBridge(url="nats://test:4222")
            assert bridge._available is False


# =============================================================================
# Test: connect()
# =============================================================================


class TestConnect:
    """Tests for NatsBridge.connect()."""

    @pytest.mark.asyncio
    async def test_connect_success(self, mock_nats_module, mock_db):
        from orchestrator.services.nats_bridge import NatsBridge

        mock_nc = AsyncMock()
        mock_nc.subscribe = AsyncMock()
        mock_nats_module.connect = AsyncMock(return_value=mock_nc)

        bridge = NatsBridge(url="nats://test:4222")

        mock_sudo_gate = MagicMock()
        mock_sudo_module = MagicMock()
        mock_sudo_module.sudo_gate = mock_sudo_gate
        with patch.dict(
            "sys.modules", {"orchestrator.services.sudo_gate": mock_sudo_module}
        ):
            await bridge.connect(db=mock_db)

        assert bridge._available is True
        assert bridge._nc is mock_nc
        assert bridge._db is mock_db

        # Verify subscriptions were created (4 VM lifecycle + 1 sudo + 1 session.events)
        assert mock_nc.subscribe.call_count == 6

        # Check subscription subjects
        subjects = [call.args[0] for call in mock_nc.subscribe.call_args_list]
        assert "vm.lifecycle.status" in subjects
        assert "agent.vm.*.register" in subjects
        assert "agent.vm.*.heartbeat" in subjects
        assert "agent.vm.*.status" in subjects
        assert "sudo.request.>" in subjects
        assert "session.events.>" in subjects

    @pytest.mark.asyncio
    async def test_connect_stores_on_vm_ready_callback(self, mock_nats_module, mock_db):
        from orchestrator.services.nats_bridge import NatsBridge

        mock_nc = AsyncMock()
        mock_nc.subscribe = AsyncMock()
        mock_nats_module.connect = AsyncMock(return_value=mock_nc)

        bridge = NatsBridge(url="nats://test:4222")
        callback = MagicMock()

        mock_sudo_module = MagicMock()
        with patch.dict(
            "sys.modules", {"orchestrator.services.sudo_gate": mock_sudo_module}
        ):
            await bridge.connect(db=mock_db, on_vm_ready=callback)

        assert bridge._on_vm_ready is callback

    @pytest.mark.asyncio
    async def test_connect_already_connected_noop(self, bridge, mock_db):
        """If already connected, connect() is a no-op."""
        original_nc = bridge._nc
        await bridge.connect(db=mock_db)
        assert bridge._nc is original_nc

    @pytest.mark.asyncio
    async def test_connect_no_url_stays_unavailable(self, mock_nats_module, mock_db):
        from orchestrator.services.nats_bridge import NatsBridge

        with patch.dict("os.environ", {}, clear=True):
            bridge = NatsBridge(url=None)
            await bridge.connect(db=mock_db)
            assert bridge._available is False
            assert bridge._nc is None

    @pytest.mark.asyncio
    async def test_connect_nats_unavailable(self, mock_db):
        """When nats-py is not installed, connect() is a no-op."""
        with patch("orchestrator.services.nats_bridge.NATS_AVAILABLE", False):
            from orchestrator.services.nats_bridge import NatsBridge

            bridge = NatsBridge(url="nats://test:4222")
            await bridge.connect(db=mock_db)
            assert bridge._available is False

    @pytest.mark.asyncio
    async def test_connect_failure_sets_unavailable(self, mock_nats_module, mock_db):
        from orchestrator.services.nats_bridge import NatsBridge

        mock_nats_module.connect = AsyncMock(
            side_effect=ConnectionRefusedError("refused")
        )
        bridge = NatsBridge(url="nats://test:4222")

        await bridge.connect(db=mock_db)

        assert bridge._available is False
        assert bridge._nc is None

    @pytest.mark.asyncio
    async def test_connect_passes_nats_options(self, mock_nats_module, mock_db):
        from orchestrator.services.nats_bridge import NatsBridge

        mock_nc = AsyncMock()
        mock_nc.subscribe = AsyncMock()
        mock_nats_module.connect = AsyncMock(return_value=mock_nc)

        bridge = NatsBridge(url="nats://test:4222")

        mock_sudo_module = MagicMock()
        with patch.dict(
            "sys.modules", {"orchestrator.services.sudo_gate": mock_sudo_module}
        ):
            await bridge.connect(db=mock_db)

        # Verify nats.connect was called with expected parameters
        call_kwargs = mock_nats_module.connect.call_args
        assert call_kwargs.args[0] == "nats://test:4222"
        assert call_kwargs.kwargs["max_reconnect_attempts"] == -1
        assert call_kwargs.kwargs["reconnect_time_wait"] == 2


# =============================================================================
# Test: disconnect()
# =============================================================================


class TestDisconnect:
    """Tests for NatsBridge.disconnect()."""

    @pytest.mark.asyncio
    async def test_disconnect_drains_and_cleans_up(self, bridge, mock_nc):
        await bridge.disconnect()

        mock_nc.drain.assert_awaited_once()
        assert bridge._nc is None
        assert bridge._available is False

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self, disconnected_bridge):
        """Disconnect when not connected is a no-op."""
        disconnected_bridge._nc = None
        await disconnected_bridge.disconnect()
        # No exception should be raised
        assert disconnected_bridge._nc is None

    @pytest.mark.asyncio
    async def test_disconnect_drain_error_handled(self, bridge, mock_nc):
        """Drain errors are caught and logged, not raised."""
        mock_nc.drain = AsyncMock(side_effect=RuntimeError("drain error"))

        await bridge.disconnect()

        # Should still clean up despite drain error
        assert bridge._nc is None
        assert bridge._available is False

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self, bridge, mock_nc):
        """Calling disconnect twice is safe."""
        await bridge.disconnect()
        await bridge.disconnect()  # Should not raise
        assert bridge._nc is None


# =============================================================================
# Test: request_vm_create()
# =============================================================================


class TestRequestVmCreate:
    """Tests for NatsBridge.request_vm_create()."""

    @pytest.mark.asyncio
    async def test_create_publishes_correct_payload(
        self, bridge_with_db, mock_nc, mock_db
    ):
        result = await bridge_with_db.request_vm_create(
            job_id="test-job-123",
            agent_config="scholar",
            cpu_cores=4,
            memory="8Gi",
            description="Test job description",
        )

        assert result is True
        mock_nc.publish.assert_awaited_once()

        call_args = mock_nc.publish.call_args
        assert call_args.args[0] == "vm.lifecycle.create"

        payload = json.loads(call_args.args[1].decode())
        assert payload["job_id"] == "test-job-123"
        assert payload["agent_config"] == "scholar"
        assert payload["cpu_cores"] == 4
        assert payload["memory"] == "8Gi"
        assert payload["nats_url"] == "nats://test:4222"
        assert payload["description"] == "Test job description"
        assert "vm_image" not in payload  # Not provided, should be absent

    @pytest.mark.asyncio
    async def test_create_includes_vm_image_when_provided(
        self, bridge_with_db, mock_nc
    ):
        await bridge_with_db.request_vm_create(
            job_id="test-job-123",
            vm_image="registry.example.com/vm-base:latest",
        )

        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert payload["vm_image"] == "registry.example.com/vm-base:latest"

    @pytest.mark.asyncio
    async def test_create_excludes_vm_image_when_none(self, bridge_with_db, mock_nc):
        await bridge_with_db.request_vm_create(
            job_id="test-job-123",
            vm_image=None,
        )

        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert "vm_image" not in payload

    @pytest.mark.asyncio
    async def test_create_uses_default_params(self, bridge_with_db, mock_nc):
        await bridge_with_db.request_vm_create(job_id="test-job-123")

        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert payload["agent_config"] == "defaults"
        assert payload["cpu_cores"] == 2
        assert payload["memory"] == "4Gi"
        assert payload["description"] == ""

    @pytest.mark.asyncio
    async def test_create_updates_vm_context(self, bridge_with_db, mock_db):
        await bridge_with_db.request_vm_create(job_id="test-job-123")

        mock_db.merge_vm_context.assert_awaited_once_with(
            "test-job-123", {"status": "provisioning"}
        )

    @pytest.mark.asyncio
    async def test_create_returns_false_when_unavailable(self, disconnected_bridge):
        result = await disconnected_bridge.request_vm_create(job_id="test-job-123")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_returns_false_on_publish_error(self, bridge_with_db, mock_nc):
        mock_nc.publish = AsyncMock(side_effect=RuntimeError("publish failed"))

        result = await bridge_with_db.request_vm_create(job_id="test-job-123")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_empty_string_vm_image_excluded(self, bridge_with_db, mock_nc):
        """An empty string vm_image is falsy and should not be included."""
        await bridge_with_db.request_vm_create(job_id="test-job-123", vm_image="")

        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert "vm_image" not in payload


# =============================================================================
# Test: request_vm_delete()
# =============================================================================


class TestRequestVmDelete:
    """Tests for NatsBridge.request_vm_delete()."""

    @pytest.mark.asyncio
    async def test_delete_publishes_correct_payload(
        self, bridge_with_db, mock_nc, mock_db
    ):
        result = await bridge_with_db.request_vm_delete(job_id="test-job-456")

        assert result is True
        mock_nc.publish.assert_awaited_once()

        call_args = mock_nc.publish.call_args
        assert call_args.args[0] == "vm.lifecycle.delete"

        payload = json.loads(call_args.args[1].decode())
        assert payload == {"job_id": "test-job-456"}

    @pytest.mark.asyncio
    async def test_delete_updates_vm_context(self, bridge_with_db, mock_db):
        await bridge_with_db.request_vm_delete(job_id="test-job-456")

        mock_db.merge_vm_context.assert_awaited_once_with(
            "test-job-456", {"status": "deleting"}
        )

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_unavailable(self, disconnected_bridge):
        result = await disconnected_bridge.request_vm_delete(job_id="test-job-456")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_returns_false_on_publish_error(self, bridge_with_db, mock_nc):
        mock_nc.publish = AsyncMock(side_effect=RuntimeError("publish failed"))

        result = await bridge_with_db.request_vm_delete(job_id="test-job-456")
        assert result is False


# =============================================================================
# Test: query_vm_status()
# =============================================================================


class TestQueryVmStatus:
    """Tests for NatsBridge.query_vm_status()."""

    @pytest.mark.asyncio
    async def test_query_returns_status_dict(self, bridge, mock_nc):
        response_data = {
            "job_id": "test-job-789",
            "status": "running",
            "vm_name": "vm-test-job-789",
            "namespace": "agent-vms",
        }
        response_msg = MagicMock()
        response_msg.data = json.dumps(response_data).encode()
        mock_nc.request = AsyncMock(return_value=response_msg)

        result = await bridge.query_vm_status("test-job-789")

        assert result == response_data
        mock_nc.request.assert_awaited_once()

        call_args = mock_nc.request.call_args
        assert call_args.args[0] == "vm.lifecycle.get"
        payload = json.loads(call_args.args[1].decode())
        assert payload == {"job_id": "test-job-789"}
        assert call_args.kwargs["timeout"] == 5.0

    @pytest.mark.asyncio
    async def test_query_custom_timeout(self, bridge, mock_nc):
        response_msg = MagicMock()
        response_msg.data = json.dumps({"status": "ok"}).encode()
        mock_nc.request = AsyncMock(return_value=response_msg)

        await bridge.query_vm_status("test-job", timeout=10.0)

        assert mock_nc.request.call_args.kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_query_returns_none_on_timeout(self, bridge, mock_nc):
        mock_nc.request = AsyncMock(side_effect=TimeoutError("request timed out"))

        result = await bridge.query_vm_status("test-job-789")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_returns_none_when_unavailable(self, disconnected_bridge):
        result = await disconnected_bridge.query_vm_status("test-job-789")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_returns_none_on_generic_error(self, bridge, mock_nc):
        mock_nc.request = AsyncMock(side_effect=RuntimeError("connection lost"))

        result = await bridge.query_vm_status("test-job-789")
        assert result is None


# =============================================================================
# Test: send_control()
# =============================================================================


class TestSendControl:
    """Tests for NatsBridge.send_control()."""

    @pytest.mark.asyncio
    async def test_send_freeze(self, bridge, mock_nc):
        result = await bridge.send_control("test-job-123", "freeze")

        assert result is True
        mock_nc.publish.assert_awaited_once()

        call_args = mock_nc.publish.call_args
        assert call_args.args[0] == "agent.vm.test-job-123.control"
        payload = json.loads(call_args.args[1].decode())
        assert payload == {"action": "freeze"}

    @pytest.mark.asyncio
    async def test_send_resume(self, bridge, mock_nc):
        result = await bridge.send_control("test-job-123", "resume")

        assert result is True
        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert payload == {"action": "resume"}

    @pytest.mark.asyncio
    async def test_send_terminate(self, bridge, mock_nc):
        result = await bridge.send_control("test-job-123", "terminate")

        assert result is True
        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert payload == {"action": "terminate"}

    @pytest.mark.asyncio
    async def test_send_control_subject_format(self, bridge, mock_nc):
        """Verify the subject includes the job_id."""
        await bridge.send_control("abc-def-ghi", "freeze")
        subject = mock_nc.publish.call_args.args[0]
        assert subject == "agent.vm.abc-def-ghi.control"

    @pytest.mark.asyncio
    async def test_send_control_returns_false_when_unavailable(
        self, disconnected_bridge
    ):
        result = await disconnected_bridge.send_control("test-job-123", "freeze")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_control_returns_false_on_publish_error(self, bridge, mock_nc):
        mock_nc.publish = AsyncMock(side_effect=RuntimeError("publish failed"))

        result = await bridge.send_control("test-job-123", "freeze")
        assert result is False


# =============================================================================
# Test: _on_vm_lifecycle_status()
# =============================================================================


class TestOnVmLifecycleStatus:
    """Tests for NatsBridge._on_vm_lifecycle_status()."""

    @pytest.mark.asyncio
    async def test_basic_status_update(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "job_id": "job-001",
                "status": "running",
                "vm_name": "vm-job-001",
                "namespace": "agent-vms",
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        mock_db.merge_vm_context.assert_awaited_once_with(
            "job-001",
            {
                "status": "running",
                "vm_name": "vm-job-001",
                "namespace": "agent-vms",
            },
        )

    @pytest.mark.asyncio
    async def test_status_with_error(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "job_id": "job-002",
                "status": "failed",
                "vm_name": "vm-job-002",
                "namespace": "agent-vms",
                "error": "Insufficient resources",
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        call_args = mock_db.merge_vm_context.call_args
        updates = call_args.args[1]
        assert updates["error"] == "Insufficient resources"
        assert updates["status"] == "failed"

    @pytest.mark.asyncio
    async def test_status_with_pod_ip(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "job_id": "job-003",
                "status": "running",
                "vm_name": "vm-job-003",
                "namespace": "agent-vms",
                "pod_ip": "10.0.0.42",
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["pod_ip"] == "10.0.0.42"

    @pytest.mark.asyncio
    async def test_status_with_ssh_nodeport(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "job_id": "job-004",
                "status": "running",
                "vm_name": "vm-job-004",
                "namespace": "agent-vms",
                "ssh_nodeport": 30022,
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["ssh_nodeport"] == 30022

    @pytest.mark.asyncio
    async def test_status_without_optional_fields(self, bridge_with_db, mock_db):
        """Optional fields (error, pod_ip, ssh_nodeport) should not appear if absent."""
        msg = make_msg(
            {
                "job_id": "job-005",
                "status": "creating",
                "vm_name": "vm-job-005",
                "namespace": "agent-vms",
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert "error" not in updates
        assert "pod_ip" not in updates
        assert "ssh_nodeport" not in updates

    @pytest.mark.asyncio
    async def test_status_missing_job_id_ignored(self, bridge_with_db, mock_db):
        msg = make_msg({"status": "running", "vm_name": "vm-orphan"})

        await bridge_with_db._on_vm_lifecycle_status(msg)

        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_malformed_json(self, bridge_with_db, mock_db):
        """Malformed JSON should be handled gracefully, not raise."""
        msg = make_msg_raw(b"not-valid-json{{{")

        await bridge_with_db._on_vm_lifecycle_status(msg)

        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_empty_payload(self, bridge_with_db, mock_db):
        msg = make_msg({})

        await bridge_with_db._on_vm_lifecycle_status(msg)

        mock_db.merge_vm_context.assert_not_awaited()


# =============================================================================
# Test: _on_daemon_register()
# =============================================================================


class TestOnDaemonRegister:
    """Tests for NatsBridge._on_daemon_register()."""

    @pytest.mark.asyncio
    async def test_register_with_ip(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "job_id": "job-reg-001",
                "hostname": "vm-agent-001",
                "ip": "100.64.1.5",
                "pid": 12345,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        mock_db.merge_vm_context.assert_awaited_once_with(
            "job-reg-001",
            {
                "status": "ready",
                "ssh_host": "100.64.1.5",
                "ssh_port": 22,
                "hostname": "vm-agent-001",
                "daemon_pid": 12345,
                "recovering": False,
            },
        )

    @pytest.mark.asyncio
    async def test_register_falls_back_to_hostname(self, bridge_with_db, mock_db):
        """When ip is missing, hostname is used as ssh_host."""
        msg = make_msg(
            {
                "job_id": "job-reg-002",
                "hostname": "vm-agent-002.local",
                "pid": 99,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["ssh_host"] == "vm-agent-002.local"
        assert updates["ssh_port"] == 22

    @pytest.mark.asyncio
    async def test_register_ip_preferred_over_hostname(self, bridge_with_db, mock_db):
        """When both ip and hostname are present, ip is preferred for ssh_host."""
        msg = make_msg(
            {
                "job_id": "job-reg-003",
                "hostname": "vm-agent-003",
                "ip": "100.64.2.10",
                "pid": 50,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["ssh_host"] == "100.64.2.10"
        assert updates["hostname"] == "vm-agent-003"

    @pytest.mark.asyncio
    async def test_register_triggers_on_vm_ready_callback(
        self, bridge_with_db, mock_db
    ):
        callback = MagicMock()
        bridge_with_db._on_vm_ready = callback

        msg = make_msg(
            {
                "job_id": "job-reg-004",
                "hostname": "vm-agent-004",
                "ip": "100.64.3.1",
                "pid": 1,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_callback_error_handled(self, bridge_with_db, mock_db):
        """Callback errors should not prevent registration from completing."""
        callback = MagicMock(side_effect=RuntimeError("callback boom"))
        bridge_with_db._on_vm_ready = callback

        msg = make_msg(
            {
                "job_id": "job-reg-005",
                "hostname": "vm-agent-005",
                "ip": "100.64.4.1",
                "pid": 2,
            }
        )

        # Should not raise
        await bridge_with_db._on_daemon_register(msg)

        # Context update should still have happened
        mock_db.merge_vm_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_register_no_callback_set(self, bridge_with_db, mock_db):
        """Register works fine when no on_vm_ready callback is set."""
        bridge_with_db._on_vm_ready = None

        msg = make_msg(
            {
                "job_id": "job-reg-006",
                "hostname": "vm-agent-006",
                "ip": "100.64.5.1",
                "pid": 3,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        mock_db.merge_vm_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_register_missing_job_id_ignored(self, bridge_with_db, mock_db):
        msg = make_msg({"hostname": "orphan-vm", "ip": "100.64.6.1"})

        await bridge_with_db._on_daemon_register(msg)

        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_register_malformed_json(self, bridge_with_db, mock_db):
        msg = make_msg_raw(b"{{not json")

        await bridge_with_db._on_daemon_register(msg)

        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_register_clears_recovering_flag(self, bridge_with_db, mock_db):
        """Registration should set recovering=False to clear any recovery guard."""
        msg = make_msg(
            {
                "job_id": "job-reg-007",
                "hostname": "vm-agent-007",
                "ip": "100.64.7.1",
                "pid": 7,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["recovering"] is False

    @pytest.mark.asyncio
    async def test_register_neither_ip_nor_hostname(self, bridge_with_db, mock_db):
        """When both ip and hostname are missing, ssh_host should be None."""
        msg = make_msg(
            {
                "job_id": "job-reg-008",
                "pid": 8,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["ssh_host"] is None


# =============================================================================
# Test: _on_daemon_heartbeat()
# =============================================================================


class TestOnDaemonHeartbeat:
    """Tests for NatsBridge._on_daemon_heartbeat()."""

    @pytest.mark.asyncio
    async def test_heartbeat_updates_last_heartbeat(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "job_id": "job-hb-001",
                "agent_pid": 5555,
                "agent_running": True,
                "cpu_percent": 45.2,
                "memory_percent": 62.1,
                "disk_percent": 30.0,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        mock_db.merge_vm_context.assert_awaited_once()
        call_args = mock_db.merge_vm_context.call_args
        assert call_args.args[0] == "job-hb-001"
        updates = call_args.args[1]
        assert "last_heartbeat" in updates
        # Should be an ISO format timestamp
        assert "T" in updates["last_heartbeat"]

    @pytest.mark.asyncio
    async def test_heartbeat_with_code_server_connections(
        self, bridge_with_db, mock_db
    ):
        """Heartbeat with code_server_connections triggers IDE session update."""
        msg = make_msg(
            {
                "job_id": "job-hb-002",
                "agent_pid": 6666,
                "agent_running": True,
                "code_server_connections": 2,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        # Should update both VM context and IDE session
        mock_db.merge_vm_context.assert_awaited_once()
        mock_db.merge_ide_session_context.assert_awaited_once()

        ide_call = mock_db.merge_ide_session_context.call_args
        assert ide_call.args[0] == "job-hb-002"
        ide_updates = ide_call.args[1]
        assert ide_updates["code_server_connections"] == 2
        assert ide_updates["status"] == "active"
        assert "last_activity" in ide_updates

    @pytest.mark.asyncio
    async def test_heartbeat_zero_code_server_connections(
        self, bridge_with_db, mock_db
    ):
        """Zero connections marks IDE session as idle."""
        msg = make_msg(
            {
                "job_id": "job-hb-003",
                "agent_pid": 7777,
                "agent_running": True,
                "code_server_connections": 0,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        ide_call = mock_db.merge_ide_session_context.call_args
        ide_updates = ide_call.args[1]
        assert ide_updates["code_server_connections"] == 0
        assert ide_updates["status"] == "idle"
        # Zero connections should NOT set last_activity
        assert "last_activity" not in ide_updates

    @pytest.mark.asyncio
    async def test_heartbeat_no_code_server_field(self, bridge_with_db, mock_db):
        """When code_server_connections is absent, IDE session is not updated."""
        msg = make_msg(
            {
                "job_id": "job-hb-004",
                "agent_pid": 8888,
                "agent_running": True,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        mock_db.merge_vm_context.assert_awaited_once()
        mock_db.merge_ide_session_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_ide_session_error_non_fatal(self, bridge_with_db, mock_db):
        """IDE session update errors should not break heartbeat processing."""
        mock_db.merge_ide_session_context = AsyncMock(
            side_effect=RuntimeError("db error")
        )

        msg = make_msg(
            {
                "job_id": "job-hb-005",
                "agent_pid": 9999,
                "agent_running": True,
                "code_server_connections": 1,
            }
        )

        # Should not raise
        await bridge_with_db._on_daemon_heartbeat(msg)

        # VM context should still be updated
        mock_db.merge_vm_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_heartbeat_missing_job_id(self, bridge_with_db, mock_db):
        msg = make_msg(
            {
                "agent_pid": 1111,
                "agent_running": True,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_malformed_json(self, bridge_with_db, mock_db):
        msg = make_msg_raw(b"corrupted-heartbeat")

        await bridge_with_db._on_daemon_heartbeat(msg)

        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_no_db_skips_ide_update(self, bridge, mock_nc):
        """When _db is None, IDE session update is skipped (code_server_connections path)."""
        bridge._db = None

        msg = make_msg(
            {
                "job_id": "job-hb-006",
                "agent_pid": 2222,
                "agent_running": True,
                "code_server_connections": 3,
            }
        )

        # Should not raise despite _db being None
        await bridge._on_daemon_heartbeat(msg)


# =============================================================================
# Test: _on_daemon_status()
# =============================================================================


class TestOnDaemonStatus:
    """Tests for NatsBridge._on_daemon_status()."""

    @pytest.mark.asyncio
    async def test_status_completed(self, bridge_with_db):
        """Completed status is logged without error."""
        msg = make_msg(
            {
                "job_id": "job-st-001",
                "status": "completed",
                "exit_code": 0,
            }
        )

        # Should not raise
        await bridge_with_db._on_daemon_status(msg)

    @pytest.mark.asyncio
    async def test_status_failed(self, bridge_with_db):
        """Failed status is logged without error."""
        msg = make_msg(
            {
                "job_id": "job-st-002",
                "status": "failed",
                "exit_code": 1,
            }
        )

        await bridge_with_db._on_daemon_status(msg)

    @pytest.mark.asyncio
    async def test_status_missing_job_id(self, bridge_with_db):
        """Missing job_id is handled (early return)."""
        msg = make_msg({"status": "completed", "exit_code": 0})

        await bridge_with_db._on_daemon_status(msg)

    @pytest.mark.asyncio
    async def test_status_malformed_json(self, bridge_with_db):
        msg = make_msg_raw(b"bad json")

        await bridge_with_db._on_daemon_status(msg)

    @pytest.mark.asyncio
    async def test_status_empty_payload(self, bridge_with_db):
        msg = make_msg({})

        await bridge_with_db._on_daemon_status(msg)


# =============================================================================
# Test: _set_vm_context()
# =============================================================================


class TestSetVmContext:
    """Tests for NatsBridge._set_vm_context() helper."""

    @pytest.mark.asyncio
    async def test_set_vm_context_calls_db(self, bridge_with_db, mock_db):
        await bridge_with_db._set_vm_context("job-ctx-001", {"status": "ready"})

        mock_db.merge_vm_context.assert_awaited_once_with(
            "job-ctx-001", {"status": "ready"}
        )

    @pytest.mark.asyncio
    async def test_set_vm_context_no_db(self, bridge):
        """When _db is None, _set_vm_context is a no-op."""
        bridge._db = None
        # Should not raise
        await bridge._set_vm_context("job-ctx-002", {"status": "ready"})

    @pytest.mark.asyncio
    async def test_set_vm_context_db_error_handled(self, bridge_with_db, mock_db):
        """Database errors are caught and logged, not raised."""
        mock_db.merge_vm_context = AsyncMock(side_effect=RuntimeError("db down"))

        # Should not raise
        await bridge_with_db._set_vm_context("job-ctx-003", {"status": "ready"})


# =============================================================================
# Test: NATS connection callbacks
# =============================================================================


class TestConnectionCallbacks:
    """Tests for NATS connection event callbacks."""

    @pytest.mark.asyncio
    async def test_on_error(self, bridge):
        """_on_error logs but does not raise."""
        await bridge._on_error(RuntimeError("test error"))

    @pytest.mark.asyncio
    async def test_on_disconnect(self, bridge):
        """_on_disconnect logs but does not raise."""
        await bridge._on_disconnect()

    @pytest.mark.asyncio
    async def test_on_reconnect(self, bridge):
        """_on_reconnect logs but does not raise."""
        await bridge._on_reconnect()


# =============================================================================
# Test: Graceful degradation (NATS unavailable)
# =============================================================================


class TestGracefulDegradation:
    """Tests that all operations gracefully degrade when NATS is unavailable."""

    @pytest.mark.asyncio
    async def test_create_returns_false(self, disconnected_bridge):
        result = await disconnected_bridge.request_vm_create(job_id="job-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_returns_false(self, disconnected_bridge):
        result = await disconnected_bridge.request_vm_delete(job_id="job-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_query_returns_none(self, disconnected_bridge):
        result = await disconnected_bridge.query_vm_status("job-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_control_returns_false(self, disconnected_bridge):
        result = await disconnected_bridge.send_control("job-1", "freeze")
        assert result is False

    @pytest.mark.asyncio
    async def test_all_actions_return_falsy(self, disconnected_bridge):
        """Comprehensive check: every public method returns a falsy value."""
        assert await disconnected_bridge.request_vm_create(job_id="j") is False
        assert await disconnected_bridge.request_vm_delete(job_id="j") is False
        assert await disconnected_bridge.query_vm_status("j") is None
        assert await disconnected_bridge.send_control("j", "freeze") is False


# =============================================================================
# Test: Module-level singleton
# =============================================================================


class TestModuleSingleton:
    """Tests for the module-level nats_bridge singleton."""

    def test_singleton_exists(self):
        from orchestrator.services.nats_bridge import nats_bridge

        assert nats_bridge is not None
        # Should be a NatsBridge instance
        assert hasattr(nats_bridge, "is_available")
        assert hasattr(nats_bridge, "connect")
        assert hasattr(nats_bridge, "disconnect")


# =============================================================================
# Test: Edge cases and robustness
# =============================================================================


class TestEdgeCases:
    """Edge cases and robustness tests."""

    @pytest.mark.asyncio
    async def test_lifecycle_status_with_all_optional_fields(
        self, bridge_with_db, mock_db
    ):
        """Status message with every optional field populated."""
        msg = make_msg(
            {
                "job_id": "job-edge-001",
                "status": "running",
                "vm_name": "vm-job-edge-001",
                "namespace": "agent-vms",
                "error": "some warning",
                "pod_ip": "10.0.0.100",
                "ssh_nodeport": 31022,
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["status"] == "running"
        assert updates["vm_name"] == "vm-job-edge-001"
        assert updates["namespace"] == "agent-vms"
        assert updates["error"] == "some warning"
        assert updates["pod_ip"] == "10.0.0.100"
        assert updates["ssh_nodeport"] == 31022

    @pytest.mark.asyncio
    async def test_register_with_unicode_hostname(self, bridge_with_db, mock_db):
        """Hostnames with unusual characters should be handled."""
        msg = make_msg(
            {
                "job_id": "job-edge-002",
                "hostname": "vm-agent-\u00e4\u00f6\u00fc",
                "ip": "100.64.10.1",
                "pid": 42,
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        updates = mock_db.merge_vm_context.call_args.args[1]
        assert updates["hostname"] == "vm-agent-\u00e4\u00f6\u00fc"

    @pytest.mark.asyncio
    async def test_create_with_special_chars_in_description(
        self, bridge_with_db, mock_nc
    ):
        """Description with JSON-special characters must be encoded properly."""
        desc = 'Process "file.pdf" with keys: {a: 1, b: 2}\nnewline'
        await bridge_with_db.request_vm_create(
            job_id="job-edge-003",
            description=desc,
        )

        payload = json.loads(mock_nc.publish.call_args.args[1].decode())
        assert payload["description"] == desc

    @pytest.mark.asyncio
    async def test_heartbeat_with_none_code_server_connections(
        self, bridge_with_db, mock_db
    ):
        """Explicit null for code_server_connections should not update IDE session.

        The code checks `if cs_connections is not None`, so None should skip.
        """
        msg = make_msg(
            {
                "job_id": "job-edge-004",
                "agent_pid": 100,
                "agent_running": True,
                "code_server_connections": None,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        mock_db.merge_vm_context.assert_awaited_once()
        mock_db.merge_ide_session_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_merge_vm_context_error_in_lifecycle_status(
        self, bridge_with_db, mock_db
    ):
        """DB errors in lifecycle status handler should be caught."""
        mock_db.merge_vm_context = AsyncMock(side_effect=RuntimeError("db gone"))

        msg = make_msg(
            {
                "job_id": "job-edge-005",
                "status": "running",
                "vm_name": "vm-edge",
                "namespace": "ns",
            }
        )

        # Should not raise — the handler has its own try/except
        await bridge_with_db._on_vm_lifecycle_status(msg)

    @pytest.mark.asyncio
    async def test_db_merge_vm_context_error_in_register(self, bridge_with_db, mock_db):
        """DB errors in daemon register handler should be caught."""
        mock_db.merge_vm_context = AsyncMock(side_effect=RuntimeError("db gone"))

        msg = make_msg(
            {
                "job_id": "job-edge-006",
                "hostname": "vm-edge",
                "ip": "100.64.99.1",
                "pid": 1,
            }
        )

        # The outer try/except in _on_daemon_register catches this
        await bridge_with_db._on_daemon_register(msg)

    @pytest.mark.asyncio
    async def test_large_payload_in_status(self, bridge_with_db, mock_db):
        """Handle a status message with extra unknown fields gracefully."""
        msg = make_msg(
            {
                "job_id": "job-edge-007",
                "status": "running",
                "vm_name": "vm-edge",
                "namespace": "ns",
                "extra_field_1": "ignored",
                "extra_field_2": {"nested": True},
                "extra_field_3": [1, 2, 3],
            }
        )

        await bridge_with_db._on_vm_lifecycle_status(msg)

        # Only known fields should be in the update
        updates = mock_db.merge_vm_context.call_args.args[1]
        assert "extra_field_1" not in updates
        assert "extra_field_2" not in updates
        assert "extra_field_3" not in updates

    @pytest.mark.asyncio
    async def test_concurrent_create_and_delete(self, bridge_with_db, mock_nc, mock_db):
        """Create and delete for different jobs should both succeed."""
        import asyncio

        result_create, result_delete = await asyncio.gather(
            bridge_with_db.request_vm_create(job_id="job-A"),
            bridge_with_db.request_vm_delete(job_id="job-B"),
        )

        assert result_create is True
        assert result_delete is True
        assert mock_nc.publish.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_string_job_id_in_register(self, bridge_with_db, mock_db):
        """Empty string job_id is falsy, should be treated as missing."""
        msg = make_msg(
            {
                "job_id": "",
                "hostname": "vm-agent",
                "ip": "100.64.1.1",
            }
        )

        await bridge_with_db._on_daemon_register(msg)

        # Empty string is falsy in Python, so `if not job_id: return`
        mock_db.merge_vm_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_negative_code_server_connections(
        self, bridge_with_db, mock_db
    ):
        """Negative connection count (edge case) should still be processed.

        The code does `if cs_connections > 0` so negative should go to idle.
        """
        msg = make_msg(
            {
                "job_id": "job-edge-008",
                "agent_pid": 200,
                "agent_running": True,
                "code_server_connections": -1,
            }
        )

        await bridge_with_db._on_daemon_heartbeat(msg)

        ide_call = mock_db.merge_ide_session_context.call_args
        ide_updates = ide_call.args[1]
        assert ide_updates["status"] == "idle"
        assert ide_updates["code_server_connections"] == -1
