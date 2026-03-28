"""Tests for the VM management daemon (management-daemon.py).

Tests cover:
1. load_config() — env vars, job config file parsing, missing vars
2. detect_ip() — Tailscale IP preference, LAN fallback, hostname fallback
3. read_agent_pid() — PID file reading, process alive check, missing/invalid PID
4. get_system_metrics() — psutil metrics, ImportError fallback
5. ManagementDaemon.connect_nats() — connection with retry, shutdown during retry
6. ManagementDaemon.register() — payload format, NATS publish
7. ManagementDaemon._on_control() — freeze/resume/terminate/unknown actions
8. ManagementDaemon.heartbeat_loop() — periodic publish, shutdown handling
9. ManagementDaemon.agent_monitor_loop() — exit detection, status reporting
10. ManagementDaemon.ip_update_loop() — IP change detection, re-registration
11. ManagementDaemon._wait_for_cloud_init() — file existence polling, timeout
12. ManagementDaemon._wait_for_tailscale() — tailscale polling, not-installed handling
13. Signal handling in main()

The management daemon is a standalone script (not a package import), so we
import it via importlib from its file path.
"""

import asyncio
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# ---------------------------------------------------------------------------
# Ensure a mock 'nats' module exists in sys.modules so that
# `import nats` inside connect_nats() and `patch("nats.connect")` work
# even when the nats-py package is not installed.
# ---------------------------------------------------------------------------

if "nats" not in sys.modules:
    _mock_nats = MagicMock()
    _mock_nats.connect = AsyncMock()
    sys.modules["nats"] = _mock_nats

# ---------------------------------------------------------------------------
# Import the management daemon as a module from its file path
# ---------------------------------------------------------------------------

DAEMON_PATH = (
    project_root / "docker" / "agent-vm-base" / "files" / "management-daemon.py"
)


