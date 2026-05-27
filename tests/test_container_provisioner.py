"""Tests for the container provisioner service."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.workspace_lifecycle import WorkspaceOwner


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
            owner=WorkspaceOwner.job("abc123-full-uuid"),
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
        # PR 3: default tier label always present so the network policy
        # selector can match. Pods without a resolvable project tier fall
        # back to internet-only.
        assert labels["srw.io/network-tier"] == "internet-only"

    def test_manifest_tier_label_home_allowed(self):
        """Explicitly passing network_tier='home-allowed' propagates to the pod label."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc",
            owner=WorkspaceOwner.job("abc"),
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
            network_tier="home-allowed",
        )
        assert manifest["metadata"]["labels"]["srw.io/network-tier"] == "home-allowed"

    def test_manifest_container_spec(self):
        """Container spec has correct ports, resources, and probes."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            owner=WorkspaceOwner.job("abc123-full-uuid"),
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
        assert ports["ssh"] == 30022
        assert ports["code-server"] == 38080

        # Resources
        assert container["resources"]["requests"]["cpu"] == "1000m"
        assert container["resources"]["requests"]["memory"] == "2Gi"
        assert container["resources"]["limits"]["cpu"] == "4000m"
        assert container["resources"]["limits"]["memory"] == "8Gi"

        # Probes
        assert container["readinessProbe"]["tcpSocket"]["port"] == 30022
        assert container["livenessProbe"]["tcpSocket"]["port"] == 30022

    def test_manifest_volumes(self):
        """Pod has workspace emptyDir and SSH public key secret volumes."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._ssh_secret_name = "test-ssh-secret"

        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            owner=WorkspaceOwner.job("abc123"),
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
            owner=WorkspaceOwner.job("abc123"),
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        assert manifest["spec"]["restartPolicy"] == "Never"

    def test_manifest_termination_grace_period(self):
        """Pod has terminationGracePeriodSeconds for graceful artifact upload."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            owner=WorkspaceOwner.job("abc123"),
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        assert manifest["spec"]["terminationGracePeriodSeconds"] == 120

    def test_manifest_volume_mounts(self):
        """Container has correct volume mounts for workspace and SSH key."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            owner=WorkspaceOwner.job("abc123"),
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        container = manifest["spec"]["containers"][0]
        mounts = {m["name"]: m for m in container["volumeMounts"]}

        assert mounts["workspace-data"]["mountPath"] == "/home/agent-host"
        # SSH pubkey mounted to staging path — entrypoint copies to authorized_keys
        assert mounts["ssh-pubkey"]["mountPath"] == "/tmp/ssh-pubkey"
        assert mounts["ssh-pubkey"]["readOnly"] is True

    def test_manifest_security_context(self):
        """Pod and container security contexts are properly hardened."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="workspace-abc123",
            owner=WorkspaceOwner.job("abc123"),
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

        # Pod-level: seccomp profile
        pod_sc = manifest["spec"]["securityContext"]
        assert pod_sc["seccompProfile"]["type"] == "RuntimeDefault"

        # Container-level: drop ALL, add back only SSHD essentials
        container = manifest["spec"]["containers"][0]
        container_sc = container["securityContext"]

        assert container_sc["capabilities"]["drop"] == ["ALL"]
        added = set(container_sc["capabilities"]["add"])
        # SSHD needs these to function
        assert {"SETUID", "SETGID", "NET_BIND_SERVICE", "SYS_CHROOT"} <= added
        # Dangerous capabilities must NOT be present
        assert "NET_RAW" not in added
        assert "SYS_PTRACE" not in added
        assert "SYS_ADMIN" not in added
        assert "MKNOD" not in added

        # allowPrivilegeEscalation must be true (SSHD setuid requirement)
        # but sudo is not installed, so agent-host cannot escalate
        assert container_sc["allowPrivilegeEscalation"] is True


class TestNetworkTierResolution:
    """Tests for _resolve_network_tier — the orchestrator → DB → pod label path."""

    @pytest.mark.asyncio
    async def test_returns_default_when_db_missing(self):
        """No DB attached → falls back to the default tier (no exception)."""
        from orchestrator.services.container_provisioner import (
            DEFAULT_NETWORK_TIER,
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        # _db starts as None — no connect() call here
        tier = await provisioner._resolve_network_tier("any-id", kind="job")
        assert tier == DEFAULT_NETWORK_TIER

    @pytest.mark.asyncio
    async def test_returns_default_when_project_unmapped(self):
        """DB returns None (job without project_id) → falls back to default."""
        from orchestrator.services.container_provisioner import (
            DEFAULT_NETWORK_TIER,
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        mock_db = MagicMock()
        mock_db.get_workspace_network_tier = AsyncMock(return_value=None)
        provisioner._db = mock_db

        tier = await provisioner._resolve_network_tier("job-id", kind="job")
        assert tier == DEFAULT_NETWORK_TIER
        mock_db.get_workspace_network_tier.assert_awaited_once_with("job-id", "job")

    @pytest.mark.asyncio
    async def test_returns_resolved_tier(self):
        """DB returns 'home-allowed' → that's the tier emitted to the pod label."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        mock_db = MagicMock()
        mock_db.get_workspace_network_tier = AsyncMock(return_value="home-allowed")
        provisioner._db = mock_db

        tier = await provisioner._resolve_network_tier("thread-id", kind="thread")
        assert tier == "home-allowed"
        mock_db.get_workspace_network_tier.assert_awaited_once_with(
            "thread-id", "thread"
        )

    @pytest.mark.asyncio
    async def test_db_exception_is_swallowed(self):
        """A DB error must not block pod creation — falls back to default."""
        from orchestrator.services.container_provisioner import (
            DEFAULT_NETWORK_TIER,
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        mock_db = MagicMock()
        mock_db.get_workspace_network_tier = AsyncMock(
            side_effect=RuntimeError("connection lost")
        )
        provisioner._db = mock_db

        tier = await provisioner._resolve_network_tier("job-id", kind="job")
        assert tier == DEFAULT_NETWORK_TIER


class TestSecurityHardening:
    """Tests verifying workspace container security hardening (Phase 1).

    These tests ensure the pod manifest and container image enforce
    the security posture described in docs/features/hardened_container.md.
    """

    @staticmethod
    def _build_manifest():
        """Helper to build a manifest with default params."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        return provisioner._build_pod_manifest(
            pod_name="workspace-test",
            owner=WorkspaceOwner.job("test-job-id"),
            image="test:latest",
            cpu="500m",
            memory="1Gi",
            cpu_limit="2000m",
            memory_limit="4Gi",
        )

    def test_no_privileged_container(self):
        """Container must not run in privileged mode."""
        manifest = self._build_manifest()
        container = manifest["spec"]["containers"][0]
        sc = container.get("securityContext", {})
        assert sc.get("privileged") is not True

    def test_no_host_namespaces(self):
        """Pod must not share host namespaces (network, PID, IPC)."""
        manifest = self._build_manifest()
        spec = manifest["spec"]
        assert spec.get("hostNetwork") is not True
        assert spec.get("hostPID") is not True
        assert spec.get("hostIPC") is not True

    def test_no_host_path_volumes(self):
        """No volumes may use hostPath (prevents host filesystem access)."""
        manifest = self._build_manifest()
        for vol in manifest["spec"]["volumes"]:
            assert "hostPath" not in vol, f"Volume {vol['name']} uses hostPath"

    def test_capabilities_drop_all(self):
        """Container must drop ALL capabilities before adding specific ones."""
        manifest = self._build_manifest()
        container_sc = manifest["spec"]["containers"][0]["securityContext"]
        assert container_sc["capabilities"]["drop"] == ["ALL"]

    def test_only_sshd_capabilities_added(self):
        """Only the minimum capabilities required for SSHD are added back."""
        manifest = self._build_manifest()
        container_sc = manifest["spec"]["containers"][0]["securityContext"]
        added = set(container_sc["capabilities"]["add"])
        # Exact expected set — nothing more, nothing less
        expected = {
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "SETGID",
            "SETUID",
            "NET_BIND_SERVICE",
            "SYS_CHROOT",
            "KILL",
            "AUDIT_WRITE",
        }
        assert added == expected, f"Unexpected capabilities: {added - expected}"

    def test_dangerous_capabilities_excluded(self):
        """Explicitly verify dangerous capabilities are never added."""
        manifest = self._build_manifest()
        container_sc = manifest["spec"]["containers"][0]["securityContext"]
        added = set(container_sc["capabilities"]["add"])
        dangerous = {
            "NET_RAW",
            "SYS_PTRACE",
            "SYS_ADMIN",
            "MKNOD",
            "DAC_READ_SEARCH",
            "SYS_RAWIO",
            "SYS_MODULE",
            "SYS_BOOT",
        }
        overlap = added & dangerous
        assert not overlap, f"Dangerous capabilities present: {overlap}"

    def test_seccomp_profile_set(self):
        """Pod must have RuntimeDefault seccomp profile."""
        manifest = self._build_manifest()
        pod_sc = manifest["spec"]["securityContext"]
        assert pod_sc["seccompProfile"]["type"] == "RuntimeDefault"

    def test_single_container_only(self):
        """Pod must have exactly one container (no sidecars with elevated privs)."""
        manifest = self._build_manifest()
        assert len(manifest["spec"]["containers"]) == 1

    def test_workspace_data_is_emptydir(self):
        """Workspace storage must be ephemeral (emptyDir), not persistent."""
        manifest = self._build_manifest()
        volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
        assert "emptyDir" in volumes["workspace-data"]

    def test_ssh_key_volume_is_readonly(self):
        """SSH key volume mount must be read-only."""
        manifest = self._build_manifest()
        container = manifest["spec"]["containers"][0]
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        assert mounts["ssh-pubkey"]["readOnly"] is True

    def test_ssh_key_staged_not_direct(self):
        """SSH key must mount to staging path, not directly to authorized_keys.

        Direct mount results in root-owned authorized_keys which breaks
        OpenSSH StrictModes. The entrypoint copies with correct ownership.
        """
        manifest = self._build_manifest()
        container = manifest["spec"]["containers"][0]
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        mount_path = mounts["ssh-pubkey"]["mountPath"]
        assert mount_path == "/tmp/ssh-pubkey"
        assert ".ssh/authorized_keys" not in mount_path

    def test_restart_policy_never(self):
        """Pod must not restart (ephemeral — created per-job, deleted after)."""
        manifest = self._build_manifest()
        assert manifest["spec"]["restartPolicy"] == "Never"


class TestDockerfileHardening:
    """Static analysis tests for workspace Dockerfile and entrypoint.

    These verify the image itself enforces security, independent of K8s.
    """

    @staticmethod
    def _read_file(path):
        import pathlib

        return pathlib.Path(path).read_text()

    def test_dockerfile_no_sudo_package(self):
        """Dockerfile must not install the sudo package."""
        content = self._read_file("docker/Dockerfile.workspace")
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()
            # Skip comments
            if stripped.startswith("#"):
                continue
            # Check apt-get install lines for 'sudo' as a standalone package
            if "apt-get install" in stripped or (
                stripped.endswith("\\") and not stripped.startswith("#")
            ):
                # Look for 'sudo' as a standalone word (not 'pseudo' or 'libsudo')
                tokens = stripped.replace("\\", "").split()
                assert "sudo" not in tokens, (
                    "sudo package found in Dockerfile apt-get install"
                )

    def test_dockerfile_no_sudoers_entry(self):
        """Dockerfile must not create a sudoers entry."""
        content = self._read_file("docker/Dockerfile.workspace")
        assert "NOPASSWD" not in content
        assert "sudoers.d" not in content

    def test_dockerfile_no_sudo_commands(self):
        """Dockerfile must not use sudo in RUN commands (uses su -c instead)."""
        content = self._read_file("docker/Dockerfile.workspace")
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("RUN") or (
                stripped and not stripped.startswith("#") and "sudo " in stripped
            ):
                # Allow the comment about sudo being excluded
                if "intentionally excluded" in stripped:
                    continue
                assert "sudo " not in stripped, (
                    f"Line {i}: sudo command found in Dockerfile: {stripped}"
                )

    def test_dockerfile_has_user_writable_pip(self):
        """Dockerfile must set PIP_TARGET for user-space pip installs."""
        content = self._read_file("docker/Dockerfile.workspace")
        assert "PIP_TARGET=" in content

    def test_dockerfile_has_user_writable_npm(self):
        """Dockerfile must set npm_config_prefix for user-space npm installs."""
        content = self._read_file("docker/Dockerfile.workspace")
        assert "npm_config_prefix=" in content

    def test_dockerfile_creates_local_dirs(self):
        """Dockerfile must pre-create .local, .npm-global, .cache directories."""
        content = self._read_file("docker/Dockerfile.workspace")
        assert ".local/bin" in content
        assert ".npm-global" in content
        assert ".cache" in content

    def test_entrypoint_copies_ssh_key(self):
        """Entrypoint must copy SSH key from staging path to /etc/ssh/authorized_keys/."""
        content = self._read_file("docker/workspace-entrypoint.sh")
        assert "/tmp/ssh-pubkey" in content
        assert "/etc/ssh/authorized_keys/agent-host" in content

    def test_entrypoint_does_not_run_as_user(self):
        """Entrypoint must run SSHD as root (required for user session management).

        code-server runs as agent-host via su -c.
        """
        content = self._read_file("docker/workspace-entrypoint.sh")
        assert "exec /usr/sbin/sshd" in content
        assert "su -c" in content and "agent-host" in content


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
            result = await provisioner.create_workspace(
                WorkspaceOwner.job("test-job-id-123456")
            )

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

        result = await provisioner.create_workspace(
            WorkspaceOwner.job("test-job-id-123456")
        )
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
            await provisioner.create_workspace(
                WorkspaceOwner.job("abcdef123456-rest-of-uuid")
            )

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
                WorkspaceOwner.job("test-job-123456"),
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

        result = await provisioner.create_workspace(WorkspaceOwner.job("test-job-123456"))

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

        result = await provisioner.delete_workspace(WorkspaceOwner.job("test-job-123456"))

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

        result = await provisioner.delete_workspace(WorkspaceOwner.job("test-job-123456"))
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_workspace_not_available(self):
        """Returns False when K8s is not available."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = False

        result = await provisioner.delete_workspace(WorkspaceOwner.job("test-job-123456"))
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
            result = await provisioner.get_workspace_status(
                WorkspaceOwner.job("test-job-123456")
            )

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

        result = await provisioner.get_workspace_status(
            WorkspaceOwner.job("test-job-123456")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_status_not_available(self):
        """Returns None when K8s is not available."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = False

        result = await provisioner.get_workspace_status(
            WorkspaceOwner.job("test-job-123456")
        )
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

    def test_job_needs_container_explicit_sandbox(self):
        """Job with backend: sandbox is detected."""
        import json

        job = {
            "config_override": json.dumps({"workspace": {"backend": "sandbox"}}),
        }
        assert self._get_backend(job) == "sandbox"

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


class TestOwnerKeyedWorkspace:
    """Tests for WorkspaceOwner-keyed provisioning (Task 2 refactor)."""

    @pytest.mark.asyncio
    async def test_create_workspace_session_uses_thread_naming_and_store(
        self, monkeypatch
    ):
        """create_workspace(WorkspaceOwner.session(...)) uses thread pod name,
        srw/thread-id label, and calls merge_thread_workspace_context (not job)."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._namespace = "test-ns"
        provisioner._db = AsyncMock()
        provisioner._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock()
        provisioner._core_api = mock_core_api

        monkeypatch.setattr(
            provisioner, "_wait_for_ready", AsyncMock(return_value="10.0.0.5")
        )

        ok = await provisioner.create_workspace(WorkspaceOwner.session("thread-abc"))

        assert ok is True
        body = mock_core_api.create_namespaced_pod.call_args.kwargs["body"]
        assert body["metadata"]["name"] == "ws-thread-thread-abc"
        assert body["metadata"]["labels"]["srw/thread-id"] == "thread-abc"
        assert body["metadata"]["labels"]["srw/component"] == "thread-workspace"
        assert "srw/job-id" not in body["metadata"]["labels"]
        provisioner._db.merge_thread_workspace_context.assert_awaited()
        provisioner._db.merge_workspace_container_context.assert_not_called()
