"""Unit tests for the dual-transport VM management daemon."""

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

PROJECT_ROOT = Path(__file__).parent.parent
DAEMON_PATH = (
    PROJECT_ROOT / "docker" / "agent-vm-base" / "files" / "management-daemon.py"
)


def _import_daemon() -> ModuleType:
    spec = importlib.util.spec_from_file_location("management_daemon", DAEMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if "nats" not in sys.modules:
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    sys.modules["nats"] = mock_nats

daemon_mod = _import_daemon()
ManagementDaemon = daemon_mod.ManagementDaemon
load_config = daemon_mod.load_config
detect_ip = daemon_mod.detect_ip
read_agent_pid = daemon_mod.read_agent_pid
get_system_metrics = daemon_mod.get_system_metrics
main = daemon_mod.main

AGENT_PID_FILE = daemon_mod.AGENT_PID_FILE
AGENT_EXIT_CODE_FILE = daemon_mod.AGENT_EXIT_CODE_FILE
JOB_CONFIG_FILE = daemon_mod.JOB_CONFIG_FILE
HEARTBEAT_INTERVAL = daemon_mod.HEARTBEAT_INTERVAL
AGENT_POLL_INTERVAL = daemon_mod.AGENT_POLL_INTERVAL
NATS_RETRY_INTERVAL = daemon_mod.NATS_RETRY_INTERVAL
TAILSCALE_WAIT_TIMEOUT = daemon_mod.TAILSCALE_WAIT_TIMEOUT
IP_RECHECK_INTERVAL = daemon_mod.IP_RECHECK_INTERVAL


def _http_config(**overrides) -> dict:
    config = {
        "transport": "http",
        "orchestrator_url": "http://orchestrator.srw.svc.cluster.local:8085",
        "vm_auth_token": "a" * 64,
        "entity_id": "11111111-1111-4111-8111-111111111111",
        "entity_type": "job",
        "job_id": "11111111-1111-4111-8111-111111111111",
        "vm_id": "agent-vm-11111111-1111-4111-8111-111111111111",
        "nats_url": "",
        "orchestrator_id": "",
    }
    config.update(overrides)
    return config


def _nats_config(**overrides) -> dict:
    config = {
        "transport": "nats",
        "orchestrator_url": "",
        "vm_auth_token": "",
        "entity_id": "",
        "entity_type": "",
        "job_id": "job-1",
        "vm_id": "agent-vm-job-1",
        "nats_url": "nats://nats:4222",
        "orchestrator_id": "orch-1",
    }
    config.update(overrides)
    return config


def _make_config(**overrides) -> dict:
    """Create the legacy NATS fixture used by the pre-HTTP test matrix."""
    config = _nats_config(
        nats_url="nats://localhost:4222",
        job_id="test-job-001",
        vm_id="agent-vm-test-job-001",
        orchestrator_id="test-orch-001",
    )
    config.update(overrides)
    return config


def _make_daemon(**config_overrides) -> ManagementDaemon:
    return ManagementDaemon(_make_config(**config_overrides))


@pytest.fixture(autouse=True)
def fast_intervals(monkeypatch):
    for name in (
        "HEARTBEAT_INTERVAL",
        "AGENT_POLL_INTERVAL",
        "NATS_RETRY_INTERVAL",
        "IP_RECHECK_INTERVAL",
    ):
        monkeypatch.setattr(daemon_mod, name, 0.001)


@pytest.fixture
def daemon():
    return _make_daemon()


@pytest.fixture
def connected_daemon():
    daemon = _make_daemon()
    daemon.nc = AsyncMock()
    daemon.nc.is_connected = True
    daemon.nc.publish = AsyncMock()
    daemon.nc.subscribe = AsyncMock()
    daemon.nc.drain = AsyncMock()
    return daemon


class TestLoadConfig:
    def test_http_mode_is_preferred_when_both_credentials_exist(self, tmp_path):
        job_config = tmp_path / "job-config.json"
        job_config.write_text(json.dumps({"agent_config": "developer"}))
        env = {
            "ORCHESTRATOR_URL": "http://orchestrator:8085",
            "VM_AUTH_TOKEN": "b" * 64,
            "ENTITY_ID": "entity-1",
            "ENTITY_TYPE": "job",
            "JOB_ID": "entity-1",
            "VM_ID": "agent-vm-entity-1",
            "NATS_URL": "nats://nats:4222",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", job_config),
        ):
            config = daemon_mod.load_config()

        assert config["transport"] == "http"
        assert config["orchestrator_url"] == "http://orchestrator:8085"
        assert config["vm_auth_token"] == "b" * 64
        assert config["agent_config"] == "developer"

    def test_nats_mode_preserves_external_contract(self, tmp_path):
        with (
            patch.dict(
                os.environ,
                {
                    "NATS_URL": "nats://nats:4222",
                    "JOB_ID": "job-1",
                    "ORCHESTRATOR_ID": "orch-1",
                },
                clear=True,
            ),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
        ):
            config = daemon_mod.load_config()

        assert config["transport"] == "nats"
        assert config["nats_url"] == "nats://nats:4222"
        assert config["job_id"] == "job-1"
        assert config["orchestrator_id"] == "orch-1"

    def test_partial_http_credentials_fall_back_to_nats(self, tmp_path):
        with (
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_URL": "http://orchestrator:8085",
                    "NATS_URL": "nats://nats:4222",
                    "JOB_ID": "job-1",
                    "ORCHESTRATOR_ID": "orch-1",
                },
                clear=True,
            ),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
        ):
            config = daemon_mod.load_config()

        assert config["transport"] == "nats"

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"ORCHESTRATOR_URL": "http://orchestrator:8085"},
            {"VM_AUTH_TOKEN": "a" * 64},
            {"NATS_URL": "nats://nats:4222", "JOB_ID": "job-1"},
        ],
    )
    def test_invalid_transport_configuration_exits(self, env, tmp_path):
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            pytest.raises(SystemExit, match="1"),
        ):
            daemon_mod.load_config()

    def test_http_requires_entity_id(self, tmp_path):
        with (
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_URL": "http://orchestrator:8085",
                    "VM_AUTH_TOKEN": "a" * 64,
                },
                clear=True,
            ),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            pytest.raises(SystemExit, match="1"),
        ):
            daemon_mod.load_config()

    def test_job_config_cannot_override_transport_auth_or_identity(self, tmp_path):
        job_config = tmp_path / "job-config.json"
        job_config.write_text(
            json.dumps(
                {
                    "transport": "nats",
                    "orchestrator_url": "http://attacker",
                    "vm_auth_token": "stolen",
                    "entity_id": "other-entity",
                    "orchestrator_id": "other-orchestrator",
                    "vm_id": "other-vm",
                    "nats_url": "nats://attacker:4222",
                    "agent_config": "developer",
                }
            )
        )
        env = {
            "ORCHESTRATOR_URL": "http://orchestrator:8085",
            "VM_AUTH_TOKEN": "a" * 64,
            "ENTITY_ID": "entity-1",
            "JOB_ID": "entity-1",
            "ORCHESTRATOR_ID": "orch-1",
            "VM_ID": "agent-vm-entity-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", job_config),
        ):
            config = daemon_mod.load_config()

        assert config["transport"] == "http"
        assert config["orchestrator_url"] == "http://orchestrator:8085"
        assert config["vm_auth_token"] == "a" * 64
        assert config["entity_id"] == "entity-1"
        assert config["orchestrator_id"] == "orch-1"
        assert config["vm_id"] == "agent-vm-entity-1"
        assert config["job_id"] == "entity-1"
        assert config["nats_url"] == ""
        assert config["agent_config"] == "developer"

    def test_nats_job_config_keeps_legacy_non_transport_overrides(self, tmp_path):
        job_config = tmp_path / "job-config.json"
        job_config.write_text(json.dumps({"job_id": "job-from-file"}))
        with (
            patch.dict(
                os.environ,
                {
                    "NATS_URL": "nats://nats:4222",
                    "JOB_ID": "job-from-env",
                    "ORCHESTRATOR_ID": "orch-1",
                },
                clear=True,
            ),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", job_config),
        ):
            config = daemon_mod.load_config()

        assert config["transport"] == "nats"
        assert config["nats_url"] == "nats://nats:4222"
        assert config["job_id"] == "job-from-file"

    def test_loads_from_env_vars(self, tmp_path):
        env = {
            "NATS_URL": "nats://10.0.0.1:4222",
            "JOB_ID": "job-abc-123",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
        ):
            config = load_config()

        assert config["transport"] == "nats"
        assert config["nats_url"] == "nats://10.0.0.1:4222"
        assert config["job_id"] == "job-abc-123"
        assert config["orchestrator_id"] == "orch-1"

    def test_loads_job_config_file(self, tmp_path):
        config_file = tmp_path / "job-config.json"
        config_file.write_text(
            json.dumps({"agent_config": "developer", "extra_key": "extra_value"})
        )
        env = {
            "NATS_URL": "nats://localhost:4222",
            "JOB_ID": "job-xyz",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
        ):
            config = load_config()

        assert config["agent_config"] == "developer"
        assert config["extra_key"] == "extra_value"
        assert config["nats_url"] == "nats://localhost:4222"

    def test_exits_when_nats_url_missing(self, tmp_path):
        env = {"JOB_ID": "job-123", "ORCHESTRATOR_ID": "orch-1"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            pytest.raises(SystemExit, match="1"),
        ):
            load_config()

    def test_exits_when_job_id_missing(self, tmp_path):
        env = {
            "NATS_URL": "nats://localhost:4222",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            pytest.raises(SystemExit, match="1"),
        ):
            load_config()

    def test_exits_when_orchestrator_id_missing(self, tmp_path):
        env = {"NATS_URL": "nats://localhost:4222", "JOB_ID": "job-123"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            pytest.raises(SystemExit, match="1"),
        ):
            load_config()

    def test_exits_when_both_missing(self, tmp_path):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            pytest.raises(SystemExit, match="1"),
        ):
            load_config()

    def test_handles_malformed_job_config_file(self, tmp_path):
        config_file = tmp_path / "job-config.json"
        config_file.write_text("{invalid json!")
        env = {
            "NATS_URL": "nats://localhost:4222",
            "JOB_ID": "job-xyz",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
        ):
            config = load_config()

        assert config["transport"] == "nats"
        assert config["nats_url"] == "nats://localhost:4222"
        assert config["job_id"] == "job-xyz"

    def test_handles_missing_job_config_file(self, tmp_path):
        env = {
            "NATS_URL": "nats://localhost:4222",
            "JOB_ID": "job-abc",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
        ):
            config = load_config()

        assert config["transport"] == "nats"
        assert config["nats_url"] == "nats://localhost:4222"
        assert config["job_id"] == "job-abc"

    def test_job_config_file_overrides_env_job_id(self, tmp_path):
        config_file = tmp_path / "job-config.json"
        config_file.write_text(
            json.dumps(
                {"job_id": "overridden-job-id", "nats_url": "nats://attacker:4222"}
            )
        )
        env = {
            "NATS_URL": "nats://localhost:4222",
            "JOB_ID": "env-job-id",
            "ORCHESTRATOR_ID": "orch-1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config_file),
        ):
            config = load_config()

        assert config["job_id"] == "overridden-job-id"
        assert config["nats_url"] == "nats://localhost:4222"


