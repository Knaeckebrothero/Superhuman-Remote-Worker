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
            os.environ.pop("WORKSPACE_FUSE_ENABLED", None)
            os.environ.pop("WORKSPACE_FUSE_PRIVILEGED", None)

            from orchestrator.services.container_provisioner import (
                ContainerProvisioner,
            )

            provisioner = ContainerProvisioner()
            assert provisioner._namespace == "superhuman-remote-worker"
            assert "workspace" in provisioner._workspace_image
            assert provisioner._fuse_enabled is True
            assert provisioner._fuse_privileged is True

    def test_custom_env_values(self):
        """Provisioner picks up custom environment variables."""
        with patch.dict(
            os.environ,
            {
                "WORKSPACE_NAMESPACE": "custom-ns",
                "WORKSPACE_IMAGE": "my-registry/workspace:v1",
                "WORKSPACE_SSH_SECRET": "my-ssh-key",
                "WORKSPACE_FUSE_ENABLED": "false",
            },
        ):
            from orchestrator.services.container_provisioner import (
                ContainerProvisioner,
            )

            provisioner = ContainerProvisioner()
            assert provisioner._namespace == "custom-ns"
            assert provisioner._workspace_image == "my-registry/workspace:v1"
            assert provisioner._ssh_secret_name == "my-ssh-key"
            assert provisioner._fuse_enabled is False
            assert provisioner._fuse_privileged is False

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


class TestWorkspacePodLive:
    """Drift probe: workspace_pod_live(owner) — True=live, False=confirmed
    dead/gone, None=can't tell (must be treated as 'assume live')."""

    def _provisioner(self, k8s=True):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = k8s
        p._core_api = MagicMock()
        return p

    @pytest.mark.asyncio
    async def test_none_without_k8s(self):
        p = self._provisioner(k8s=False)
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is None

    @pytest.mark.asyncio
    async def test_false_on_404(self):
        p = self._provisioner()

        class _ApiExc(Exception):
            status = 404

        p._core_api.read_namespaced_pod = MagicMock(side_effect=_ApiExc())
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is False

    @pytest.mark.asyncio
    async def test_true_when_running(self):
        p = self._provisioner()
        pod = MagicMock()
        pod.status.phase = "Running"
        p._core_api.read_namespaced_pod = MagicMock(return_value=pod)
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is True

    @pytest.mark.asyncio
    async def test_false_on_failed_tombstone(self):
        p = self._provisioner()
        pod = MagicMock()
        pod.status.phase = "Failed"
        p._core_api.read_namespaced_pod = MagicMock(return_value=pod)
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is False

    @pytest.mark.asyncio
    async def test_none_on_transient_error(self):
        p = self._provisioner()

        class _ApiExc(Exception):
            status = 503

        p._core_api.read_namespaced_pod = MagicMock(side_effect=_ApiExc())
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is None