def _import_daemon() -> ModuleType:
    """Import management-daemon.py as a module via importlib."""
    spec = importlib.util.spec_from_file_location("management_daemon", DAEMON_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


daemon_mod = _import_daemon()

# Pull out the names we need
load_config = daemon_mod.load_config
detect_ip = daemon_mod.detect_ip
read_agent_pid = daemon_mod.read_agent_pid
get_system_metrics = daemon_mod.get_system_metrics
ManagementDaemon = daemon_mod.ManagementDaemon
main = daemon_mod.main

AGENT_PID_FILE = daemon_mod.AGENT_PID_FILE
AGENT_EXIT_CODE_FILE = daemon_mod.AGENT_EXIT_CODE_FILE
JOB_CONFIG_FILE = daemon_mod.JOB_CONFIG_FILE
HEARTBEAT_INTERVAL = daemon_mod.HEARTBEAT_INTERVAL
AGENT_POLL_INTERVAL = daemon_mod.AGENT_POLL_INTERVAL
NATS_RETRY_INTERVAL = daemon_mod.NATS_RETRY_INTERVAL
TAILSCALE_WAIT_TIMEOUT = daemon_mod.TAILSCALE_WAIT_TIMEOUT
IP_RECHECK_INTERVAL = daemon_mod.IP_RECHECK_INTERVAL


# =============================================================================
# Helpers / fixtures
# =============================================================================


def _make_config(**overrides) -> dict:
    """Create a minimal daemon config dict."""
    base = {"nats_url": "nats://localhost:4222", "job_id": "test-job-001"}
    base.update(overrides)
    return base


def _make_daemon(**config_overrides) -> ManagementDaemon:
    """Create a ManagementDaemon with sensible defaults."""
    return ManagementDaemon(_make_config(**config_overrides))


@pytest.fixture
def daemon():
    """Provide a fresh ManagementDaemon instance for each test."""
    return _make_daemon()


@pytest.fixture
def connected_daemon():
    """Provide a daemon with a mocked NATS connection."""
    d = _make_daemon()
    nc = AsyncMock()
    nc.is_connected = True
    nc.publish = AsyncMock()
    nc.subscribe = AsyncMock()
    nc.drain = AsyncMock()
    d.nc = nc
    return d


# =============================================================================
# Test: load_config()
# =============================================================================


class TestLoadConfig:
    """Tests for load_config() — env vars, job config file, missing vars."""

    def test_loads_from_env_vars(self):
        """load_config reads NATS_URL and JOB_ID from environment."""
        env = {"NATS_URL": "nats://10.0.0.1:4222", "JOB_ID": "job-abc-123"}
        with patch.dict(os.environ, env, clear=False):
            config = load_config()

        assert config["nats_url"] == "nats://10.0.0.1:4222"
        assert config["job_id"] == "job-abc-123"

    def test_loads_job_config_file(self, tmp_path):
        """load_config merges keys from the job config JSON file."""
        job_config = {"agent_config": "developer", "extra_key": "extra_value"}
        config_file = tmp_path / "job-config.json"
        config_file.write_text(json.dumps(job_config))

        env = {"NATS_URL": "nats://localhost:4222", "JOB_ID": "job-xyz"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
        ):
            config = load_config()

        assert config["agent_config"] == "developer"
        assert config["extra_key"] == "extra_value"
        assert config["nats_url"] == "nats://localhost:4222"

    def test_exits_when_nats_url_missing(self):
        """load_config calls sys.exit(1) when NATS_URL is not set."""
        env = {"JOB_ID": "job-123"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.dict(os.environ, {"NATS_URL": ""}, clear=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            load_config()

        assert exc_info.value.code == 1

    def test_exits_when_job_id_missing(self):
        """load_config calls sys.exit(1) when JOB_ID is not set."""
        env = {"NATS_URL": "nats://localhost:4222"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.dict(os.environ, {"JOB_ID": ""}, clear=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            load_config()

        assert exc_info.value.code == 1

    def test_exits_when_both_missing(self):
        """load_config calls sys.exit(1) when both vars are missing."""
        with (
            patch.dict(os.environ, {"NATS_URL": "", "JOB_ID": ""}, clear=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            load_config()

        assert exc_info.value.code == 1

    def test_handles_malformed_job_config_file(self, tmp_path):
        """load_config warns but continues when job config JSON is invalid."""
        config_file = tmp_path / "job-config.json"
        config_file.write_text("{invalid json!")

        env = {"NATS_URL": "nats://localhost:4222", "JOB_ID": "job-xyz"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
        ):
            config = load_config()

        # Should still return the env-based config
        assert config["nats_url"] == "nats://localhost:4222"
        assert config["job_id"] == "job-xyz"

    def test_handles_missing_job_config_file(self):
        """load_config works fine when job config file does not exist."""
        non_existent = Path("/tmp/nonexistent-job-config.json")
        env = {"NATS_URL": "nats://localhost:4222", "JOB_ID": "job-abc"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", non_existent),
        ):
            config = load_config()

        assert config["nats_url"] == "nats://localhost:4222"
        assert config["job_id"] == "job-abc"

    def test_job_config_file_overrides_env_job_id(self, tmp_path):
        """Job config file values override env-derived values when keys overlap."""
        job_config = {"job_id": "overridden-job-id", "nats_url": "nats://override:4222"}
        config_file = tmp_path / "job-config.json"
        config_file.write_text(json.dumps(job_config))

        env = {"NATS_URL": "nats://localhost:4222", "JOB_ID": "env-job-id"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
        ):
            config = load_config()

        # config.update(json.load(f)) means file values win
        assert config["job_id"] == "overridden-job-id"
        assert config["nats_url"] == "nats://override:4222"


# =============================================================================
# Test: detect_ip()
# =============================================================================


class TestDetectIp:
    """Tests for detect_ip() — Tailscale, LAN fallback, hostname fallback."""

    def test_prefers_tailscale_ip(self):
        """detect_ip returns Tailscale IP when tailscale command succeeds."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "100.64.1.42\n"

        with patch("subprocess.run", return_value=mock_result):
            ip = detect_ip()

        assert ip == "100.64.1.42"

    def test_falls_back_to_lan_when_tailscale_not_installed(self):
        """detect_ip falls back to LAN IP when tailscale binary is missing."""
        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("192.168.1.100", 0)

        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("socket.socket", return_value=mock_socket),
        ):
            ip = detect_ip()

        assert ip == "192.168.1.100"

    def test_falls_back_to_lan_when_tailscale_returns_nonzero(self):
        """detect_ip falls back to LAN when tailscale exits with error."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("10.0.0.5", 0)

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("socket.socket", return_value=mock_socket),
        ):
            ip = detect_ip()

        assert ip == "10.0.0.5"

    def test_falls_back_to_lan_when_tailscale_returns_empty(self):
        """detect_ip falls back to LAN when tailscale returns empty string."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  \n"

        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("172.16.0.10", 0)

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("socket.socket", return_value=mock_socket),
        ):
            ip = detect_ip()

        assert ip == "172.16.0.10"

    def test_skips_loopback_from_lan_detection(self):
        """detect_ip skips 127.x.x.x from UDP trick and tries gethostbyname."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("127.0.0.1", 0)

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("socket.socket", return_value=mock_socket),
            patch("socket.gethostbyname", return_value="10.0.0.99"),
            patch("socket.gethostname", return_value="vm-agent-01"),
        ):
            ip = detect_ip()

        assert ip == "10.0.0.99"

    def test_falls_back_to_hostname_when_all_ip_detection_fails(self):
        """detect_ip returns hostname when all IP detection methods fail."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("socket.socket", side_effect=OSError("no network")),
            patch("socket.gethostbyname", side_effect=OSError("unresolvable")),
            patch("socket.gethostname", return_value="my-vm-host"),
        ):
            ip = detect_ip()

        assert ip == "my-vm-host"

    def test_hostname_fallback_when_gethostbyname_returns_loopback(self):
        """detect_ip returns hostname when gethostbyname gives 127.x.x.x."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("socket.socket", side_effect=OSError("no network")),
            patch("socket.gethostbyname", return_value="127.0.1.1"),
            patch("socket.gethostname", return_value="loopback-vm"),
        ):
            ip = detect_ip()

        assert ip == "loopback-vm"

    def test_tailscale_timeout_falls_through(self):
        """detect_ip handles tailscale subprocess timeout gracefully."""
        import subprocess

        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("10.0.0.77", 0)

        with (
            patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 5)
            ),
            patch("socket.socket", return_value=mock_socket),
        ):
            ip = detect_ip()

        assert ip == "10.0.0.77"


# =============================================================================
# Test: read_agent_pid()
# =============================================================================


class TestReadAgentPid:
    """Tests for read_agent_pid() — PID file reading, alive check."""

    def test_returns_pid_when_process_alive(self, tmp_path):
        """read_agent_pid returns the PID when the file exists and process is alive."""
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("12345\n")

        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill") as mock_kill,
        ):
            mock_kill.return_value = None  # os.kill(pid, 0) succeeds
            result = read_agent_pid()

        assert result == 12345
        mock_kill.assert_called_once_with(12345, 0)

    def test_returns_none_when_pid_file_missing(self, tmp_path):
        """read_agent_pid returns None when PID file does not exist."""
        pid_file = tmp_path / "agent.pid"  # Does not exist

        with patch.object(daemon_mod, "AGENT_PID_FILE", pid_file):
            result = read_agent_pid()

        assert result is None

    def test_returns_none_when_process_not_alive(self, tmp_path):
        """read_agent_pid returns None when PID file exists but process is dead."""
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("99999\n")

        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            result = read_agent_pid()

        assert result is None

    def test_returns_none_when_pid_file_contains_garbage(self, tmp_path):
        """read_agent_pid returns None when PID file has non-numeric content."""
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("not-a-number\n")

        with patch.object(daemon_mod, "AGENT_PID_FILE", pid_file):
            result = read_agent_pid()

        assert result is None

    def test_returns_none_when_permission_denied(self, tmp_path):
        """read_agent_pid returns None when os.kill raises PermissionError."""
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("1\n")

        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill", side_effect=PermissionError),
        ):
            result = read_agent_pid()

        assert result is None

    def test_returns_none_when_pid_file_is_empty(self, tmp_path):
        """read_agent_pid returns None when PID file is empty."""
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("")

        with patch.object(daemon_mod, "AGENT_PID_FILE", pid_file):
            result = read_agent_pid()

        assert result is None

    def test_strips_whitespace_from_pid(self, tmp_path):
        """read_agent_pid strips whitespace/newlines from PID file content."""
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("  42  \n")

        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill") as mock_kill,
        ):
            mock_kill.return_value = None
            result = read_agent_pid()

        assert result == 42