class TestMetrics:
    def test_system_metrics_from_psutil(self):
        psutil = MagicMock()
        psutil.cpu_percent.return_value = 12.5
        psutil.virtual_memory.return_value.percent = 34.5
        psutil.disk_usage.return_value.percent = 56.5
        with patch.dict(sys.modules, {"psutil": psutil}):
            assert daemon_mod.get_system_metrics() == {
                "cpu_percent": 12.5,
                "memory_percent": 34.5,
                "disk_percent": 56.5,
            }

    def test_system_metrics_fail_closed_to_zeroes(self):
        with patch.dict(sys.modules, {"psutil": None}):
            assert daemon_mod.get_system_metrics() == {
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "disk_percent": 0.0,
            }

    def test_counts_established_code_server_connections(self, tmp_path):
        proc_tcp = tmp_path / "tcp"
        proc_tcp.write_text(
            "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt\n"
            "   0: 0100007F:1F90 0100007F:C001 01 00000000:00000000 00:0 0\n"
            "   1: 0100007F:1F90 0100007F:C002 01 00000000:00000000 00:0 0\n"
            "   2: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:0 0\n"
            "   3: 00000000:0016 0100007F:C003 01 00000000:00000000 00:0 0\n"
        )
        with patch.object(daemon_mod, "PROC_NET_TCP", proc_tcp):
            assert daemon_mod.count_code_server_connections() == 2

    def test_missing_proc_tcp_returns_zero(self, tmp_path):
        with patch.object(daemon_mod, "PROC_NET_TCP", tmp_path / "missing"):
            assert daemon_mod.count_code_server_connections() == 0