class TestReleaseWorkspace:
    """Owner-keyed release_workspace: snapshot (if ready) then delete pod, and —
    only when the caller says the work is truly over — its PVC."""

    def _prov(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p.get_workspace_status = AsyncMock(
            return_value={"pod_ip": "10.0.0.5", "ready": True}
        )
        p.delete_workspace = AsyncMock(return_value=True)
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        snap = MagicMock()
        snap.is_available = True
        snap.capture_vm_snapshot = AsyncMock()
        p._snapshot_service = snap
        return p

    @pytest.mark.asyncio
    async def test_session_snapshots_then_deletes(self):
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        assert await p.release_workspace(owner) is True
        p._snapshot_service.capture_vm_snapshot.assert_awaited_once()
        assert (
            p._snapshot_service.capture_vm_snapshot.call_args.kwargs["entity_type"]
            == "threads"
        )
        p.delete_workspace.assert_awaited_once_with(owner)
        p.delete_workspace_pvc.assert_awaited_once_with(owner)

    @pytest.mark.asyncio
    async def test_job_uses_jobs_entity_type(self):
        p = self._prov()
        assert await p.release_workspace(WorkspaceOwner.job("j1")) is True
        assert (
            p._snapshot_service.capture_vm_snapshot.call_args.kwargs["entity_type"]
            == "jobs"
        )

    @pytest.mark.asyncio
    async def test_skips_snapshot_when_not_ready(self):
        p = self._prov()
        p.get_workspace_status = AsyncMock(
            return_value={"pod_ip": None, "ready": False}
        )
        owner = WorkspaceOwner.session("t1")
        assert await p.release_workspace(owner) is True
        p._snapshot_service.capture_vm_snapshot.assert_not_awaited()
        p.delete_workspace.assert_awaited_once_with(owner)

    @pytest.mark.asyncio
    async def test_returns_false_without_k8s(self):
        p = self._prov()
        p._k8s_available = False
        assert await p.release_workspace(WorkspaceOwner.session("t1")) is False
        p.delete_workspace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reclaim_volume_false_keeps_the_pvc(self):
        """The resumable-session contract: snapshot + drop the pod, keep the volume.

        ``end_thread`` without ?permanent leaves the thread in 'ended', which
        ``resume_thread`` accepts — so destroying the volume here would hand the
        user an empty workspace the next time they reopen a session they never
        deleted. The pod and Service go (both are cheap to recreate); the data
        stays.
        """
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        assert await p.release_workspace(owner, reclaim_volume=False) is True
        # Still snapshotted + torn down — only the volume is spared.
        p._snapshot_service.capture_vm_snapshot.assert_awaited_once()
        p.delete_workspace.assert_awaited_once_with(owner)
        p.delete_workspace_pvc.assert_not_called()
        # The Service follows the pod, not the volume: recreating it on the next
        # create_workspace is a 409-idempotent no-op, so it costs nothing to drop.
        p._delete_service.assert_awaited_once_with(owner)

    @pytest.mark.asyncio
    async def test_reclaim_volume_defaults_to_true(self):
        """The permanent-delete path is the default — a caller that means
        "this work is over" needs no extra argument to reclaim storage."""
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        assert await p.release_workspace(owner) is True
        p.delete_workspace_pvc.assert_awaited_once_with(owner)


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

    def test_manifest_has_lifecycle_annotation(self):
        """Pods carry a lifecycle-managed annotation as a GC backstop hook."""
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
        ann = manifest["metadata"].get("annotations", {})
        assert ann.get("srw.io/managed-by") == "lifecycle-reconciler"

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

        # Pod-level: FUSE-capable default relaxes seccomp for rclone mount.
        pod_sc = manifest["spec"]["securityContext"]
        assert pod_sc["seccompProfile"]["type"] == "Unconfined"

        # Container-level: drop ALL, add back SSHD essentials plus FUSE.
        container = manifest["spec"]["containers"][0]
        container_sc = container["securityContext"]

        assert container_sc["capabilities"]["drop"] == ["ALL"]
        added = set(container_sc["capabilities"]["add"])
        # SSHD needs these to function
        assert {"SETUID", "SETGID", "NET_BIND_SERVICE", "SYS_CHROOT"} <= added
        # rclone mount needs FUSE support in the workspace runtime.
        assert "SYS_ADMIN" in added
        assert container_sc["privileged"] is True
        # Unrelated dangerous capabilities must NOT be present.
        assert "NET_RAW" not in added
        assert "SYS_PTRACE" not in added
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

    def test_fuse_default_uses_privileged_container(self):
        """Default rclone/FUSE runtime needs privileged mode on k3d."""
        manifest = self._build_manifest()
        container = manifest["spec"]["containers"][0]
        sc = container.get("securityContext", {})
        assert sc.get("privileged") is True

    def test_no_host_namespaces(self):
        """Pod must not share host namespaces (network, PID, IPC)."""
        manifest = self._build_manifest()
        spec = manifest["spec"]
        assert spec.get("hostNetwork") is not True
        assert spec.get("hostPID") is not True
        assert spec.get("hostIPC") is not True

    def test_only_fuse_host_path_volume(self):
        """/dev/fuse is the only hostPath volume allowed by default."""
        manifest = self._build_manifest()
        for vol in manifest["spec"]["volumes"]:
            if "hostPath" not in vol:
                continue
            assert vol["name"] == "dev-fuse"
            assert vol["hostPath"] == {"path": "/dev/fuse", "type": "CharDevice"}

    def test_capabilities_drop_all(self):
        """Container must drop ALL capabilities before adding specific ones."""
        manifest = self._build_manifest()
        container_sc = manifest["spec"]["containers"][0]["securityContext"]
        assert container_sc["capabilities"]["drop"] == ["ALL"]

    def test_only_sshd_and_fuse_capabilities_added(self):
        """Only SSHD essentials plus FUSE mount support are added back."""
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
            "SYS_ADMIN",
        }
        assert added == expected, f"Unexpected capabilities: {added - expected}"

    def test_unrelated_dangerous_capabilities_excluded(self):
        """Only the FUSE-required elevated capability is added."""
        manifest = self._build_manifest()
        container_sc = manifest["spec"]["containers"][0]["securityContext"]
        added = set(container_sc["capabilities"]["add"])
        dangerous = {
            "NET_RAW",
            "SYS_PTRACE",
            "MKNOD",
            "DAC_READ_SEARCH",
            "SYS_RAWIO",
            "SYS_MODULE",
            "SYS_BOOT",
        }
        overlap = added & dangerous
        assert not overlap, f"Dangerous capabilities present: {overlap}"
        assert "SYS_ADMIN" in added

    def test_fuse_can_be_disabled(self):
        """Restricted clusters can opt out of the FUSE runtime profile."""
        with patch.dict(os.environ, {"WORKSPACE_FUSE_ENABLED": "false"}):
            from orchestrator.services.container_provisioner import ContainerProvisioner

            provisioner = ContainerProvisioner()
            manifest = provisioner._build_pod_manifest(
                pod_name="workspace-test",
                owner=WorkspaceOwner.job("test-job-id"),
                image="test:latest",
                cpu="500m",
                memory="1Gi",
                cpu_limit="2000m",
                memory_limit="4Gi",
            )

        container = manifest["spec"]["containers"][0]
        added = set(container["securityContext"]["capabilities"]["add"])
        volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        assert "SYS_ADMIN" not in added
        assert "dev-fuse" not in volumes
        assert "dev-fuse" not in mounts
        assert container["securityContext"].get("privileged") is not True
        assert manifest["spec"]["securityContext"]["seccompProfile"]["type"] == (
            "RuntimeDefault"
        )

    def test_fuse_privileged_profile_can_be_disabled(self):
        """Clusters that support non-privileged FUSE can keep the narrower profile."""
        with patch.dict(os.environ, {"WORKSPACE_FUSE_PRIVILEGED": "false"}):
            from orchestrator.services.container_provisioner import ContainerProvisioner

            provisioner = ContainerProvisioner()
            manifest = provisioner._build_pod_manifest(
                pod_name="workspace-test",
                owner=WorkspaceOwner.job("test-job-id"),
                image="test:latest",
                cpu="500m",
                memory="1Gi",
                cpu_limit="2000m",
                memory_limit="4Gi",
            )

        container = manifest["spec"]["containers"][0]
        added = set(container["securityContext"]["capabilities"]["add"])
        volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
        assert "SYS_ADMIN" in added
        assert "dev-fuse" in volumes
        assert container["securityContext"].get("privileged") is not True
        assert manifest["spec"]["securityContext"]["seccompProfile"]["type"] == (
            "RuntimeDefault"
        )

    def test_fuse_default_uses_unconfined_seccomp(self):
        """Default rclone/FUSE runtime relaxes seccomp for FUSE mounts."""
        manifest = self._build_manifest()
        pod_sc = manifest["spec"]["securityContext"]
        assert pod_sc["seccompProfile"]["type"] == "Unconfined"

    def test_single_container_only(self):
        """Pod must have exactly one container (no sidecars with elevated privs)."""
        manifest = self._build_manifest()
        assert len(manifest["spec"]["containers"]) == 1

    def test_workspace_data_defaults_to_emptydir_without_a_claim(self):
        """A manifest built with no PVC gets emptyDir — the flag-off default.

        NOT a statement that workspace storage must be ephemeral: under
        ``WORKSPACE_PVC_ENABLED`` both jobs and sessions are PVC-backed and the
        builder mounts the claim instead (see TestCreateWorkspacePvc). The
        security posture this class pins is about privilege and host access, and
        is indifferent to which of the two volume kinds is mounted; what it does
        pin is that a workspace never silently gets *some other* volume source
        when the caller asked for no claim.
        """
        manifest = self._build_manifest()
        volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
        assert "emptyDir" in volumes["workspace-data"]
        assert "persistentVolumeClaim" not in volumes["workspace-data"]

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

        sshd is launched directly by the (root) entrypoint and the container is
        anchored on it via `wait` (it is backgrounded before the state-sentinel
        wait, not `exec`-ed as PID 1). code-server is the only thing dropped to
        the unprivileged agent-host user via su -c.
        """
        content = self._read_file("docker/workspace-entrypoint.sh")
        # sshd is launched directly — runs as root, never wrapped in su.
        assert "/usr/sbin/sshd -D" in content
        assert "su -c" in content and "agent-host" in content
        # The only su -c drop is for code-server; sshd must not run under su.
        for line in content.splitlines():
            if "su -c" in line:
                assert "sshd" not in line, (
                    f"sshd must run as root, not under su: {line!r}"
                )


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

        result = await provisioner.create_workspace(
            WorkspaceOwner.job("test-job-123456")
        )

        assert result is False
        # Should have set failed status
        context_calls = provisioner._db.merge_workspace_container_context.call_args_list
        last_call_updates = context_calls[-1][0][1]
        assert last_call_updates["status"] == "failed"
        assert "API error" in last_call_updates["error"]


class TestAuthenticatedWorkspaceReadiness:
    """Kubernetes readiness is not enough; the configured key must log in."""

    @staticmethod
    def _ready_pod():
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.status.pod_ip = "10.42.0.50"
        container = MagicMock()
        container.ready = True
        pod.status.container_statuses = [container]
        return pod

    @pytest.mark.asyncio
    async def test_wait_for_ready_requires_authenticated_probe(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._core_api = MagicMock()
        provisioner._core_api.read_namespaced_pod.return_value = self._ready_pod()

        with (
            patch(
                "orchestrator.services.container_provisioner.resolve_ssh_key_path",
                return_value="/run/secrets/test-key",
            ),
            patch(
                "orchestrator.services.container_provisioner.workspace_private_key_fingerprint",
                return_value="SHA256:test",
            ),
            patch(
                "orchestrator.services.container_provisioner.wait_for_agent_ssh",
                new=AsyncMock(return_value=(True, 2, "")),
            ) as probe,
        ):
            result = await provisioner._wait_for_ready("workspace-test", timeout=1)

        assert result == "10.42.0.50"
        probe.assert_awaited_once_with(
            "10.42.0.50",
            30022,
            deadline_s=provisioner._ssh_auth_ready_timeout,
            connect_timeout_s=provisioner._ssh_auth_connect_timeout,
            interval_s=provisioner._ssh_auth_poll_interval,
            key_path="/run/secrets/test-key",
        )

    @pytest.mark.asyncio
    async def test_auth_rejection_is_not_published_as_ready(self):
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
            WorkspaceSSHAuthenticationError,
        )

        provisioner = ContainerProvisioner()
        provisioner._core_api = MagicMock()
        provisioner._core_api.read_namespaced_pod.return_value = self._ready_pod()

        with (
            patch(
                "orchestrator.services.container_provisioner.resolve_ssh_key_path",
                return_value="/run/secrets/test-key",
            ),
            patch(
                "orchestrator.services.container_provisioner.workspace_private_key_fingerprint",
                return_value="SHA256:test",
            ),
            patch(
                "orchestrator.services.container_provisioner.wait_for_agent_ssh",
                new=AsyncMock(return_value=(False, 3, "Permission denied (publickey)")),
            ),
        ):
            with pytest.raises(
                WorkspaceSSHAuthenticationError, match="rejected.*SSH key"
            ):
                await provisioner._wait_for_ready("workspace-test", timeout=1)

    @pytest.mark.asyncio
    async def test_create_workspace_records_auth_failure_instead_of_ready(self):
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
            WorkspaceSSHAuthenticationError,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)
        provisioner._core_api = MagicMock()
        provisioner._core_api.create_namespaced_pod = MagicMock()

        with patch.object(
            provisioner,
            "_wait_for_ready",
            new=AsyncMock(
                side_effect=WorkspaceSSHAuthenticationError(
                    "workspace rejected configured public key"
                )
            ),
        ):
            result = await provisioner.create_workspace(
                WorkspaceOwner.job("test-job-auth-123456")
            )

        assert result is False
        updates = [
            call.args[1]
            for call in provisioner._db.merge_workspace_container_context.call_args_list
        ]
        assert updates[-1] == {
            "status": "failed",
            "error": "workspace rejected configured public key",
        }
        assert not any(update.get("status") == "ready" for update in updates)


class TestCreateWorkspacePvc:
    """Branch (a): PVC-backed workspaces (WORKSPACE_PVC_ENABLED).

    Covers BOTH owner kinds. Sessions were scoped out of v1 on the theory that
    they rehydrate from Postgres, but Postgres holds the conversation, not the
    working tree — and a session's pod is idle-reaped while the thread stays
    resumable, so emptyDir quietly destroyed files a user could still reopen.
    """

    @staticmethod
    def _provisioner(pvc_enabled: bool):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p._pvc_enabled = pvc_enabled
        p._pvc_size = "10Gi"
        p._db = MagicMock()
        p._db.merge_workspace_container_context = AsyncMock(return_value=True)
        p._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        p._db.execute = AsyncMock(return_value=None)
        return p

    @staticmethod
    def _capture_core(pod_body: dict, pvc_body: dict | None = None):
        core = MagicMock()
        core.create_namespaced_pod = lambda **kw: pod_body.update(kw.get("body", {}))
        if pvc_body is not None:
            core.create_namespaced_persistent_volume_claim = (
                lambda **kw: pvc_body.update(kw.get("body", {}))
            )
        return core

    @pytest.mark.asyncio
    async def test_pvc_mode_creates_and_mounts_pvc_for_job(self):
        p = self._provisioner(pvc_enabled=True)
        pod_body, pvc_body = {}, {}
        p._core_api = self._capture_core(pod_body, pvc_body)

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            result = await p.create_workspace(
                WorkspaceOwner.job("abcdef123456-rest-uuid")
            )

        assert result is True
        # Deterministic UUID-keyed PVC name + owner label + RWO access mode
        assert pvc_body["metadata"]["name"] == "pvc-workspace-abcdef123456"
        assert pvc_body["metadata"]["labels"]["srw/job-id"] == "abcdef123456-rest-uuid"
        assert pvc_body["spec"]["accessModes"] == ["ReadWriteOnce"]
        # The pod mounts the PVC, not an emptyDir
        vols = {v["name"]: v for v in pod_body["spec"]["volumes"]}
        assert (
            vols["workspace-data"]["persistentVolumeClaim"]["claimName"]
            == "pvc-workspace-abcdef123456"
        )
        assert "emptyDir" not in vols["workspace-data"]

    @pytest.mark.asyncio
    async def test_disabled_uses_emptydir_and_creates_no_pvc(self):
        p = self._provisioner(pvc_enabled=False)
        pod_body = {}
        core = self._capture_core(pod_body)
        core.create_namespaced_persistent_volume_claim = MagicMock()
        p._core_api = core

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        core.create_namespaced_persistent_volume_claim.assert_not_called()
        vols = {v["name"]: v for v in pod_body["spec"]["volumes"]}
        assert "emptyDir" in vols["workspace-data"]
        assert "persistentVolumeClaim" not in vols["workspace-data"]

    @pytest.mark.asyncio
    async def test_pvc_mode_creates_and_mounts_pvc_for_session(self):
        """Sessions are PVC-backed on the same flag as jobs, under their own name.

        The mirror of test_pvc_mode_creates_and_mounts_pvc_for_job, and the
        create-side half of the invariant the reclaim rules protect: an ended
        session can only come back to its files if those files were on a volume
        in the first place.
        """
        p = self._provisioner(pvc_enabled=True)
        pod_body, pvc_body = {}, {}
        p._core_api = self._capture_core(pod_body, pvc_body)

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            result = await p.create_workspace(
                WorkspaceOwner.session("abcdef123456-thread-uuid")
            )

        assert result is True
        # Session prefix (mirrors the ws-thread-* pod split so `kubectl get
        # pod,pvc` reads straight), thread label, RWO access mode.
        assert pvc_body["metadata"]["name"] == "pvc-ws-thread-abcdef123456"
        assert (
            pvc_body["metadata"]["labels"]["srw/thread-id"]
            == "abcdef123456-thread-uuid"
        )
        assert pvc_body["spec"]["accessModes"] == ["ReadWriteOnce"]
        # The pod mounts the PVC, not an emptyDir
        vols = {v["name"]: v for v in pod_body["spec"]["volumes"]}
        assert (
            vols["workspace-data"]["persistentVolumeClaim"]["claimName"]
            == "pvc-ws-thread-abcdef123456"
        )
        assert "emptyDir" not in vols["workspace-data"]

    def test_session_name_never_collides_with_a_job_name(self):
        """Distinct prefixes, so a job and a session sharing an id prefix can
        never be handed each other's volume."""
        from orchestrator.services.container_provisioner import _pvc_name_for

        shared = "abcdef123456-rest"
        assert _pvc_name_for(WorkspaceOwner.job(shared)) == "pvc-workspace-abcdef123456"
        assert (
            _pvc_name_for(WorkspaceOwner.session(shared))
            == "pvc-ws-thread-abcdef123456"
        )

    @pytest.mark.asyncio
    async def test_session_disabled_uses_emptydir_and_creates_no_pvc(self):
        """Mixed-fleet safety: one flag governs both kinds, and a cluster that
        hasn't flipped it keeps today's emptyDir behavior for sessions too."""
        p = self._provisioner(pvc_enabled=False)
        pod_body = {}
        core = self._capture_core(pod_body)
        core.create_namespaced_persistent_volume_claim = MagicMock()
        p._core_api = core

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            await p.create_workspace(WorkspaceOwner.session("abcdef123456-thread"))

        core.create_namespaced_persistent_volume_claim.assert_not_called()
        vols = {v["name"]: v for v in pod_body["spec"]["volumes"]}
        assert "emptyDir" in vols["workspace-data"]
        assert "persistentVolumeClaim" not in vols["workspace-data"]

    @pytest.mark.asyncio
    async def test_session_pvc_create_failure_aborts_before_pod(self):
        """Fail closed for sessions too — never downgrade to emptyDir on a PVC
        error, which would trade a visible failure for silent data loss."""
        p = self._provisioner(pvc_enabled=True)
        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_persistent_volume_claim = MagicMock(
            side_effect=Exception("PVC API down")
        )
        p._core_api = core

        result = await p.create_workspace(WorkspaceOwner.session("abcdef123456-thread"))

        assert result is False
        core.create_namespaced_pod.assert_not_called()
        updates = p._db.merge_thread_workspace_context.call_args_list[-1][0][1]
        assert updates["status"] == "failed"
        assert "PVC" in updates["error"]

    @pytest.mark.asyncio
    async def test_pvc_create_failure_aborts_before_pod(self):
        p = self._provisioner(pvc_enabled=True)
        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_persistent_volume_claim = MagicMock(
            side_effect=Exception("PVC API down")
        )
        p._core_api = core

        result = await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        assert result is False
        # No pod (or ConfigMap) provisioned once the prerequisite PVC failed
        core.create_namespaced_pod.assert_not_called()
        updates = p._db.merge_workspace_container_context.call_args_list[-1][0][1]
        assert updates["status"] == "failed"
        assert "PVC" in updates["error"]

    @pytest.mark.asyncio
    async def test_pvc_quota_403_fails_closed_with_capacity_log(self, caplog):
        # Capacity guard (Phase 3a): a ResourceQuota rejection surfaces as a 403.
        # It must fail closed (no pod, no emptyDir fallback — capacity exhaustion
        # never silently drops durability) AND log a distinct capacity-exceeded
        # line so an operator/alert can tell "fleet at capacity" from a genuine
        # infra failure (which logs the generic "Failed to create PVC").
        import logging

        p = self._provisioner(pvc_enabled=True)

        class _QuotaExc(Exception):
            status = 403
            body = "exceeded quota: srw-workspace-storage, requested: ..."

        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_persistent_volume_claim = MagicMock(
            side_effect=_QuotaExc()
        )
        p._core_api = core

        with caplog.at_level(logging.ERROR):
            result = await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        assert result is False
        core.create_namespaced_pod.assert_not_called()
        updates = p._db.merge_workspace_container_context.call_args_list[-1][0][1]
        assert updates["status"] == "failed"
        # Distinct, greppable capacity signal (not the generic "Failed to create")
        assert "capacity quota exceeded" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_create_pvc_returns_status_created_reused_none(self):
        # The recreate path keys the single-replica fallback on this: "reused"
        # (409) = a reattach (can wedge on a dead node), "created" = fresh.
        p = self._provisioner(pvc_enabled=True)

        class _Exc(Exception):
            def __init__(self, status):
                self.status = status

        p._core_api = MagicMock()
        p._core_api.create_namespaced_persistent_volume_claim = MagicMock()
        assert await p._create_pvc("pvc-x") == "created"
        p._core_api.create_namespaced_persistent_volume_claim = MagicMock(
            side_effect=_Exc(409)
        )
        assert await p._create_pvc("pvc-x") == "reused"
        p._core_api.create_namespaced_persistent_volume_claim = MagicMock(
            side_effect=_Exc(403)
        )
        assert await p._create_pvc("pvc-x") is None
        p._core_api.create_namespaced_persistent_volume_claim = MagicMock(
            side_effect=_Exc(500)
        )
        assert await p._create_pvc("pvc-x") is None

    @pytest.mark.asyncio
    async def test_pod_volume_attach_failing_matches_attach_events(self):
        p = self._provisioner(pvc_enabled=True)
        p._core_api = MagicMock()

        def _events(reason="", message=""):
            ev = MagicMock()
            ev.reason, ev.message = reason, message
            res = MagicMock()
            res.items = [ev]
            return res

        p._core_api.list_namespaced_event = MagicMock(
            return_value=_events(reason="FailedAttachVolume")
        )
        assert await p._pod_volume_attach_failing("pod-x") is True
        p._core_api.list_namespaced_event = MagicMock(
            return_value=_events(message="Multi-Attach error for volume pvc-...")
        )
        assert await p._pod_volume_attach_failing("pod-x") is True
        # An unrelated event must NOT look like a volume failure (no false discard)
        p._core_api.list_namespaced_event = MagicMock(
            return_value=_events(reason="Scheduled")
        )
        assert await p._pod_volume_attach_failing("pod-x") is False

    @pytest.mark.asyncio
    async def test_reattach_wedged_on_dead_node_falls_back_to_fresh_pvc(self):
        # Single-replica node-loss fallback (Phase 3b): a reattach (409) whose
        # volume can't attach (lone replica on a dead node) is DISCARDED and
        # recovered onto a fresh empty PVC, so the agent clone+checkpoint-resumes.
        p = self._provisioner(pvc_enabled=True)
        pod_body = {}
        p._core_api = self._capture_core(pod_body)
        p._create_pvc = AsyncMock(side_effect=["reused", "created"])  # reattach→fresh
        p._delete_pvc_and_wait = AsyncMock()
        p.delete_workspace = AsyncMock(return_value=True)
        p._pod_volume_attach_failing = AsyncMock(return_value=True)

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.side_effect = [None, "10.42.0.5"]  # wedged reattach, then fresh ready
            result = await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        assert result is True
        assert p._create_pvc.await_count == 2  # reattach attempt + fresh recreate
        p._delete_pvc_and_wait.assert_awaited_once()  # discarded the dead volume
        p.delete_workspace.assert_awaited_once()  # tore down the wedged pod
        merges = [
            c[0][1] for c in p._db.merge_workspace_container_context.call_args_list
        ]
        assert any(m.get("workspace_reset") for m in merges)  # observability flag
        assert any(m.get("status") == "ready" for m in merges)  # fresh pod came up

    @pytest.mark.asyncio
    async def test_reattach_not_ready_but_volume_ok_does_not_discard(self):
        # Guard: a slow/odd reattach that is NOT a volume-attach failure must keep
        # the existing PVC (status=creating), never discard data.
        p = self._provisioner(pvc_enabled=True)
        p._core_api = self._capture_core({})
        p._create_pvc = AsyncMock(return_value="reused")
        p._delete_pvc_and_wait = AsyncMock()
        p.delete_workspace = AsyncMock(return_value=True)
        p._pod_volume_attach_failing = AsyncMock(return_value=False)

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = None
            result = await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        assert result is True
        p._delete_pvc_and_wait.assert_not_awaited()
        p.delete_workspace.assert_not_awaited()
        assert p._create_pvc.await_count == 1  # no fresh recreate
        merges = [
            c[0][1] for c in p._db.merge_workspace_container_context.call_args_list
        ]
        assert any(m.get("status") == "creating" for m in merges)

    @pytest.mark.asyncio
    async def test_fresh_create_timeout_never_discards(self):
        # A first-time create ("created", not a reattach) that times out must
        # NEVER hit the fallback — there is no prior data to preserve/discard.
        p = self._provisioner(pvc_enabled=True)
        p._core_api = self._capture_core({})
        p._create_pvc = AsyncMock(return_value="created")
        p._pod_volume_attach_failing = AsyncMock(return_value=True)
        p._delete_pvc_and_wait = AsyncMock()

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = None
            await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        p._pod_volume_attach_failing.assert_not_awaited()  # gated off (not reattach)
        p._delete_pvc_and_wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_kill_switch_disables_discard(self):
        # WORKSPACE_REATTACH_FRESH_FALLBACK=false → pure reattach-or-creating,
        # never discards (operator kill-switch for the data-destructive path).
        p = self._provisioner(pvc_enabled=True)
        p._fresh_fallback_enabled = False
        p._core_api = self._capture_core({})
        p._create_pvc = AsyncMock(return_value="reused")
        p._pod_volume_attach_failing = AsyncMock(return_value=True)
        p._delete_pvc_and_wait = AsyncMock()

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = None
            await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        p._pod_volume_attach_failing.assert_not_awaited()  # short-circuited by flag
        p._delete_pvc_and_wait.assert_not_awaited()