# =============================================================================
# Test: get_system_metrics()
# =============================================================================


class TestGetSystemMetrics:
    """Tests for get_system_metrics() — psutil metrics, ImportError fallback."""

    def test_returns_metrics_when_psutil_available(self):
        """get_system_metrics returns CPU, memory, disk usage from psutil."""
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 45.2
        mock_psutil.virtual_memory.return_value = MagicMock(percent=72.1)
        mock_psutil.disk_usage.return_value = MagicMock(percent=55.0)

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            metrics = get_system_metrics()

        assert metrics["cpu_percent"] == 45.2
        assert metrics["memory_percent"] == 72.1
        assert metrics["disk_percent"] == 55.0

    def test_returns_zeros_when_psutil_not_installed(self):
        """get_system_metrics returns zero metrics when psutil ImportError."""
        # Remove psutil from modules to force ImportError on re-import
        with patch.dict("sys.modules", {"psutil": None}):
            # The function does `import psutil` inside a try block.
            # When sys.modules has None, import raises ImportError.
            metrics = get_system_metrics()

        assert metrics == {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "disk_percent": 0.0,
        }

    def test_returns_zeros_on_psutil_exception(self):
        """get_system_metrics returns zero metrics when psutil raises."""
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.side_effect = RuntimeError("sensor error")

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            metrics = get_system_metrics()

        assert metrics == {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "disk_percent": 0.0,
        }


# =============================================================================
# Test: ManagementDaemon.__init__
# =============================================================================


class TestDaemonInit:
    """Tests for ManagementDaemon initialization."""

    def test_init_stores_config(self):
        """Daemon stores the provided config and extracts job_id and nats_url."""
        config = _make_config(extra="value")
        d = ManagementDaemon(config)

        assert d.config is config
        assert d.job_id == "test-job-001"
        assert d.nats_url == "nats://localhost:4222"
        assert d.nc is None
        assert d._agent_exit_reported is False

    def test_shutdown_event_is_initially_unset(self):
        """The _shutdown event starts unset."""
        d = _make_daemon()
        assert not d._shutdown.is_set()


# =============================================================================
# Test: ManagementDaemon.connect_nats()
# =============================================================================