class TestHTTPMode:
    def test_http_post_sets_contract_headers_and_timeout(self):
        daemon = ManagementDaemon(_http_config())
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False

        with patch.object(
            daemon_mod.urllib.request, "urlopen", return_value=context
        ) as urlopen:
            result = daemon._http_post("/api/internal/vm/entity-1/register", {"pid": 1})

        request = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        assert request.full_url.endswith("/api/internal/vm/entity-1/register")
        assert request.method == "POST"
        assert headers["authorization"] == "Bearer " + "a" * 64
        assert headers["content-type"] == "application/json"
        assert json.loads(request.data) == {"pid": 1}
        assert urlopen.call_args.kwargs["timeout"] == daemon_mod.HTTP_TIMEOUT
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_register_retries_until_success(self):
        daemon = ManagementDaemon(_http_config(entity_id="entity with space"))
        daemon._http_post = MagicMock(
            side_effect=[OSError("refused"), OSError("reset"), {"ok": True}]
        )
        daemon._wait_or_shutdown = AsyncMock(return_value=False)

        async def inline_to_thread(function, *args):
            return function(*args)

        with (
            patch.object(daemon_mod.asyncio, "to_thread", side_effect=inline_to_thread),
            patch.object(daemon_mod, "detect_ip", return_value="10.0.2.15"),
            patch.object(daemon_mod.socket, "gethostname", return_value="vm-1"),
            patch.object(daemon_mod.os, "getpid", return_value=123),
        ):
            await daemon.register()

        assert daemon._http_post.call_count == 3
        assert [call.args[0] for call in daemon._http_post.call_args_list] == [
            "/api/internal/vm/entity%20with%20space/register"
        ] * 3
        assert daemon._wait_or_shutdown.await_args_list[0].args == (1,)
        assert daemon._wait_or_shutdown.await_args_list[1].args == (2,)
        payload = daemon._http_post.call_args.args[1]
        assert payload == {"hostname": "vm-1", "ip": "10.0.2.15", "pid": 123}

    @pytest.mark.asyncio
    async def test_http_heartbeat_has_exact_wire_fields(self):
        daemon = ManagementDaemon(_http_config())
        daemon._http_post = MagicMock(return_value={"ok": True})

        async def inline_to_thread(function, *args):
            result = function(*args)
            daemon.request_shutdown()
            return result

        with (
            patch.object(daemon_mod.asyncio, "to_thread", side_effect=inline_to_thread),
            patch.object(
                daemon_mod,
                "get_system_metrics",
                return_value={
                    "cpu_percent": 10.0,
                    "memory_percent": 20.0,
                    "disk_percent": 30.0,
                },
            ),
            patch.object(daemon_mod, "count_code_server_connections", return_value=4),
        ):
            await daemon.heartbeat_loop()

        path, payload = daemon._http_post.call_args.args
        assert path.endswith("/heartbeat")
        assert payload == {
            "cpu_percent": 10.0,
            "memory_percent": 20.0,
            "disk_percent": 30.0,
            "code_server_connections": 4,
        }

    @pytest.mark.asyncio
    async def test_http_run_skips_tailscale_nats_ssh_and_legacy_loops(self):
        daemon = ManagementDaemon(_http_config())
        daemon._wait_for_cloud_init = AsyncMock()
        daemon._register_http = AsyncMock()
        daemon._wait_for_tailscale = AsyncMock()
        daemon.connect_nats = AsyncMock()
        daemon.agent_monitor_loop = AsyncMock()
        daemon.ip_update_loop = AsyncMock()

        async def heartbeat_once():
            daemon.request_shutdown()

        daemon.heartbeat_loop = AsyncMock(side_effect=heartbeat_once)
        with patch.object(daemon_mod, "check_ssh_ready") as check_ssh_ready:
            await daemon.run()

        daemon._wait_for_cloud_init.assert_awaited_once()
        daemon._register_http.assert_awaited_once()
        daemon.heartbeat_loop.assert_awaited_once()
        daemon._wait_for_tailscale.assert_not_awaited()
        daemon.connect_nats.assert_not_awaited()
        daemon.agent_monitor_loop.assert_not_awaited()
        daemon.ip_update_loop.assert_not_awaited()
        check_ssh_ready.assert_not_called()


class TestNATSMode:
    @pytest.fixture
    def connected(self):
        daemon = ManagementDaemon(_nats_config())
        daemon.nc = AsyncMock()
        daemon.nc.is_connected = True
        return daemon

    @pytest.mark.asyncio
    async def test_connect_nats_uses_existing_options(self):
        daemon = ManagementDaemon(_nats_config())
        nc = AsyncMock()
        with patch("nats.connect", new_callable=AsyncMock, return_value=nc) as connect:
            await daemon.connect_nats()

        assert daemon.nc is nc
        assert connect.call_args.args == ("nats://nats:4222",)
        assert connect.call_args.kwargs["max_reconnect_attempts"] == -1
        assert connect.call_args.kwargs["reconnect_time_wait"] == 2

    @pytest.mark.asyncio
    async def test_register_preserves_legacy_subject_and_payload(self, connected):
        with (
            patch.object(daemon_mod, "detect_ip", return_value="100.64.1.2"),
            patch.object(daemon_mod, "check_ssh_ready", return_value=True),
            patch.object(daemon_mod.socket, "gethostname", return_value="vm-1"),
            patch.object(daemon_mod.os, "getpid", return_value=123),
        ):
            await connected.register()

        subject, encoded = connected.nc.publish.call_args.args
        assert subject == "agent.vm.orch-1.job-1.register"
        assert json.loads(encoded) == {
            "job_id": "job-1",
            "hostname": "vm-1",
            "ip": "100.64.1.2",
            "pid": 123,
            "ssh_ready": True,
        }

    @pytest.mark.asyncio
    async def test_heartbeat_preserves_legacy_fields(self, connected):
        async def stop_after_publish():
            while not connected.nc.publish.called:
                await asyncio.sleep(0)
            connected.request_shutdown()

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=321),
            patch.object(
                daemon_mod,
                "get_system_metrics",
                return_value={
                    "cpu_percent": 1.0,
                    "memory_percent": 2.0,
                    "disk_percent": 3.0,
                },
            ),
            patch.object(
                daemon_mod, "count_code_server_connections"
            ) as count_connections,
        ):
            await asyncio.gather(connected.heartbeat_loop(), stop_after_publish())

        subject, encoded = connected.nc.publish.call_args.args
        assert subject == "agent.vm.orch-1.job-1.heartbeat"
        assert json.loads(encoded) == {
            "job_id": "job-1",
            "agent_pid": 321,
            "agent_running": True,
            "cpu_percent": 1.0,
            "memory_percent": 2.0,
            "disk_percent": 3.0,
        }
        count_connections.assert_not_called()

    @pytest.mark.asyncio
    async def test_control_command_still_signals_agent(self, connected):
        message = MagicMock(data=json.dumps({"action": "freeze"}).encode())
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=321),
            patch.object(daemon_mod.os, "kill") as kill,
        ):
            await connected._on_control(message)
        kill.assert_called_once_with(321, signal.SIGSTOP)

    @pytest.mark.asyncio
    async def test_nats_run_is_contained_before_any_network_or_process_action(
        self, connected
    ):
        connected._wait_for_cloud_init = AsyncMock()
        connected._wait_for_tailscale = AsyncMock()
        connected.connect_nats = AsyncMock()
        connected.register = AsyncMock()

        connected.heartbeat_loop = AsyncMock()
        connected.agent_monitor_loop = AsyncMock()
        connected.ip_update_loop = AsyncMock()
        await connected.run()

        connected._wait_for_cloud_init.assert_not_awaited()
        connected._wait_for_tailscale.assert_not_awaited()
        connected.connect_nats.assert_not_awaited()
        connected.register.assert_not_awaited()
        connected.nc.subscribe.assert_not_awaited()
        connected.heartbeat_loop.assert_not_awaited()
        connected.agent_monitor_loop.assert_not_awaited()
        connected.ip_update_loop.assert_not_awaited()
        connected.nc.drain.assert_not_awaited()


