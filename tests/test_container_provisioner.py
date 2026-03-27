"""Tests for the container provisioner service."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestContainerProvisionerInit:
    """Tests for ContainerProvisioner initialization."""

    def test_not_available_without_k8s(self):
        """Provisioner reports unavailable when kubernetes is not installed."""
        with patch.dict("sys.modules", {"kubernetes": None, "kubernetes.client": None}):
            # Re-import to pick up mocked modules
            import importlib
            from orchestrator.services import container_provisioner as mod

            importlib.reload(mod)

            provisioner = mod.ContainerProvisioner()
            assert provisioner.is_available is False

    def test_default_env_values(self):
        """Provisioner uses correct default environment values."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKSPACE_NAMESPACE", None)
            os.environ.pop("WORKSPACE_IMAGE", None)

            from orchestrator.services.container_provisioner import (
                ContainerProvisioner,
            )

            provisioner = ContainerProvisioner()
            assert provisioner._namespace == "superhuman-remote-worker"
            assert "workspace" in provisioner._workspace_image

    def test_custom_env_values(self):
        """Provisioner picks up custom environment variables."""
        with patch.dict(
            os.environ,
            {
                "WORKSPACE_NAMESPACE": "custom-ns",
                "WORKSPACE_IMAGE": "my-registry/workspace:v1",
                "WORKSPACE_SSH_SECRET": "my-ssh-key",
            },
        ):
            from orchestrator.services.container_provisioner import (
                ContainerProvisioner,
            )

            provisioner = ContainerProvisioner()
            assert provisioner._namespace == "custom-ns"
            assert provisioner._workspace_image == "my-registry/workspace:v1"
            assert provisioner._ssh_secret_name == "my-ssh-key"

    def test_connect_initializes_k8s(self):
        """connect() initializes the K8s client and stores db reference."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        mock_db = MagicMock()

        with patch.object(provisioner, "_init_k8s") as mock_init:
            provisioner.connect(db=mock_db)
            mock_init.assert_called_once()
            assert provisioner._db is mock_db


class TestPodManifest:
    """Tests for pod manifest generation."""

    def test_manifest_structure(self):
        """Generated manifest has correct structure and labels."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            job_id="abc123-full-uuid",
            image="test-image:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        assert manifest["apiVersion"] == "v1"
        assert manifest["kind"] == "Pod"
        assert manifest["metadata"]["name"] == "workspace-abc123"

        labels = manifest["metadata"]["labels"]
        assert labels["app"] == "srw-workspace"
        assert labels["srw/job-id"] == "abc123-full-uuid"
        assert labels["srw/component"] == "workspace"

    def test_manifest_container_spec(self):
        """Container spec has correct ports, resources, and probes."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            job_id="abc123-full-uuid",
            image="test-image:v2",
            cpu="1000m",
            memory="2Gi",
            cpu_limit="4000m",
            memory_limit="8Gi",
        )

        container = manifest["spec"]["containers"][0]
        assert container["name"] == "workspace"
        assert container["image"] == "test-image:v2"

        # Ports
        ports = {p["name"]: p["containerPort"] for p in container["ports"]}
        assert ports["ssh"] == 22
        assert ports["code-server"] == 8080

        # Resources
        assert container["resources"]["requests"]["cpu"] == "1000m"
        assert container["resources"]["requests"]["memory"] == "2Gi"
        assert container["resources"]["limits"]["cpu"] == "4000m"
        assert container["resources"]["limits"]["memory"] == "8Gi"

        # Probes
        assert container["readinessProbe"]["tcpSocket"]["port"] == 22
        assert container["livenessProbe"]["tcpSocket"]["port"] == 22

    def test_manifest_volumes(self):
        """Pod has workspace emptyDir and SSH public key secret volumes."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._ssh_secret_name = "test-ssh-secret"

        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            job_id="abc123",
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
        assert "workspace-data" in volumes
        assert "emptyDir" in volumes["workspace-data"]
        assert volumes["workspace-data"]["emptyDir"]["sizeLimit"] == "10Gi"

        assert "ssh-pubkey" in volumes
        assert volumes["ssh-pubkey"]["secret"]["secretName"] == "test-ssh-secret"

    def test_manifest_restart_policy(self):
        """Pod has restartPolicy: Never."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            job_id="abc123",
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        assert manifest["spec"]["restartPolicy"] == "Never"

    def test_manifest_volume_mounts(self):
        """Container has correct volume mounts for workspace and SSH key."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            job_id="abc123",
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        container = manifest["spec"]["containers"][0]
        mounts = {m["name"]: m for m in container["volumeMounts"]}

        assert mounts["workspace-data"]["mountPath"] == "/home/agent-host/workspace"
        # SSH pubkey mounted to staging path — entrypoint copies to authorized_keys
        assert mounts["ssh-pubkey"]["mountPath"] == "/tmp/ssh-pubkey"
        assert mounts["ssh-pubkey"]["readOnly"] is True