class TestConnectNats:
    """Tests for connect_nats() — connection with retry, shutdown during retry."""

    @pytest.mark.asyncio
    async def test_connects_successfully_on_first_try(self, daemon):
        """connect_nats connects to NATS on the first attempt."""
        mock_nc = AsyncMock()

        with patch("nats.connect", new_callable=AsyncMock, return_value=mock_nc):
            await daemon.connect_nats()

        assert daemon.nc is mock_nc

    @pytest.mark.asyncio
    async def test_retries_on_connection_failure(self, daemon):
        """connect_nats retries when initial connection fails."""
        mock_nc = AsyncMock()
        attempt = 0

        async def connect_side_effect(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                raise ConnectionRefusedError("Connection refused")
            return mock_nc

        with patch("nats.connect", side_effect=connect_side_effect):
            await daemon.connect_nats()

        assert daemon.nc is mock_nc
        assert attempt == 3

    @pytest.mark.asyncio
    async def test_stops_retrying_on_shutdown(self, daemon):
        """connect_nats exits the retry loop when shutdown is requested."""
        call_count = 0

        async def connect_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionRefusedError("refused")
            raise ConnectionRefusedError("refused again")

        async def set_shutdown_during_wait():
            # Give connect_nats time to fail once, then trigger shutdown
            await asyncio.sleep(0.01)
            daemon.request_shutdown()

        with patch("nats.connect", side_effect=connect_side_effect):
            # Run both concurrently — connect_nats should exit when shutdown fires
            await asyncio.gather(
                daemon.connect_nats(),
                set_shutdown_during_wait(),
            )

        # Should have tried at least once but stopped (nc remains None or unset)
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_passes_correct_nats_url(self, daemon):
        """connect_nats passes the configured NATS URL to nats.connect."""
        mock_nc = AsyncMock()

        with patch(
            "nats.connect", new_callable=AsyncMock, return_value=mock_nc
        ) as mock_connect:
            await daemon.connect_nats()

        mock_connect.assert_called_once()
        args, kwargs = mock_connect.call_args
        assert args[0] == "nats://localhost:4222"

    @pytest.mark.asyncio
    async def test_sets_reconnect_callbacks(self, daemon):
        """connect_nats passes error/disconnect/reconnect callbacks."""
        mock_nc = AsyncMock()

        with patch(
            "nats.connect", new_callable=AsyncMock, return_value=mock_nc
        ) as mock_connect:
            await daemon.connect_nats()

        _, kwargs = mock_connect.call_args
        assert "error_cb" in kwargs
        assert "disconnected_cb" in kwargs
        assert "reconnected_cb" in kwargs
        assert kwargs["max_reconnect_attempts"] == -1


# =============================================================================
# Test: ManagementDaemon.register()
# =============================================================================


class TestRegister:
    """Tests for register() — payload format, NATS publish."""

    @pytest.mark.asyncio
    async def test_publishes_registration_payload(self, connected_daemon):
        """register publishes a JSON payload with job_id, hostname, ip, pid."""
        with (
            patch.object(daemon_mod, "detect_ip", return_value="100.64.1.10"),
            patch("socket.gethostname", return_value="vm-agent-42"),
            patch("os.getpid", return_value=1234),
        ):
            await connected_daemon.register()

        connected_daemon.nc.publish.assert_called_once()
        args = connected_daemon.nc.publish.call_args
        subject = args[0][0]
        payload = json.loads(args[0][1].decode())

        assert subject == "agent.vm.test-job-001.register"
        assert payload["job_id"] == "test-job-001"
        assert payload["hostname"] == "vm-agent-42"
        assert payload["ip"] == "100.64.1.10"
        assert payload["pid"] == 1234

    @pytest.mark.asyncio
    async def test_stores_registered_ip(self, connected_daemon):
        """register stores the detected IP in _registered_ip for ip_update_loop."""
        with (
            patch.object(daemon_mod, "detect_ip", return_value="10.0.0.5"),
            patch("socket.gethostname", return_value="host"),
            patch("os.getpid", return_value=1),
        ):
            await connected_daemon.register()

        assert connected_daemon._registered_ip == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_uses_correct_nats_subject(self, connected_daemon):
        """register uses agent.vm.{job_id}.register as the NATS subject."""
        connected_daemon.job_id = "my-special-job-99"

        with (
            patch.object(daemon_mod, "detect_ip", return_value="1.2.3.4"),
            patch("socket.gethostname", return_value="h"),
            patch("os.getpid", return_value=1),
        ):
            await connected_daemon.register()

        subject = connected_daemon.nc.publish.call_args[0][0]
        assert subject == "agent.vm.my-special-job-99.register"


# =============================================================================
# Test: ManagementDaemon._on_control()
# =============================================================================


class TestOnControl:
    """Tests for _on_control() — freeze, resume, terminate, unknown action."""

    def _make_msg(self, action: str, **extra) -> MagicMock:
        """Create a mock NATS message with a JSON payload."""
        data = {"action": action, **extra}
        msg = MagicMock()
        msg.data = json.dumps(data).encode()
        return msg

    @pytest.mark.asyncio
    async def test_freeze_sends_sigstop(self, connected_daemon):
        """_on_control with action=freeze sends SIGSTOP to the agent."""
        msg = self._make_msg("freeze")

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill") as mock_kill,
        ):
            await connected_daemon._on_control(msg)

        mock_kill.assert_called_once_with(5678, signal.SIGSTOP)

    @pytest.mark.asyncio
    async def test_resume_sends_sigcont(self, connected_daemon):
        """_on_control with action=resume sends SIGCONT to the agent."""
        msg = self._make_msg("resume")

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill") as mock_kill,
        ):
            await connected_daemon._on_control(msg)

        mock_kill.assert_called_once_with(5678, signal.SIGCONT)

    @pytest.mark.asyncio
    async def test_terminate_sends_sigterm_then_sigkill(self, connected_daemon):
        """_on_control with action=terminate sends SIGTERM, waits, then SIGKILL."""
        msg = self._make_msg("terminate")
        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            # On the second os.kill(pid, 0) check, process is still alive
            if sig == 0 and len([c for c in kill_calls if c[1] == 0]) <= 1:
                return  # Process still alive

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill", side_effect=fake_kill),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await connected_daemon._on_control(msg)

        signals_sent = [sig for _, sig in kill_calls]
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent

    @pytest.mark.asyncio
    async def test_terminate_skips_sigkill_if_process_already_dead(
        self, connected_daemon
    ):
        """_on_control with action=terminate skips SIGKILL if process exited."""
        msg = self._make_msg("terminate")
        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError("gone")

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill", side_effect=fake_kill),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await connected_daemon._on_control(msg)

        signals_sent = [sig for _, sig in kill_calls]
        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL not in signals_sent

    @pytest.mark.asyncio
    async def test_unknown_action_logs_warning(self, connected_daemon):
        """_on_control with unknown action logs a warning and does nothing."""
        msg = self._make_msg("explode")

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill") as mock_kill,
        ):
            await connected_daemon._on_control(msg)

        # os.kill should not be called for unknown actions
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_agent_pid_skips_action(self, connected_daemon):
        """_on_control does nothing when no agent process is found."""
        msg = self._make_msg("freeze")

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=None),
            patch("os.kill") as mock_kill,
        ):
            await connected_daemon._on_control(msg)

        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_malformed_message_gracefully(self, connected_daemon):
        """_on_control handles non-JSON messages without crashing."""
        msg = MagicMock()
        msg.data = b"not json at all"

        # Should not raise — the except block catches everything
        await connected_daemon._on_control(msg)


# =============================================================================
# Test: ManagementDaemon.heartbeat_loop()
# =============================================================================