class TestHelpers:
    def test_detect_ip_prefers_tailscale(self):
        result = MagicMock(returncode=0, stdout="100.64.1.5\n")
        with patch("subprocess.run", return_value=result):
            assert daemon_mod.detect_ip() == "100.64.1.5"

    def test_check_ssh_ready_rejects_non_tailnet_without_probe(self):
        with patch.object(daemon_mod.socket, "create_connection") as connect:
            assert daemon_mod.check_ssh_ready("10.0.2.15") is False
        connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_init_wait_returns_when_file_exists(self, tmp_path):
        config = tmp_path / "job-config.json"
        config.write_text("{}")
        daemon = ManagementDaemon(_http_config())
        with patch.object(daemon_mod, "JOB_CONFIG_FILE", config):
            await daemon._wait_for_cloud_init(timeout=1)

    def test_request_shutdown_sets_event(self):
        daemon = ManagementDaemon(_http_config())
        daemon.request_shutdown()
        assert daemon._shutdown.is_set()


class TestDetectIp:
    """Legacy external-mode IP detection behavior."""

    def test_prefers_tailscale_ip(self):
        result = MagicMock(returncode=0, stdout="100.64.1.42\n")
        with patch("subprocess.run", return_value=result):
            assert detect_ip() == "100.64.1.42"

    def test_falls_back_to_lan_when_tailscale_not_installed(self):
        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("192.168.1.100", 0)
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("socket.socket", return_value=mock_socket),
        ):
            assert detect_ip() == "192.168.1.100"

    def test_falls_back_to_lan_when_tailscale_returns_nonzero(self):
        result = MagicMock(returncode=1, stdout="")
        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("10.0.0.5", 0)
        with (
            patch("subprocess.run", return_value=result),
            patch("socket.socket", return_value=mock_socket),
        ):
            assert detect_ip() == "10.0.0.5"

    def test_falls_back_to_lan_when_tailscale_returns_empty(self):
        result = MagicMock(returncode=0, stdout="  \n")
        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("172.16.0.10", 0)
        with (
            patch("subprocess.run", return_value=result),
            patch("socket.socket", return_value=mock_socket),
        ):
            assert detect_ip() == "172.16.0.10"

    def test_skips_loopback_from_lan_detection(self):
        result = MagicMock(returncode=1, stdout="")
        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("127.0.0.1", 0)
        with (
            patch("subprocess.run", return_value=result),
            patch("socket.socket", return_value=mock_socket),
            patch("socket.gethostbyname", return_value="10.0.0.99"),
            patch("socket.gethostname", return_value="vm-agent-01"),
        ):
            assert detect_ip() == "10.0.0.99"

    def test_falls_back_to_hostname_when_all_ip_detection_fails(self):
        result = MagicMock(returncode=1, stdout="")
        with (
            patch("subprocess.run", return_value=result),
            patch("socket.socket", side_effect=OSError("no network")),
            patch("socket.gethostbyname", side_effect=OSError("unresolvable")),
            patch("socket.gethostname", return_value="my-vm-host"),
        ):
            assert detect_ip() == "my-vm-host"

    def test_hostname_fallback_when_gethostbyname_returns_loopback(self):
        result = MagicMock(returncode=1, stdout="")
        with (
            patch("subprocess.run", return_value=result),
            patch("socket.socket", side_effect=OSError("no network")),
            patch("socket.gethostbyname", return_value="127.0.1.1"),
            patch("socket.gethostname", return_value="loopback-vm"),
        ):
            assert detect_ip() == "loopback-vm"

    def test_tailscale_timeout_falls_through(self):
        import subprocess

        mock_socket = MagicMock()
        mock_socket.__enter__ = MagicMock(return_value=mock_socket)
        mock_socket.__exit__ = MagicMock(return_value=False)
        mock_socket.getsockname.return_value = ("10.0.0.77", 0)
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("tailscale", 5),
            ),
            patch("socket.socket", return_value=mock_socket),
        ):
            assert detect_ip() == "10.0.0.77"


class TestCheckSshReady:
    def test_non_tailnet_ip_false_without_socket_probe(self):
        with patch("socket.create_connection") as connect:
            assert daemon_mod.check_ssh_ready("10.0.2.15") is False
        connect.assert_not_called()

    def test_hostname_false(self):
        with patch("socket.create_connection") as connect:
            assert daemon_mod.check_ssh_ready("vm-agent-42") is False
        connect.assert_not_called()

    def test_tailnet_ip_with_sshd_listening_true(self):
        with patch("socket.create_connection") as connect:
            connect.return_value.__enter__ = MagicMock()
            connect.return_value.__exit__ = MagicMock(return_value=False)
            assert daemon_mod.check_ssh_ready("100.64.1.10") is True
        connect.assert_called_once_with(("127.0.0.1", 22), timeout=2)

    def test_tailnet_ip_with_sshd_down_false(self):
        with patch("socket.create_connection", side_effect=ConnectionRefusedError):
            assert daemon_mod.check_ssh_ready("100.64.1.10") is False


