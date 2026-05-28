"""Tests for the VM Controller — KubeVirt VM Lifecycle Manager.

Tests cover:
1. load_template() — template file loading, missing file handling
2. render_template() — variable substitution (JOB_ID, CPU_CORES, MEMORY, etc.)
3. init_k8s() — Kubernetes client initialization
4. connect_nats() — NATS connection and subscription setup
5. handle_create() — VM creation flow including Headscale key, template rendering, K8s API
6. handle_delete() — VM deletion including Headscale node cleanup
7. handle_status_query() — status query with request/reply
8. _publish_status() — status message format
9. run() and request_shutdown() — lifecycle management
10. Error handling — K8s API errors, NATS errors, Headscale failures, invalid requests
11. Edge cases — duplicate create requests, deleting non-existent VM, malformed messages
"""

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Project root on sys.path (conftest.py also does this, belt-and-suspenders)
# ---------------------------------------------------------------------------
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# ---------------------------------------------------------------------------
# Mock external dependencies that are unavailable in the test environment.
#
# The controller module (vm/controller/controller.py) does:
#   from headscale_client import HeadscaleClient
# This only works inside the vm/controller/ directory.  We inject a mock
# module into sys.modules BEFORE importing the controller so the import
# succeeds.  Similarly we pre-seed kubernetes and nats stubs.
# ---------------------------------------------------------------------------

# --- headscale_client -------------------------------------------------------
_mock_headscale_module = types.ModuleType("headscale_client")
_MockHeadscaleClient = MagicMock
_mock_headscale_module.HeadscaleClient = _MockHeadscaleClient  # type: ignore[attr-defined]
sys.modules.setdefault("headscale_client", _mock_headscale_module)

# --- kubernetes (only needed at import-time for type hints / constants) ------
_mock_k8s = types.ModuleType("kubernetes")
_mock_k8s_client = types.ModuleType("kubernetes.client")
_mock_k8s_config = types.ModuleType("kubernetes.config")
_mock_k8s_exc = types.ModuleType("kubernetes.client.exceptions")


class _FakeApiException(Exception):
    """Stand-in for kubernetes.client.exceptions.ApiException."""

    def __init__(self, status=500, body=""):
        self.status = status
        self.body = body
        super().__init__(f"{status}: {body}")


_mock_k8s_exc.ApiException = _FakeApiException  # type: ignore[attr-defined]
_mock_k8s_client.exceptions = _mock_k8s_exc  # type: ignore[attr-defined]
_mock_k8s_client.CustomObjectsApi = MagicMock  # type: ignore[attr-defined]
_mock_k8s.client = _mock_k8s_client  # type: ignore[attr-defined]
_mock_k8s.config = _mock_k8s_config  # type: ignore[attr-defined]
_mock_k8s_config.load_incluster_config = MagicMock()  # type: ignore[attr-defined]
sys.modules["kubernetes"] = _mock_k8s
sys.modules["kubernetes.client"] = _mock_k8s_client
sys.modules["kubernetes.config"] = _mock_k8s_config
sys.modules["kubernetes.client.exceptions"] = _mock_k8s_exc

# --- nats -------------------------------------------------------------------
_mock_nats = types.ModuleType("nats")
_mock_nats.connect = AsyncMock()  # type: ignore[attr-defined]
sys.modules.setdefault("nats", _mock_nats)

# ---------------------------------------------------------------------------
# NOW import the controller — the mocked modules make this succeed
# ---------------------------------------------------------------------------
from vm.controller.controller import (  # noqa: E402
    KUBEVIRT_GROUP,
    KUBEVIRT_PLURAL,
    KUBEVIRT_VERSION,
    VMController,
)


# =============================================================================
# Sample VM template used across tests
# =============================================================================

SAMPLE_TEMPLATE = """\
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: agent-vm-${JOB_ID}
  labels:
    job-id: ${JOB_ID}
spec:
  dataVolumeTemplates:
    - metadata:
        name: agent-vm-${JOB_ID}-rootdisk
      spec:
        storage:
          accessModes:
            - ReadWriteOnce
          storageClassName: ${VM_STORAGE_CLASS}
          resources:
            requests:
              storage: ${VM_DISK_SIZE}
        source:
          registry:
            url: docker://${VM_IMAGE}
  template:
    spec:
      domain:
        cpu:
          cores: ${CPU_CORES}
        memory:
          guest: ${MEMORY}
      volumes:
        - name: rootdisk
          dataVolume:
            name: agent-vm-${JOB_ID}-rootdisk
        - name: cloud-init
          cloudInitNoCloud:
            userData: |
              NATS_URL=${NATS_URL}
              JOB_ID=${JOB_ID}
              AGENT_CONFIG=${AGENT_CONFIG}
              DESCRIPTION=${DESCRIPTION}
              TAILSCALE_AUTH_KEY=${TAILSCALE_AUTH_KEY}
              HEADSCALE_URL=${HEADSCALE_URL}
"""

SAMPLE_JOB_CONFIG = {
    "job_id": "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb",
    "agent_config": "developer",
    "vm_image": "ghcr.io/example/agent-vm:latest",
    "cpu_cores": 4,
    "memory": "8Gi",
    "nats_url": "nats://orchestrator-nats:4222",
    "description": "Build the feature module",
}


# =============================================================================
# Helpers
# =============================================================================


def make_nats_msg(data: dict, reply: str | None = None) -> MagicMock:
    """Create a mock NATS message with JSON-encoded data."""
    msg = MagicMock()
    msg.data = json.dumps(data).encode()
    msg.reply = reply
    return msg


def make_nats_msg_raw(raw_bytes: bytes, reply: str | None = None) -> MagicMock:
    """Create a mock NATS message with raw bytes (for malformed message tests)."""
    msg = MagicMock()
    msg.data = raw_bytes
    msg.reply = reply
    return msg


# =============================================================================
# Fixtures
# =============================================================================


def _make_headscale_mock(available: bool = True):
    hs = MagicMock()
    hs.is_available = available
    hs.init = AsyncMock()
    hs.close = AsyncMock()
    if available:
        hs.create_auth_key = AsyncMock(return_value="hskey-preauth-abc123")
        hs.delete_node = AsyncMock(return_value=True)
    else:
        hs.create_auth_key = AsyncMock(return_value=None)
        hs.delete_node = AsyncMock(return_value=False)
    return hs