class TestHeartbeatLoop:
    """Tests for heartbeat_loop() — periodic publish, shutdown handling."""

    @pytest.mark.asyncio
    async def test_publishes_heartbeat_payload(self, connected_daemon):
        """heartbeat_loop publishes a heartbeat with agent status and metrics."""
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=4321),
            patch.object(
                daemon_mod,
                "get_system_metrics",
                return_value={
                    "cpu_percent": 10.0,
                    "memory_percent": 20.0,
                    "disk_percent": 30.0,
                },
            ),
        ):
            # Let it run one iteration then shutdown
            async def shutdown_after_publish():
                # Wait for the publish call
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.heartbeat_loop(),
                shutdown_after_publish(),
            )

        connected_daemon.nc.publish.assert_called()
        args = connected_daemon.nc.publish.call_args
        subject = args[0][0]
        payload = json.loads(args[0][1].decode())

        assert subject == "agent.vm.test-job-001.heartbeat"
        assert payload["job_id"] == "test-job-001"
        assert payload["agent_pid"] == 4321
        assert payload["agent_running"] is True
        assert payload["cpu_percent"] == 10.0

    @pytest.mark.asyncio
    async def test_reports_agent_not_running(self, connected_daemon):
        """heartbeat_loop reports agent_running=False when no agent PID."""
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=None),
            patch.object(
                daemon_mod,
                "get_system_metrics",
                return_value={
                    "cpu_percent": 0.0,
                    "memory_percent": 0.0,
                    "disk_percent": 0.0,
                },
            ),
        ):

            async def shutdown_after_publish():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.heartbeat_loop(),
                shutdown_after_publish(),
            )

        payload = json.loads(connected_daemon.nc.publish.call_args[0][1].decode())
        assert payload["agent_running"] is False
        assert payload["agent_pid"] is None

    @pytest.mark.asyncio
    async def test_stops_on_shutdown(self, connected_daemon):
        """heartbeat_loop exits when shutdown is requested."""
        connected_daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=None),
            patch.object(daemon_mod, "get_system_metrics", return_value={}),
        ):
            # Should complete quickly since shutdown is already set
            await asyncio.wait_for(connected_daemon.heartbeat_loop(), timeout=2.0)

    @pytest.mark.asyncio
    async def test_skips_publish_when_nats_disconnected(self, connected_daemon):
        """heartbeat_loop skips publish when NATS is not connected."""
        connected_daemon.nc.is_connected = False

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=100),
            patch.object(daemon_mod, "get_system_metrics", return_value={}),
        ):

            async def shutdown_shortly():
                await asyncio.sleep(0.05)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.heartbeat_loop(),
                shutdown_shortly(),
            )

        connected_daemon.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_exception_in_heartbeat(self, connected_daemon):
        """heartbeat_loop continues after an exception in one iteration."""
        call_count = 0

        def side_effect_read_pid():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            return 123

        with (
            patch.object(
                daemon_mod, "read_agent_pid", side_effect=side_effect_read_pid
            ),
            patch.object(
                daemon_mod,
                "get_system_metrics",
                return_value={
                    "cpu_percent": 0.0,
                    "memory_percent": 0.0,
                    "disk_percent": 0.0,
                },
            ),
        ):

            async def shutdown_after_second_call():
                while call_count < 2:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.heartbeat_loop(),
                shutdown_after_second_call(),
            )

        # Second call succeeded, so publish was called at least once
        assert connected_daemon.nc.publish.called


# =============================================================================
# Test: ManagementDaemon.agent_monitor_loop()
# =============================================================================


class TestAgentMonitorLoop:
    """Tests for agent_monitor_loop() — exit detection, status reporting."""

    @pytest.mark.asyncio
    async def test_reports_completed_on_exit_code_zero(
        self, connected_daemon, tmp_path
    ):
        """agent_monitor_loop reports status=completed when exit code is 0."""
        exit_code_file = tmp_path / "agent.exit_code"
        exit_code_file.write_text("0\n")

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return 9999  # Agent running
            return None  # Agent gone

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_code_file),
        ):

            async def shutdown_after_report():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_after_report(),
            )

        payload = json.loads(connected_daemon.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "completed"
        assert payload["exit_code"] == 0
        assert payload["job_id"] == "test-job-001"

    @pytest.mark.asyncio
    async def test_reports_failed_on_nonzero_exit_code(
        self, connected_daemon, tmp_path
    ):
        """agent_monitor_loop reports status=failed when exit code != 0."""
        exit_code_file = tmp_path / "agent.exit_code"
        exit_code_file.write_text("1\n")

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 9999
            return None

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_code_file),
        ):

            async def shutdown_after_report():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_after_report(),
            )

        payload = json.loads(connected_daemon.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_defaults_to_failure_when_no_exit_code_file(
        self, connected_daemon, tmp_path
    ):
        """agent_monitor_loop defaults exit_code=1 when exit code file missing."""
        non_existent = tmp_path / "no-such-file"

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 8888
            return None

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", non_existent),
        ):

            async def shutdown_after_report():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_after_report(),
            )

        payload = json.loads(connected_daemon.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_reports_exit_only_once(self, connected_daemon, tmp_path):
        """agent_monitor_loop sets _agent_exit_reported and does not re-report."""
        exit_code_file = tmp_path / "agent.exit_code"
        exit_code_file.write_text("0\n")

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 7777
            return None

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_code_file),
        ):

            async def shutdown_after_multiple_polls():
                while call_count < 4:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_after_multiple_polls(),
            )

        # publish should be called exactly once
        assert connected_daemon.nc.publish.call_count == 1
        assert connected_daemon._agent_exit_reported is True

    @pytest.mark.asyncio
    async def test_no_report_when_agent_never_started(self, connected_daemon):
        """agent_monitor_loop does not report if agent was never running."""
        call_count = 0

        def always_none():
            nonlocal call_count
            call_count += 1
            return None

        with patch.object(daemon_mod, "read_agent_pid", side_effect=always_none):

            async def shutdown_shortly():
                while call_count < 3:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_shortly(),
            )

        connected_daemon.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_publish_when_nats_disconnected(
        self, connected_daemon, tmp_path
    ):
        """agent_monitor_loop skips publish when NATS is disconnected."""
        exit_code_file = tmp_path / "agent.exit_code"
        exit_code_file.write_text("0\n")

        connected_daemon.nc.is_connected = False

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 6666
            return None

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_code_file),
        ):

            async def shutdown_shortly():
                while call_count < 3:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_shortly(),
            )

        connected_daemon.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_correct_nats_subject(self, connected_daemon, tmp_path):
        """agent_monitor_loop publishes to agent.vm.{job_id}.status."""
        exit_code_file = tmp_path / "agent.exit_code"
        exit_code_file.write_text("0\n")

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 5555
            return None

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_code_file),
        ):

            async def shutdown_after_report():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_after_report(),
            )

        subject = connected_daemon.nc.publish.call_args[0][0]
        assert subject == "agent.vm.test-job-001.status"

    @pytest.mark.asyncio
    async def test_handles_malformed_exit_code_file(self, connected_daemon, tmp_path):
        """agent_monitor_loop defaults to exit_code=1 when file is not a number."""
        exit_code_file = tmp_path / "agent.exit_code"
        exit_code_file.write_text("crash!\n")

        call_count = 0

        def pid_sequence():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 4444
            return None

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=pid_sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_code_file),
        ):

            async def shutdown_after_report():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                shutdown_after_report(),
            )

        payload = json.loads(connected_daemon.nc.publish.call_args[0][1].decode())
        assert payload["exit_code"] == 1
        assert payload["status"] == "failed"