class TestReadAgentPid:
    def test_returns_pid_when_process_alive(self, tmp_path):
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("12345\n")
        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill") as kill,
        ):
            assert read_agent_pid() == 12345
        kill.assert_called_once_with(12345, 0)

    def test_returns_none_when_pid_file_missing(self, tmp_path):
        with patch.object(daemon_mod, "AGENT_PID_FILE", tmp_path / "missing"):
            assert read_agent_pid() is None

    def test_returns_none_when_process_not_alive(self, tmp_path):
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("99999\n")
        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            assert read_agent_pid() is None

    def test_returns_none_when_pid_file_contains_garbage(self, tmp_path):
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("not-a-number\n")
        with patch.object(daemon_mod, "AGENT_PID_FILE", pid_file):
            assert read_agent_pid() is None

    def test_returns_none_when_permission_denied(self, tmp_path):
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("1\n")
        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill", side_effect=PermissionError),
        ):
            assert read_agent_pid() is None

    def test_returns_none_when_pid_file_is_empty(self, tmp_path):
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("")
        with patch.object(daemon_mod, "AGENT_PID_FILE", pid_file):
            assert read_agent_pid() is None

    def test_strips_whitespace_from_pid(self, tmp_path):
        pid_file = tmp_path / "agent.pid"
        pid_file.write_text("  42  \n")
        with (
            patch.object(daemon_mod, "AGENT_PID_FILE", pid_file),
            patch("os.kill") as kill,
        ):
            assert read_agent_pid() == 42
        kill.assert_called_once_with(42, 0)


class TestGetSystemMetrics:
    def test_returns_metrics_when_psutil_available(self):
        psutil = MagicMock()
        psutil.cpu_percent.return_value = 45.2
        psutil.virtual_memory.return_value = MagicMock(percent=72.1)
        psutil.disk_usage.return_value = MagicMock(percent=55.0)
        with patch.dict("sys.modules", {"psutil": psutil}):
            metrics = get_system_metrics()
        assert metrics == {
            "cpu_percent": 45.2,
            "memory_percent": 72.1,
            "disk_percent": 55.0,
        }

    def test_returns_zeros_when_psutil_not_installed(self):
        with patch.dict("sys.modules", {"psutil": None}):
            metrics = get_system_metrics()
        assert metrics == {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "disk_percent": 0.0,
        }

    def test_returns_zeros_on_psutil_exception(self):
        psutil = MagicMock()
        psutil.cpu_percent.side_effect = RuntimeError("sensor error")
        with patch.dict("sys.modules", {"psutil": psutil}):
            metrics = get_system_metrics()
        assert metrics == {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "disk_percent": 0.0,
        }


class TestDaemonInit:
    def test_init_stores_config(self):
        config = _make_config(extra="value")
        daemon = ManagementDaemon(config)
        assert daemon.config is config
        assert daemon.job_id == "test-job-001"
        assert daemon.nats_url == "nats://localhost:4222"
        assert daemon.nc is None
        assert daemon._agent_exit_reported is False

    def test_shutdown_event_is_initially_unset(self):
        assert not _make_daemon()._shutdown.is_set()


class TestConnectNats:
    @pytest.mark.asyncio
    async def test_connects_successfully_on_first_try(self, daemon):
        nc = AsyncMock()
        with patch("nats.connect", new_callable=AsyncMock, return_value=nc):
            await daemon.connect_nats()
        assert daemon.nc is nc

    @pytest.mark.asyncio
    async def test_retries_on_connection_failure(self, daemon):
        nc = AsyncMock()
        attempts = 0

        async def connect(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionRefusedError("refused")
            return nc

        with patch("nats.connect", side_effect=connect):
            await daemon.connect_nats()
        assert daemon.nc is nc
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_stops_retrying_on_shutdown(self, daemon):
        attempts = 0

        async def connect(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise ConnectionRefusedError("refused")

        async def stop():
            await asyncio.sleep(0.01)
            daemon.request_shutdown()

        with patch("nats.connect", side_effect=connect):
            await asyncio.gather(daemon.connect_nats(), stop())
        assert attempts >= 1
        assert daemon.nc is None

    @pytest.mark.asyncio
    async def test_passes_correct_nats_url(self, daemon):
        with patch(
            "nats.connect", new_callable=AsyncMock, return_value=AsyncMock()
        ) as connect:
            await daemon.connect_nats()
        assert connect.call_args.args == ("nats://localhost:4222",)

    @pytest.mark.asyncio
    async def test_sets_reconnect_callbacks(self, daemon):
        with patch(
            "nats.connect", new_callable=AsyncMock, return_value=AsyncMock()
        ) as connect:
            await daemon.connect_nats()
        kwargs = connect.call_args.kwargs
        assert "error_cb" in kwargs
        assert "disconnected_cb" in kwargs
        assert "reconnected_cb" in kwargs
        assert kwargs["max_reconnect_attempts"] == -1


class TestRegister:
    @pytest.mark.asyncio
    async def test_publishes_registration_payload(self, connected_daemon):
        with (
            patch.object(daemon_mod, "detect_ip", return_value="100.64.1.10"),
            patch.object(daemon_mod, "check_ssh_ready", return_value=True),
            patch("socket.gethostname", return_value="vm-agent-42"),
            patch("os.getpid", return_value=1234),
        ):
            await connected_daemon.register()
        subject, encoded = connected_daemon.nc.publish.call_args.args
        assert subject == "agent.vm.test-orch-001.test-job-001.register"
        assert json.loads(encoded) == {
            "job_id": "test-job-001",
            "hostname": "vm-agent-42",
            "ip": "100.64.1.10",
            "pid": 1234,
            "ssh_ready": True,
        }

    @pytest.mark.asyncio
    async def test_stores_registered_ip(self, connected_daemon):
        with (
            patch.object(daemon_mod, "detect_ip", return_value="10.0.0.5"),
            patch.object(daemon_mod, "check_ssh_ready", return_value=False),
            patch("socket.gethostname", return_value="host"),
            patch("os.getpid", return_value=1),
        ):
            await connected_daemon.register()
        assert connected_daemon._registered_ip == "10.0.0.5"
        assert connected_daemon._registered_ssh_ready is False

    @pytest.mark.asyncio
    async def test_uses_correct_nats_subject(self, connected_daemon):
        connected_daemon.job_id = "my-special-job-99"
        with (
            patch.object(daemon_mod, "detect_ip", return_value="1.2.3.4"),
            patch.object(daemon_mod, "check_ssh_ready", return_value=False),
            patch("socket.gethostname", return_value="host"),
            patch("os.getpid", return_value=1),
        ):
            await connected_daemon.register()
        assert (
            connected_daemon.nc.publish.call_args.args[0]
            == "agent.vm.test-orch-001.my-special-job-99.register"
        )


class TestOnControl:
    @staticmethod
    def _message(action: str) -> MagicMock:
        return MagicMock(data=json.dumps({"action": action}).encode())

    @pytest.mark.asyncio
    async def test_freeze_sends_sigstop(self, connected_daemon):
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill") as kill,
        ):
            await connected_daemon._on_control(self._message("freeze"))
        kill.assert_called_once_with(5678, signal.SIGSTOP)

    @pytest.mark.asyncio
    async def test_resume_sends_sigcont(self, connected_daemon):
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill") as kill,
        ):
            await connected_daemon._on_control(self._message("resume"))
        kill.assert_called_once_with(5678, signal.SIGCONT)

    @pytest.mark.asyncio
    async def test_terminate_sends_sigterm_then_sigkill(self, connected_daemon):
        calls = []

        def kill(pid, sig):
            calls.append((pid, sig))

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill", side_effect=kill),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await connected_daemon._on_control(self._message("terminate"))
        assert signal.SIGTERM in [sig for _, sig in calls]
        assert signal.SIGKILL in [sig for _, sig in calls]

    @pytest.mark.asyncio
    async def test_terminate_skips_sigkill_if_process_already_dead(
        self, connected_daemon
    ):
        calls = []

        def kill(pid, sig):
            calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill", side_effect=kill),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await connected_daemon._on_control(self._message("terminate"))
        assert signal.SIGKILL not in [sig for _, sig in calls]

    @pytest.mark.asyncio
    async def test_unknown_action_logs_warning(self, connected_daemon):
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=5678),
            patch("os.kill") as kill,
        ):
            await connected_daemon._on_control(self._message("explode"))
        kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_agent_pid_skips_action(self, connected_daemon):
        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=None),
            patch("os.kill") as kill,
        ):
            await connected_daemon._on_control(self._message("freeze"))
        kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_malformed_message_gracefully(self, connected_daemon):
        await connected_daemon._on_control(MagicMock(data=b"not json"))


class TestHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_publishes_heartbeat_payload(self, connected_daemon):
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

            async def stop():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0)
                connected_daemon.request_shutdown()

            await asyncio.gather(connected_daemon.heartbeat_loop(), stop())

        subject, encoded = connected_daemon.nc.publish.call_args.args
        assert subject == "agent.vm.test-orch-001.test-job-001.heartbeat"
        assert json.loads(encoded) == {
            "job_id": "test-job-001",
            "agent_pid": 4321,
            "agent_running": True,
            "cpu_percent": 10.0,
            "memory_percent": 20.0,
            "disk_percent": 30.0,
        }

    @pytest.mark.asyncio
    async def test_reports_agent_not_running(self, connected_daemon):
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

            async def stop():
                while not connected_daemon.nc.publish.called:
                    await asyncio.sleep(0)
                connected_daemon.request_shutdown()

            await asyncio.gather(connected_daemon.heartbeat_loop(), stop())
        payload = json.loads(connected_daemon.nc.publish.call_args.args[1])
        assert payload["agent_running"] is False
        assert payload["agent_pid"] is None

    @pytest.mark.asyncio
    async def test_stops_on_shutdown(self, connected_daemon):
        connected_daemon.request_shutdown()
        await asyncio.wait_for(connected_daemon.heartbeat_loop(), timeout=2)

    @pytest.mark.asyncio
    async def test_skips_publish_when_nats_disconnected(self, connected_daemon):
        connected_daemon.nc.is_connected = False

        async def stop():
            await asyncio.sleep(0.01)
            connected_daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "read_agent_pid", return_value=100),
            patch.object(daemon_mod, "get_system_metrics", return_value={}),
        ):
            await asyncio.gather(connected_daemon.heartbeat_loop(), stop())
        connected_daemon.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_exception_in_heartbeat(self, connected_daemon):
        calls = 0

        def read_pid():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            return 123

        async def stop():
            while calls < 2:
                await asyncio.sleep(0)
            connected_daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=read_pid),
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
            await asyncio.gather(connected_daemon.heartbeat_loop(), stop())
        assert connected_daemon.nc.publish.called


class TestAgentMonitorLoop:
    @staticmethod
    def _pid_sequence(running_polls=1):
        calls = 0

        def sequence():
            nonlocal calls
            calls += 1
            return 9999 if calls <= running_polls else None

        return sequence

    @staticmethod
    async def _stop_after_publish(daemon):
        while not daemon.nc.publish.called:
            await asyncio.sleep(0)
        daemon.request_shutdown()

    @pytest.mark.asyncio
    async def test_reports_completed_on_exit_code_zero(
        self, connected_daemon, tmp_path
    ):
        exit_file = tmp_path / "agent.exit_code"
        exit_file.write_text("0\n")
        with (
            patch.object(
                daemon_mod, "read_agent_pid", side_effect=self._pid_sequence(2)
            ),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_file),
        ):
            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                self._stop_after_publish(connected_daemon),
            )
        payload = json.loads(connected_daemon.nc.publish.call_args.args[1])
        assert payload == {
            "job_id": "test-job-001",
            "status": "completed",
            "exit_code": 0,
        }

    @pytest.mark.asyncio
    async def test_reports_failed_on_nonzero_exit_code(
        self, connected_daemon, tmp_path
    ):
        exit_file = tmp_path / "agent.exit_code"
        exit_file.write_text("2\n")
        with (
            patch.object(
                daemon_mod, "read_agent_pid", side_effect=self._pid_sequence()
            ),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_file),
        ):
            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                self._stop_after_publish(connected_daemon),
            )
        payload = json.loads(connected_daemon.nc.publish.call_args.args[1])
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 2

    @pytest.mark.asyncio
    async def test_defaults_to_failure_when_no_exit_code_file(
        self, connected_daemon, tmp_path
    ):
        with (
            patch.object(
                daemon_mod, "read_agent_pid", side_effect=self._pid_sequence()
            ),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", tmp_path / "missing"),
        ):
            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                self._stop_after_publish(connected_daemon),
            )
        payload = json.loads(connected_daemon.nc.publish.call_args.args[1])
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_reports_exit_only_once(self, connected_daemon, tmp_path):
        exit_file = tmp_path / "agent.exit_code"
        exit_file.write_text("0\n")
        calls = 0

        def sequence():
            nonlocal calls
            calls += 1
            return 7777 if calls == 1 else None

        async def stop():
            while calls < 4:
                await asyncio.sleep(0)
            connected_daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_file),
        ):
            await asyncio.gather(connected_daemon.agent_monitor_loop(), stop())
        assert connected_daemon.nc.publish.call_count == 1
        assert connected_daemon._agent_exit_reported is True

    @pytest.mark.asyncio
    async def test_no_report_when_agent_never_started(self, connected_daemon):
        calls = 0

        def never_started():
            nonlocal calls
            calls += 1
            return None

        async def stop():
            while calls < 3:
                await asyncio.sleep(0)
            connected_daemon.request_shutdown()

        with patch.object(daemon_mod, "read_agent_pid", side_effect=never_started):
            await asyncio.gather(connected_daemon.agent_monitor_loop(), stop())
        connected_daemon.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_publish_when_nats_disconnected(
        self, connected_daemon, tmp_path
    ):
        connected_daemon.nc.is_connected = False
        exit_file = tmp_path / "agent.exit_code"
        exit_file.write_text("0\n")
        calls = 0

        def sequence():
            nonlocal calls
            calls += 1
            return 6666 if calls == 1 else None

        async def stop():
            while calls < 3:
                await asyncio.sleep(0)
            connected_daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "read_agent_pid", side_effect=sequence),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_file),
        ):
            await asyncio.gather(connected_daemon.agent_monitor_loop(), stop())
        connected_daemon.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_correct_nats_subject(self, connected_daemon, tmp_path):
        exit_file = tmp_path / "agent.exit_code"
        exit_file.write_text("0\n")
        with (
            patch.object(
                daemon_mod, "read_agent_pid", side_effect=self._pid_sequence()
            ),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_file),
        ):
            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                self._stop_after_publish(connected_daemon),
            )
        assert (
            connected_daemon.nc.publish.call_args.args[0]
            == "agent.vm.test-orch-001.test-job-001.status"
        )

    @pytest.mark.asyncio
    async def test_handles_malformed_exit_code_file(self, connected_daemon, tmp_path):
        exit_file = tmp_path / "agent.exit_code"
        exit_file.write_text("crash!\n")
        with (
            patch.object(
                daemon_mod, "read_agent_pid", side_effect=self._pid_sequence()
            ),
            patch.object(daemon_mod, "AGENT_EXIT_CODE_FILE", exit_file),
        ):
            await asyncio.gather(
                connected_daemon.agent_monitor_loop(),
                self._stop_after_publish(connected_daemon),
            )
        payload = json.loads(connected_daemon.nc.publish.call_args.args[1])
        assert payload["exit_code"] == 1
        assert payload["status"] == "failed"