def _make_controller(headscale_available: bool = True) -> VMController:
    """Build a VMController with fully mocked I/O dependencies."""
    ctrl = VMController.__new__(VMController)
    ctrl.headscale = _make_headscale_mock(headscale_available)
    ctrl.template_text = SAMPLE_TEMPLATE
    ctrl._shutdown = asyncio.Event()

    # Mock NATS client
    ctrl.nc = AsyncMock()
    ctrl.nc.is_connected = True
    ctrl.nc.publish = AsyncMock()
    ctrl.nc.subscribe = AsyncMock()
    ctrl.nc.drain = AsyncMock()

    # Mock K8s client
    ctrl.k8s_client = MagicMock()

    ctrl.http_runner = None

    return ctrl


@pytest.fixture(autouse=True)
def _scoped_orchestrator_id():
    """Patch the module-level ORCHESTRATOR_ID for every test in this file.

    The controller reads ORCHESTRATOR_ID at module import time and uses it
    to scope vm.lifecycle.* subjects. Real deployments set the env var via
    Helm; tests use this fixture so existing assertions on subject names
    keep working with a stable "test-oid" suffix. The dedicated
    TestOrchestratorIdRequired class below opts out by patching to "".
    """
    with patch("vm.controller.controller.ORCHESTRATOR_ID", "test-oid"):
        yield


@pytest.fixture
def controller():
    return _make_controller(headscale_available=True)


@pytest.fixture
def controller_no_headscale():
    return _make_controller(headscale_available=False)


# =============================================================================
# Tests: load_template()
# =============================================================================