# =============================================================================
# Test: ManagementDaemon.ip_update_loop()
# =============================================================================


class TestIpUpdateLoop:
    """Tests for ip_update_loop() — IP change detection, re-registration."""

    @pytest.mark.asyncio
    async def test_re_registers_when_ip_changes(self, connected_daemon):
        """ip_update_loop calls register() when IP changes."""
        connected_daemon._registered_ip = "10.0.0.1"
        call_count = 0

        def changing_ip():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "100.64.1.50"  # Changed
            return "100.64.1.50"

        with (
            patch.object(daemon_mod, "detect_ip", side_effect=changing_ip),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as mock_register,
        ):

            async def shutdown_after_reregister():
                while not mock_register.called:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.ip_update_loop(),
                shutdown_after_reregister(),
            )

        mock_register.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_re_register_when_ip_unchanged(self, connected_daemon):
        """ip_update_loop does not call register() when IP stays the same."""
        connected_daemon._registered_ip = "10.0.0.1"
        check_count = 0

        def same_ip():
            nonlocal check_count
            check_count += 1
            return "10.0.0.1"

        with (
            patch.object(daemon_mod, "detect_ip", side_effect=same_ip),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as mock_register,
        ):

            async def shutdown_after_checks():
                while check_count < 3:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.ip_update_loop(),
                shutdown_after_checks(),
            )

        mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_on_shutdown(self, connected_daemon):
        """ip_update_loop exits promptly when shutdown is requested."""
        connected_daemon._registered_ip = "10.0.0.1"
        connected_daemon.request_shutdown()

        with patch.object(daemon_mod, "detect_ip", return_value="10.0.0.1"):
            await asyncio.wait_for(connected_daemon.ip_update_loop(), timeout=2.0)

    @pytest.mark.asyncio
    async def test_handles_detect_ip_exception(self, connected_daemon):
        """ip_update_loop continues after detect_ip raises an exception."""
        connected_daemon._registered_ip = "10.0.0.1"
        call_count = 0

        def flaky_detect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("network flake")
            return "10.0.0.1"

        with (
            patch.object(daemon_mod, "detect_ip", side_effect=flaky_detect),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as mock_register,
        ):

            async def shutdown_after_recovery():
                while call_count < 2:
                    await asyncio.sleep(0.01)
                connected_daemon.request_shutdown()

            await asyncio.gather(
                connected_daemon.ip_update_loop(),
                shutdown_after_recovery(),
            )

        # Should not have re-registered since second call returned same IP
        mock_register.assert_not_called()


# =============================================================================
# Test: ManagementDaemon._wait_for_cloud_init()
# =============================================================================


class TestWaitForCloudInit:
    """Tests for _wait_for_cloud_init() — file existence polling, timeout.

    These tests patch asyncio.sleep to avoid real 1-second waits per iteration.
    """

    @pytest.mark.asyncio
    async def test_returns_immediately_when_file_exists(self, daemon, tmp_path):
        """_wait_for_cloud_init returns quickly when job config is present."""
        config_file = tmp_path / "job-config.json"
        config_file.write_text('{"job_id": "test"}')

        with patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file):
            await asyncio.wait_for(daemon._wait_for_cloud_init(timeout=5), timeout=2.0)

    @pytest.mark.asyncio
    async def test_times_out_when_file_never_appears(self, daemon, tmp_path):
        """_wait_for_cloud_init logs warning and returns after timeout."""
        non_existent = tmp_path / "nonexistent.json"

        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", non_existent),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await asyncio.wait_for(daemon._wait_for_cloud_init(timeout=3), timeout=2.0)

    @pytest.mark.asyncio
    async def test_waits_until_file_appears(self, daemon, tmp_path):
        """_wait_for_cloud_init polls until the file appears."""
        config_file = tmp_path / "job-config.json"
        check_count = 0

        # File appears on the 3rd check
        def delayed_exists():
            nonlocal check_count
            check_count += 1
            if check_count >= 3:
                config_file.write_text('{"job_id": "test"}')
                return True
            return False

        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
            patch.object(Path, "exists", side_effect=delayed_exists),
            patch.object(Path, "stat", return_value=MagicMock(st_size=20)),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await asyncio.wait_for(daemon._wait_for_cloud_init(timeout=10), timeout=2.0)

        assert check_count >= 3

    @pytest.mark.asyncio
    async def test_exits_early_on_shutdown(self, daemon, tmp_path):
        """_wait_for_cloud_init returns early when shutdown is requested."""
        non_existent = tmp_path / "nonexistent.json"

        async def fake_sleep(_):
            """Trigger shutdown during the sleep."""
            daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", non_existent),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await asyncio.wait_for(daemon._wait_for_cloud_init(timeout=60), timeout=2.0)

        assert daemon._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_ignores_empty_file(self, daemon, tmp_path):
        """_wait_for_cloud_init does not return for a zero-byte config file."""
        config_file = tmp_path / "job-config.json"
        config_file.write_text("")  # Empty file

        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await asyncio.wait_for(daemon._wait_for_cloud_init(timeout=3), timeout=2.0)

        # If we get here without error, the function timed out (correct behavior)


# =============================================================================
# Test: ManagementDaemon._wait_for_tailscale()
# =============================================================================