class TestCreateWorkspace:
    """Tests for workspace creation."""

    @pytest.mark.asyncio
    async def test_create_workspace_success(self):
        """Successful workspace creation provisions pod and waits for IP."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock()
        provisioner._core_api = mock_core_api

        with patch.object(
            provisioner, "_wait_for_ready", new_callable=AsyncMock
        ) as mock_wait:
            mock_wait.return_value = "10.42.0.100"
            result = await provisioner.create_workspace("test-job-id-123456")

        assert result is True
        # Verify pod was created
        mock_core_api.create_namespaced_pod.assert_called_once()
        # Verify context was updated (created + ready)
        assert provisioner._db.merge_workspace_container_context.call_count == 2

    @pytest.mark.asyncio
    async def test_create_workspace_not_available(self):
        """Returns False when K8s is not available."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = False

        result = await provisioner.create_workspace("test-job-id-123456")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_workspace_pod_name(self):
        """Pod name uses first 12 chars of job_id."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock()
        provisioner._core_api = mock_core_api

        with patch.object(
            provisioner, "_wait_for_ready", new_callable=AsyncMock
        ) as mock_wait:
            mock_wait.return_value = "10.42.0.100"
            await provisioner.create_workspace("abcdef123456-rest-of-uuid")

        # Verify via the context update — pod_name should be workspace-abcdef12345
        context_calls = provisioner._db.merge_workspace_container_context.call_args_list
        first_call = context_calls[0]
        assert first_call[0][1]["pod_name"] == "workspace-abcdef123456"

    @pytest.mark.asyncio
    async def test_create_workspace_custom_resources(self):
        """Custom CPU/memory values are passed through."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        captured_body = {}

        def capture_create(**kwargs):
            captured_body.update(kwargs.get("body", {}))

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = capture_create
        provisioner._core_api = mock_core_api

        with patch.object(
            provisioner, "_wait_for_ready", new_callable=AsyncMock
        ) as mock_wait:
            mock_wait.return_value = "10.42.0.100"
            await provisioner.create_workspace(
                "test-job-123456",
                cpu="1000m",
                memory="2Gi",
                cpu_limit="4000m",
                memory_limit="8Gi",
            )

        container = captured_body["spec"]["containers"][0]
        assert container["resources"]["requests"]["cpu"] == "1000m"
        assert container["resources"]["requests"]["memory"] == "2Gi"
        assert container["resources"]["limits"]["cpu"] == "4000m"
        assert container["resources"]["limits"]["memory"] == "8Gi"

    @pytest.mark.asyncio
    async def test_create_workspace_failure_sets_failed_context(self):
        """Failed creation sets status to 'failed' in context."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(
            side_effect=Exception("API error")
        )
        provisioner._core_api = mock_core_api

        result = await provisioner.create_workspace("test-job-123456")

        assert result is False
        # Should have set failed status
        context_calls = provisioner._db.merge_workspace_container_context.call_args_list
        last_call_updates = context_calls[-1][0][1]
        assert last_call_updates["status"] == "failed"
        assert "API error" in last_call_updates["error"]


class TestDeleteWorkspace:
    """Tests for workspace deletion."""

    @pytest.mark.asyncio
    async def test_delete_workspace_success(self):
        """Successful deletion removes pod and updates context."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        mock_core_api = MagicMock()
        mock_core_api.delete_namespaced_pod = MagicMock()
        provisioner._core_api = mock_core_api

        result = await provisioner.delete_workspace("test-job-123456")

        assert result is True
        mock_core_api.delete_namespaced_pod.assert_called_once()
        # Verify context set to deleted
        context_call = provisioner._db.merge_workspace_container_context.call_args
        assert context_call[0][1]["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_workspace_already_gone(self):
        """Deleting a non-existent pod returns True (idempotent)."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()

        mock_404 = MagicMock()
        mock_404.status = 404
        mock_core_api = MagicMock()
        mock_core_api.delete_namespaced_pod = MagicMock(side_effect=mock_404)
        provisioner._core_api = mock_core_api

        # Simulate K8s ApiException with status 404

        error = Exception("Not Found")
        error.status = 404
        mock_core_api.delete_namespaced_pod = MagicMock(side_effect=error)

        result = await provisioner.delete_workspace("test-job-123456")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_workspace_not_available(self):
        """Returns False when K8s is not available."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = False

        result = await provisioner.delete_workspace("test-job-123456")
        assert result is False


class TestGetWorkspaceStatus:
    """Tests for workspace status queries."""

    @pytest.mark.asyncio
    async def test_status_running_pod(self):
        """Returns correct status for a running pod."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True

        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.status.pod_ip = "10.42.0.50"
        mock_cs = MagicMock()
        mock_cs.ready = True
        mock_pod.status.container_statuses = [mock_cs]

        mock_core_api = MagicMock()
        mock_core_api.read_namespaced_pod = MagicMock(return_value=mock_pod)
        provisioner._core_api = mock_core_api

        # Mock asyncio.to_thread to execute the function synchronously
        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await provisioner.get_workspace_status("test-job-123456")

        assert result is not None
        assert result["phase"] == "Running"
        assert result["pod_ip"] == "10.42.0.50"
        assert result["ready"] is True
        assert result["pod_name"] == "workspace-test-job-123"

    @pytest.mark.asyncio
    async def test_status_not_found(self):
        """Returns None for a non-existent pod."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True

        error = Exception("Not Found")
        error.status = 404
        mock_core_api = MagicMock()
        mock_core_api.read_namespaced_pod = MagicMock(side_effect=error)
        provisioner._core_api = mock_core_api

        result = await provisioner.get_workspace_status("test-job-123456")
        assert result is None

    @pytest.mark.asyncio
    async def test_status_not_available(self):
        """Returns None when K8s is not available."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = False

        result = await provisioner.get_workspace_status("test-job-123456")
        assert result is None


class TestDispatchHelpers:
    """Tests for the dispatch helper logic.

    These test the logic directly rather than importing from orchestrator.main,
    which requires database and service dependencies that aren't available in
    the test environment. The logic is simple enough to test inline.
    """

    @staticmethod
    def _parse_config_override(job: dict) -> dict:
        """Replicate config_override parsing from orchestrator/main.py."""
        import json

        co = job.get("config_override") or {}
        if isinstance(co, str):
            try:
                co = json.loads(co)
            except (json.JSONDecodeError, TypeError):
                co = {}
        return co

    @staticmethod
    def _get_backend(job: dict) -> str | None:
        """Extract workspace backend from config_override."""
        import json

        co = job.get("config_override") or {}
        if isinstance(co, str):
            try:
                co = json.loads(co)
            except (json.JSONDecodeError, TypeError):
                co = {}
        return co.get("workspace", {}).get("backend")

    @staticmethod
    def _get_container_context(job: dict) -> dict:
        """Replicate _get_container_context from orchestrator/main.py."""
        import json

        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        return ctx.get("workspace_container", {})

    def test_job_needs_container_explicit(self):
        """Job with backend: container is detected."""
        import json

        job = {
            "config_override": json.dumps({"workspace": {"backend": "container"}}),
        }
        assert self._get_backend(job) == "container"

    def test_job_needs_container_explicit_local(self):
        """Job with backend: local is detected."""
        import json

        job = {
            "config_override": json.dumps({"workspace": {"backend": "local"}}),
        }
        assert self._get_backend(job) == "local"

    def test_job_needs_container_explicit_remote(self):
        """Job with backend: remote is detected."""
        import json

        job = {
            "config_override": json.dumps({"workspace": {"backend": "remote"}}),
        }
        assert self._get_backend(job) == "remote"

    def test_job_no_explicit_backend(self):
        """Job with no backend returns None (default behavior)."""
        job = {"config_override": "{}"}
        assert self._get_backend(job) is None

    def test_get_container_context_present(self):
        """Extracts workspace_container from job context."""
        import json

        job = {
            "context": json.dumps(
                {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.42.0.100",
                    }
                }
            )
        }
        ctx = self._get_container_context(job)
        assert ctx["status"] == "ready"
        assert ctx["pod_ip"] == "10.42.0.100"

    def test_get_container_context_missing(self):
        """Returns empty dict when workspace_container not in context."""
        job = {"context": "{}"}
        ctx = self._get_container_context(job)
        assert ctx == {}

    def test_get_container_context_dict_input(self):
        """Handles context as dict (not JSON string)."""
        job = {
            "context": {
                "workspace_container": {"status": "creating"},
            }
        }
        ctx = self._get_container_context(job)
        assert ctx["status"] == "creating"

    def test_get_container_context_none(self):
        """Handles None context gracefully."""
        job = {"context": None}
        ctx = self._get_container_context(job)
        assert ctx == {}

    def test_get_container_context_invalid_json(self):
        """Handles malformed JSON context gracefully."""
        job = {"context": "not-valid-json"}
        ctx = self._get_container_context(job)
        assert ctx == {}