class TestLoadTemplate:
    """Tests for VM template file loading."""

    def test_load_template_success(self, tmp_path):
        """Loading a valid template file stores its content."""
        template_path = tmp_path / "vm-template.yaml"
        template_path.write_text(SAMPLE_TEMPLATE)

        ctrl = _make_controller()
        with patch("vm.controller.controller.VM_TEMPLATE_PATH", str(template_path)):
            ctrl.load_template()

        assert ctrl.template_text == SAMPLE_TEMPLATE
        assert "${JOB_ID}" in ctrl.template_text

    def test_load_template_missing_file(self, tmp_path):
        """Loading a non-existent template calls sys.exit(1)."""
        ctrl = _make_controller()
        with patch(
            "vm.controller.controller.VM_TEMPLATE_PATH",
            str(tmp_path / "nonexistent.yaml"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                ctrl.load_template()
            assert exc_info.value.code == 1

    def test_load_template_empty_file(self, tmp_path):
        """Loading an empty template file stores an empty string."""
        template_path = tmp_path / "vm-template.yaml"
        template_path.write_text("")

        ctrl = _make_controller()
        with patch("vm.controller.controller.VM_TEMPLATE_PATH", str(template_path)):
            ctrl.load_template()

        assert ctrl.template_text == ""

    def test_load_template_preserves_multiline(self, tmp_path):
        """Template loading preserves multi-line YAML content."""
        content = "line1: value1\nline2:\n  nested: true\n  list:\n    - item1\n"
        template_path = tmp_path / "vm-template.yaml"
        template_path.write_text(content)

        ctrl = _make_controller()
        with patch("vm.controller.controller.VM_TEMPLATE_PATH", str(template_path)):
            ctrl.load_template()

        assert ctrl.template_text == content


# =============================================================================
# Tests: render_template()
# =============================================================================


class TestRenderTemplate:
    """Tests for VM template variable substitution."""

    def test_render_all_placeholders(self, controller):
        """All placeholders are substituted with job config values."""
        result = controller.render_template(SAMPLE_JOB_CONFIG, "ts-key-123")

        assert result["metadata"]["name"] == f"agent-vm-{SAMPLE_JOB_CONFIG['job_id']}"
        assert result["metadata"]["labels"]["job-id"] == SAMPLE_JOB_CONFIG["job_id"]

        spec = result["spec"]["template"]["spec"]
        assert spec["domain"]["cpu"]["cores"] == 4
        assert spec["domain"]["memory"]["guest"] == "8Gi"
        assert (
            result["spec"]["dataVolumeTemplates"][0]["spec"]["source"]["registry"][
                "url"
            ]
            == f"docker://{SAMPLE_JOB_CONFIG['vm_image']}"
        )

        user_data = spec["volumes"][1]["cloudInitNoCloud"]["userData"]
        assert SAMPLE_JOB_CONFIG["job_id"] in user_data
        assert "developer" in user_data
        assert "Build the feature module" in user_data
        assert "ts-key-123" in user_data

    def test_render_uses_defaults_for_missing_keys(self, controller):
        """Missing optional keys fall back to module-level defaults."""
        minimal_config = {"job_id": "minimal-job-id"}

        with (
            patch("vm.controller.controller.DEFAULT_VM_IMAGE", "default-image:v1"),
            patch("vm.controller.controller.DEFAULT_CPU", 2),
            patch("vm.controller.controller.DEFAULT_MEMORY", "4Gi"),
        ):
            result = controller.render_template(minimal_config)

        assert result["metadata"]["name"] == "agent-vm-minimal-job-id"
        spec = result["spec"]["template"]["spec"]
        assert spec["domain"]["cpu"]["cores"] == 2
        assert spec["domain"]["memory"]["guest"] == "4Gi"
        assert (
            result["spec"]["dataVolumeTemplates"][0]["spec"]["source"]["registry"][
                "url"
            ]
            == "docker://default-image:v1"
        )

    def test_render_default_agent_config(self, controller):
        """agent_config defaults to 'defaults' when not specified."""
        config = {"job_id": "test-id"}
        result = controller.render_template(config)

        user_data = result["spec"]["template"]["spec"]["volumes"][1][
            "cloudInitNoCloud"
        ]["userData"]
        assert "AGENT_CONFIG=defaults" in user_data

    def test_render_empty_tailscale_key(self, controller):
        """Empty tailscale auth key results in empty placeholder."""
        config = {"job_id": "test-id"}
        result = controller.render_template(config, tailscale_auth_key="")

        user_data = result["spec"]["template"]["spec"]["volumes"][1][
            "cloudInitNoCloud"
        ]["userData"]
        assert "TAILSCALE_AUTH_KEY=\n" in user_data

    def test_render_injects_headscale_url_from_env(self, controller):
        """HEADSCALE_URL is read from environment and injected."""
        config = {"job_id": "test-id"}

        with patch.dict("os.environ", {"HEADSCALE_URL": "https://hs.example.com"}):
            result = controller.render_template(config, "key-abc")

        user_data = result["spec"]["template"]["spec"]["volumes"][1][
            "cloudInitNoCloud"
        ]["userData"]
        assert "HEADSCALE_URL=https://hs.example.com" in user_data

    def test_render_uses_local_nats_url(self, controller):
        """NATS_URL is always the local leaf node, not the one from job config."""
        config = {
            "job_id": "test-id",
            "nats_url": "nats://remote-orchestrator:4222",
        }

        with patch("vm.controller.controller.NATS_URL", "nats://local-leaf:4222"):
            result = controller.render_template(config)

        user_data = result["spec"]["template"]["spec"]["volumes"][1][
            "cloudInitNoCloud"
        ]["userData"]
        assert "NATS_URL=nats://local-leaf:4222" in user_data
        assert "nats://remote-orchestrator:4222" not in user_data

    def test_render_empty_description(self, controller):
        """Empty description is substituted as empty string."""
        config = {"job_id": "test-id"}
        result = controller.render_template(config)

        user_data = result["spec"]["template"]["spec"]["volumes"][1][
            "cloudInitNoCloud"
        ]["userData"]
        assert "DESCRIPTION=\n" in user_data

    def test_render_description_with_special_characters(self, controller):
        """Description with special characters is substituted verbatim."""
        config = {
            "job_id": "test-id",
            "description": "Parse $HOME & /tmp files; echo 'hello'",
        }
        result = controller.render_template(config)

        user_data = result["spec"]["template"]["spec"]["volumes"][1][
            "cloudInitNoCloud"
        ]["userData"]
        assert "Parse $HOME & /tmp files; echo 'hello'" in user_data

    def test_render_returns_valid_yaml_dict(self, controller):
        """render_template returns a dict (parsed YAML), not a string."""
        result = controller.render_template(SAMPLE_JOB_CONFIG)
        assert isinstance(result, dict)
        assert "apiVersion" in result
        assert result["kind"] == "VirtualMachine"

    def test_render_cpu_cores_as_integer(self, controller):
        """CPU cores are rendered as an integer, not a string."""
        config = {"job_id": "test-id", "cpu_cores": 8}
        result = controller.render_template(config)

        cores = result["spec"]["template"]["spec"]["domain"]["cpu"]["cores"]
        assert isinstance(cores, int)
        assert cores == 8


# =============================================================================
# Tests: init_k8s()
# =============================================================================


class TestInitK8s:
    """Tests for Kubernetes client initialization."""

    def test_init_k8s_loads_in_cluster_config(self):
        """init_k8s calls load_incluster_config."""
        ctrl = _make_controller()
        ctrl.k8s_client = None

        mock_config = MagicMock()
        mock_client = MagicMock()
        mock_api = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_api

        with patch.dict(
            "sys.modules",
            {
                "kubernetes": MagicMock(client=mock_client, config=mock_config),
                "kubernetes.client": mock_client,
                "kubernetes.config": mock_config,
            },
        ):
            ctrl.init_k8s()

        mock_config.load_incluster_config.assert_called_once()

    def test_init_k8s_sets_custom_objects_api(self):
        """After init_k8s, k8s_client is a CustomObjectsApi instance."""
        ctrl = _make_controller()
        ctrl.k8s_client = None

        mock_api = MagicMock()
        mock_client = MagicMock()
        mock_client.CustomObjectsApi.return_value = mock_api

        with patch.dict(
            "sys.modules",
            {
                "kubernetes": MagicMock(client=mock_client, config=MagicMock()),
                "kubernetes.client": mock_client,
                "kubernetes.config": MagicMock(),
            },
        ):
            ctrl.init_k8s()

        assert ctrl.k8s_client is mock_api


# =============================================================================
# Tests: connect_nats()
# =============================================================================


class TestConnectNats:
    """Tests for NATS connection setup."""

    @pytest.mark.asyncio
    async def test_connect_nats_success(self):
        """connect_nats establishes a NATS connection."""
        ctrl = _make_controller()
        ctrl.nc = None

        mock_nc = AsyncMock()
        mock_nats_mod = MagicMock()
        mock_nats_mod.connect = AsyncMock(return_value=mock_nc)

        with patch.dict("sys.modules", {"nats": mock_nats_mod}):
            await ctrl.connect_nats()

        assert ctrl.nc is mock_nc
        mock_nats_mod.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_nats_uses_configured_url(self):
        """connect_nats passes the module-level NATS_URL."""
        ctrl = _make_controller()
        ctrl.nc = None

        mock_nats_mod = MagicMock()
        mock_nats_mod.connect = AsyncMock(return_value=AsyncMock())

        with (
            patch("vm.controller.controller.NATS_URL", "nats://custom:4222"),
            patch.dict("sys.modules", {"nats": mock_nats_mod}),
        ):
            await ctrl.connect_nats()

        first_positional = mock_nats_mod.connect.call_args[0][0]
        assert first_positional == "nats://custom:4222"

    @pytest.mark.asyncio
    async def test_connect_nats_infinite_reconnect(self):
        """connect_nats configures infinite reconnect attempts (-1)."""
        ctrl = _make_controller()
        ctrl.nc = None

        mock_nats_mod = MagicMock()
        mock_nats_mod.connect = AsyncMock(return_value=AsyncMock())

        with patch.dict("sys.modules", {"nats": mock_nats_mod}):
            await ctrl.connect_nats()

        kw = mock_nats_mod.connect.call_args[1]
        assert kw["max_reconnect_attempts"] == -1

    @pytest.mark.asyncio
    async def test_connect_nats_error_propagates(self):
        """Connection failure propagates the exception."""
        ctrl = _make_controller()
        ctrl.nc = None

        mock_nats_mod = MagicMock()
        mock_nats_mod.connect = AsyncMock(side_effect=ConnectionError("refused"))

        with patch.dict("sys.modules", {"nats": mock_nats_mod}):
            with pytest.raises(ConnectionError, match="refused"):
                await ctrl.connect_nats()


# =============================================================================
# Tests: handle_create()
# =============================================================================


class TestHandleCreate:
    """Tests for VM creation handler."""

    @pytest.mark.asyncio
    async def test_create_vm_success(self, controller):
        """Successful VM creation calls K8s API and publishes 'created' status."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller.handle_create(msg)

        controller.k8s_client.create_namespaced_custom_object.assert_called_once()
        kw = controller.k8s_client.create_namespaced_custom_object.call_args[1]
        assert kw["group"] == "kubevirt.io"
        assert kw["version"] == "v1"
        assert kw["plural"] == "virtualmachines"

        # Verify status published
        controller.nc.publish.assert_awaited()
        subject, raw = controller.nc.publish.call_args[0]
        assert subject == "vm.lifecycle.status.test-oid"
        payload = json.loads(raw.decode())
        assert payload["status"] == "created"
        assert payload["job_id"] == SAMPLE_JOB_CONFIG["job_id"]

    @pytest.mark.asyncio
    async def test_create_vm_generates_headscale_key(self, controller):
        """VM creation generates a Headscale pre-auth key when available."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller.handle_create(msg)

        controller.headscale.create_auth_key.assert_awaited_once_with(
            SAMPLE_JOB_CONFIG["job_id"]
        )

    @pytest.mark.asyncio
    async def test_create_vm_without_headscale(self, controller_no_headscale):
        """VM creation proceeds without Headscale when unavailable."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller_no_headscale.handle_create(msg)

        controller_no_headscale.headscale.create_auth_key.assert_not_awaited()
        controller_no_headscale.k8s_client.create_namespaced_custom_object.assert_called_once()

        payload = json.loads(
            controller_no_headscale.nc.publish.call_args[0][1].decode()
        )
        assert payload["status"] == "created"

    @pytest.mark.asyncio
    async def test_create_vm_headscale_key_failure(self, controller):
        """VM creation continues when Headscale key generation returns None."""
        controller.headscale.create_auth_key = AsyncMock(return_value=None)
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller.handle_create(msg)

        controller.k8s_client.create_namespaced_custom_object.assert_called_once()
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "created"

    @pytest.mark.asyncio
    async def test_create_vm_renders_manifest_correctly(self, controller):
        """Created VM manifest contains the correct job_id in its name."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller.handle_create(msg)

        kw = controller.k8s_client.create_namespaced_custom_object.call_args[1]
        assert (
            kw["body"]["metadata"]["name"] == f"agent-vm-{SAMPLE_JOB_CONFIG['job_id']}"
        )

    @pytest.mark.asyncio
    async def test_create_vm_uses_correct_namespace(self, controller):
        """VM is created in the configured namespace."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        with patch("vm.controller.controller.VM_NAMESPACE", "test-namespace"):
            await controller.handle_create(msg)

        kw = controller.k8s_client.create_namespaced_custom_object.call_args[1]
        assert kw["namespace"] == "test-namespace"

    @pytest.mark.asyncio
    async def test_create_vm_k8s_api_error(self, controller):
        """K8s API error during creation publishes 'failed' status."""
        controller.k8s_client.create_namespaced_custom_object.side_effect = (
            RuntimeError("Internal Server Error")
        )

        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller.handle_create(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"
        assert payload["job_id"] == SAMPLE_JOB_CONFIG["job_id"]
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_create_vm_conflict_retry_succeeds(self, controller):
        """409 Conflict with 'is being deleted' triggers retry, second call succeeds."""
        conflict = _FakeApiException(
            status=409, body="vm agent-vm-test is being deleted"
        )

        controller.k8s_client.create_namespaced_custom_object.side_effect = [
            conflict,
            MagicMock(),  # success on second attempt
        ]

        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await controller.handle_create(msg)

        assert controller.k8s_client.create_namespaced_custom_object.call_count == 2
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "created"

    @pytest.mark.asyncio
    async def test_create_vm_conflict_exhausted_retries(self, controller):
        """409 Conflict persisting after all retries publishes 'failed'."""

        def make_conflict():
            return _FakeApiException(
                status=409, body="vm agent-vm-test is being deleted"
            )

        # 13 calls = initial + 12 retries
        controller.k8s_client.create_namespaced_custom_object.side_effect = [
            make_conflict() for _ in range(14)
        ]

        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await controller.handle_create(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"

    @pytest.mark.asyncio
    async def test_create_vm_409_not_being_deleted_raises_immediately(self, controller):
        """409 without 'is being deleted' body raises immediately (no retry)."""
        conflict = _FakeApiException(status=409, body="already exists")

        controller.k8s_client.create_namespaced_custom_object.side_effect = conflict

        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await controller.handle_create(msg)

        # No sleep = no retry
        mock_sleep.assert_not_awaited()
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"

    @pytest.mark.asyncio
    async def test_create_vm_malformed_json(self, controller):
        """Malformed JSON in NATS message publishes 'failed' status."""
        msg = make_nats_msg_raw(b"not valid json")
        await controller.handle_create(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"
        assert payload["job_id"] == "unknown"

    @pytest.mark.asyncio
    async def test_create_vm_missing_job_id_key_fails(self, controller):
        """Missing job_id key in payload causes KeyError during render, publishes failure."""
        msg = make_nats_msg({"agent_config": "developer"})
        await controller.handle_create(msg)

        # render_template requires job_config["job_id"] — missing key fails
        controller.k8s_client.create_namespaced_custom_object.assert_not_called()
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"
        assert payload["job_id"] == "unknown"

    @pytest.mark.asyncio
    async def test_create_vm_status_includes_vm_name(self, controller):
        """Published status includes the vm_name derived from the manifest."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        await controller.handle_create(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["vm_name"] == f"agent-vm-{SAMPLE_JOB_CONFIG['job_id']}"

    @pytest.mark.asyncio
    async def test_create_vm_status_includes_namespace(self, controller):
        """Published status includes the VM namespace."""
        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        with patch("vm.controller.controller.VM_NAMESPACE", "custom-ns"):
            await controller.handle_create(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["namespace"] == "custom-ns"

    @pytest.mark.asyncio
    async def test_create_vm_minimal_config(self, controller):
        """VM creation works with only job_id in payload."""
        msg = make_nats_msg({"job_id": "only-job-id"})
        await controller.handle_create(msg)

        controller.k8s_client.create_namespaced_custom_object.assert_called_once()
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "created"
        assert payload["job_id"] == "only-job-id"

    @pytest.mark.asyncio
    async def test_create_vm_with_extra_fields_in_payload(self, controller):
        """Create handler ignores extra fields in the payload."""
        config = dict(SAMPLE_JOB_CONFIG)
        config["extra_field"] = "should be ignored"
        config["another_extra"] = 42

        msg = make_nats_msg(config)
        await controller.handle_create(msg)

        controller.k8s_client.create_namespaced_custom_object.assert_called_once()
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "created"


# =============================================================================
# Tests: handle_delete()
# =============================================================================


class TestHandleDelete:
    """Tests for VM deletion handler."""

    @pytest.mark.asyncio
    async def test_delete_vm_success(self, controller):
        """Successful VM deletion calls K8s API and publishes 'deleted' status."""
        job_id = "delete-me-1234"
        msg = make_nats_msg({"job_id": job_id})
        await controller.handle_delete(msg)

        kw = controller.k8s_client.delete_namespaced_custom_object.call_args[1]
        assert kw["name"] == f"agent-vm-{job_id}"
        assert kw["group"] == "kubevirt.io"
        assert kw["version"] == "v1"
        assert kw["plural"] == "virtualmachines"

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "deleted"
        assert payload["job_id"] == job_id
        assert payload["vm_name"] == f"agent-vm-{job_id}"

    @pytest.mark.asyncio
    async def test_delete_vm_cleans_headscale_node(self, controller):
        """VM deletion also removes the Headscale node."""
        job_id = "delete-me-5678"
        msg = make_nats_msg({"job_id": job_id})
        await controller.handle_delete(msg)

        controller.headscale.delete_node.assert_awaited_once_with(job_id)

    @pytest.mark.asyncio
    async def test_delete_vm_without_headscale(self, controller_no_headscale):
        """VM deletion skips Headscale cleanup when unavailable."""
        job_id = "delete-no-hs"
        msg = make_nats_msg({"job_id": job_id})
        await controller_no_headscale.handle_delete(msg)

        controller_no_headscale.headscale.delete_node.assert_not_awaited()
        controller_no_headscale.k8s_client.delete_namespaced_custom_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_vm_already_gone_404(self, controller):
        """Deleting a non-existent VM (404) is treated as success."""
        controller.k8s_client.delete_namespaced_custom_object.side_effect = (
            _FakeApiException(status=404, body="Not Found")
        )

        job_id = "already-gone"
        msg = make_nats_msg({"job_id": job_id})
        await controller.handle_delete(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "deleted"
        assert payload["job_id"] == job_id

    @pytest.mark.asyncio
    async def test_delete_vm_k8s_api_error_non_404(self, controller):
        """K8s API error (non-404) during deletion publishes 'delete_failed'."""
        controller.k8s_client.delete_namespaced_custom_object.side_effect = (
            _FakeApiException(status=503, body="Service Unavailable")
        )

        job_id = "fail-delete"
        msg = make_nats_msg({"job_id": job_id})
        await controller.handle_delete(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "delete_failed"
        assert payload["job_id"] == job_id
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_delete_vm_generic_exception(self, controller):
        """Generic exception during deletion publishes 'delete_failed'."""
        controller.k8s_client.delete_namespaced_custom_object.side_effect = (
            RuntimeError("unexpected failure")
        )

        msg = make_nats_msg({"job_id": "err-test"})
        await controller.handle_delete(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "delete_failed"

    @pytest.mark.asyncio
    async def test_delete_vm_malformed_json(self, controller):
        """Malformed JSON in delete message publishes 'delete_failed'."""
        msg = make_nats_msg_raw(b"{{invalid json")
        await controller.handle_delete(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "delete_failed"
        assert payload["job_id"] == "unknown"

    @pytest.mark.asyncio
    async def test_delete_vm_missing_job_id_key(self, controller):
        """Missing job_id key in delete payload publishes failure."""
        msg = make_nats_msg({"vm_name": "agent-vm-xyz"})
        await controller.handle_delete(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "delete_failed"

    @pytest.mark.asyncio
    async def test_delete_vm_uses_correct_namespace(self, controller):
        """VM deletion uses the configured namespace."""
        msg = make_nats_msg({"job_id": "ns-test"})
        with patch("vm.controller.controller.VM_NAMESPACE", "my-namespace"):
            await controller.handle_delete(msg)

        kw = controller.k8s_client.delete_namespaced_custom_object.call_args[1]
        assert kw["namespace"] == "my-namespace"

    @pytest.mark.asyncio
    async def test_delete_vm_headscale_failure_does_not_block(self, controller):
        """Headscale node deletion failure does not prevent VM deletion success."""
        controller.headscale.delete_node = AsyncMock(return_value=False)

        msg = make_nats_msg({"job_id": "hs-fail-ok"})
        await controller.handle_delete(msg)

        controller.k8s_client.delete_namespaced_custom_object.assert_called_once()
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_constructs_vm_name_from_job_id(self, controller):
        """Delete handler constructs vm_name as 'agent-vm-{job_id}'."""
        job_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        msg = make_nats_msg({"job_id": job_id})
        await controller.handle_delete(msg)

        kw = controller.k8s_client.delete_namespaced_custom_object.call_args[1]
        assert kw["name"] == f"agent-vm-{job_id}"


# =============================================================================
# Tests: handle_status_query()
# =============================================================================


class TestHandleStatusQuery:
    """Tests for VM status query handler."""

    @pytest.mark.asyncio
    async def test_status_query_ready_vm(self, controller):
        """Status query for a ready VM returns ready=True and correct phase."""
        job_id = "status-test"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "metadata": {"name": f"agent-vm-{job_id}"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True"},
                    {"type": "Initialized", "status": "True"},
                ],
                "printableStatus": "Running",
                "created": True,
            },
        }

        msg = make_nats_msg({"job_id": job_id}, reply="reply.inbox.123")
        await controller.handle_status_query(msg)

        subject, raw = controller.nc.publish.call_args[0]
        assert subject == "reply.inbox.123"
        payload = json.loads(raw.decode())
        assert payload["ready"] is True
        assert payload["phase"] == "Running"
        assert payload["created"] is True
        assert payload["job_id"] == job_id
        assert payload["vm_name"] == f"agent-vm-{job_id}"

    @pytest.mark.asyncio
    async def test_status_query_not_ready_vm(self, controller):
        """Status query for a non-ready VM returns ready=False."""
        job_id = "not-ready"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "metadata": {"name": f"agent-vm-{job_id}"},
            "status": {
                "conditions": [{"type": "Ready", "status": "False"}],
                "printableStatus": "Provisioning",
                "created": False,
            },
        }

        msg = make_nats_msg({"job_id": job_id}, reply="reply.inbox.456")
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["ready"] is False
        assert payload["phase"] == "Provisioning"
        assert payload["created"] is False

    @pytest.mark.asyncio
    async def test_status_query_no_conditions(self, controller):
        """Status query for a VM with no conditions returns ready=False."""
        job_id = "no-conditions"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "metadata": {"name": f"agent-vm-{job_id}"},
            "status": {"printableStatus": "Starting"},
        }

        msg = make_nats_msg({"job_id": job_id}, reply="reply.inbox.789")
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["ready"] is False
        assert payload["phase"] == "Starting"

    @pytest.mark.asyncio
    async def test_status_query_empty_status(self, controller):
        """Status query for a VM with no status block returns defaults."""
        job_id = "empty-status"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "metadata": {"name": f"agent-vm-{job_id}"},
        }

        msg = make_nats_msg({"job_id": job_id}, reply="reply.inbox.000")
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["ready"] is False
        assert payload["phase"] == "Unknown"
        assert payload["created"] is False

    @pytest.mark.asyncio
    async def test_status_query_no_reply_publishes_to_status_subject(self, controller):
        """When msg.reply is None, status is published to vm.lifecycle.status.{oid}."""
        job_id = "no-reply"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "metadata": {"name": f"agent-vm-{job_id}"},
            "status": {
                "conditions": [],
                "printableStatus": "Running",
                "created": True,
            },
        }

        msg = make_nats_msg({"job_id": job_id}, reply=None)
        await controller.handle_status_query(msg)

        subject = controller.nc.publish.call_args[0][0]
        assert subject == "vm.lifecycle.status.test-oid"

    @pytest.mark.asyncio
    async def test_status_query_vm_not_found_error(self, controller):
        """Status query for a non-existent VM returns error via reply."""
        controller.k8s_client.get_namespaced_custom_object.side_effect = Exception(
            "Not Found"
        )

        msg = make_nats_msg({"job_id": "missing"}, reply="reply.inbox.err")
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "query_failed"
        assert payload["job_id"] == "missing"
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_status_query_error_without_reply(self, controller):
        """Error without reply subject publishes to vm.lifecycle.status.{oid}."""
        controller.k8s_client.get_namespaced_custom_object.side_effect = Exception(
            "Server Error"
        )

        msg = make_nats_msg({"job_id": "error-no-reply"}, reply=None)
        await controller.handle_status_query(msg)

        subject = controller.nc.publish.call_args[0][0]
        assert subject == "vm.lifecycle.status.test-oid"
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "query_failed"

    @pytest.mark.asyncio
    async def test_status_query_error_with_reply(self, controller):
        """Error with reply subject sends error to the reply address."""
        controller.k8s_client.get_namespaced_custom_object.side_effect = Exception(
            "Timeout"
        )

        msg = make_nats_msg({"job_id": "error-reply"}, reply="reply.inbox.err2")
        await controller.handle_status_query(msg)

        subject = controller.nc.publish.call_args[0][0]
        assert subject == "reply.inbox.err2"
        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "query_failed"

    @pytest.mark.asyncio
    async def test_status_query_malformed_json(self, controller):
        """Malformed JSON in status query returns error."""
        msg = make_nats_msg_raw(b"not json", reply="reply.inbox.bad")
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "query_failed"

    @pytest.mark.asyncio
    async def test_status_query_multiple_conditions_ready_false(self, controller):
        """Ready is True only when type=Ready and status=True is present."""
        job_id = "multi-cond"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "metadata": {"name": f"agent-vm-{job_id}"},
            "status": {
                "conditions": [
                    {"type": "LiveMigratable", "status": "True"},
                    {"type": "Ready", "status": "False"},
                    {"type": "AgentConnected", "status": "True"},
                ],
                "printableStatus": "Starting",
            },
        }

        msg = make_nats_msg({"job_id": job_id}, reply="reply.inbox.multi")
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["ready"] is False

    @pytest.mark.asyncio
    async def test_status_query_uses_correct_k8s_coordinates(self, controller):
        """Status query uses KubeVirt group/version/plural."""
        job_id = "coords-test"
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "status": {"conditions": [], "printableStatus": "Running"},
        }

        msg = make_nats_msg({"job_id": job_id}, reply="reply.test")
        await controller.handle_status_query(msg)

        kw = controller.k8s_client.get_namespaced_custom_object.call_args[1]
        assert kw["group"] == "kubevirt.io"
        assert kw["version"] == "v1"
        assert kw["plural"] == "virtualmachines"
        assert kw["name"] == f"agent-vm-{job_id}"

    @pytest.mark.asyncio
    async def test_status_query_with_extra_fields(self, controller):
        """Status query ignores extra fields in the payload."""
        controller.k8s_client.get_namespaced_custom_object.return_value = {
            "status": {"conditions": [], "printableStatus": "Running"},
        }

        msg = make_nats_msg(
            {"job_id": "extra-fields", "extra": "ignored"},
            reply="reply.test",
        )
        await controller.handle_status_query(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["job_id"] == "extra-fields"


# =============================================================================
# Tests: _publish_status()
# =============================================================================


class TestPublishStatus:
    """Tests for status publishing."""

    @pytest.mark.asyncio
    async def test_publish_status_success(self, controller):
        """_publish_status publishes JSON to vm.lifecycle.status.{oid}."""
        payload = {"job_id": "test", "status": "created", "vm_name": "agent-vm-test"}
        await controller._publish_status("test", payload)

        subject, raw = controller.nc.publish.call_args[0]
        assert subject == "vm.lifecycle.status.test-oid"
        assert json.loads(raw.decode()) == payload

    @pytest.mark.asyncio
    async def test_publish_status_nats_error_does_not_raise(self, controller):
        """_publish_status logs error but does not propagate NATS failure."""
        controller.nc.publish.side_effect = Exception("NATS disconnected")
        # Should not raise
        await controller._publish_status("test", {"status": "test"})

    @pytest.mark.asyncio
    async def test_publish_status_encodes_utf8(self, controller):
        """_publish_status encodes payload as UTF-8 JSON bytes."""
        payload = {"job_id": "test", "description": "Umlauts: \u00e4\u00f6\u00fc"}
        await controller._publish_status("test", payload)

        raw_bytes = controller.nc.publish.call_args[0][1]
        assert isinstance(raw_bytes, bytes)
        decoded = json.loads(raw_bytes.decode("utf-8"))
        assert "\u00e4\u00f6\u00fc" in decoded["description"]

    @pytest.mark.asyncio
    async def test_publish_status_subject_is_always_lifecycle_status(self, controller):
        """_publish_status always uses the vm.lifecycle.status.{oid} subject."""
        for payload in [
            {"status": "created"},
            {"status": "deleted"},
            {"status": "failed"},
        ]:
            controller.nc.publish.reset_mock()
            await controller._publish_status("any-job", payload)
            assert (
                controller.nc.publish.call_args[0][0] == "vm.lifecycle.status.test-oid"
            )


# =============================================================================
# Tests: run() and request_shutdown()
# =============================================================================


class TestRunAndShutdown:
    """Tests for controller lifecycle management."""

    @pytest.mark.asyncio
    async def test_run_subscribes_to_all_subjects(self, controller):
        """run() subscribes to create, delete, and get subjects."""
        controller._shutdown.set()

        with (
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            await controller.run()

        subjects = [call[0][0] for call in controller.nc.subscribe.call_args_list]
        assert "vm.lifecycle.create.test-oid" in subjects
        assert "vm.lifecycle.delete.test-oid" in subjects
        assert "vm.lifecycle.get.test-oid" in subjects
        # Regression: flat subjects must NOT be subscribed
        assert "vm.lifecycle.create" not in subjects
        assert "vm.lifecycle.delete" not in subjects
        assert "vm.lifecycle.get" not in subjects

    @pytest.mark.asyncio
    async def test_run_calls_init_sequence_in_order(self, controller):
        """run() calls load_template, init_k8s, headscale.init, connect_nats."""
        controller._shutdown.set()
        call_order = []

        def track(name):
            def fn(*_a, **_kw):
                call_order.append(name)

            return fn

        async def track_async(name):
            async def fn(*_a, **_kw):
                call_order.append(name)

            return fn

        with (
            patch.object(
                controller, "load_template", side_effect=track("load_template")
            ),
            patch.object(controller, "init_k8s", side_effect=track("init_k8s")),
            patch.object(controller, "connect_nats", side_effect=track("connect_nats")),
        ):
            controller.headscale.init = AsyncMock(side_effect=track("headscale_init"))
            await controller.run()

        assert call_order == [
            "load_template",
            "init_k8s",
            "headscale_init",
            "connect_nats",
        ]

    @pytest.mark.asyncio
    async def test_run_drains_nats_on_shutdown(self, controller):
        """run() drains NATS connection during shutdown."""
        controller._shutdown.set()

        with (
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            await controller.run()

        controller.nc.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_closes_headscale_on_shutdown(self, controller):
        """run() closes the Headscale client during shutdown."""
        controller._shutdown.set()

        with (
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            await controller.run()

        controller.headscale.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_skips_drain_when_disconnected(self, controller):
        """run() skips NATS drain when already disconnected."""
        controller._shutdown.set()
        controller.nc.is_connected = False

        with (
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            await controller.run()

        controller.nc.drain.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_skips_drain_when_nc_is_none(self):
        """run() handles nc=None during shutdown gracefully."""
        ctrl = _make_controller()
        ctrl.nc = None
        ctrl._shutdown.set()

        mock_nc = AsyncMock()
        mock_nc.is_connected = True
        mock_nc.subscribe = AsyncMock()
        mock_nc.drain = AsyncMock()

        async def fake_connect():
            ctrl.nc = mock_nc

        with (
            patch.object(ctrl, "load_template"),
            patch.object(ctrl, "init_k8s"),
            patch.object(ctrl, "connect_nats", side_effect=fake_connect),
        ):
            await ctrl.run()

        mock_nc.drain.assert_awaited_once()

    def test_request_shutdown_sets_event(self, controller):
        """request_shutdown() sets the internal shutdown event."""
        assert not controller._shutdown.is_set()
        controller.request_shutdown()
        assert controller._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_request_shutdown_unblocks_run(self, controller):
        """request_shutdown() unblocks the run() wait loop."""
        with (
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):

            async def delayed_shutdown():
                await asyncio.sleep(0.05)
                controller.request_shutdown()

            task = asyncio.create_task(delayed_shutdown())
            await controller.run()
            await task

        assert controller._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_run_subscribe_callbacks_are_handlers(self, controller):
        """run() wires the correct handler to each subscription."""
        controller._shutdown.set()

        with (
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            await controller.run()

        # Build a map of subject -> callback name
        cb_map = {}
        for call in controller.nc.subscribe.call_args_list:
            subject = call[0][0]
            cb = call[1].get("cb") or (call[0][1] if len(call[0]) > 1 else None)
            cb_map[subject] = (
                getattr(cb, "__name__", None) or getattr(cb, "__func__", cb).__name__
            )

        assert cb_map["vm.lifecycle.create.test-oid"] == "handle_create"
        assert cb_map["vm.lifecycle.delete.test-oid"] == "handle_delete"
        assert cb_map["vm.lifecycle.get.test-oid"] == "handle_status_query"


# =============================================================================
# Tests: ORCHESTRATOR_ID gating
# =============================================================================


class TestOrchestratorIdRequired:
    """run() refuses to subscribe to flat vm.lifecycle.* subjects.

    Without per-orchestrator scoping the controller would race other
    controllers sharing the NATS hub and provision duplicate VMs for every
    create request. The startup-time sys.exit(1) is the failsafe.
    """

    @pytest.mark.asyncio
    async def test_run_exits_when_orchestrator_id_unset(self, controller):
        controller._shutdown.set()
        with (
            patch("vm.controller.controller.ORCHESTRATOR_ID", ""),
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            with pytest.raises(SystemExit) as exc:
                await controller.run()
            assert exc.value.code == 1

    @pytest.mark.asyncio
    async def test_run_does_not_subscribe_when_orchestrator_id_unset(self, controller):
        controller._shutdown.set()
        with (
            patch("vm.controller.controller.ORCHESTRATOR_ID", ""),
            patch.object(controller, "load_template"),
            patch.object(controller, "init_k8s"),
            patch.object(controller, "connect_nats", new_callable=AsyncMock),
        ):
            with pytest.raises(SystemExit):
                await controller.run()

        controller.nc.subscribe.assert_not_awaited()


# =============================================================================
# Tests: main() entry point
# =============================================================================


class TestMain:
    """Tests for the main() entry point and signal handling."""

    def test_main_registers_signal_handlers(self):
        """main() registers SIGTERM and SIGINT handlers."""
        import signal as signal_module
        from vm.controller.controller import main

        registered = {}
        with (
            patch("vm.controller.controller.VMController") as mock_cls,
            patch(
                "vm.controller.controller.signal.signal",
                side_effect=lambda s, h: registered.update({s: h}),
            ),
            patch("vm.controller.controller.asyncio.run"),
        ):
            mock_cls.return_value = MagicMock()
            main()

        assert signal_module.SIGTERM in registered
        assert signal_module.SIGINT in registered

    def test_main_calls_asyncio_run(self):
        """main() calls asyncio.run with controller.run()."""
        from vm.controller.controller import main

        mock_ctrl = MagicMock()
        mock_coro = MagicMock()
        mock_ctrl.run.return_value = mock_coro

        with (
            patch("vm.controller.controller.VMController", return_value=mock_ctrl),
            patch("vm.controller.controller.signal.signal"),
            patch("vm.controller.controller.asyncio.run") as mock_arun,
        ):
            main()

        mock_arun.assert_called_once_with(mock_coro)

    def test_signal_handler_calls_request_shutdown(self):
        """Signal handler invokes request_shutdown on the controller."""
        import signal as signal_module
        from vm.controller.controller import main

        handlers = {}
        with (
            patch("vm.controller.controller.VMController") as mock_cls,
            patch(
                "vm.controller.controller.signal.signal",
                side_effect=lambda s, h: handlers.update({s: h}),
            ),
            patch("vm.controller.controller.asyncio.run"),
        ):
            mock_ctrl = MagicMock()
            mock_cls.return_value = mock_ctrl
            main()

        # Invoke the captured SIGTERM handler
        handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
        mock_ctrl.request_shutdown.assert_called_once()


# =============================================================================
# Tests: VMController.__init__()
# =============================================================================


class TestVMControllerInit:
    """Tests for VMController initialization."""

    def test_init_defaults(self):
        """VMController starts with None clients and empty template."""
        ctrl = VMController()
        assert ctrl.nc is None
        assert ctrl.k8s_client is None
        assert ctrl.template_text == ""
        assert not ctrl._shutdown.is_set()

    def test_init_creates_headscale_client(self):
        """VMController creates a HeadscaleClient instance on init."""
        ctrl = VMController()
        # headscale is set (even if it is a MagicMock from our stub)
        assert ctrl.headscale is not None

    def test_init_shutdown_event_not_set(self):
        """The internal _shutdown event starts unset."""
        ctrl = VMController()
        assert not ctrl._shutdown.is_set()


# =============================================================================
# Tests: Edge cases and integration scenarios
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and unusual scenarios."""

    @pytest.mark.asyncio
    async def test_create_then_delete_same_job(self, controller):
        """Creating and then deleting a VM for the same job works correctly."""
        job_id = "create-delete-test"
        await controller.handle_create(
            make_nats_msg(
                {
                    "job_id": job_id,
                    "agent_config": "developer",
                }
            )
        )
        await controller.handle_delete(make_nats_msg({"job_id": job_id}))

        controller.k8s_client.create_namespaced_custom_object.assert_called_once()
        controller.k8s_client.delete_namespaced_custom_object.assert_called_once()

        # Last status should be "deleted"
        last_payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert last_payload["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_concurrent_create_requests(self, controller):
        """Multiple concurrent create requests each produce their own VM."""
        jobs = [
            {"job_id": f"concurrent-{i}", "agent_config": "developer"} for i in range(3)
        ]
        msgs = [make_nats_msg(job) for job in jobs]

        await asyncio.gather(*[controller.handle_create(m) for m in msgs])

        assert controller.k8s_client.create_namespaced_custom_object.call_count == 3

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_crash_create(self, controller):
        """NATS publish failure during create does not crash the handler."""
        controller.nc.publish.side_effect = Exception("NATS down")

        msg = make_nats_msg(SAMPLE_JOB_CONFIG)
        # Should not raise
        await controller.handle_create(msg)
        controller.k8s_client.create_namespaced_custom_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_crash_delete(self, controller):
        """NATS publish failure during delete does not crash the handler."""
        controller.nc.publish.side_effect = Exception("NATS down")

        msg = make_nats_msg({"job_id": "test-pub-fail"})
        # Should not raise
        await controller.handle_delete(msg)

    @pytest.mark.asyncio
    async def test_empty_bytes_message_create(self, controller):
        """Empty bytes message on create is handled gracefully."""
        msg = make_nats_msg_raw(b"")
        await controller.handle_create(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "failed"

    @pytest.mark.asyncio
    async def test_empty_bytes_message_delete(self, controller):
        """Empty bytes message on delete is handled gracefully."""
        msg = make_nats_msg_raw(b"")
        await controller.handle_delete(msg)

        payload = json.loads(controller.nc.publish.call_args[0][1].decode())
        assert payload["status"] == "delete_failed"

    @pytest.mark.asyncio
    async def test_double_shutdown_is_idempotent(self, controller):
        """Calling request_shutdown() twice does not cause issues."""
        controller.request_shutdown()
        controller.request_shutdown()
        assert controller._shutdown.is_set()

    @pytest.mark.asyncio
    async def test_handle_create_double_call_same_job(self, controller):
        """Two create requests for the same job ID each call K8s API."""
        msg1 = make_nats_msg({"job_id": "dup-job"})
        msg2 = make_nats_msg({"job_id": "dup-job"})

        await controller.handle_create(msg1)
        await controller.handle_create(msg2)

        assert controller.k8s_client.create_namespaced_custom_object.call_count == 2


# =============================================================================
# Tests: Module-level constants
# =============================================================================


class TestModuleConstants:
    """Tests for module-level configuration constants."""

    def test_kubevirt_api_coordinates(self):
        """KubeVirt API coordinates are correctly defined."""
        assert KUBEVIRT_GROUP == "kubevirt.io"
        assert KUBEVIRT_VERSION == "v1"
        assert KUBEVIRT_PLURAL == "virtualmachines"

    def test_default_config_values(self):
        """Default configuration values are sensible."""
        from vm.controller.controller import DEFAULT_CPU, DEFAULT_MEMORY, VM_NAMESPACE

        assert isinstance(DEFAULT_CPU, int)
        assert DEFAULT_CPU > 0
        assert "Gi" in DEFAULT_MEMORY or "Mi" in DEFAULT_MEMORY
        assert VM_NAMESPACE  # Not empty

    def test_nats_url_has_nats_scheme(self):
        """NATS_URL has the nats:// scheme."""
        from vm.controller.controller import NATS_URL

        assert "nats://" in NATS_URL
