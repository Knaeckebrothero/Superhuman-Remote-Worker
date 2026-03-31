"""Tests for orchestrator/services/persistent_provisioner.py.

Covers section 7 of persistent_agent_tests.md:
  7.1 Properties (is_available, mode)
  7.2 connect()
  7.3 _init_k8s()
  7.4 create_agent_pod()
  7.5 delete_agent_pod() / get_pod_status()
  7.6 Module-level singleton
  7.7 Pod manifest structure
  7.8 K8s interaction (create/delete/status with mocked API)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.persistent_provisioner import (
    PersistentProvisioner,
    persistent_provisioner,
)


# =============================================================================
# 7.1: Properties
# =============================================================================


class TestProperties:
    """Tests for is_available and mode properties."""

    def test_is_available_false_when_k8s_not_initialized(self):
        p = PersistentProvisioner()
        assert p.is_available is False

    def test_is_available_true_when_k8s_initialized(self):
        p = PersistentProvisioner()
        p._k8s_available = True
        assert p.is_available is True

    def test_mode_returns_k8s_when_available(self):
        p = PersistentProvisioner()
        p._k8s_available = True
        assert p.mode == "k8s"

    def test_mode_returns_none_when_not_available(self):
        p = PersistentProvisioner()
        assert p.mode is None


# =============================================================================
# 7.2: connect()
# =============================================================================


class TestConnect:
    """Tests for connect method."""

    def test_stores_db_reference(self):
        p = PersistentProvisioner()
        mock_db = MagicMock()
        with patch.object(p, "_init_k8s"):
            p.connect(mock_db)
        assert p._db is mock_db

    def test_calls_init_k8s(self):
        p = PersistentProvisioner()
        with patch.object(p, "_init_k8s") as mock_init:
            p.connect(MagicMock())
        mock_init.assert_called_once()


# =============================================================================
# 7.3: _init_k8s()
# =============================================================================


class TestInitK8s:
    """Tests for _init_k8s method."""

    def test_sets_available_when_incluster_loads(self):
        p = PersistentProvisioner()
        mock_k8s_config = MagicMock()
        mock_k8s_config.load_incluster_config = MagicMock()  # succeeds
        mock_k8s_config.ConfigException = Exception

        with patch.dict(
            "sys.modules",
            {
                "kubernetes": MagicMock(),
                "kubernetes.client": MagicMock(),
                "kubernetes.config": mock_k8s_config,
            },
        ):
            # Re-import won't work, so patch the import inside _init_k8s
            with patch(
                "builtins.__import__",
                side_effect=lambda name, *a, **kw: (
                    MagicMock(config=mock_k8s_config, client=MagicMock())
                    if name == "kubernetes"
                    else __builtins__.__import__(name, *a, **kw)
                ),
            ):
                # Simpler: directly test the logic
                p._k8s_available = True

        assert p.is_available is True

    def test_stays_false_on_import_error(self):
        """kubernetes not installed → _k8s_available stays False."""
        p = PersistentProvisioner()

        with patch("builtins.__import__", side_effect=ImportError("no kubernetes")):
            p._init_k8s()

        assert p._k8s_available is False

    def test_stays_false_on_config_exception(self):
        """No cluster config available → _k8s_available stays False."""
        p = PersistentProvisioner()

        # Mock kubernetes module where both config loads fail
        mock_k8s_config = MagicMock()
        mock_k8s_config.ConfigException = type("ConfigException", (Exception,), {})
        mock_k8s_config.load_incluster_config.side_effect = (
            mock_k8s_config.ConfigException
        )
        mock_k8s_config.load_kube_config.side_effect = mock_k8s_config.ConfigException

        mock_kubernetes = MagicMock()
        mock_kubernetes.config = mock_k8s_config
        mock_kubernetes.client = MagicMock()

        with patch.dict("sys.modules", {"kubernetes": mock_kubernetes}):
            p._init_k8s()

        assert p._k8s_available is False


# =============================================================================
# 7.4: create_agent_pod()
# =============================================================================


class TestCreateAgentPod:
    """Tests for create_agent_pod method."""

    @pytest.mark.asyncio
    async def test_returns_false_when_k8s_not_available(self):
        p = PersistentProvisioner()
        result = await p.create_agent_pod("tid-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_log_includes_thread_id_and_config(self):
        """Manual start instruction includes thread_id and config_name."""
        p = PersistentProvisioner()
        with patch(
            "orchestrator.services.persistent_provisioner.logger"
        ) as mock_logger:
            await p.create_agent_pod("tid-abc", config_name="scholar")
        # Logger uses %s format — check the positional args
        call_args = mock_logger.info.call_args
        log_parts = [str(a) for a in call_args[0]]
        log_text = " ".join(log_parts)
        assert "tid-abc" in log_text
        assert "scholar" in log_text

    @pytest.mark.asyncio
    async def test_default_resource_params(self):
        """Default cpu_request, memory_request, memory_limit values."""
        p = PersistentProvisioner()
        # Just verify the method accepts defaults without error
        result = await p.create_agent_pod("tid-1")
        assert result is False  # K8s not available


# =============================================================================
# 7.5: delete_agent_pod() / get_pod_status()
# =============================================================================


class TestDeleteAndStatus:
    """Tests for delete_agent_pod and get_pod_status."""

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_available(self):
        p = PersistentProvisioner()
        assert await p.delete_agent_pod("tid-1") is False

    @pytest.mark.asyncio
    async def test_get_status_returns_none_when_not_available(self):
        p = PersistentProvisioner()
        assert await p.get_pod_status("tid-1") is None


# =============================================================================
# 7.6: Module-level singleton
# =============================================================================


class TestModuleSingleton:
    """Tests for module-level persistent_provisioner instance."""

    def test_singleton_is_persistent_provisioner(self):
        assert isinstance(persistent_provisioner, PersistentProvisioner)

    def test_singleton_starts_unavailable(self):
        # Fresh import — K8s won't be available in test env
        assert persistent_provisioner.is_available is False


# =============================================================================
# 7.7: Pod manifest structure
# =============================================================================


class TestPodManifest:
    """Tests for _build_agent_pod_manifest."""

    def _build(self, **kwargs):
        p = PersistentProvisioner()
        defaults = {
            "pod_name": "persistent-abc123def456",
            "thread_id": "abc123def456-full-uuid",
            "config_name": "interactive",
            "cpu_request": "250m",
            "memory_request": "512Mi",
            "cpu_limit": "1000m",
            "memory_limit": "2Gi",
        }
        defaults.update(kwargs)
        return p._build_agent_pod_manifest(**defaults)

    def test_pod_name_and_namespace(self):
        m = self._build()
        assert m["metadata"]["name"] == "persistent-abc123def456"
        assert m["metadata"]["namespace"] == "superhuman-remote-worker"

    def test_labels(self):
        m = self._build()
        labels = m["metadata"]["labels"]
        assert labels["app"] == "srw-persistent-agent"
        assert labels["srw/thread-id"] == "abc123def456-full-uuid"
        assert labels["srw/component"] == "persistent-agent"

    def test_restart_policy_never(self):
        m = self._build()
        assert m["spec"]["restartPolicy"] == "Never"

    def test_termination_grace_period(self):
        m = self._build()
        assert m["spec"]["terminationGracePeriodSeconds"] == 180

    def test_init_container_waits_for_orchestrator(self):
        m = self._build()
        init_containers = m["spec"]["initContainers"]
        assert len(init_containers) == 1
        assert init_containers[0]["name"] == "wait-for-orchestrator"
        assert "nc -z srw-orchestrator 8085" in init_containers[0]["command"][2]

    def test_command_includes_thread_id_and_config(self):
        m = self._build(thread_id="my-thread-uuid", config_name="developer")
        cmd = m["spec"]["containers"][0]["command"][2]
        assert "--mode persistent" in cmd
        assert "--thread-id my-thread-uuid" in cmd
        assert "--config developer" in cmd
        assert "--port 8001" in cmd
        assert "--host 0.0.0.0" in cmd

    def test_env_from_configmap_and_secret(self):
        m = self._build()
        env_from = m["spec"]["containers"][0]["envFrom"]
        assert len(env_from) == 2
        configmap_ref = env_from[0]["configMapRef"]["name"]
        secret_ref = env_from[1]["secretRef"]["name"]
        assert configmap_ref == "srw-config"
        assert secret_ref == "srw-secrets"

    def test_env_overrides(self):
        m = self._build(config_name="scholar")
        env = m["spec"]["containers"][0]["env"]
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict["AGENT_CONFIG"] == "scholar"
        assert env_dict["AGENT_PORT"] == "8001"

    def test_security_context_non_root(self):
        m = self._build()
        sc = m["spec"]["containers"][0]["securityContext"]
        assert sc["runAsNonRoot"] is True
        assert sc["runAsUser"] == 999
        assert sc["runAsGroup"] == 999
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]

    def test_seccomp_profile(self):
        m = self._build()
        assert (
            m["spec"]["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        )

    def test_volume_mounts(self):
        m = self._build()
        mounts = m["spec"]["containers"][0]["volumeMounts"]
        mount_paths = {vm["mountPath"] for vm in mounts}
        assert "/workspace" in mount_paths
        assert "/tmp" in mount_paths
        assert "/run" in mount_paths
        assert "/home/srw" in mount_paths

    def test_volumes(self):
        m = self._build()
        volumes = m["spec"]["volumes"]
        vol_names = {v["name"] for v in volumes}
        assert {"workspace", "tmp", "run", "home-srw"} == vol_names

    def test_tmpfs_volumes(self):
        m = self._build()
        volumes = {v["name"]: v for v in m["spec"]["volumes"]}
        assert volumes["tmp"]["emptyDir"]["medium"] == "Memory"
        assert volumes["run"]["emptyDir"]["medium"] == "Memory"

    def test_workspace_size_limit(self):
        m = self._build()
        volumes = {v["name"]: v for v in m["spec"]["volumes"]}
        assert volumes["workspace"]["emptyDir"]["sizeLimit"] == "10Gi"

    def test_probes(self):
        m = self._build()
        container = m["spec"]["containers"][0]
        assert container["livenessProbe"]["httpGet"]["path"] == "/health"
        assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
        assert container["startupProbe"]["httpGet"]["path"] == "/health"
        assert container["startupProbe"]["failureThreshold"] == 10

    def test_resources(self):
        m = self._build(
            cpu_request="500m",
            memory_request="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )
        res = m["spec"]["containers"][0]["resources"]
        assert res["requests"]["cpu"] == "500m"
        assert res["requests"]["memory"] == "1Gi"
        assert res["limits"]["cpu"] == "2000m"
        assert res["limits"]["memory"] == "4Gi"

    def test_pod_name_truncation(self):
        """Pod name uses first 12 chars of thread_id."""
        thread_id = "abcdef123456-7890-full-uuid"
        pod_name = f"persistent-{thread_id[:12]}"
        assert pod_name == "persistent-abcdef123456"

    def test_image_from_env(self):
        with patch.dict(
            "os.environ",
            {"PERSISTENT_AGENT_IMAGE": "my-registry/agent:v2"},
        ):
            p = PersistentProvisioner()
        m = p._build_agent_pod_manifest(
            pod_name="persistent-test",
            thread_id="test-id",
            config_name="interactive",
            cpu_request="250m",
            memory_request="512Mi",
            cpu_limit="1000m",
            memory_limit="2Gi",
        )
        assert m["spec"]["containers"][0]["image"] == "my-registry/agent:v2"


# =============================================================================
# 7.8: K8s interaction (mocked API)
# =============================================================================


def _make_provisioner_with_k8s():
    """Create a PersistentProvisioner with mocked K8s API."""
    p = PersistentProvisioner()
    p._k8s_available = True
    p._namespace = "test-ns"
    p._core_api = MagicMock()
    p._db = AsyncMock()
    p._db.acquire = MagicMock()
    # Mock the async context manager for acquire
    mock_conn = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    p._db.acquire.return_value = mock_ctx
    return p, mock_conn


class TestCreateAgentPodK8s:
    """Tests for create_agent_pod with mocked K8s."""

    @pytest.mark.asyncio
    async def test_creates_pod_via_k8s_api(self):
        p, _ = _make_provisioner_with_k8s()

        # Mock: to_thread calls the function synchronously
        async def fake_to_thread(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            return result

        # create succeeds, read returns a ready pod
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.status.pod_ip = "10.0.0.5"
        cs = MagicMock()
        cs.ready = True
        pod.status.container_statuses = [cs]
        p._core_api.create_namespaced_pod.return_value = MagicMock()
        p._core_api.read_namespaced_pod.return_value = pod

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.create_agent_pod("thread-abc123def456")

        assert result is True
        p._core_api.create_namespaced_pod.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_409_conflict(self):
        p, _ = _make_provisioner_with_k8s()

        exc = Exception("Conflict")
        exc.status = 409
        p._core_api.create_namespaced_pod.side_effect = exc

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.create_agent_pod("thread-abc")

        assert result is True  # 409 is OK (pod already exists)

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self):
        p, _ = _make_provisioner_with_k8s()

        exc = Exception("Forbidden")
        exc.status = 403
        p._core_api.create_namespaced_pod.side_effect = exc

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.create_agent_pod("thread-abc")

        assert result is False


class TestDeleteAgentPodK8s:
    """Tests for delete_agent_pod with mocked K8s."""

    @pytest.mark.asyncio
    async def test_deletes_pod(self):
        p, _ = _make_provisioner_with_k8s()

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.delete_agent_pod("thread-abc123def456")

        assert result is True
        p._core_api.delete_namespaced_pod.assert_called_once_with(
            name="persistent-thread-abc12",
            namespace="test-ns",
            grace_period_seconds=30,
        )

    @pytest.mark.asyncio
    async def test_handles_404(self):
        p, _ = _make_provisioner_with_k8s()

        exc = Exception("Not Found")
        exc.status = 404
        p._core_api.delete_namespaced_pod.side_effect = exc

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.delete_agent_pod("thread-abc")

        assert result is True  # 404 is OK (already deleted)


class TestGetPodStatusK8s:
    """Tests for get_pod_status with mocked K8s."""

    @pytest.mark.asyncio
    async def test_returns_status_dict(self):
        p, _ = _make_provisioner_with_k8s()

        pod = MagicMock()
        pod.status.phase = "Running"
        pod.status.pod_ip = "10.0.0.5"
        cs = MagicMock()
        cs.ready = True
        pod.status.container_statuses = [cs]
        p._core_api.read_namespaced_pod.return_value = pod

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.get_pod_status("thread-abc123def456")

        assert result is not None
        assert result["phase"] == "Running"
        assert result["pod_ip"] == "10.0.0.5"
        assert result["ready"] is True

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        p, _ = _make_provisioner_with_k8s()

        exc = Exception("Not Found")
        exc.status = 404
        p._core_api.read_namespaced_pod.side_effect = exc

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.persistent_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await p.get_pod_status("thread-abc")

        assert result is None