class TestWaitForTailscale:
    """Tests for _wait_for_tailscale() — polling, not-installed, timeout.

    These tests patch asyncio.sleep to avoid real 1-second waits per iteration.
    """

    @pytest.mark.asyncio
    async def test_returns_immediately_when_tailscale_ready(self, daemon):
        """_wait_for_tailscale returns when tailscale ip -4 succeeds."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "100.64.2.3\n"

        with patch("subprocess.run", return_value=mock_result):
            await asyncio.wait_for(daemon._wait_for_tailscale(), timeout=2.0)

    @pytest.mark.asyncio
    async def test_returns_when_tailscale_not_installed(self, daemon):
        """_wait_for_tailscale returns immediately when tailscale binary missing."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            await asyncio.wait_for(daemon._wait_for_tailscale(), timeout=2.0)

    @pytest.mark.asyncio
    async def test_polls_until_tailscale_connects(self, daemon):
        """_wait_for_tailscale keeps polling until tailscale returns an IP."""
        call_count = 0

        def delayed_tailscale(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count < 3:
                result.returncode = 1
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = "100.64.5.6\n"
            return result

        with (
            patch("subprocess.run", side_effect=delayed_tailscale),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await asyncio.wait_for(daemon._wait_for_tailscale(), timeout=2.0)

        assert call_count >= 3

    @pytest.mark.asyncio
    async def test_times_out_when_tailscale_never_connects(self, daemon):
        """_wait_for_tailscale returns after TAILSCALE_WAIT_TIMEOUT seconds."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        original_timeout = daemon_mod.TAILSCALE_WAIT_TIMEOUT
        try:
            daemon_mod.TAILSCALE_WAIT_TIMEOUT = 3

            with (
                patch("subprocess.run", return_value=mock_result),
                patch("asyncio.sleep", new_callable=AsyncMock),
            ):
                await asyncio.wait_for(daemon._wait_for_tailscale(), timeout=2.0)
        finally:
            daemon_mod.TAILSCALE_WAIT_TIMEOUT = original_timeout

    @pytest.mark.asyncio
    async def test_exits_early_on_shutdown(self, daemon):
        """_wait_for_tailscale returns early when shutdown is requested."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        async def fake_sleep(_):
            daemon.request_shutdown()

        with (
            patch("subprocess.run", return_value=mock_result),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await asyncio.wait_for(daemon._wait_for_tailscale(), timeout=2.0)

        assert daemon._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_handles_subprocess_exception(self, daemon):
        """_wait_for_tailscale continues polling after subprocess errors."""
        call_count = 0

        def exception_then_success(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("broken pipe")
            result = MagicMock()
            result.returncode = 0
            result.stdout = "100.64.9.1\n"
            return result

        with (
            patch("subprocess.run", side_effect=exception_then_success),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await asyncio.wait_for(daemon._wait_for_tailscale(), timeout=2.0)

        assert call_count >= 2


# =============================================================================
# Test: ManagementDaemon.run() — integration
# =============================================================================


class TestDaemonRun:
    """Tests for the run() method — orchestrating the full daemon lifecycle."""

    @pytest.mark.asyncio
    async def test_run_calls_lifecycle_methods_in_order(self, daemon):
        """run() calls cloud-init wait, tailscale wait, connect, register, subscribe."""
        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        mock_nc.publish = AsyncMock()
        mock_nc.subscribe = AsyncMock()
        mock_nc.drain = AsyncMock()

        with (
            patch.object(
                daemon, "_wait_for_cloud_init", new_callable=AsyncMock
            ) as mock_cloud,
            patch.object(
                daemon, "_wait_for_tailscale", new_callable=AsyncMock
            ) as mock_ts,
            patch.object(
                daemon, "connect_nats", new_callable=AsyncMock
            ) as mock_connect,
            patch.object(daemon, "register", new_callable=AsyncMock) as mock_register,
            patch.object(daemon, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(daemon, "agent_monitor_loop", new_callable=AsyncMock),
            patch.object(daemon, "ip_update_loop", new_callable=AsyncMock),
        ):
            # Set nc after connect is called
            async def fake_connect():
                daemon.nc = mock_nc

            mock_connect.side_effect = fake_connect

            # Shutdown after a brief moment
            async def trigger_shutdown():
                await asyncio.sleep(0.05)
                daemon.request_shutdown()

            await asyncio.gather(
                daemon.run(),
                trigger_shutdown(),
            )

        mock_cloud.assert_called_once()
        mock_ts.assert_called_once()
        mock_connect.assert_called_once()
        mock_register.assert_called_once()
        mock_nc.subscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_exits_early_when_shutdown_during_connect(self, daemon):
        """run() exits without registering if shutdown happens during connect."""
        with (
            patch.object(daemon, "_wait_for_cloud_init", new_callable=AsyncMock),
            patch.object(daemon, "_wait_for_tailscale", new_callable=AsyncMock),
            patch.object(
                daemon, "connect_nats", new_callable=AsyncMock
            ) as mock_connect,
            patch.object(daemon, "register", new_callable=AsyncMock) as mock_register,
        ):

            async def fake_connect():
                daemon.request_shutdown()

            mock_connect.side_effect = fake_connect

            await daemon.run()

        mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_drains_nats_on_shutdown(self, daemon):
        """run() drains the NATS connection on shutdown."""
        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        mock_nc.publish = AsyncMock()
        mock_nc.subscribe = AsyncMock()
        mock_nc.drain = AsyncMock()

        with (
            patch.object(daemon, "_wait_for_cloud_init", new_callable=AsyncMock),
            patch.object(daemon, "_wait_for_tailscale", new_callable=AsyncMock),
            patch.object(
                daemon, "connect_nats", new_callable=AsyncMock
            ) as mock_connect,
            patch.object(daemon, "register", new_callable=AsyncMock),
            patch.object(daemon, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(daemon, "agent_monitor_loop", new_callable=AsyncMock),
            patch.object(daemon, "ip_update_loop", new_callable=AsyncMock),
        ):

            async def fake_connect():
                daemon.nc = mock_nc

            mock_connect.side_effect = fake_connect

            async def trigger_shutdown():
                await asyncio.sleep(0.05)
                daemon.request_shutdown()

            await asyncio.gather(
                daemon.run(),
                trigger_shutdown(),
            )

        mock_nc.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_subscribes_to_control_subject(self, daemon):
        """run() subscribes to the correct control subject."""
        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        mock_nc.publish = AsyncMock()
        mock_nc.subscribe = AsyncMock()
        mock_nc.drain = AsyncMock()

        with (
            patch.object(daemon, "_wait_for_cloud_init", new_callable=AsyncMock),
            patch.object(daemon, "_wait_for_tailscale", new_callable=AsyncMock),
            patch.object(
                daemon, "connect_nats", new_callable=AsyncMock
            ) as mock_connect,
            patch.object(daemon, "register", new_callable=AsyncMock),
            patch.object(daemon, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(daemon, "agent_monitor_loop", new_callable=AsyncMock),
            patch.object(daemon, "ip_update_loop", new_callable=AsyncMock),
        ):

            async def fake_connect():
                daemon.nc = mock_nc

            mock_connect.side_effect = fake_connect

            async def trigger_shutdown():
                await asyncio.sleep(0.05)
                daemon.request_shutdown()

            await asyncio.gather(
                daemon.run(),
                trigger_shutdown(),
            )

        mock_nc.subscribe.assert_called_once()
        subject = mock_nc.subscribe.call_args[0][0]
        assert subject == "agent.vm.test-job-001.control"


# =============================================================================
# Test: ManagementDaemon.request_shutdown()
# =============================================================================


class TestRequestShutdown:
    """Tests for request_shutdown()."""

    def test_sets_shutdown_event(self, daemon):
        """request_shutdown sets the _shutdown event."""
        assert not daemon._shutdown.is_set()
        daemon.request_shutdown()
        assert daemon._shutdown.is_set()

    def test_idempotent(self, daemon):
        """request_shutdown can be called multiple times without error."""
        daemon.request_shutdown()
        daemon.request_shutdown()
        assert daemon._shutdown.is_set()


# =============================================================================
# Test: NATS callbacks
# =============================================================================


class TestNatsCallbacks:
    """Tests for the NATS error/disconnect/reconnect callbacks."""

    @pytest.mark.asyncio
    async def test_on_error_does_not_raise(self, daemon):
        """_on_error logs but does not raise."""
        await daemon._on_error(RuntimeError("test error"))

    @pytest.mark.asyncio
    async def test_on_disconnect_does_not_raise(self, daemon):
        """_on_disconnect logs but does not raise."""
        await daemon._on_disconnect()

    @pytest.mark.asyncio
    async def test_on_reconnect_does_not_raise(self, daemon):
        """_on_reconnect logs but does not raise."""
        await daemon._on_reconnect()


# =============================================================================
# Test: main() — signal handling
# =============================================================================


class TestMain:
    """Tests for the main() entry point and signal handling."""

    def test_main_registers_signal_handlers(self):
        """main() registers SIGTERM and SIGINT handlers."""
        registered_signals = {}

        def fake_signal(sig, handler):
            registered_signals[sig] = handler

        mock_config = _make_config()

        with (
            patch.object(daemon_mod, "load_config", return_value=mock_config),
            patch("signal.signal", side_effect=fake_signal),
            patch("asyncio.run"),
        ):
            main()

        assert signal.SIGTERM in registered_signals
        assert signal.SIGINT in registered_signals

    def test_main_calls_asyncio_run(self):
        """main() calls asyncio.run with daemon.run()."""
        mock_config = _make_config()

        with (
            patch.object(daemon_mod, "load_config", return_value=mock_config),
            patch("signal.signal"),
            patch("asyncio.run") as mock_run,
        ):
            main()

        mock_run.assert_called_once()

    def test_signal_handler_triggers_shutdown(self):
        """The signal handler registered by main() calls request_shutdown."""
        captured_handler = None

        def capture_signal(sig, handler):
            nonlocal captured_handler
            if sig == signal.SIGTERM:
                captured_handler = handler

        mock_config = _make_config()

        with (
            patch.object(daemon_mod, "load_config", return_value=mock_config),
            patch("signal.signal", side_effect=capture_signal),
            patch("asyncio.run"),
        ):
            main()

        assert captured_handler is not None

        # Create a daemon manually to check the handler's target
        # The handler in main() calls daemon.request_shutdown()
        # We verify by calling the handler and checking it doesn't raise
        captured_handler(signal.SIGTERM, None)

    def test_main_creates_daemon_with_config(self):
        """main() creates a ManagementDaemon with the loaded config."""
        mock_config = _make_config(extra="extra_value")
        created_daemons = []

        original_init = ManagementDaemon.__init__

        def tracking_init(self, config):
            original_init(self, config)
            created_daemons.append(self)

        with (
            patch.object(daemon_mod, "load_config", return_value=mock_config),
            patch.object(ManagementDaemon, "__init__", tracking_init),
            patch("signal.signal"),
            patch("asyncio.run"),
        ):
            main()

        assert len(created_daemons) == 1
        assert created_daemons[0].config is mock_config


# =============================================================================
# Test: Module-level constants
# =============================================================================


class TestConstants:
    """Verify module-level constants have expected values."""

    def test_agent_pid_file_path(self):
        assert AGENT_PID_FILE == Path("/run/agent/agent.pid")

    def test_agent_exit_code_file_path(self):
        assert AGENT_EXIT_CODE_FILE == Path("/run/agent/agent.exit_code")

    def test_job_config_file_path(self):
        assert JOB_CONFIG_FILE == Path("/run/agent/job-config.json")

    def test_heartbeat_interval(self):
        assert HEARTBEAT_INTERVAL == 30

    def test_agent_poll_interval(self):
        assert AGENT_POLL_INTERVAL == 10

    def test_nats_retry_interval(self):
        assert NATS_RETRY_INTERVAL == 5

    def test_tailscale_wait_timeout(self):
        assert TAILSCALE_WAIT_TIMEOUT == 60

    def test_ip_recheck_interval(self):
        assert IP_RECHECK_INTERVAL == 15