class TestIpUpdateLoop:
    @pytest.mark.asyncio
    async def test_re_registers_when_ip_changes(self, connected_daemon):
        connected_daemon._registered_ip = "10.0.0.1"
        connected_daemon._registered_ssh_ready = True
        with (
            patch.object(daemon_mod, "detect_ip", return_value="100.64.1.50"),
            patch.object(daemon_mod, "check_ssh_ready", return_value=True),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as register,
        ):

            async def stop():
                while not register.called:
                    await asyncio.sleep(0)
                connected_daemon.request_shutdown()

            await asyncio.gather(connected_daemon.ip_update_loop(), stop())
        register.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_re_registers_when_ssh_readiness_flips(self, connected_daemon):
        connected_daemon._registered_ip = "100.64.1.50"
        connected_daemon._registered_ssh_ready = False
        with (
            patch.object(daemon_mod, "detect_ip", return_value="100.64.1.50"),
            patch.object(daemon_mod, "check_ssh_ready", return_value=True),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as register,
        ):

            async def stop():
                while not register.called:
                    await asyncio.sleep(0)
                connected_daemon.request_shutdown()

            await asyncio.gather(connected_daemon.ip_update_loop(), stop())
        register.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_re_register_when_ip_unchanged(self, connected_daemon):
        connected_daemon._registered_ip = "10.0.0.1"
        connected_daemon._registered_ssh_ready = False
        checks = 0

        def same_ip():
            nonlocal checks
            checks += 1
            return "10.0.0.1"

        with (
            patch.object(daemon_mod, "detect_ip", side_effect=same_ip),
            patch.object(daemon_mod, "check_ssh_ready", return_value=False),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as register,
        ):

            async def stop():
                while checks < 3:
                    await asyncio.sleep(0)
                connected_daemon.request_shutdown()

            await asyncio.gather(connected_daemon.ip_update_loop(), stop())
        register.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stops_on_shutdown(self, connected_daemon):
        connected_daemon.request_shutdown()
        with patch.object(daemon_mod, "detect_ip") as detect:
            await asyncio.wait_for(connected_daemon.ip_update_loop(), timeout=2)
        detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_detect_ip_exception(self, connected_daemon):
        connected_daemon._registered_ip = "10.0.0.1"
        connected_daemon._registered_ssh_ready = False
        calls = 0

        def detect():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("network flake")
            return "10.0.0.1"

        with (
            patch.object(daemon_mod, "detect_ip", side_effect=detect),
            patch.object(daemon_mod, "check_ssh_ready", return_value=False),
            patch.object(
                connected_daemon, "register", new_callable=AsyncMock
            ) as register,
        ):

            async def stop():
                while calls < 2:
                    await asyncio.sleep(0)
                connected_daemon.request_shutdown()

            await asyncio.gather(connected_daemon.ip_update_loop(), stop())
        register.assert_not_awaited()