class TestWorkspaceService:
    """Option 1: stable headless Service so reattach/recovery reconnects to a
    constant DNS name instead of an ephemeral pod IP."""

    @staticmethod
    def _provisioner(pvc_enabled: bool):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p._pvc_enabled = pvc_enabled
        p._pvc_size = "10Gi"
        p._db = MagicMock()
        p._db.merge_workspace_container_context = AsyncMock(return_value=True)
        p._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        p._db.execute = AsyncMock(return_value=None)
        return p

    @staticmethod
    def _ready_ctx(p):
        """The updates dict from the status=ready _set_context call."""
        for call in p._db.merge_workspace_container_context.call_args_list:
            updates = call[0][1]
            if updates.get("status") == "ready":
                return updates
        return {}

    @staticmethod
    def _ready_thread_ctx(p):
        """_ready_ctx for a session — _set_context routes threads to the other
        merge helper."""
        for call in p._db.merge_thread_workspace_context.call_args_list:
            updates = call[0][1]
            if updates.get("status") == "ready":
                return updates
        return {}

    @pytest.mark.asyncio
    async def test_pvc_mode_creates_headless_service_and_sets_dns_host(self):
        p = self._provisioner(pvc_enabled=True)
        svc_body = {}
        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_persistent_volume_claim = MagicMock()
        core.create_namespaced_service = lambda **kw: svc_body.update(
            kw.get("body", {})
        )
        p._core_api = core

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            ok = await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        assert ok is True
        # Headless Service named after the pod, selecting the workspace pod, ssh port
        assert svc_body["metadata"]["name"] == "workspace-abcdef123456"
        assert svc_body["spec"]["clusterIP"] == "None"
        assert svc_body["spec"]["selector"]["srw/job-id"] == "abcdef123456-rest"
        assert svc_body["spec"]["selector"]["app"] == "srw-workspace"
        assert 30022 in {pp["port"] for pp in svc_body["spec"]["ports"]}
        # The agent is handed the stable DNS, not the ephemeral IP
        assert (
            self._ready_ctx(p).get("host")
            == f"workspace-abcdef123456.{p._namespace}.svc.cluster.local"
        )

    @pytest.mark.asyncio
    async def test_pvc_mode_creates_headless_service_for_sessions_too(self):
        """A PVC-backed session outlives its pod by design, so it needs the
        stable DNS at least as much as a job: the agent caches its SSH dial
        target, and every idle-reap-then-resume hands it a new pod IP."""
        p = self._provisioner(pvc_enabled=True)
        svc_body = {}
        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_persistent_volume_claim = MagicMock()
        core.create_namespaced_service = lambda **kw: svc_body.update(
            kw.get("body", {})
        )
        p._core_api = core

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            ok = await p.create_workspace(WorkspaceOwner.session("abcdef123456-thread"))

        assert ok is True
        assert svc_body["metadata"]["name"] == "ws-thread-abcdef123456"
        assert svc_body["spec"]["clusterIP"] == "None"
        assert svc_body["spec"]["selector"]["srw/thread-id"] == "abcdef123456-thread"
        assert (
            self._ready_thread_ctx(p).get("host")
            == f"ws-thread-abcdef123456.{p._namespace}.svc.cluster.local"
        )

    @pytest.mark.asyncio
    async def test_disabled_creates_no_service_and_no_host(self):
        p = self._provisioner(pvc_enabled=False)
        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_service = MagicMock()
        p._core_api = core

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            await p.create_workspace(WorkspaceOwner.job("abcdef123456-rest"))

        core.create_namespaced_service.assert_not_called()
        # emptyDir workspaces keep the IP path (no stable host)
        assert "host" not in self._ready_ctx(p)

    @pytest.mark.asyncio
    async def test_disabled_creates_no_service_for_sessions_either(self):
        p = self._provisioner(pvc_enabled=False)
        core = MagicMock()
        core.create_namespaced_pod = MagicMock()
        core.create_namespaced_service = MagicMock()
        p._core_api = core

        with patch.object(p, "_wait_for_ready", new_callable=AsyncMock) as w:
            w.return_value = "10.42.0.100"
            await p.create_workspace(WorkspaceOwner.session("abcdef123456-thread"))

        core.create_namespaced_service.assert_not_called()
        assert "host" not in self._ready_thread_ctx(p)

    @pytest.mark.asyncio
    async def test_delete_service_idempotent_on_404(self):
        p = self._provisioner(pvc_enabled=True)
        core = MagicMock()
        not_found = type("ApiErr", (Exception,), {"status": 404})()
        core.delete_namespaced_service = MagicMock(side_effect=not_found)
        p._core_api = core
        assert await p._delete_service(WorkspaceOwner.job("abcdef123456-rest")) is True

    @pytest.mark.asyncio
    async def test_create_service_idempotent_on_409(self):
        p = self._provisioner(pvc_enabled=True)
        core = MagicMock()
        conflict = type("ApiErr", (Exception,), {"status": 409})()
        core.create_namespaced_service = MagicMock(side_effect=conflict)
        p._core_api = core
        assert await p._create_service(WorkspaceOwner.job("abcdef123456-rest")) is True


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

        result = await provisioner.delete_workspace(
            WorkspaceOwner.job("test-job-123456")
        )

        assert result is True
        mock_core_api.delete_namespaced_pod.assert_called_once()
        # Verify context set to deleted and the stale IP dropped
        context_call = provisioner._db.merge_workspace_container_context.call_args
        assert context_call[0][1] == {"status": "deleted", "pod_ip": None}

    @pytest.mark.asyncio
    async def test_delete_workspace_already_gone(self):
        """Deleting a non-existent pod returns True (idempotent) and still
        clears the stale container context — a pod that died on its own leaves
        status='ready' + a dead pod_ip behind otherwise."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_workspace_container_context = AsyncMock(return_value=True)

        mock_404 = MagicMock()
        mock_404.status = 404
        mock_core_api = MagicMock()
        mock_core_api.delete_namespaced_pod = MagicMock(side_effect=mock_404)
        provisioner._core_api = mock_core_api

        # Simulate K8s ApiException with status 404

        error = Exception("Not Found")
        error.status = 404
        mock_core_api.delete_namespaced_pod = MagicMock(side_effect=error)

        result = await provisioner.delete_workspace(
            WorkspaceOwner.job("test-job-123456")
        )
        assert result is True
        provisioner._db.merge_workspace_container_context.assert_awaited_once_with(
            "test-job-123456", {"status": "deleted", "pod_ip": None}
        )

    @pytest.mark.asyncio
    async def test_delete_workspace_not_available(self):
        """Returns False when K8s is not available."""
        from orchestrator.services.container_provisioner import (
            ContainerProvisioner,
        )

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = False

        result = await provisioner.delete_workspace(
            WorkspaceOwner.job("test-job-123456")
        )
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


class TestCreateWorkspaceTeardownRace:
    """Restore-path 409 resolution — the suspend→resume teardown race.

    Issue: session_agent_drift_drain_kills_idle_sessions (option c). A
    just-suspended session leaves its old pod (same deterministic name)
    Terminating inside its delete grace; a fast resume 409s on create.
    """

    @staticmethod
    def _conflict() -> Exception:
        err = Exception("Conflict")
        err.status = 409
        return err

    @staticmethod
    async def _sync_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _provisioner(self, monkeypatch):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._namespace = "test-ns"
        provisioner._db = AsyncMock()
        provisioner._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        monkeypatch.setattr(
            provisioner,
            "_resolve_network_tier",
            AsyncMock(return_value="internet-only"),
        )
        monkeypatch.setattr(provisioner, "_adopt_configmap", AsyncMock())
        monkeypatch.setattr(
            provisioner, "_wait_for_ready", AsyncMock(return_value="10.0.0.9")
        )
        return provisioner

    @pytest.mark.asyncio
    async def test_terminating_pod_waits_then_recreates(self, monkeypatch):
        """409 + incumbent Terminating → wait for teardown, then recreate fresh."""
        provisioner = self._provisioner(monkeypatch)

        fresh_pod = MagicMock()
        fresh_pod.metadata.uid = "uid-new"
        fresh_pod.metadata.name = "ws-thread-thread-abc12"
        create_seq = [self._conflict(), fresh_pod]

        def fake_create(**kwargs):
            item = create_seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        terminating = MagicMock()
        terminating.metadata.deletion_timestamp = "2026-06-12T10:00:00Z"

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(side_effect=fake_create)
        mock_core_api.read_namespaced_pod = MagicMock(return_value=terminating)
        provisioner._core_api = mock_core_api

        monkeypatch.setattr(
            provisioner, "_wait_for_pod_gone", AsyncMock(return_value=True)
        )
        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            ok = await provisioner.create_workspace(
                WorkspaceOwner.session("thread-abc12345")
            )

        assert ok is True
        # First create 409'd, second (post-teardown) succeeded.
        assert mock_core_api.create_namespaced_pod.call_count == 2
        provisioner._wait_for_pod_gone.assert_awaited_once()
        # Fresh pod → ConfigMap adopted.
        provisioner._adopt_configmap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_live_pod_is_idempotent_adopt(self, monkeypatch):
        """409 + incumbent live (no deletion ts) → idempotent, no recreate/adopt."""
        provisioner = self._provisioner(monkeypatch)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(side_effect=self._conflict())
        live = MagicMock()
        live.metadata.deletion_timestamp = None
        mock_core_api.read_namespaced_pod = MagicMock(return_value=live)
        provisioner._core_api = mock_core_api

        gone = AsyncMock(return_value=True)
        monkeypatch.setattr(provisioner, "_wait_for_pod_gone", gone)
        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            ok = await provisioner.create_workspace(
                WorkspaceOwner.session("thread-abc12345")
            )

        assert ok is True
        # No recreate (incumbent is live), no teardown wait.
        assert mock_core_api.create_namespaced_pod.call_count == 1
        gone.assert_not_awaited()
        # created_pod is None → adoption skipped (live pod owns its own CM).
        provisioner._adopt_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_teardown_timeout_fails_cleanly(self, monkeypatch):
        """409 + incumbent never finishes terminating → failed, no infinite retry."""
        provisioner = self._provisioner(monkeypatch)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(side_effect=self._conflict())
        terminating = MagicMock()
        terminating.metadata.deletion_timestamp = "2026-06-12T10:00:00Z"
        mock_core_api.read_namespaced_pod = MagicMock(return_value=terminating)
        provisioner._core_api = mock_core_api

        monkeypatch.setattr(
            provisioner, "_wait_for_pod_gone", AsyncMock(return_value=False)
        )
        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            ok = await provisioner.create_workspace(
                WorkspaceOwner.session("thread-abc12345")
            )

        assert ok is False
        # Single create attempt — we never recreate over a stuck terminator.
        assert mock_core_api.create_namespaced_pod.call_count == 1
        last = provisioner._db.merge_thread_workspace_context.call_args_list[-1][0][1]
        assert last["status"] == "failed"

    @pytest.mark.asyncio
    async def test_non_409_error_still_fails(self, monkeypatch):
        """A non-conflict API error keeps the original failure semantics."""
        provisioner = self._provisioner(monkeypatch)

        boom = Exception("API down")
        boom.status = 500
        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(side_effect=boom)
        mock_core_api.read_namespaced_pod = MagicMock()
        provisioner._core_api = mock_core_api

        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            ok = await provisioner.create_workspace(
                WorkspaceOwner.session("thread-abc12345")
            )

        assert ok is False
        # We never even looked at the incumbent — non-409 re-raises immediately.
        mock_core_api.read_namespaced_pod.assert_not_called()
        last = provisioner._db.merge_thread_workspace_context.call_args_list[-1][0][1]
        assert last["status"] == "failed"


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