class TestWaitForCloudInit:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_file_exists(self, daemon, tmp_path):
        config = tmp_path / "job-config.json"
        config.write_text("{}")
        with patch.object(daemon_mod, "JOB_CONFIG_FILE", config):
            await daemon._wait_for_cloud_init(timeout=5)

    @pytest.mark.asyncio
    async def test_times_out_when_file_never_appears(self, daemon, tmp_path):
        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await daemon._wait_for_cloud_init(timeout=3)

    @pytest.mark.asyncio
    async def test_waits_until_file_appears(self, daemon, tmp_path):
        config = tmp_path / "job-config.json"
        checks = 0

        def exists():
            nonlocal checks
            checks += 1
            return checks >= 3

        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config),
            patch.object(Path, "exists", side_effect=exists),
            patch.object(Path, "stat", return_value=MagicMock(st_size=2)),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await daemon._wait_for_cloud_init(timeout=10)
        assert checks == 3

    @pytest.mark.asyncio
    async def test_exits_early_on_shutdown(self, daemon, tmp_path):
        async def stop(_delay):
            daemon.request_shutdown()

        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", tmp_path / "missing"),
            patch("asyncio.sleep", side_effect=stop),
        ):
            await daemon._wait_for_cloud_init(timeout=60)
        assert daemon._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_ignores_empty_file(self, daemon, tmp_path):
        config = tmp_path / "job-config.json"
        config.write_text("")
        with (
            patch.object(daemon_mod, "JOB_CONFIG_FILE", config),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await daemon._wait_for_cloud_init(timeout=3)


class TestWaitForTailscale:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_tailscale_ready(self, daemon):
        result = MagicMock(returncode=0, stdout="100.64.2.3\n")
        with patch("subprocess.run", return_value=result):
            await daemon._wait_for_tailscale()

    @pytest.mark.asyncio
    async def test_returns_when_tailscale_not_installed(self, daemon):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            await daemon._wait_for_tailscale()

    @pytest.mark.asyncio
    async def test_polls_until_tailscale_connects(self, daemon):
        calls = 0

        def tailscale(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="100.64.5.6\n")

        with (
            patch("subprocess.run", side_effect=tailscale),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await daemon._wait_for_tailscale()
        assert calls == 3

    @pytest.mark.asyncio
    async def test_times_out_when_tailscale_never_connects(self, daemon):
        with (
            patch.object(daemon_mod, "TAILSCALE_WAIT_TIMEOUT", 3),
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await daemon._wait_for_tailscale()

    @pytest.mark.asyncio
    async def test_exits_early_on_shutdown(self, daemon):
        async def stop(_delay):
            daemon.request_shutdown()

        with (
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
            patch("asyncio.sleep", side_effect=stop),
        ):
            await daemon._wait_for_tailscale()
        assert daemon._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_handles_subprocess_exception(self, daemon):
        calls = 0

        def tailscale(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("broken pipe")
            return MagicMock(returncode=0, stdout="100.64.9.1\n")

        with (
            patch("subprocess.run", side_effect=tailscale),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await daemon._wait_for_tailscale()
        assert calls == 2


class TestDaemonRun:
    @pytest.mark.asyncio
    async def test_run_refuses_legacy_nats_before_lifecycle_methods(self, daemon):
        mock_nc = AsyncMock(is_connected=True)
        mock_nc.subscribe = AsyncMock()
        mock_nc.drain = AsyncMock()

        with (
            patch.object(
                daemon, "_wait_for_cloud_init", new_callable=AsyncMock
            ) as mock_cloud,
            patch.object(
                daemon, "_wait_for_tailscale", new_callable=AsyncMock
            ) as mock_tailscale,
            patch.object(
                daemon, "connect_nats", new_callable=AsyncMock
            ) as mock_connect,
            patch.object(daemon, "register", new_callable=AsyncMock) as mock_register,
            patch.object(daemon, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(daemon, "agent_monitor_loop", new_callable=AsyncMock),
            patch.object(daemon, "ip_update_loop", new_callable=AsyncMock),
        ):

            async def connect():
                daemon.nc = mock_nc

            mock_connect.side_effect = connect

            async def shutdown():
                await asyncio.sleep(0.01)
                daemon.request_shutdown()

            await asyncio.gather(daemon.run(), shutdown())

        mock_cloud.assert_not_called()
        mock_tailscale.assert_not_called()
        mock_connect.assert_not_called()
        mock_register.assert_not_called()
        mock_nc.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_exits_early_when_shutdown_during_connect(self, daemon):
        with (
            patch.object(daemon, "_wait_for_cloud_init", new_callable=AsyncMock),
            patch.object(daemon, "_wait_for_tailscale", new_callable=AsyncMock),
            patch.object(
                daemon, "connect_nats", new_callable=AsyncMock
            ) as mock_connect,
            patch.object(daemon, "register", new_callable=AsyncMock) as mock_register,
        ):

            async def connect():
                daemon.request_shutdown()

            mock_connect.side_effect = connect
            await daemon.run()

        mock_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_never_connects_or_drains_legacy_nats(self, daemon):
        mock_nc = AsyncMock(is_connected=True)
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

            async def connect():
                daemon.nc = mock_nc

            mock_connect.side_effect = connect

            async def shutdown():
                await asyncio.sleep(0.01)
                daemon.request_shutdown()

            await asyncio.gather(daemon.run(), shutdown())

        mock_connect.assert_not_called()
        mock_nc.drain.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_never_subscribes_to_legacy_control_subject(self, daemon):
        mock_nc = AsyncMock(is_connected=True)
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

            async def connect():
                daemon.nc = mock_nc

            mock_connect.side_effect = connect

            async def shutdown():
                await asyncio.sleep(0.01)
                daemon.request_shutdown()

            await asyncio.gather(daemon.run(), shutdown())

        mock_connect.assert_not_awaited()
        mock_nc.subscribe.assert_not_awaited()


class TestRequestShutdown:
    def test_sets_shutdown_event(self, daemon):
        assert not daemon._shutdown.is_set()
        daemon.request_shutdown()
        assert daemon._shutdown.is_set()

    def test_idempotent(self, daemon):
        daemon.request_shutdown()
        daemon.request_shutdown()
        assert daemon._shutdown.is_set()


class TestNatsCallbacks:
    @pytest.mark.asyncio
    async def test_on_error_does_not_raise(self, daemon):
        await daemon._on_error(RuntimeError("test error"))

    @pytest.mark.asyncio
    async def test_on_disconnect_does_not_raise(self, daemon):
        await daemon._on_disconnect()

    @pytest.mark.asyncio
    async def test_on_reconnect_does_not_raise(self, daemon):
        await daemon._on_reconnect()


class TestMain:
    def test_main_registers_signal_handlers(self):
        registered_signals = {}

        def register_signal(sig, handler):
            registered_signals[sig] = handler

        with (
            patch.object(daemon_mod, "load_config", return_value=_make_config()),
            patch.object(
                ManagementDaemon,
                "run",
                new=MagicMock(return_value="daemon-run"),
            ),
            patch("signal.signal", side_effect=register_signal),
            patch("asyncio.run"),
        ):
            main()

        assert signal.SIGTERM in registered_signals
        assert signal.SIGINT in registered_signals

    def test_main_calls_asyncio_run(self):
        with (
            patch.object(daemon_mod, "load_config", return_value=_make_config()),
            patch.object(
                ManagementDaemon,
                "run",
                new=MagicMock(return_value="daemon-run"),
            ),
            patch("signal.signal"),
            patch("asyncio.run") as mock_run,
        ):
            main()

        mock_run.assert_called_once_with("daemon-run")

    def test_signal_handler_triggers_shutdown(self):
        handlers = {}

        def register_signal(sig, handler):
            handlers[sig] = handler

        with (
            patch.object(daemon_mod, "load_config", return_value=_make_config()),
            patch.object(
                ManagementDaemon,
                "run",
                new=MagicMock(return_value="daemon-run"),
            ),
            patch("signal.signal", side_effect=register_signal),
            patch("asyncio.run"),
        ):
            main()

        handlers[signal.SIGTERM](signal.SIGTERM, None)

    def test_main_creates_daemon_with_config(self):
        config = _make_config(extra="extra-value")
        created_daemons = []
        original_init = ManagementDaemon.__init__

        def track_init(instance, daemon_config):
            original_init(instance, daemon_config)
            created_daemons.append(instance)

        with (
            patch.object(daemon_mod, "load_config", return_value=config),
            patch.object(ManagementDaemon, "__init__", track_init),
            patch.object(
                ManagementDaemon,
                "run",
                new=MagicMock(return_value="daemon-run"),
            ),
            patch("signal.signal"),
            patch("asyncio.run"),
        ):
            main()

        assert len(created_daemons) == 1
        assert created_daemons[0].config is config


class TestConstants:
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
