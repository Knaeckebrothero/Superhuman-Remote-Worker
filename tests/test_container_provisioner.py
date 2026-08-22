"""Tests for the container provisioner service."""

import asyncio
import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.workspace_lifecycle import WorkspaceOwner


_TEST_POD_UID = "11111111-1111-4111-8111-111111111111"
_TEST_RESOURCE_UID = "22222222-2222-4222-8222-222222222222"


def _pod_from_manifest(body, *, uid=_TEST_POD_UID, phase="Running"):
    metadata = body["metadata"]
    spec = body["spec"]
    container_status = SimpleNamespace(
        name="workspace",
        ready=True,
        state=SimpleNamespace(terminated=None),
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=metadata["name"],
            namespace=metadata["namespace"],
            uid=uid,
            labels=dict(metadata.get("labels") or {}),
            annotations=dict(metadata.get("annotations") or {}),
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(
            volumes=list(spec.get("volumes") or []),
            containers=list(spec.get("containers") or []),
            init_containers=list(spec.get("initContainers") or []),
            ephemeral_containers=list(spec.get("ephemeralContainers") or []),
        ),
        status=SimpleNamespace(
            phase=phase,
            pod_ip="10.42.0.100",
            container_statuses=[container_status],
            init_container_statuses=[],
            ephemeral_container_statuses=[],
        ),
    )


def _pvc_from_manifest(body, *, uid=_TEST_RESOURCE_UID):
    metadata = body["metadata"]
    spec = body["spec"]
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=metadata["name"],
            namespace=metadata["namespace"],
            uid=uid,
            labels=dict(metadata.get("labels") or {}),
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(
            access_modes=list(spec["accessModes"]),
            storage_class_name=spec["storageClassName"],
            volume_mode=spec.get("volumeMode", "Filesystem"),
            selector=spec.get("selector"),
            data_source=spec.get("dataSource"),
            data_source_ref=spec.get("dataSourceRef"),
        ),
    )


def _service_from_manifest(body, *, uid=_TEST_RESOURCE_UID):
    metadata = body["metadata"]
    spec = body["spec"]
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=metadata["name"],
            namespace=metadata["namespace"],
            uid=uid,
            labels=dict(metadata.get("labels") or {}),
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(
            cluster_ip=spec["clusterIP"],
            selector=dict(spec["selector"]),
            type="ClusterIP",
            ports=[
                SimpleNamespace(
                    name=port["name"],
                    port=port["port"],
                    target_port=port["targetPort"],
                    protocol=port.get("protocol", "TCP"),
                )
                for port in spec["ports"]
            ],
        ),
    )


def _owned_pod(
    owner,
    *,
    uid=_TEST_POD_UID,
    namespace="superhuman-remote-worker",
    component=None,
    pod_name=None,
    deleting=False,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name or owner.pod_name,
            namespace=namespace,
            uid=uid,
            deletion_timestamp="now" if deleting else None,
            labels={
                "app": "srw-workspace",
                "srw/component": component or owner.component_label,
                "srw.io/component": "agent-workspace",
                owner.label_key: owner.id,
            },
        ),
        status=SimpleNamespace(
            phase="Running",
            pod_ip="10.42.0.100",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )


class TestWorkspaceRuntimeAttestation:
    OWNER = WorkspaceOwner.job("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    RUNTIME = "11111111-1111-4111-8111-111111111111"
    FINGERPRINT = "SHA256:" + ("A" * 43)

    @classmethod
    def _provisioner(cls):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._core_api = MagicMock()
        provisioner._resolve_network_tier = AsyncMock(return_value="internet-only")
        manifest = provisioner._build_pod_manifest(
            cls.OWNER.pod_name,
            cls.OWNER,
            "workspace:test",
            "500m",
            "1Gi",
            "2",
            "4Gi",
            network_tier="internet-only",
        )
        provisioner._core_api.read_namespaced_pod.return_value = _pod_from_manifest(
            manifest,
            uid=cls.RUNTIME,
        )
        return provisioner

    @pytest.mark.asyncio
    async def test_attests_exact_job_backing_runtime_endpoint_and_host_key(self):
        provisioner = self._provisioner()
        backing_id = (
            "k8s-pod:superhuman-remote-worker:11111111-1111-4111-8111-111111111111"
        )
        provisioner._trusted_pod_ssh_identity = AsyncMock(
            return_value=(backing_id, self.FINGERPRINT, self.RUNTIME)
        )

        attested = await provisioner.attest_workspace_runtime(self.OWNER)

        assert attested.backing_id == backing_id
        assert attested.workspace_generation == self.RUNTIME
        assert attested.runtime_incarnation == self.RUNTIME
        assert attested.ssh_host_key_fingerprint == self.FINGERPRINT
        assert attested.host == "10.42.0.100"
        assert attested.pod_ip == "10.42.0.100"
        assert attested.port == 30022
        provisioner._trusted_pod_ssh_identity.assert_awaited_once_with(
            self.OWNER.pod_name,
            pvc_name=None,
            expected_owner=self.OWNER,
            expected_runtime_incarnation=self.RUNTIME,
            expected_network_tier="internet-only",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("backing_id", "fingerprint", "confirmed_runtime"),
        [
            (
                "k8s-pod:wrong:11111111-1111-4111-8111-111111111111",
                FINGERPRINT,
                RUNTIME,
            ),
            (
                "k8s-pod:superhuman-remote-worker:not-a-uuid",
                FINGERPRINT,
                RUNTIME,
            ),
            (
                "k8s-pod:superhuman-remote-worker:11111111-1111-4111-8111-111111111111",
                "SHA256:short",
                RUNTIME,
            ),
            (
                "k8s-pod:superhuman-remote-worker:11111111-1111-4111-8111-111111111111",
                FINGERPRINT,
                "22222222-2222-4222-8222-222222222222",
            ),
        ],
    )
    async def test_refuses_malformed_or_drifting_attestation(
        self, backing_id, fingerprint, confirmed_runtime
    ):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        provisioner = self._provisioner()
        provisioner._trusted_pod_ssh_identity = AsyncMock(
            return_value=(backing_id, fingerprint, confirmed_runtime)
        )

        with pytest.raises(WorkspaceRuntimeAuthorityError):
            await provisioner.attest_workspace_runtime(self.OWNER)


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

    @staticmethod
    def _owned(pod, owner=None):
        owner = owner or WorkspaceOwner.session("t1")
        pod.metadata.name = owner.pod_name
        pod.metadata.namespace = "superhuman-remote-worker"
        pod.metadata.labels = {
            "app": "srw-workspace",
            "srw/component": owner.component_label,
            "srw.io/component": "agent-workspace",
            owner.label_key: owner.id,
        }
        pod.metadata.uid = str(
            getattr(pod.metadata, "uid", None)
            if isinstance(getattr(pod.metadata, "uid", None), str)
            else "11111111-1111-4111-8111-111111111111"
        )
        return pod

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
        pod.metadata.deletion_timestamp = None
        p._core_api.read_namespaced_pod = MagicMock(return_value=self._owned(pod))
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is True

    @pytest.mark.asyncio
    async def test_true_only_for_expected_running_pod_uid(self):
        p = self._provisioner()
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.metadata.deletion_timestamp = None
        pod.metadata.uid = "11111111-1111-4111-8111-111111111111"
        p._core_api.read_namespaced_pod = MagicMock(return_value=self._owned(pod))

        assert (
            await p.workspace_pod_live(
                WorkspaceOwner.session("t1"),
                expected_runtime_incarnation=("11111111-1111-4111-8111-111111111111"),
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_false_for_same_name_replacement_pod_uid(self):
        p = self._provisioner()
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.metadata.deletion_timestamp = None
        pod.metadata.uid = "22222222-2222-4222-8222-222222222222"
        p._core_api.read_namespaced_pod = MagicMock(return_value=self._owned(pod))

        assert (
            await p.workspace_pod_live(
                WorkspaceOwner.session("t1"),
                expected_runtime_incarnation=("11111111-1111-4111-8111-111111111111"),
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_ambiguous_when_running_pod_is_terminating(self):
        p = self._provisioner()
        pod = MagicMock()
        pod.status.phase = "Running"
        pod.metadata.deletion_timestamp = "2026-08-11T09:00:00Z"
        pod.metadata.uid = "11111111-1111-4111-8111-111111111111"
        p._core_api.read_namespaced_pod = MagicMock(return_value=self._owned(pod))

        assert (
            await p.workspace_pod_live(
                WorkspaceOwner.session("t1"),
                expected_runtime_incarnation=("11111111-1111-4111-8111-111111111111"),
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_false_on_failed_tombstone(self):
        p = self._provisioner()
        pod = MagicMock()
        pod.status.phase = "Failed"
        pod.metadata.deletion_timestamp = None
        p._core_api.read_namespaced_pod = MagicMock(return_value=self._owned(pod))
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is False

    @pytest.mark.asyncio
    async def test_none_on_transient_error(self):
        p = self._provisioner()

        class _ApiExc(Exception):
            status = 503

        p._core_api.read_namespaced_pod = MagicMock(side_effect=_ApiExc())
        assert await p.workspace_pod_live(WorkspaceOwner.session("t1")) is None


class TestWorkspacePodAuthority:
    RUNTIME = "11111111-1111-4111-8111-111111111111"

    @staticmethod
    def _status(name: str, *, terminated: bool):
        return SimpleNamespace(
            name=name,
            state=SimpleNamespace(
                terminated=SimpleNamespace(exit_code=0) if terminated else None,
                running=None if terminated else SimpleNamespace(started_at="now"),
            ),
        )

    def _pod(
        self,
        *,
        regular_terminated: bool = True,
        init_terminated: bool | None = None,
        ephemeral_terminated: bool | None = None,
        deleting: bool = False,
    ):
        owner = WorkspaceOwner.session("t1")
        containers = [SimpleNamespace(name="workspace")]
        init_containers = (
            [SimpleNamespace(name="restartable-init")]
            if init_terminated is not None
            else []
        )
        ephemeral_containers = (
            [SimpleNamespace(name="debugger")]
            if ephemeral_terminated is not None
            else []
        )
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=owner.pod_name,
                namespace="superhuman-remote-worker",
                uid=self.RUNTIME,
                labels={
                    "app": "srw-workspace",
                    "srw/component": owner.component_label,
                    "srw.io/component": "agent-workspace",
                    owner.label_key: owner.id,
                },
                deletion_timestamp="now" if deleting else None,
            ),
            spec=SimpleNamespace(
                containers=containers,
                init_containers=init_containers,
                ephemeral_containers=ephemeral_containers,
            ),
            status=SimpleNamespace(
                phase="Failed",
                container_statuses=[
                    self._status("workspace", terminated=regular_terminated)
                ],
                init_container_statuses=(
                    [self._status("restartable-init", terminated=init_terminated)]
                    if init_terminated is not None
                    else []
                ),
                ephemeral_container_statuses=(
                    [self._status("debugger", terminated=ephemeral_terminated)]
                    if ephemeral_terminated is not None
                    else []
                ),
            ),
        )

    @staticmethod
    def _provisioner():
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._core_api = MagicMock()
        return provisioner

    @pytest.mark.asyncio
    async def test_all_regular_init_and_ephemeral_containers_terminal_is_proof(self):
        provisioner = self._provisioner()
        provisioner._core_api.read_namespaced_pod.return_value = self._pod(
            init_terminated=True,
            ephemeral_terminated=True,
            deleting=True,
        )

        assert (
            await provisioner.workspace_pod_authority(
                WorkspaceOwner.session("t1"),
                expected_runtime_incarnation=self.RUNTIME,
            )
            == "exact_terminal"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("init_terminated", "ephemeral_terminated"),
        [(False, None), (True, False)],
        ids=["restartable-init-running", "ephemeral-running"],
    )
    async def test_running_extra_container_keeps_terminal_authority_unknown(
        self, init_terminated, ephemeral_terminated
    ):
        provisioner = self._provisioner()
        provisioner._core_api.read_namespaced_pod.return_value = self._pod(
            init_terminated=init_terminated,
            ephemeral_terminated=ephemeral_terminated,
            deleting=True,
        )

        assert (
            await provisioner.workspace_pod_authority(
                WorkspaceOwner.session("t1"),
                expected_runtime_incarnation=self.RUNTIME,
            )
            == "unknown"
        )

    @pytest.mark.asyncio
    async def test_missing_status_for_declared_ephemeral_container_is_unknown(self):
        provisioner = self._provisioner()
        pod = self._pod(ephemeral_terminated=True)
        pod.status.ephemeral_container_statuses = []
        provisioner._core_api.read_namespaced_pod.return_value = pod

        assert (
            await provisioner.workspace_pod_authority(
                WorkspaceOwner.session("t1"),
                expected_runtime_incarnation=self.RUNTIME,
            )
            == "unknown"
        )


class TestReleaseWorkspace:
    """Owner-keyed release_workspace: snapshot (if ready) then delete pod, and —
    only when the caller says the work is truly over — its PVC."""

    def _prov(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        runtime = "11111111-1111-4111-8111-111111111111"
        p.get_workspace_status = AsyncMock(
            return_value={
                "pod_ip": "10.0.0.5",
                "ready": True,
                "runtime_incarnation": runtime,
            }
        )
        p.workspace_pod_live = AsyncMock(return_value=True)
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
        p.delete_workspace.assert_awaited_once_with(
            owner,
            expected_runtime_incarnation=("11111111-1111-4111-8111-111111111111"),
        )
        p.delete_workspace_pvc.assert_awaited_once_with(
            owner,
            require_exact_owner=True,
        )

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
            return_value={
                "pod_ip": None,
                "ready": False,
                "runtime_incarnation": ("11111111-1111-4111-8111-111111111111"),
            }
        )
        owner = WorkspaceOwner.session("t1")
        assert await p.release_workspace(owner) is True
        p._snapshot_service.capture_vm_snapshot.assert_not_awaited()
        p.delete_workspace.assert_awaited_once_with(
            owner,
            expected_runtime_incarnation=("11111111-1111-4111-8111-111111111111"),
        )

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
        p.delete_workspace.assert_awaited_once_with(
            owner,
            expected_runtime_incarnation=("11111111-1111-4111-8111-111111111111"),
        )
        p.delete_workspace_pvc.assert_not_called()
        # The Service follows the pod, not the volume: recreating it on the next
        # create_workspace is a 409-idempotent no-op, so it costs nothing to drop.
        p._delete_service.assert_awaited_once_with(
            owner,
            require_exact_owner=True,
        )

    @pytest.mark.asyncio
    async def test_reclaim_volume_defaults_to_true(self):
        """The permanent-delete path is the default — a caller that means
        "this work is over" needs no extra argument to reclaim storage."""
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        assert await p.release_workspace(owner) is True
        p.delete_workspace_pvc.assert_awaited_once_with(
            owner,
            require_exact_owner=True,
        )

    @pytest.mark.asyncio
    async def test_strict_release_waits_for_exact_uid_absence_before_cleanup(self):
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        runtime = "11111111-1111-4111-8111-111111111111"
        p.workspace_pod_live = AsyncMock(return_value=True)
        p.get_workspace_status.return_value = {
            "pod_ip": "10.0.0.5",
            "ready": True,
            "runtime_incarnation": runtime,
        }

        assert (
            await p.release_workspace(
                owner,
                reclaim_volume=False,
                capture_snapshot=False,
                expected_runtime_incarnation=runtime,
                strict=True,
            )
            is True
        )

        p.delete_workspace.assert_awaited_once_with(
            owner,
            expected_runtime_incarnation=runtime,
            wait_for_exact_absence=True,
            exact_absence_timeout_seconds=30.0,
        )


class TestReleaseAbsentWorkspace:
    """Absent cleanup requires a Kubernetes 404, never an ambiguous None."""

    def _prov(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p._core_api = MagicMock()
        p._db = MagicMock()
        p._set_context = AsyncMock()
        p.delete_workspace = AsyncMock(return_value=True)
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        p._delete_seed_configmap = AsyncMock(return_value=True)
        return p

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expected_runtime", ["runtime-a", None])
    async def test_exact_absent_cleans_context_metering_service_and_pvc(
        self, expected_runtime
    ):
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        error = Exception("Not Found")
        error.status = 404
        p._core_api.read_namespaced_pod = MagicMock(side_effect=error)

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.close_interval",
            new_callable=AsyncMock,
        ) as close_interval:
            assert (
                await p.release_absent_workspace(
                    owner,
                    reclaim_volume=True,
                    expected_runtime_incarnation=expected_runtime,
                    strict=True,
                )
                is True
            )

        p._set_context.assert_awaited_once_with(
            owner,
            {
                "status": "deleted",
                "pod_ip": None,
                "_runtime_incarnation": None,
            },
        )
        close_interval.assert_awaited_once_with(p._db, owner)
        p.delete_workspace.assert_not_awaited()
        p._delete_seed_configmap.assert_awaited_once_with(
            owner.pod_name,
            expected_owner=owner,
            expected_pod_uid=expected_runtime,
        )
        p.delete_workspace_pvc.assert_awaited_once_with(
            owner,
            require_exact_owner=True,
        )
        p._delete_service.assert_awaited_once_with(
            owner,
            require_exact_owner=True,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "authority",
        ["exact_live", "exact_terminal", "replacement", "unknown"],
    )
    async def test_non_absent_authority_has_no_cleanup_effects(self, authority):
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        p.workspace_pod_authority = AsyncMock(return_value=authority)

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.close_interval",
            new_callable=AsyncMock,
        ) as close_interval:
            assert (
                await p.release_absent_workspace(
                    owner,
                    reclaim_volume=True,
                    expected_runtime_incarnation="runtime-a",
                    strict=True,
                )
                is False
            )

        p.workspace_pod_authority.assert_awaited_once_with(
            owner,
            expected_runtime_incarnation="runtime-a",
        )
        p._set_context.assert_not_awaited()
        close_interval.assert_not_awaited()
        p.delete_workspace.assert_not_awaited()
        p._delete_seed_configmap.assert_not_awaited()
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [Exception("API 500"), TimeoutError()])
    async def test_api_failure_has_zero_mutations(self, error):
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        if not isinstance(error, TimeoutError):
            error.status = 500
        p._core_api.read_namespaced_pod = MagicMock(side_effect=error)

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.close_interval",
            new_callable=AsyncMock,
        ) as close_interval:
            assert (
                await p.release_absent_workspace(
                    owner,
                    reclaim_volume=True,
                    expected_runtime_incarnation="runtime-a",
                    strict=True,
                )
                is False
            )

        p._set_context.assert_not_awaited()
        close_interval.assert_not_awaited()
        p.delete_workspace.assert_not_awaited()
        p._delete_seed_configmap.assert_not_awaited()
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_seed_cleanup_failure_keeps_absent_cleanup_retryable(self):
        p = self._prov()
        owner = WorkspaceOwner.session("t1")
        p.workspace_pod_authority = AsyncMock(return_value="exact_absent")
        p._delete_seed_configmap.return_value = False

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.close_interval",
            new_callable=AsyncMock,
        ) as close_interval:
            assert (
                await p.release_absent_workspace(
                    owner,
                    reclaim_volume=True,
                    expected_runtime_incarnation="runtime-a",
                    strict=True,
                )
                is False
            )

        p._delete_seed_configmap.assert_awaited_once_with(
            owner.pod_name,
            expected_owner=owner,
            expected_pod_uid="runtime-a",
        )
        p._set_context.assert_not_awaited()
        close_interval.assert_not_awaited()
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()


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

        env = {item["name"]: item for item in container["env"]}
        assert env["SRW_WORKSPACE_OWNER_KIND"]["value"] == "job"
        assert env["SRW_WORKSPACE_OWNER_ID"]["value"] == "abc123-full-uuid"
        assert env["SRW_WORKSPACE_RUNTIME_UID"]["valueFrom"]["fieldRef"] == {
            "fieldPath": "metadata.uid"
        }

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
    the security posture described in knowledge-base/knowledge/features/hardened_container.md.
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
        assert "agent-host -c" in content
        # The only su -c drop is for code-server; sshd must not run under su.
        for line in content.splitlines():
            if "agent-host -c" in line:
                assert "sshd" not in line, (
                    f"sshd must run as root, not under su: {line!r}"
                )

    def test_entrypoint_tags_code_server_and_terminal_descendants(self):
        content = self._read_file("docker/workspace-entrypoint.sh")
        assert "SRW_WORKSPACE_OWNER_ID" in content
        assert "SRW_WORKSPACE_RUNTIME_UID" in content
        assert 'export SRW_WORKSPACE_PROCESS_TAG="v1:' in content
        assert (
            'CODE_SERVER_PROCESS_PREFIX="exec env SRW_WORKSPACE_PROCESS_TAG=' in content
        )

    def _entrypoint_authority_prefix(self) -> str:
        content = self._read_file("docker/workspace-entrypoint.sh")
        return content.split(
            "# ---------------------------------------------------------------------------\n# 1.",
            maxsplit=1,
        )[0]

    def test_legacy_entrypoint_without_authority_remains_untagged(self):
        script = self._entrypoint_authority_prefix()
        completed = subprocess.run(
            [
                "bash",
                "-c",
                script + '\nprintf "%s" "${SRW_WORKSPACE_PROCESS_TAG-unset}"',
            ],
            env={"PATH": os.environ.get("PATH", "")},
            text=True,
            capture_output=True,
            check=False,
        )

        assert completed.returncode == 0
        assert completed.stdout == "unset"

    def test_entrypoint_refuses_partial_process_authority(self):
        script = self._entrypoint_authority_prefix()
        completed = subprocess.run(
            ["bash", "-c", script],
            env={
                "PATH": os.environ.get("PATH", ""),
                "SRW_WORKSPACE_OWNER_KIND": "session",
            },
            text=True,
            capture_output=True,
            check=False,
        )

        assert completed.returncode == 78

    def test_entrypoint_exports_complete_process_authority(self):
        script = self._entrypoint_authority_prefix()
        owner = "11111111-1111-4111-8111-111111111111"
        runtime = "22222222-2222-4222-8222-222222222222"
        completed = subprocess.run(
            ["bash", "-c", script + '\nprintf "%s" "$SRW_WORKSPACE_PROCESS_TAG"'],
            env={
                "PATH": os.environ.get("PATH", ""),
                "SRW_WORKSPACE_OWNER_KIND": "session",
                "SRW_WORKSPACE_OWNER_ID": owner,
                "SRW_WORKSPACE_RUNTIME_UID": runtime,
            },
            text=True,
            capture_output=True,
            check=False,
        )

        assert completed.returncode == 0
        assert completed.stdout == f"v1:session:{owner}:{runtime}"

    @pytest.mark.parametrize(
        "malformed",
        [
            "deadbeef-",
            "deadbeef-1111-4111-8111-11111111111",
            "deadbeef-1111-4111-8111-1111111111111",
            "deadbeef-1111-4111-8111-11111111111g",
            "DEADBEEF-1111-4111-8111-111111111111",
        ],
    )
    def test_entrypoint_refuses_noncanonical_process_authority(self, malformed):
        script = self._entrypoint_authority_prefix()
        canonical = "11111111-1111-4111-8111-111111111111"
        for owner, runtime in (
            (malformed, canonical),
            (canonical, malformed),
        ):
            completed = subprocess.run(
                ["bash", "-c", script],
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SRW_WORKSPACE_OWNER_KIND": "session",
                    "SRW_WORKSPACE_OWNER_ID": owner,
                    "SRW_WORKSPACE_RUNTIME_UID": runtime,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            assert completed.returncode == 78


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
        mock_core_api.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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
        context_updates = [
            call.args[1]
            for call in provisioner._db.merge_workspace_container_context.call_args_list
        ]
        assert [update["status"] for update in context_updates] == ["created", "ready"]
        assert all(update["provisioner"] == "k8s" for update in context_updates)

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
        mock_core_api.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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


class _StrictCreationDB:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.valid = True
        self.claimed = True
        self.published = True
        self.completed = {
            "workspace_generation": "44444444-4444-4444-8444-444444444444"
        }

    async def validate_stateless_thread_workspace_creation_attempt(
        self, thread_id, **kwargs
    ):
        self.events.append(("validate", thread_id, kwargs))
        return self.valid

    async def claim_stateless_thread_workspace_creation_attempt(
        self, thread_id, **kwargs
    ):
        self.events.append(("claim", thread_id, kwargs))
        return self.claimed

    async def publish_stateless_thread_workspace_runtime(self, thread_id, **kwargs):
        self.events.append(("publish", thread_id, kwargs))
        return self.published

    async def complete_stateless_thread_workspace_creation(self, thread_id, **kwargs):
        self.events.append(("complete", thread_id, kwargs))
        return self.completed

    async def clear_stateless_thread_workspace_runtime_for_recreation(
        self, thread_id, **kwargs
    ):
        self.events.append(("clear", thread_id, kwargs))
        return True

    async def prepare_stateless_thread_workspace_creation(self, thread_id, **kwargs):
        self.events.append(("prepare", thread_id, kwargs))
        return {
            "state": "prepared",
            "creation": {
                "generation": kwargs["proposed_generation"],
                "mode": kwargs["mode"],
                "attempted": False,
                "replaces_uid": kwargs.get("expected_runtime_incarnation"),
            },
        }

    async def get_workspace_network_tier(self, *_args):
        return "internet-only"

    async def get_thread(self, *_args):
        return {"user_id": None}


class _DirectSessionLaneDB:
    def __init__(self, requires_authority):
        self.requires_authority = requires_authority

    async def stateless_thread_workspace_creation_requires_authority(self, _thread_id):
        return self.requires_authority


class _PinnedSessionContainerDB:
    """Minimal real-class DB double for legacy pinned session provisioning."""

    def __init__(self):
        self.merge_workspace_container_context = AsyncMock(return_value=True)
        self.merge_thread_workspace_context = AsyncMock(return_value=True)
        self.execute = AsyncMock(return_value=None)
        self.get_workspace_network_tier = AsyncMock(return_value=None)
        self.get_thread = AsyncMock(return_value=None)

    async def stateless_thread_workspace_creation_requires_authority(self, _thread_id):
        return False


class TestStrictStatelessWorkspaceCreation:
    THREAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    GENERATION = "11111111-2222-4333-8444-555555555555"
    RUNTIME = "66666666-7777-4888-8999-aaaaaaaaaaaa"

    @classmethod
    def _owner(cls):
        return WorkspaceOwner.session(cls.THREAD_ID)

    @classmethod
    def _pod(
        cls,
        *,
        generation=None,
        runtime=None,
        owner_id=None,
        pvc_name=None,
        seed_name=None,
        network_tier="internet-only",
    ):
        owner = cls._owner()
        container = SimpleNamespace(
            name="workspace",
            ready=True,
            state=SimpleNamespace(terminated=None),
            volume_mounts=[
                SimpleNamespace(
                    name="workspace-data",
                    mount_path="/home/agent-host",
                    read_only=False,
                    sub_path=None,
                    sub_path_expr=None,
                    mount_propagation=None,
                ),
                *(
                    [
                        SimpleNamespace(
                            name="code-server-config",
                            mount_path="/mnt/code-server-config",
                            read_only=True,
                            sub_path=None,
                            sub_path_expr=None,
                        )
                    ]
                    if seed_name
                    else []
                ),
            ],
            volume_devices=[],
            resources=SimpleNamespace(
                requests={"cpu": "500m", "memory": "1Gi"},
            ),
        )
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=owner.pod_name,
                namespace="superhuman-remote-worker",
                uid=runtime or cls.RUNTIME,
                labels={
                    "app": "srw-workspace",
                    owner.label_key: owner_id or owner.id,
                    "srw/component": owner.component_label,
                    "srw.io/component": "agent-workspace",
                    "srw.io/network-tier": network_tier,
                },
                annotations={
                    "srw.io/runtime-creation-generation": (generation or cls.GENERATION)
                },
                deletion_timestamp=None,
            ),
            spec=SimpleNamespace(
                containers=[container],
                init_containers=[],
                ephemeral_containers=[],
                volumes=[
                    SimpleNamespace(
                        name="workspace-data",
                        persistent_volume_claim=(
                            SimpleNamespace(claim_name=pvc_name, read_only=False)
                            if pvc_name
                            else None
                        ),
                        empty_dir=(SimpleNamespace() if not pvc_name else None),
                    ),
                    *(
                        [
                            SimpleNamespace(
                                name="code-server-config",
                                config_map=SimpleNamespace(name=seed_name),
                            )
                        ]
                        if seed_name
                        else []
                    ),
                ],
            ),
            status=SimpleNamespace(
                phase="Running",
                pod_ip="10.42.0.8",
                container_statuses=[container],
            ),
        )

    @classmethod
    def _claim(
        cls,
        provisioner,
        *,
        owner_id=None,
        component="workspace-pvc",
        deleting=False,
        access_modes=None,
        uid="77777777-8888-4999-8aaa-bbbbbbbbbbbb",
    ):
        owner = cls._owner()
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=f"pvc-ws-thread-{owner.id[:12]}",
                namespace=provisioner._namespace,
                uid=uid,
                labels={
                    "app": "srw-workspace",
                    "srw/component": component,
                    "srw.io/component": "agent-workspace",
                    owner.label_key: owner_id or owner.id,
                },
                deletion_timestamp="now" if deleting else None,
            ),
            spec=SimpleNamespace(
                access_modes=access_modes or ["ReadWriteOnce"],
                storage_class_name=provisioner._storage_class,
                volume_mode="Filesystem",
                selector=None,
                data_source=None,
                data_source_ref=None,
            ),
        )

    @classmethod
    def _seed(cls, provisioner, *, owner_id=None, generation=None, pod_uid=None):
        owner = cls._owner()
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=f"code-server-config-{owner.pod_name}",
                namespace=provisioner._namespace,
                uid="99999999-aaaa-4bbb-8ccc-dddddddddddd",
                resource_version="7",
                labels={
                    "app": "srw-workspace",
                    "srw/component": "workspace-seed",
                    "srw.io/component": "agent-workspace",
                    owner.label_key: owner_id or owner.id,
                },
                annotations={
                    "srw.io/runtime-creation-generation": (generation or cls.GENERATION)
                },
                deletion_timestamp=None,
                owner_references=[
                    SimpleNamespace(
                        name=owner.pod_name,
                        uid=pod_uid or cls.RUNTIME,
                        controller=True,
                    )
                ],
            )
        )

    @classmethod
    def _service(cls, provisioner, *, owner_id=None, protocol="TCP"):
        owner = cls._owner()
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=owner.pod_name,
                namespace=provisioner._namespace,
                uid="aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff",
                deletion_timestamp=None,
                labels={
                    "app": "srw-workspace",
                    "srw/component": "workspace-svc",
                    "srw.io/component": "agent-workspace",
                    owner.label_key: owner_id or owner.id,
                },
            ),
            spec=SimpleNamespace(
                cluster_ip="None",
                selector={"app": "srw-workspace", owner.label_key: owner.id},
                type="ClusterIP",
                ports=[
                    SimpleNamespace(
                        name=name,
                        port=port,
                        target_port=port,
                        protocol=protocol,
                    )
                    for name, port in (
                        ("ssh", 30022),
                        ("code-server", 38080),
                        ("cdp", 9222),
                    )
                ],
            ),
        )

    @classmethod
    def _provisioner(cls, db=None):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p._db = db or _StrictCreationDB()
        p._core_api = MagicMock()
        p._pvc_enabled = False
        p._resolve_network_tier = AsyncMock(return_value="internet-only")
        p._resolve_ide_seed_files = AsyncMock(return_value={})
        p._resolve_ide_extensions = AsyncMock(return_value=[])
        p._resolve_ide_needs_state = AsyncMock(return_value=True)
        p._create_seed_configmap = AsyncMock(return_value=None)
        p._delete_seed_configmap = AsyncMock(return_value=True)
        p._adopt_configmap = AsyncMock(return_value=True)
        p._seed_workspace_state = AsyncMock(return_value=None)
        return p

    @pytest.mark.asyncio
    async def test_attest_workspace_runtime_returns_exact_pvc_authority(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAttestation,
        )

        p = self._provisioner()
        owner = self._owner()
        pvc_name = f"pvc-ws-thread-{self.THREAD_ID[:12]}"
        backing_uid = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
        fingerprint = f"SHA256:{'A' * 43}"
        p._core_api.read_namespaced_pod.return_value = self._pod(pvc_name=pvc_name)
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pvc:{p._namespace}:{backing_uid}",
                fingerprint,
                self.RUNTIME,
            )
        )

        attestation = await p.attest_workspace_runtime(owner)

        assert attestation == WorkspaceRuntimeAttestation(
            backing_id=f"k8s-pvc:{p._namespace}:{backing_uid}",
            workspace_generation=backing_uid,
            runtime_incarnation=self.RUNTIME,
            ssh_host_key_fingerprint=fingerprint,
            host=f"{owner.pod_name}.{p._namespace}.svc.cluster.local",
            pod_ip="10.42.0.8",
        )
        p._trusted_pod_ssh_identity.assert_awaited_once_with(
            owner.pod_name,
            pvc_name=pvc_name,
            expected_owner=owner,
            expected_runtime_incarnation=self.RUNTIME,
            expected_network_tier="internet-only",
        )

    @pytest.mark.asyncio
    async def test_attest_workspace_runtime_rejects_post_probe_pod_uid_drift(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        pvc_name = f"pvc-ws-thread-{self.THREAD_ID[:12]}"
        p._core_api.read_namespaced_pod.return_value = self._pod(pvc_name=pvc_name)
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pvc:{p._namespace}:77777777-8888-4999-8aaa-bbbbbbbbbbbb",
                f"SHA256:{'A' * 43}",
                "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            )
        )

        with pytest.raises(WorkspaceRuntimeAuthorityError, match="Pod UID changed"):
            await p.attest_workspace_runtime(self._owner())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drift", ["owner", "phase", "pod-ip", "readiness"])
    async def test_attest_workspace_runtime_rejects_pod_authority_drift(self, drift):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        pod = self._pod(
            pvc_name=f"pvc-ws-thread-{self.THREAD_ID[:12]}",
            owner_id=(
                "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff" if drift == "owner" else None
            ),
        )
        if drift == "phase":
            pod.status.phase = "Pending"
        elif drift == "pod-ip":
            pod.status.pod_ip = ""
        elif drift == "readiness":
            pod.status.container_statuses[0].ready = False
        p._core_api.read_namespaced_pod.return_value = pod
        p._trusted_pod_ssh_identity = AsyncMock()

        with pytest.raises(WorkspaceRuntimeAuthorityError):
            await p.attest_workspace_runtime(self._owner())

        p._trusted_pod_ssh_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("backing_id", "fingerprint", "message"),
        [
            (
                "k8s-pod:superhuman-remote-worker:77777777-8888-4999-8aaa-bbbbbbbbbbbb",
                f"SHA256:{'A' * 43}",
                "backing identity is malformed",
            ),
            (
                "k8s-pvc:superhuman-remote-worker:not-a-uuid",
                f"SHA256:{'A' * 43}",
                "workspace backing UID is invalid",
            ),
            (
                "k8s-pvc:superhuman-remote-worker:77777777-8888-4999-8aaa-bbbbbbbbbbbb",
                "SHA256:short",
                "fingerprint is malformed",
            ),
        ],
    )
    async def test_attest_workspace_runtime_rejects_malformed_identity_material(
        self, backing_id, fingerprint, message
    ):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        pvc_name = f"pvc-ws-thread-{self.THREAD_ID[:12]}"
        p._core_api.read_namespaced_pod.return_value = self._pod(pvc_name=pvc_name)
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(backing_id, fingerprint, self.RUNTIME)
        )

        with pytest.raises(WorkspaceRuntimeAuthorityError, match=message):
            await p.attest_workspace_runtime(self._owner())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authority", [True, None])
    async def test_direct_session_create_without_nonce_refuses_before_effects(
        self, authority
    ):
        p = self._provisioner(_DirectSessionLaneDB(authority))

        assert await p.create_workspace(self._owner()) is False

        p._resolve_network_tier.assert_not_awaited()
        p._create_seed_configmap.assert_not_awaited()
        p._core_api.create_namespaced_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_strict_stateless_fresh_never_deletes_truncated_pvc_name(self):
        p = self._provisioner()
        p._pvc_enabled = True
        p._delete_pvc_and_wait = AsyncMock()

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
                fresh=True,
            )
            is False
        )

        p._delete_pvc_and_wait.assert_not_awaited()
        p._resolve_network_tier.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fresh_create_claims_once_then_publishes_uid_before_ready(self):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        pod = self._pod()
        created_body = {}

        def create_namespaced_pod(**kwargs):
            events.append(("create",))
            created_body.update(kwargs["body"])
            return pod

        p._core_api.create_namespaced_pod = create_namespaced_pod

        async def wait(*_args, **kwargs):
            events.append(("wait", kwargs))
            return "10.42.0.8"

        p._wait_for_ready = AsyncMock(side_effect=wait)
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pod:{p._namespace}:{self.RUNTIME}",
                "SHA256:trusted",
                self.RUNTIME,
            )
        )

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ) as open_interval:
            assert (
                await p.create_workspace(
                    self._owner(),
                    cpu="9",
                    memory="99Gi",
                    stateless_creation_generation=self.GENERATION,
                    allow_stateless_create=True,
                )
                is True
            )

        open_interval.assert_awaited_once_with(
            p._db,
            self._owner(),
            tier="sandbox",
            cpu="500m",
            memory="1Gi",
        )

        names = [event[0] for event in events]
        assert names.index("validate") < names.index("claim")
        assert names.index("claim") + 1 == names.index("create")
        assert names.index("create") < names.index("publish") < names.index("wait")
        assert (
            created_body["metadata"]["annotations"][
                "srw.io/runtime-creation-generation"
            ]
            == self.GENERATION
        )
        publish = next(event for event in events if event[0] == "publish")
        assert publish[2]["runtime_incarnation"] == self.RUNTIME
        wait_kwargs = p._wait_for_ready.await_args.kwargs
        assert wait_kwargs["expected_runtime_incarnation"] == self.RUNTIME
        assert wait_kwargs["expected_creation_generation"] == self.GENERATION
        p._resolve_ide_needs_state.assert_not_awaited()
        p._seed_workspace_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_conflict_adopts_only_matching_nonce_pod_once(self):
        class _Conflict(Exception):
            status = 409

        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)

        def conflict(**_kwargs):
            events.append(("create",))
            raise _Conflict()

        p._core_api.create_namespaced_pod.side_effect = conflict
        p._core_api.read_namespaced_pod.return_value = self._pod()

        async def wait(*_args, **_kwargs):
            events.append(("wait",))
            return "10.42.0.8"

        p._wait_for_ready = AsyncMock(side_effect=wait)
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pod:{p._namespace}:{self.RUNTIME}",
                "SHA256:trusted",
                self.RUNTIME,
            )
        )

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ) as open_interval:
            assert (
                await p.create_workspace(
                    self._owner(),
                    cpu="9",
                    memory="99Gi",
                    stateless_creation_generation=self.GENERATION,
                    allow_stateless_create=True,
                )
                is True
            )

        assert p._core_api.create_namespaced_pod.call_count == 1
        assert p._core_api.read_namespaced_pod.call_count == 1
        names = [event[0] for event in events]
        assert names.index("create") < names.index("publish") < names.index("wait")
        open_interval.assert_awaited_once_with(
            p._db,
            self._owner(),
            tier="sandbox",
            cpu="500m",
            memory="1Gi",
        )

    @pytest.mark.asyncio
    async def test_malformed_accepted_pod_requests_fail_before_runtime_publish(self):
        events = []
        p = self._provisioner(_StrictCreationDB(events))
        pod = self._pod()
        pod.spec.containers[0].resources.requests = {"cpu": "500m"}
        p._core_api.create_namespaced_pod.return_value = pod
        p._wait_for_ready = AsyncMock()

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
            )
            is False
        )
        assert not any(event[0] == "publish" for event in events)
        p._wait_for_ready.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "drift",
        [
            "generation",
            "owner",
            "deleting",
            "missing-uid",
            "malformed-uid",
            "gone-after-conflict",
        ],
    )
    async def test_create_conflict_never_adopts_ambiguous_name(self, drift):
        class _Conflict(Exception):
            status = 409

        class _NotFound(Exception):
            status = 404

        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p._core_api.create_namespaced_pod.side_effect = _Conflict()
        pod = self._pod()
        if drift == "generation":
            pod.metadata.annotations["srw.io/runtime-creation-generation"] = (
                "22222222-3333-4444-8555-666666666666"
            )
        elif drift == "owner":
            pod.metadata.labels[self._owner().label_key] = (
                "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
            )
        elif drift == "deleting":
            pod.metadata.deletion_timestamp = "now"
        elif drift == "missing-uid":
            pod.metadata.uid = None
        elif drift == "malformed-uid":
            pod.metadata.uid = "not-a-uuid"
        if drift == "gone-after-conflict":
            p._core_api.read_namespaced_pod.side_effect = _NotFound()
        else:
            p._core_api.read_namespaced_pod.return_value = pod
        p._wait_for_ready = AsyncMock()

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
            )
            is False
        )

        assert p._core_api.create_namespaced_pod.call_count == 1
        assert not any(event[0] == "publish" for event in events)
        p._wait_for_ready.assert_not_awaited()
        p._delete_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_after_pod_acceptance_retains_seed_for_exact_retry(self):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        seed_name = f"code-server-config-{self._owner().pod_name}"
        p._create_seed_configmap.return_value = seed_name
        pod = self._pod(seed_name=seed_name)
        p._core_api.read_namespaced_config_map.return_value = self._seed(p)

        def accepted_then_timeout(**_kwargs):
            events.append(("create",))
            p._core_api.read_namespaced_pod.return_value = pod
            raise TimeoutError("response lost after apiserver acceptance")

        p._core_api.create_namespaced_pod.side_effect = accepted_then_timeout
        p._wait_for_ready = AsyncMock(return_value="10.42.0.8")
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pod:{p._namespace}:{self.RUNTIME}",
                "SHA256:trusted",
                self.RUNTIME,
            )
        )

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
            )
            is False
        )
        assert p._core_api.create_namespaced_pod.call_count == 1
        p._delete_seed_configmap.assert_not_awaited()

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ):
            assert (
                await p.create_workspace(
                    self._owner(),
                    stateless_creation_generation=self.GENERATION,
                    allow_stateless_create=False,
                )
                is True
            )

        assert p._core_api.create_namespaced_pod.call_count == 1
        names = [event[0] for event in events]
        assert names.count("claim") == 1
        assert names.index("create") < names.index("publish") < names.index("complete")
        p._delete_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pvc_service_failure_holds_ready_then_continuation_retries(self):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p._pvc_enabled = True
        p._create_pvc = AsyncMock(return_value="created")
        p._create_service = AsyncMock(side_effect=[False, True])
        pod = self._pod(pvc_name=f"pvc-ws-thread-{self.THREAD_ID[:12]}")
        p._core_api.create_namespaced_pod.return_value = pod
        p._core_api.read_namespaced_pod.return_value = pod
        p._core_api.read_namespaced_persistent_volume_claim.return_value = self._claim(
            p
        )
        p._wait_for_ready = AsyncMock(return_value="10.42.0.8")
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                "k8s-pvc:agent-workspaces:pvc-uid",
                "SHA256:trusted",
                self.RUNTIME,
            )
        )

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
            )
            is False
        )
        assert p._core_api.create_namespaced_pod.call_count == 1
        p._wait_for_ready.assert_not_awaited()
        assert not any(event[0] == "complete" for event in events)

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ):
            assert (
                await p.create_workspace(
                    self._owner(),
                    stateless_creation_generation=self.GENERATION,
                    allow_stateless_create=False,
                )
                is True
            )

        assert p._core_api.create_namespaced_pod.call_count == 1
        assert p._create_seed_configmap.await_count == 1
        assert p._create_service.await_count == 2
        p._wait_for_ready.assert_awaited_once()
        assert [event[0] for event in events].count("complete") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "drift",
        [
            "owner",
            "opposite-owner",
            "component",
            "deleting",
            "spec",
            "uid",
            "read-error",
        ],
    )
    async def test_pvc_name_collision_never_reaches_one_shot_pod_claim(self, drift):
        class _Conflict(Exception):
            status = 409

        class _ReadError(Exception):
            status = 503

        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p._pvc_enabled = True
        p._core_api.create_namespaced_persistent_volume_claim.side_effect = _Conflict()
        claim_kwargs = {}
        if drift == "owner":
            claim_kwargs["owner_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
        elif drift == "component":
            claim_kwargs["component"] = "other-component"
        elif drift == "deleting":
            claim_kwargs["deleting"] = True
        elif drift == "spec":
            claim_kwargs["access_modes"] = ["ReadWriteMany"]
        elif drift == "uid":
            claim_kwargs["uid"] = "not-a-uuid"
        if drift == "read-error":
            p._core_api.read_namespaced_persistent_volume_claim.side_effect = (
                _ReadError()
            )
        else:
            claim = self._claim(p, **claim_kwargs)
            if drift == "opposite-owner":
                claim.metadata.labels["srw/job-id"] = self.THREAD_ID
            p._core_api.read_namespaced_persistent_volume_claim.return_value = claim

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
            )
            is False
        )

        assert not any(event[0] == "claim" for event in events)
        p._core_api.create_namespaced_pod.assert_not_called()
        p._create_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_reused_pvc_owner_is_accepted_before_pod_claim(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.create_namespaced_persistent_volume_claim.side_effect = _Conflict()
        p._core_api.read_namespaced_persistent_volume_claim.return_value = self._claim(
            p
        )

        assert (
            await p._create_pvc(
                f"pvc-ws-thread-{self.THREAD_ID[:12]}",
                labels={self._owner().label_key: self.THREAD_ID},
                expected_owner=self._owner(),
            )
            == "reused"
        )

    @pytest.mark.asyncio
    async def test_cancellation_after_attempt_submission_retains_seed(self):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p._create_seed_configmap.return_value = "seed-config"
        entered = threading.Event()
        release = threading.Event()

        def blocked_create(**_kwargs):
            events.append(("create",))
            entered.set()
            release.wait(timeout=5)
            return self._pod()

        p._core_api.create_namespaced_pod.side_effect = blocked_create
        task = asyncio.create_task(
            p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=True,
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0)

        assert p._core_api.create_namespaced_pod.call_count == 1
        p._delete_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_cas_readiness_authority_error_retains_seed(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        p._core_api.create_namespaced_pod.return_value = self._pod()
        p._wait_for_ready = AsyncMock(
            side_effect=WorkspaceRuntimeAuthorityError("UID changed while waiting")
        )

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ):
            assert (
                await p.create_workspace(
                    self._owner(),
                    stateless_creation_generation=self.GENERATION,
                    allow_stateless_create=True,
                )
                is False
            )

        p._delete_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attempted_404_never_reissues_create(self):
        class _NotFound(Exception):
            status = 404

        p = self._provisioner()
        p._core_api.read_namespaced_pod.side_effect = _NotFound()

        assert (
            await p.create_workspace(
                self._owner(),
                stateless_creation_generation=self.GENERATION,
                allow_stateless_create=False,
            )
            is False
        )

        p._core_api.create_namespaced_pod.assert_not_called()
        p._create_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drift", ["generation", "owner", "runtime"])
    async def test_attempted_replacement_or_annotation_drift_never_adopts(self, drift):
        p = self._provisioner()
        pod_kwargs = {}
        if drift == "generation":
            pod_kwargs["generation"] = "22222222-3333-4444-8555-666666666666"
        elif drift == "owner":
            pod_kwargs["owner_id"] = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        elif drift == "runtime":
            pod_kwargs["runtime"] = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        p._core_api.read_namespaced_pod.return_value = self._pod(**pod_kwargs)

        expected_runtime = self.RUNTIME if drift == "runtime" else None
        assert (
            await p.continue_stateless_workspace_creation(
                self._owner(),
                generation=self.GENERATION,
                expected_runtime_incarnation=expected_runtime,
            )
            is False
        )
        p._core_api.create_namespaced_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_attempted_pod_is_published_and_continued_without_create(
        self,
    ):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p._core_api.read_namespaced_pod.return_value = self._pod()

        async def wait(*_args, **_kwargs):
            events.append(("wait",))
            return "10.42.0.8"

        p._wait_for_ready = AsyncMock(side_effect=wait)
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pod:{p._namespace}:{self.RUNTIME}",
                "SHA256:trusted",
                self.RUNTIME,
            )
        )

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ):
            assert (
                await p.continue_stateless_workspace_creation(
                    self._owner(),
                    generation=self.GENERATION,
                    expected_runtime_incarnation=None,
                )
                is True
            )

        names = [event[0] for event in events]
        assert names.index("publish") < names.index("wait") < names.index("complete")
        p._core_api.create_namespaced_pod.assert_not_called()
        p._seed_workspace_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_terminal_delete_reaches_404_before_uid_clear(self):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p.workspace_pod_authority = AsyncMock(return_value="exact_terminal")

        def delete(**kwargs):
            events.append(("delete", kwargs))

        p._core_api.delete_namespaced_pod = delete

        async def wait(*_args, **kwargs):
            events.append(("wait_absent", kwargs))
            return True

        p._wait_for_exact_workspace_pod_absent = AsyncMock(side_effect=wait)

        generation = await p.prepare_stateless_workspace_recreation(
            self._owner(),
            expected_runtime_incarnation=self.RUNTIME,
            mode="create",
        )

        assert isinstance(generation, str)
        names = [event[0] for event in events]
        assert names == ["prepare", "validate", "delete", "wait_absent", "clear"]
        delete_kwargs = next(event[1] for event in events if event[0] == "delete")
        assert delete_kwargs["body"] == {"preconditions": {"uid": self.RUNTIME}}
        clear = next(event for event in events if event[0] == "clear")
        assert clear[2] == {
            "generation": generation,
            "expected_runtime_incarnation": self.RUNTIME,
        }
        assert "claim" not in names

    @pytest.mark.asyncio
    async def test_terminal_delete_timeout_retains_uid_until_exact_absent_retry(self):
        events = []
        db = _StrictCreationDB(events)
        p = self._provisioner(db)
        p._core_api.delete_namespaced_pod.return_value = None
        p._wait_for_exact_workspace_pod_absent = AsyncMock(return_value=False)

        assert (
            await p._delete_prepared_terminal_workspace_runtime(
                self._owner(),
                generation=self.GENERATION,
                expected_runtime_incarnation=self.RUNTIME,
            )
            is False
        )
        assert not any(event[0] == "clear" for event in events)

        p.workspace_pod_authority = AsyncMock(return_value="exact_absent")
        assert await p.finalize_stateless_workspace_recreation_deletion(
            self._owner(),
            generation=self.GENERATION,
            expected_runtime_incarnation=self.RUNTIME,
        )
        assert [event[0] for event in events].count("clear") == 1

    @pytest.mark.asyncio
    async def test_ready_probe_rejects_uid_replacement_after_ssh(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        p._core_api.read_namespaced_pod.side_effect = [
            self._pod(),
            self._pod(runtime="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"),
        ]
        with (
            patch(
                "orchestrator.services.container_provisioner.resolve_ssh_key_path",
                return_value="/run/secrets/key",
            ),
            patch(
                "orchestrator.services.container_provisioner.workspace_private_key_fingerprint",
                return_value="SHA256:key",
            ),
            patch(
                "orchestrator.services.container_provisioner.wait_for_agent_ssh",
                new=AsyncMock(return_value=(True, 1, "")),
            ),
        ):
            with pytest.raises(WorkspaceRuntimeAuthorityError):
                await p._wait_for_ready(
                    self._owner().pod_name,
                    timeout=1,
                    expected_owner=self._owner(),
                    expected_runtime_incarnation=self.RUNTIME,
                    expected_creation_generation=self.GENERATION,
                )

    @pytest.mark.asyncio
    async def test_host_key_probe_rejects_uid_replacement_after_exec(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        p._core_api.read_namespaced_pod.side_effect = [
            self._pod(),
            self._pod(runtime="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"),
        ]
        with patch(
            "orchestrator.services.container_provisioner.k8s_stream",
            return_value="256 SHA256:trusted host (ED25519)",
        ):
            with pytest.raises(WorkspaceRuntimeAuthorityError):
                await p._trusted_pod_ssh_identity(
                    self._owner().pod_name,
                    expected_owner=self._owner(),
                    expected_runtime_incarnation=self.RUNTIME,
                    expected_creation_generation=self.GENERATION,
                )

    @pytest.mark.asyncio
    async def test_host_key_probe_rechecks_full_pvc_owner_and_uid_after_exec(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        pvc_name = f"pvc-ws-thread-{self.THREAD_ID[:12]}"
        p._core_api.read_namespaced_pod.side_effect = [
            self._pod(pvc_name=pvc_name),
            self._pod(pvc_name=pvc_name),
        ]
        exact_claim = self._claim(p)
        replacement_claim = self._claim(
            p,
            owner_id="aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff",
            uid="88888888-9999-4aaa-8bbb-cccccccccccc",
        )
        p._core_api.read_namespaced_persistent_volume_claim.side_effect = [
            exact_claim,
            replacement_claim,
        ]
        with patch(
            "orchestrator.services.container_provisioner.k8s_stream",
            return_value="256 SHA256:trusted host (ED25519)",
        ):
            with pytest.raises(WorkspaceRuntimeAuthorityError):
                await p._trusted_pod_ssh_identity(
                    self._owner().pod_name,
                    pvc_name=pvc_name,
                    expected_owner=self._owner(),
                    expected_runtime_incarnation=self.RUNTIME,
                    expected_creation_generation=self.GENERATION,
                )

    @pytest.mark.parametrize(
        "drift",
        [
            "pvc-readonly",
            "mount-readonly",
            "subpath",
            "subpath-expr",
            "mount-propagation",
            "workspace-device",
            "pvc-alias",
            "seed-alias",
            "sidecar-mount",
            "init-device",
            "ephemeral-seed-mount",
        ],
    )
    def test_exact_pod_storage_rejects_aliases_and_secondary_consumers(self, drift):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        pvc_name = f"pvc-ws-thread-{self.THREAD_ID[:12]}"
        seed_name = f"code-server-config-{self._owner().pod_name}"
        pod = self._pod(pvc_name=pvc_name, seed_name=seed_name)
        workspace = pod.spec.containers[0]
        workspace_volume = pod.spec.volumes[0]
        if drift == "pvc-readonly":
            workspace_volume.persistent_volume_claim.read_only = True
        elif drift == "mount-readonly":
            workspace.volume_mounts[0].read_only = True
        elif drift == "subpath":
            workspace.volume_mounts[0].sub_path = "foreign"
        elif drift == "subpath-expr":
            workspace.volume_mounts[0].sub_path_expr = "$(FOREIGN)"
        elif drift == "mount-propagation":
            workspace.volume_mounts[0].mount_propagation = "HostToContainer"
        elif drift == "workspace-device":
            workspace.volume_devices.append(SimpleNamespace(name="workspace-data"))
        elif drift == "pvc-alias":
            pod.spec.volumes.append(
                SimpleNamespace(
                    name="steal",
                    persistent_volume_claim=SimpleNamespace(
                        claim_name=pvc_name,
                        read_only=False,
                    ),
                )
            )
        elif drift == "seed-alias":
            pod.spec.volumes.append(
                SimpleNamespace(
                    name="seed-steal",
                    config_map=SimpleNamespace(name=seed_name),
                )
            )
        else:
            secondary = SimpleNamespace(
                name="sidecar",
                volume_mounts=[],
                volume_devices=[],
            )
            if drift == "sidecar-mount":
                secondary.volume_mounts.append(SimpleNamespace(name="workspace-data"))
                pod.spec.containers.append(secondary)
            elif drift == "init-device":
                secondary.volume_devices.append(SimpleNamespace(name="workspace-data"))
                pod.spec.init_containers.append(secondary)
            else:
                secondary.volume_mounts.append(
                    SimpleNamespace(name="code-server-config")
                )
                pod.spec.ephemeral_containers.append(secondary)

        with pytest.raises(WorkspaceRuntimeAuthorityError):
            p._require_stateless_pod_storage_binding(
                pod,
                owner=self._owner(),
                expected_pvc_name=pvc_name,
                expected_seed_configmap=seed_name,
            )

    def test_exact_pod_storage_allows_unrelated_sidecar_volume(self):
        p = self._provisioner()
        pod = self._pod()
        pod.spec.volumes.append(
            SimpleNamespace(name="scratch", empty_dir=SimpleNamespace())
        )
        pod.spec.containers.append(
            SimpleNamespace(
                name="metrics",
                volume_mounts=[SimpleNamespace(name="scratch")],
                volume_devices=[],
            )
        )

        assert (
            p._require_stateless_pod_storage_binding(
                pod,
                owner=self._owner(),
                expected_pvc_name=None,
                expected_seed_configmap=None,
            )
            is None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("configured_pvc", [False, True])
    async def test_attempted_continuation_uses_immutable_pod_storage_plan(
        self, configured_pvc
    ):
        events = []
        p = self._provisioner(_StrictCreationDB(events))
        actual_pvc = (
            f"pvc-ws-thread-{self.THREAD_ID[:12]}" if not configured_pvc else None
        )
        p._pvc_enabled = configured_pvc
        pod = self._pod(pvc_name=actual_pvc)
        p._core_api.read_namespaced_pod.return_value = pod
        if actual_pvc:
            claim = self._claim(p)
            claim.spec.storage_class_name = "old-storage-class"
            p._storage_class = "new-storage-class"
            p._core_api.read_namespaced_persistent_volume_claim.return_value = claim
        p._create_service = AsyncMock(return_value=True)
        p._wait_for_ready = AsyncMock(return_value="10.42.0.8")
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                (
                    f"k8s-pvc:{p._namespace}:{_TEST_RESOURCE_UID}"
                    if actual_pvc
                    else f"k8s-pod:{p._namespace}:{self.RUNTIME}"
                ),
                "SHA256:trusted",
                self.RUNTIME,
            )
        )

        with patch(
            "orchestrator.services.container_provisioner.workspace_metering.open_interval",
            new=AsyncMock(return_value=None),
        ) as open_interval:
            assert await p.continue_stateless_workspace_creation(
                self._owner(),
                generation=self.GENERATION,
                expected_runtime_incarnation=self.RUNTIME,
                cpu="9",
                memory="99Gi",
            )

        p._core_api.create_namespaced_pod.assert_not_called()
        open_interval.assert_awaited_once_with(
            p._db,
            self._owner(),
            tier="sandbox",
            cpu="500m",
            memory="1Gi",
        )
        if actual_pvc:
            p._create_service.assert_awaited_once()
            assert (
                p._trusted_pod_ssh_identity.await_args.kwargs[
                    "expected_pvc_storage_class"
                ]
                == "old-storage-class"
            )
        else:
            p._create_service.assert_not_awaited()


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
        provisioner._core_api.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )

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
        p._db = _PinnedSessionContainerDB()
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                "k8s-pod:superhuman-remote-worker:11111111-1111-4111-8111-111111111111",
                "SHA256:trusted",
                _TEST_POD_UID,
            )
        )
        return p

    @staticmethod
    def _capture_core(pod_body: dict, pvc_body: dict | None = None):
        core = MagicMock()
        service_body = {}

        def create_pod(**kw):
            pod_body.update(kw.get("body", {}))
            return _pod_from_manifest(kw["body"])

        core.create_namespaced_pod = create_pod
        if pvc_body is not None:

            def create_claim(**kw):
                pvc_body.update(kw.get("body", {}))
                return _pvc_from_manifest(kw["body"])

            core.create_namespaced_persistent_volume_claim = create_claim

        def create_service(**kw):
            service_body.update(kw["body"])
            return _service_from_manifest(kw["body"])

        core.create_namespaced_service = create_service
        core.read_namespaced_persistent_volume_claim = lambda **kw: (
            _pvc_from_manifest(pvc_body)
        )
        core.read_namespaced_service = lambda **kw: _service_from_manifest(service_body)
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
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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
        pvc_body = {
            "metadata": {
                "name": "pvc-workspace-abcdef123456",
                "namespace": p._namespace,
                "labels": {
                    "app": "srw-workspace",
                    "srw/component": "workspace-pvc",
                    "srw.io/component": "agent-workspace",
                    "srw/job-id": "abcdef123456-rest",
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": p._storage_class,
                "volumeMode": "Filesystem",
            },
        }
        p._core_api = self._capture_core(pod_body, pvc_body)
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
        p._db = _PinnedSessionContainerDB()
        p._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                "k8s-pvc:superhuman-remote-worker:22222222-2222-4222-8222-222222222222",
                "SHA256:trusted",
                _TEST_POD_UID,
            )
        )
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
        pvc_body = {}
        core = MagicMock()
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
        core.create_namespaced_persistent_volume_claim = lambda **kw: (
            pvc_body.update(kw["body"]) or _pvc_from_manifest(kw["body"])
        )
        core.create_namespaced_service = lambda **kw: (
            svc_body.update(kw["body"]) or _service_from_manifest(kw["body"])
        )
        core.read_namespaced_persistent_volume_claim = lambda **_kw: (
            _pvc_from_manifest(pvc_body)
        )
        core.read_namespaced_service = lambda **_kw: _service_from_manifest(svc_body)
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
        assert self._ready_ctx(p).get("_runtime_incarnation") == _TEST_POD_UID

    @pytest.mark.asyncio
    async def test_pvc_mode_creates_headless_service_for_sessions_too(self):
        """A PVC-backed session outlives its pod by design, so it needs the
        stable DNS at least as much as a job: the agent caches its SSH dial
        target, and every idle-reap-then-resume hands it a new pod IP."""
        p = self._provisioner(pvc_enabled=True)
        svc_body = {}
        pvc_body = {}
        core = MagicMock()
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
        core.create_namespaced_persistent_volume_claim = lambda **kw: (
            pvc_body.update(kw["body"]) or _pvc_from_manifest(kw["body"])
        )
        core.create_namespaced_service = lambda **kw: (
            svc_body.update(kw["body"]) or _service_from_manifest(kw["body"])
        )
        core.read_namespaced_persistent_volume_claim = lambda **_kw: (
            _pvc_from_manifest(pvc_body)
        )
        core.read_namespaced_service = lambda **_kw: _service_from_manifest(svc_body)
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
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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
        core.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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
        owner = WorkspaceOwner.job("test-job-123456")
        mock_core_api.read_namespaced_pod = MagicMock(return_value=_owned_pod(owner))
        provisioner._core_api = mock_core_api

        result = await provisioner.delete_workspace(owner)

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
        mock_core_api.read_namespaced_pod = MagicMock(side_effect=error)

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

    @pytest.mark.asyncio
    async def test_strict_delete_acceptance_does_not_clear_uid_before_404(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        runtime = "11111111-1111-4111-8111-111111111111"
        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        provisioner._core_api = MagicMock()
        provisioner._wait_for_exact_workspace_pod_absent = AsyncMock(return_value=False)
        owner = WorkspaceOwner.session("test-thread-123456")
        provisioner._core_api.read_namespaced_pod.return_value = _owned_pod(
            owner,
            uid=runtime,
        )

        assert (
            await provisioner.delete_workspace(
                owner,
                expected_runtime_incarnation=runtime,
                wait_for_exact_absence=True,
            )
            is False
        )

        provisioner._wait_for_exact_workspace_pod_absent.assert_awaited_once_with(
            owner,
            expected_runtime_incarnation=runtime,
            timeout=30,
        )
        provisioner._db.merge_thread_workspace_context.assert_not_awaited()
        provisioner._core_api.delete_namespaced_config_map.assert_not_called()

    @pytest.mark.asyncio
    async def test_strict_delete_clears_uid_only_after_exact_404(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        runtime = "11111111-1111-4111-8111-111111111111"
        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        provisioner._core_api = MagicMock()
        provisioner._wait_for_exact_workspace_pod_absent = AsyncMock(return_value=True)
        provisioner._delete_seed_configmap = AsyncMock(return_value=True)
        owner = WorkspaceOwner.session("test-thread-123456")
        provisioner._core_api.read_namespaced_pod.return_value = _owned_pod(
            owner,
            uid=runtime,
        )

        assert (
            await provisioner.delete_workspace(
                owner,
                expected_runtime_incarnation=runtime,
                wait_for_exact_absence=True,
            )
            is True
        )

        provisioner._db.merge_thread_workspace_context.assert_awaited_once_with(
            owner.id,
            {
                "status": "deleted",
                "pod_ip": None,
                "_runtime_incarnation": None,
            },
        )
        provisioner._delete_seed_configmap.assert_awaited_once_with(
            owner.pod_name,
            expected_owner=owner,
            expected_pod_uid=runtime,
        )

    @pytest.mark.asyncio
    async def test_strict_delete_seed_failure_keeps_runtime_context_for_retry(self):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        runtime = "11111111-1111-4111-8111-111111111111"
        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._db = MagicMock()
        provisioner._db.merge_thread_workspace_context = AsyncMock(return_value=True)
        provisioner._core_api = MagicMock()
        provisioner._wait_for_exact_workspace_pod_absent = AsyncMock(return_value=True)
        provisioner._delete_seed_configmap = AsyncMock(return_value=False)
        owner = WorkspaceOwner.session("test-thread-123456")
        provisioner._core_api.read_namespaced_pod.return_value = _owned_pod(
            owner,
            uid=runtime,
        )

        assert (
            await provisioner.delete_workspace(
                owner,
                expected_runtime_incarnation=runtime,
                wait_for_exact_absence=True,
            )
            is False
        )

        provisioner._db.merge_thread_workspace_context.assert_not_awaited()


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
        mock_pod = _owned_pod(WorkspaceOwner.job("test-job-123456"))
        mock_pod.status.pod_ip = "10.42.0.50"

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
        provisioner._db = _PinnedSessionContainerDB()
        provisioner._trusted_pod_ssh_identity = AsyncMock(
            return_value=(
                f"k8s-pod:{provisioner._namespace}:{_TEST_POD_UID}",
                "SHA256:trusted",
                _TEST_POD_UID,
            )
        )
        monkeypatch.setattr(
            provisioner,
            "_resolve_network_tier",
            AsyncMock(return_value="internet-only"),
        )
        monkeypatch.setattr(
            provisioner,
            "_adopt_configmap",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            provisioner, "_wait_for_ready", AsyncMock(return_value="10.0.0.9")
        )
        return provisioner

    @pytest.mark.asyncio
    async def test_terminating_pod_waits_then_recreates(self, monkeypatch):
        """409 + incumbent Terminating → wait for teardown, then recreate fresh."""
        provisioner = self._provisioner(monkeypatch)

        create_seq = [self._conflict(), "fresh"]

        def fake_create(**kwargs):
            item = create_seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return _pod_from_manifest(kwargs["body"])

        owner = WorkspaceOwner.session("thread-abc12345")
        terminating = _owned_pod(owner, namespace="test-ns", deleting=True)

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
            ok = await provisioner.create_workspace(owner)

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
        owner = WorkspaceOwner.session("thread-abc12345")
        live = _pod_from_manifest(
            provisioner._build_pod_manifest(
                pod_name=owner.pod_name,
                owner=owner,
                image=provisioner._workspace_image,
                cpu="500m",
                memory="1Gi",
                cpu_limit="2000m",
                memory_limit="4Gi",
                network_tier="internet-only",
            )
        )
        live.metadata.namespace = "test-ns"
        mock_core_api.read_namespaced_pod = MagicMock(return_value=live)
        provisioner._core_api = mock_core_api

        gone = AsyncMock(return_value=True)
        monkeypatch.setattr(provisioner, "_wait_for_pod_gone", gone)
        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            ok = await provisioner.create_workspace(owner)

        assert ok is True
        # No recreate (incumbent is live), no teardown wait.
        assert mock_core_api.create_namespaced_pod.call_count == 1
        gone.assert_not_awaited()
        # The incumbent UID remains authoritative through ConfigMap adoption.
        provisioner._adopt_configmap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_malformed_live_incumbent_never_triggers_seed_cleanup(
        self, monkeypatch
    ):
        """A failed identity check must not mutate resources mounted by 409 U1."""

        provisioner = self._provisioner(monkeypatch)
        owner = WorkspaceOwner.session("thread-abc12345")
        live = _pod_from_manifest(
            provisioner._build_pod_manifest(
                pod_name=owner.pod_name,
                owner=owner,
                image=provisioner._workspace_image,
                cpu="500m",
                memory="1Gi",
                cpu_limit="2000m",
                memory_limit="4Gi",
                network_tier="internet-only",
            )
        )
        live.metadata.namespace = "test-ns"
        live.spec.volumes.append(
            {
                "name": "workspace-alias",
                "persistentVolumeClaim": {
                    "claimName": "pvc-ws-thread-foreign",
                },
            }
        )
        # Make the alias target the incumbent's canonical source so the exact
        # storage predicate rejects before any adoption/cleanup.
        live.spec.volumes[0] = {
            "name": "workspace-data",
            "persistentVolumeClaim": {
                "claimName": "pvc-ws-thread-thread-abc12",
            },
        }
        live.spec.volumes[-1]["persistentVolumeClaim"]["claimName"] = (
            "pvc-ws-thread-thread-abc12"
        )

        core = MagicMock()
        core.create_namespaced_pod.side_effect = self._conflict()
        core.read_namespaced_pod.return_value = live
        provisioner._core_api = core
        provisioner._pvc_enabled = True
        provisioner._create_pvc = AsyncMock(return_value="reused")
        provisioner._create_service = AsyncMock(return_value=True)
        provisioner._delete_seed_configmap = AsyncMock(return_value=True)

        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            assert await provisioner.create_workspace(owner) is False

        provisioner._delete_seed_configmap.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_teardown_timeout_fails_cleanly(self, monkeypatch):
        """409 + incumbent never finishes terminating → failed, no infinite retry."""
        provisioner = self._provisioner(monkeypatch)

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(side_effect=self._conflict())
        owner = WorkspaceOwner.session("thread-abc12345")
        terminating = _owned_pod(owner, namespace="test-ns", deleting=True)
        mock_core_api.read_namespaced_pod = MagicMock(return_value=terminating)
        provisioner._core_api = mock_core_api

        monkeypatch.setattr(
            provisioner, "_wait_for_pod_gone", AsyncMock(return_value=False)
        )
        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=self._sync_to_thread,
        ):
            ok = await provisioner.create_workspace(owner)

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
        provisioner._db = _PinnedSessionContainerDB()

        mock_core_api = MagicMock()
        mock_core_api.create_namespaced_pod = MagicMock(
            side_effect=lambda **kw: _pod_from_manifest(kw["body"])
        )
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


class TestWorkspaceNamedResourceAuthority:
    OWNER = WorkspaceOwner.session("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    GENERATION = "11111111-2222-4333-8444-555555555555"
    POD_UID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    RESOURCE_UID = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"

    @classmethod
    def _provisioner(cls):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p._core_api = MagicMock()
        return p

    @classmethod
    def _seed(cls, p, **overrides):
        labels = {
            "app": "srw-workspace",
            "srw/component": "workspace-seed",
            "srw.io/component": "agent-workspace",
            cls.OWNER.label_key: cls.OWNER.id,
        }
        labels.update(overrides.pop("labels", {}))
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=p._seed_configmap_name(cls.OWNER.pod_name),
                namespace=p._namespace,
                uid=overrides.pop("uid", cls.RESOURCE_UID),
                resource_version=overrides.pop("resource_version", "7"),
                labels=labels,
                annotations=overrides.pop(
                    "annotations",
                    {"srw.io/runtime-creation-generation": cls.GENERATION},
                ),
                deletion_timestamp=overrides.pop("deletion_timestamp", None),
                owner_references=overrides.pop("owner_references", []),
                **overrides,
            )
        )

    @classmethod
    def _service(cls, p, **overrides):
        labels = {
            "app": "srw-workspace",
            "srw/component": "workspace-svc",
            "srw.io/component": "agent-workspace",
            cls.OWNER.label_key: cls.OWNER.id,
        }
        labels.update(overrides.pop("labels", {}))
        protocol = overrides.pop("protocol", "TCP")
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=cls.OWNER.pod_name,
                namespace=p._namespace,
                uid=overrides.pop("uid", cls.RESOURCE_UID),
                labels=labels,
                deletion_timestamp=overrides.pop("deletion_timestamp", None),
            ),
            spec=SimpleNamespace(
                cluster_ip=overrides.pop("cluster_ip", "None"),
                selector=overrides.pop(
                    "selector",
                    {"app": "srw-workspace", cls.OWNER.label_key: cls.OWNER.id},
                ),
                type=overrides.pop("service_type", "ClusterIP"),
                ports=[
                    SimpleNamespace(
                        name=name,
                        port=port,
                        target_port=port,
                        protocol=protocol,
                    )
                    for name, port in (
                        ("ssh", 30022),
                        ("code-server", 38080),
                        ("cdp", 9222),
                    )
                ],
            ),
        )

    @pytest.mark.asyncio
    async def test_s36_capture_records_exact_pod_pvc_and_service_uids(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        pvc_name = f"pvc-ws-thread-{self.OWNER.id[:12]}"
        p._core_api.read_namespaced_pod.return_value = (
            TestStrictStatelessWorkspaceCreation._pod(pvc_name=pvc_name)
        )
        p._core_api.read_namespaced_persistent_volume_claim.return_value = (
            TestStrictStatelessWorkspaceCreation._claim(p)
        )
        p._core_api.read_namespaced_service.return_value = self._service(p)

        captured = await p.capture_workspace_teardown_identity(self.OWNER)

        assert captured == WorkspaceTeardownIdentity(
            pod_uid=self.POD_UID,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
        )
        assert p._core_api.read_namespaced_pod.call_count == 2

    @pytest.mark.asyncio
    async def test_s36_terminal_capture_combines_uid_and_ssh_attestations(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAttestation,
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        fingerprint = "SHA256:" + ("A" * 43)
        p.attest_workspace_runtime = AsyncMock(
            return_value=WorkspaceRuntimeAttestation(
                backing_id=f"k8s-pod:test:{self.POD_UID}",
                workspace_generation=self.POD_UID,
                runtime_incarnation=self.POD_UID,
                ssh_host_key_fingerprint=fingerprint,
                host="10.0.0.8",
                pod_ip="10.0.0.8",
            )
        )
        p.capture_workspace_teardown_identity = AsyncMock(
            return_value=WorkspaceTeardownIdentity(
                pod_uid=self.POD_UID,
                pvc_uid=self.RESOURCE_UID,
                service_uid=self.RESOURCE_UID,
            )
        )

        captured = await p.capture_terminal_workspace_identity(self.OWNER)

        assert captured == WorkspaceTeardownIdentity(
            pod_uid=self.POD_UID,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
            pod_ip="10.0.0.8",
            ssh_host_key_fingerprint=fingerprint,
            ssh_port=30022,
        )

    @pytest.mark.asyncio
    async def test_s36_capture_after_pod_404_records_residual_resource_uids(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        class _NotFound(Exception):
            status = 404

        p = self._provisioner()
        p._core_api.read_namespaced_pod.side_effect = _NotFound()
        p._core_api.read_namespaced_persistent_volume_claim.return_value = (
            TestStrictStatelessWorkspaceCreation._claim(p)
        )
        p._core_api.read_namespaced_service.return_value = self._service(p)

        captured = await p.capture_workspace_teardown_identity(self.OWNER)

        assert captured == WorkspaceTeardownIdentity(
            pod_uid=None,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
        )
        assert p._core_api.read_namespaced_pod.call_count == 2
        assert (
            p._core_api.read_namespaced_persistent_volume_claim.call_args.kwargs["name"]
            == f"pvc-ws-thread-{self.OWNER.id[:12]}"
        )

    @pytest.mark.asyncio
    async def test_s36_pod_404_capture_rejects_pod_appearing_during_residual_reads(
        self,
    ):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        class _NotFound(Exception):
            status = 404

        p = self._provisioner()
        p._core_api.read_namespaced_pod.side_effect = [
            _NotFound(),
            TestStrictStatelessWorkspaceCreation._pod(),
        ]
        p._core_api.read_namespaced_persistent_volume_claim.return_value = (
            TestStrictStatelessWorkspaceCreation._claim(p)
        )
        p._core_api.read_namespaced_service.return_value = self._service(p)

        with pytest.raises(WorkspaceRuntimeAuthorityError, match="appeared"):
            await p.capture_workspace_teardown_identity(self.OWNER)

    @pytest.mark.asyncio
    async def test_s36_release_without_pod_uid_cleans_only_captured_residuals(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p._captured_teardown_pod_is_absent = AsyncMock(return_value=True)
        p.workspace_pod_authority = AsyncMock()
        p.delete_workspace = AsyncMock()
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        identity = WorkspaceTeardownIdentity(
            pod_uid=None,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
        )

        assert await p.release_workspace(
            self.OWNER,
            teardown_identity=identity,
            capture_snapshot=False,
            strict=True,
        )

        assert p._captured_teardown_pod_is_absent.await_count == 2
        p.workspace_pod_authority.assert_not_awaited()
        p.delete_workspace.assert_not_awaited()
        p.delete_workspace_pvc.assert_awaited_once_with(
            self.OWNER,
            require_exact_owner=True,
            expected_uid=self.RESOURCE_UID,
        )
        p._delete_service.assert_awaited_once_with(
            self.OWNER,
            require_exact_owner=True,
            expected_uid=self.RESOURCE_UID,
        )

    @pytest.mark.asyncio
    async def test_s36_release_without_pod_uid_refuses_when_pod_name_reappears(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p._captured_teardown_pod_is_absent = AsyncMock(return_value=False)
        p.delete_workspace_pvc = AsyncMock()
        p._delete_service = AsyncMock()

        assert not await p.release_workspace(
            self.OWNER,
            teardown_identity=WorkspaceTeardownIdentity(
                pod_uid=None,
                pvc_uid=self.RESOURCE_UID,
                service_uid=self.RESOURCE_UID,
            ),
            capture_snapshot=False,
            strict=True,
        )
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_s36_release_preserves_same_name_replacement_and_all_attached_resources(
        self,
    ):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p._snapshot_service = MagicMock(is_available=True)
        p._snapshot_service.capture_vm_snapshot = AsyncMock()
        p.workspace_pod_authority = AsyncMock(
            side_effect=["replacement", "replacement"]
        )
        p.delete_workspace = AsyncMock(return_value=True)
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        captured = WorkspaceTeardownIdentity(
            pod_uid=self.POD_UID,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
        )

        assert not await p.release_workspace(
            self.OWNER,
            teardown_identity=captured,
            capture_snapshot=False,
            strict=True,
        )

        p.delete_workspace.assert_not_awaited()
        p._snapshot_service.capture_vm_snapshot.assert_not_awaited()
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_s36_classifier_proves_pod_replacement_is_superseded(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p.workspace_pod_authority = AsyncMock(return_value="replacement")

        disposition = await p.classify_workspace_teardown_identity(
            self.OWNER,
            WorkspaceTeardownIdentity(
                pod_uid=self.POD_UID,
                pvc_uid=self.RESOURCE_UID,
                service_uid=self.RESOURCE_UID,
            ),
        )

        assert disposition == "identity_superseded"
        p._core_api.read_namespaced_persistent_volume_claim.assert_not_called()
        p._core_api.read_namespaced_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_s36_classifier_proves_pvc_replacement_is_superseded(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p.workspace_pod_authority = AsyncMock(return_value="exact_absent")
        p._core_api.read_namespaced_persistent_volume_claim.return_value = (
            TestStrictStatelessWorkspaceCreation._claim(
                p,
                uid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            )
        )

        disposition = await p.classify_workspace_teardown_identity(
            self.OWNER,
            WorkspaceTeardownIdentity(
                pod_uid=self.POD_UID,
                pvc_uid=self.RESOURCE_UID,
                service_uid=self.RESOURCE_UID,
            ),
        )

        assert disposition == "identity_superseded"
        p._core_api.read_namespaced_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_s36_release_aborts_all_deletes_when_replacement_appears_at_handoff(
        self,
    ):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p._snapshot_service = MagicMock(is_available=False)
        p.workspace_pod_authority = AsyncMock(side_effect=["exact_live", "replacement"])
        p.get_workspace_status = AsyncMock(
            return_value={
                "runtime_incarnation": self.POD_UID,
                "pod_ip": "10.0.0.2",
                "ready": True,
            }
        )
        p.delete_workspace = AsyncMock()
        p.delete_workspace_pvc = AsyncMock()
        p._delete_service = AsyncMock()

        assert not await p.release_workspace(
            self.OWNER,
            teardown_identity=WorkspaceTeardownIdentity(
                pod_uid=self.POD_UID,
                pvc_uid=self.RESOURCE_UID,
                service_uid=self.RESOURCE_UID,
            ),
            capture_snapshot=False,
            strict=True,
        )

        p.delete_workspace.assert_not_awaited()
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_s36_release_preserves_residuals_when_pod_delete_finds_replacement(
        self,
    ):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        p = self._provisioner()
        p._snapshot_service = MagicMock(is_available=False)
        p.workspace_pod_authority = AsyncMock(
            side_effect=["exact_live", "exact_terminal"]
        )
        p.get_workspace_status = AsyncMock(
            return_value={
                "runtime_incarnation": self.POD_UID,
                "pod_ip": "10.0.0.2",
                "ready": True,
            }
        )
        p.delete_workspace = AsyncMock(return_value=False)
        p.delete_workspace_pvc = AsyncMock()
        p._delete_service = AsyncMock()

        assert not await p.release_workspace(
            self.OWNER,
            teardown_identity=WorkspaceTeardownIdentity(
                pod_uid=self.POD_UID,
                pvc_uid=self.RESOURCE_UID,
                service_uid=self.RESOURCE_UID,
            ),
            capture_snapshot=False,
            strict=True,
        )

        p.delete_workspace.assert_awaited_once()
        p.delete_workspace_pvc.assert_not_awaited()
        p._delete_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_s36_absence_timeout_finishes_after_default_window_first_try(
        self,
    ):
        """S36 can exceed the old 30-second poll without exceeding HTTP timeout."""
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        class _NotFound(Exception):
            status = 404

        p = self._provisioner()
        p.workspace_pod_authority = AsyncMock(
            side_effect=["exact_live", "exact_terminal"]
        )
        p.get_workspace_status = AsyncMock(
            return_value={
                "runtime_incarnation": self.POD_UID,
                "pod_ip": "10.0.0.8",
                "ready": True,
            }
        )
        p._delete_seed_configmap = AsyncMock(return_value=True)
        p._set_context = AsyncMock()
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)

        # The initial authority read plus 31 one-second absence polls still see
        # the captured UID. The next poll finally reaches authoritative 404.
        # A 30-second bound would return False before that last poll.
        reads = 0
        captured_pod = _owned_pod(self.OWNER, uid=self.POD_UID)

        def read_pod(**_kwargs):
            nonlocal reads
            reads += 1
            if reads <= 32:
                return captured_pod
            raise _NotFound()

        p._core_api.read_namespaced_pod.side_effect = read_pod
        clock = {"now": 0.0}
        loop = MagicMock()
        loop.time.side_effect = lambda: clock["now"]

        async def advance_clock(seconds):
            clock["now"] += seconds

        with (
            patch(
                "orchestrator.services.container_provisioner.asyncio.get_event_loop",
                return_value=loop,
            ),
            patch(
                "orchestrator.services.container_provisioner.asyncio.sleep",
                new=advance_clock,
            ),
            patch(
                "orchestrator.services.container_provisioner.workspace_metering.close_interval",
                new=AsyncMock(return_value=None),
            ),
        ):
            released = await p.release_workspace(
                self.OWNER,
                teardown_identity=WorkspaceTeardownIdentity(
                    pod_uid=self.POD_UID,
                    pvc_uid=self.RESOURCE_UID,
                    service_uid=self.RESOURCE_UID,
                    pod_ip="10.0.0.8",
                ),
                capture_snapshot=False,
                strict=True,
                exact_absence_timeout_seconds=45.0,
            )

        assert released is True
        assert clock["now"] == 31.0
        assert reads == 33
        assert p._core_api.delete_namespaced_pod.call_count == 1
        assert p._core_api.delete_namespaced_pod.call_args.kwargs["body"] == {
            "gracePeriodSeconds": 10,
            "preconditions": {"uid": self.POD_UID},
        }
        p.delete_workspace_pvc.assert_awaited_once()
        p._delete_service.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_s36_strict_release_captures_command_keyed_terminal_snapshot(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        generation = "12345678-1234-4678-9abc-123456789abc"
        created_at = "2026-08-13T01:02:03+00:00"
        fingerprint = "SHA256:" + ("A" * 43)
        p = self._provisioner()
        p._snapshot_service = MagicMock(is_available=True)
        p._snapshot_service.reconcile_terminal_snapshot_generation = AsyncMock(
            return_value=(False, "missing")
        )
        p._snapshot_service.capture_vm_snapshot = AsyncMock(return_value=True)
        p.workspace_pod_authority = AsyncMock(
            side_effect=["exact_live", "exact_terminal"]
        )
        p.get_workspace_status = AsyncMock(
            return_value={
                "runtime_incarnation": self.POD_UID,
                "pod_ip": "10.0.0.8",
                "ready": True,
            }
        )
        p.delete_workspace = AsyncMock(return_value=True)
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        identity = WorkspaceTeardownIdentity(
            pod_uid=self.POD_UID,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
            pod_ip="10.0.0.8",
            ssh_host_key_fingerprint=fingerprint,
        )

        assert await p.release_workspace(
            self.OWNER,
            teardown_identity=identity,
            require_snapshot=True,
            expected_runtime_incarnation=self.POD_UID,
            expected_host_key_fingerprint=fingerprint,
            strict_terminal_snapshot=True,
            terminal_snapshot_generation=generation,
            terminal_snapshot_created_at=created_at,
            strict=True,
        )

        p._snapshot_service.capture_vm_snapshot.assert_awaited_once_with(
            job_id=self.OWNER.id,
            ssh_host="10.0.0.8",
            ssh_port=30022,
            source_type="pod",
            entity_type="threads",
            expected_host_key_fingerprint=fingerprint,
            strict_terminal=True,
            terminal_generation=generation,
            terminal_created_at=created_at,
            expected_runtime_incarnation=self.POD_UID,
        )

    @pytest.mark.asyncio
    async def test_s36_replay_uses_proven_snapshot_after_pod_disappears(self):
        from orchestrator.services.container_provisioner import (
            WorkspaceTeardownIdentity,
        )

        generation = "12345678-1234-4678-9abc-123456789abc"
        fingerprint = "SHA256:" + ("A" * 43)
        p = self._provisioner()
        p._snapshot_service = MagicMock(is_available=True)
        p._snapshot_service.reconcile_terminal_snapshot_generation = AsyncMock(
            return_value=(True, "complete")
        )
        p._snapshot_service.capture_vm_snapshot = AsyncMock()
        p.workspace_pod_authority = AsyncMock(
            side_effect=["exact_absent", "exact_absent"]
        )
        p.delete_workspace = AsyncMock()
        p.delete_workspace_pvc = AsyncMock(return_value=True)
        p._delete_service = AsyncMock(return_value=True)
        identity = WorkspaceTeardownIdentity(
            pod_uid=self.POD_UID,
            pvc_uid=self.RESOURCE_UID,
            service_uid=self.RESOURCE_UID,
            pod_ip="10.0.0.8",
            ssh_host_key_fingerprint=fingerprint,
        )

        assert await p.release_workspace(
            self.OWNER,
            teardown_identity=identity,
            require_snapshot=True,
            expected_runtime_incarnation=self.POD_UID,
            expected_host_key_fingerprint=fingerprint,
            strict_terminal_snapshot=True,
            terminal_snapshot_generation=generation,
            terminal_snapshot_created_at="2026-08-13T01:02:03+00:00",
            strict=True,
        )

        p._snapshot_service.capture_vm_snapshot.assert_not_awaited()
        p.delete_workspace.assert_not_awaited()
        p.delete_workspace_pvc.assert_awaited_once()
        p._delete_service.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_s36_pvc_uid_mismatch_proves_old_object_gone_without_delete(self):
        p = self._provisioner()
        replacement_uid = "88888888-9999-4aaa-8bbb-cccccccccccc"
        replacement = TestStrictStatelessWorkspaceCreation._claim(
            p, uid=replacement_uid
        )
        p._core_api.read_namespaced_persistent_volume_claim.return_value = replacement

        assert await p.delete_workspace_pvc(
            self.OWNER,
            require_exact_owner=True,
            expected_uid=self.RESOURCE_UID,
        )
        p._core_api.delete_namespaced_persistent_volume_claim.assert_not_called()

    @pytest.mark.asyncio
    async def test_s36_pvc_uid_precondition_conflict_preserves_replacement(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.read_namespaced_persistent_volume_claim.return_value = (
            TestStrictStatelessWorkspaceCreation._claim(p)
        )
        p._core_api.delete_namespaced_persistent_volume_claim.side_effect = _Conflict()

        assert await p.delete_workspace_pvc(
            self.OWNER,
            require_exact_owner=True,
            expected_uid=self.RESOURCE_UID,
        )
        assert p._core_api.delete_namespaced_persistent_volume_claim.call_args.kwargs[
            "body"
        ] == {"preconditions": {"uid": self.RESOURCE_UID}}

    @pytest.mark.asyncio
    async def test_s36_service_uid_precondition_conflict_preserves_replacement(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.read_namespaced_service.return_value = self._service(p)
        p._core_api.delete_namespaced_service.side_effect = _Conflict()

        assert await p._delete_service(
            self.OWNER,
            require_exact_owner=True,
            expected_uid=self.RESOURCE_UID,
        )
        assert p._core_api.delete_namespaced_service.call_args.kwargs["body"] == {
            "preconditions": {"uid": self.RESOURCE_UID}
        }

    @pytest.mark.asyncio
    async def test_s36_pod_uid_precondition_conflict_preserves_replacement(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.read_namespaced_pod.return_value = (
            TestStrictStatelessWorkspaceCreation._pod()
        )
        p._core_api.delete_namespaced_pod.side_effect = _Conflict()
        p._set_context = AsyncMock()

        assert not await p.delete_workspace(
            self.OWNER,
            expected_runtime_incarnation=self.POD_UID,
            wait_for_exact_absence=True,
            captured_teardown_uid=self.POD_UID,
        )
        assert p._core_api.delete_namespaced_pod.call_args.kwargs == {
            "name": self.OWNER.pod_name,
            "namespace": p._namespace,
            "grace_period_seconds": 10,
            "body": {
                "gracePeriodSeconds": 10,
                "preconditions": {"uid": self.POD_UID},
            },
        }
        p._set_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pod_delete_conflict_remains_a_failure(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.read_namespaced_pod.return_value = (
            TestStrictStatelessWorkspaceCreation._pod()
        )
        p._core_api.delete_namespaced_pod.side_effect = _Conflict()
        p._set_context = AsyncMock()

        assert not await p.delete_workspace(
            self.OWNER,
            expected_runtime_incarnation=self.POD_UID,
            wait_for_exact_absence=True,
        )
        assert p._core_api.delete_namespaced_pod.call_args.kwargs["body"] == {
            "preconditions": {"uid": self.POD_UID}
        }
        p._set_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pvc_delete_conflict_remains_a_failure(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.read_namespaced_persistent_volume_claim.return_value = (
            TestStrictStatelessWorkspaceCreation._claim(p)
        )
        p._core_api.delete_namespaced_persistent_volume_claim.side_effect = _Conflict()

        assert not await p.delete_workspace_pvc(
            self.OWNER,
            require_exact_owner=True,
        )

    @pytest.mark.asyncio
    async def test_legacy_service_delete_conflict_remains_a_failure(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        p._core_api.read_namespaced_service.return_value = self._service(p)
        p._core_api.delete_namespaced_service.side_effect = _Conflict()

        assert not await p._delete_service(
            self.OWNER,
            require_exact_owner=True,
        )

    @pytest.mark.parametrize(
        "drift",
        [
            "owner",
            "opposite-owner",
            "component",
            "generation",
            "uid",
            "deleting",
            "labels-malformed",
        ],
    )
    def test_seed_identity_rejects_every_owner_authority_drift(self, drift):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        seed = self._seed(p)
        if drift == "owner":
            seed.metadata.labels[self.OWNER.label_key] = "foreign"
        elif drift == "opposite-owner":
            seed.metadata.labels["srw/job-id"] = self.OWNER.id
        elif drift == "component":
            seed.metadata.labels["srw/component"] = "foreign"
        elif drift == "generation":
            seed.metadata.annotations["srw.io/runtime-creation-generation"] = (
                "22222222-3333-4444-8555-666666666666"
            )
        elif drift == "uid":
            seed.metadata.uid = "not-a-uuid"
        elif drift == "deleting":
            seed.metadata.deletion_timestamp = "now"
        else:
            seed.metadata.labels = []

        with pytest.raises(WorkspaceRuntimeAuthorityError):
            p._require_stateless_seed_configmap_identity(
                seed,
                owner=self.OWNER,
                generation=self.GENERATION,
            )

    @pytest.mark.asyncio
    async def test_exact_seed_conflict_uses_resource_version_cas(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        existing = self._seed(p)
        replaced = self._seed(p)
        p._core_api.create_namespaced_config_map.side_effect = _Conflict()
        p._core_api.read_namespaced_config_map.return_value = existing
        p._core_api.replace_namespaced_config_map.return_value = replaced

        assert await p._create_seed_configmap(
            self.OWNER.pod_name,
            {"settings.json": {"content": "{}"}},
            expected_owner=self.OWNER,
            expected_creation_generation=self.GENERATION,
        ) == p._seed_configmap_name(self.OWNER.pod_name)
        body = p._core_api.replace_namespaced_config_map.call_args.kwargs["body"]
        assert body["metadata"]["resourceVersion"] == "7"
        assert body["metadata"]["labels"][self.OWNER.label_key] == self.OWNER.id

    @pytest.mark.asyncio
    async def test_seed_adoption_and_delete_are_uid_preconditioned(self):
        p = self._provisioner()
        pod = TestStrictStatelessWorkspaceCreation._pod(
            seed_name=p._seed_configmap_name(self.OWNER.pod_name)
        )
        initial = self._seed(p)
        adopted = self._seed(
            p,
            owner_references=[
                SimpleNamespace(
                    name=self.OWNER.pod_name,
                    uid=self.POD_UID,
                    controller=True,
                )
            ],
        )
        p._core_api.read_namespaced_config_map.side_effect = [initial, adopted]
        assert await p._adopt_configmap(
            p._seed_configmap_name(self.OWNER.pod_name),
            pod,
            expected_owner=self.OWNER,
            expected_creation_generation=self.GENERATION,
        )
        patch_body = p._core_api.patch_namespaced_config_map.call_args.kwargs["body"]
        assert patch_body["metadata"]["resourceVersion"] == "7"
        assert patch_body["metadata"]["ownerReferences"][0]["uid"] == self.POD_UID

        not_found = type("ApiErr", (Exception,), {"status": 404})()
        p._core_api.read_namespaced_config_map.side_effect = [adopted, not_found]
        assert await p._delete_seed_configmap(
            self.OWNER.pod_name,
            expected_owner=self.OWNER,
            expected_pod_uid=self.POD_UID,
        )
        assert p._core_api.delete_namespaced_config_map.call_args.kwargs["body"] == {
            "preconditions": {"uid": self.RESOURCE_UID}
        }

    @classmethod
    def _legacy_seed(cls, p, *, pod_uid=None, labels=None, annotations=None):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=p._seed_configmap_name(cls.OWNER.pod_name),
                namespace=p._namespace,
                uid=cls.RESOURCE_UID,
                resource_version="5",
                labels={} if labels is None else labels,
                annotations={} if annotations is None else annotations,
                deletion_timestamp=None,
                owner_references=(
                    []
                    if pod_uid is None
                    else [
                        SimpleNamespace(
                            name=cls.OWNER.pod_name,
                            uid=pod_uid,
                            controller=True,
                        )
                    ]
                ),
            )
        )

    @pytest.mark.asyncio
    async def test_legacy_seed_migration_requires_exact_controller_and_pod_owner(self):
        p = self._provisioner()
        legacy = self._legacy_seed(p, pod_uid=self.POD_UID)
        p._core_api.read_namespaced_pod.return_value = _owned_pod(
            self.OWNER,
            uid=self.POD_UID,
        )

        await p._require_legacy_seed_configmap_migration(
            legacy,
            owner=self.OWNER,
            pod_name=self.OWNER.pod_name,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "drift",
        ["no-ref", "wrong-ref", "foreign-pod", "labels", "annotations", "api"],
    )
    async def test_legacy_seed_migration_refuses_ambiguous_authority(self, drift):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        legacy = self._legacy_seed(
            p,
            pod_uid=(
                None
                if drift == "no-ref"
                else (
                    "99999999-9999-4999-8999-999999999999"
                    if drift == "wrong-ref"
                    else self.POD_UID
                )
            ),
            labels={"legacy": "unexpected"} if drift == "labels" else None,
            annotations=({"legacy": "unexpected"} if drift == "annotations" else None),
        )
        pod = _owned_pod(self.OWNER, uid=self.POD_UID)
        if drift == "foreign-pod":
            pod.metadata.labels[self.OWNER.label_key] = "foreign"
        if drift == "api":
            p._core_api.read_namespaced_pod.side_effect = RuntimeError("api down")
        else:
            p._core_api.read_namespaced_pod.return_value = pod

        with pytest.raises((WorkspaceRuntimeAuthorityError, RuntimeError)):
            await p._require_legacy_seed_configmap_migration(
                legacy,
                owner=self.OWNER,
                pod_name=self.OWNER.pod_name,
            )

    @pytest.mark.asyncio
    async def test_legacy_seed_delete_uses_exact_captured_pod_and_cm_uids(self):
        p = self._provisioner()
        legacy = self._legacy_seed(p, pod_uid=self.POD_UID)
        not_found = type("ApiErr", (Exception,), {"status": 404})()
        p._core_api.read_namespaced_config_map.side_effect = [legacy, not_found]

        assert await p._delete_seed_configmap(
            self.OWNER.pod_name,
            expected_owner=self.OWNER,
            expected_pod_uid=self.POD_UID,
        )
        assert p._core_api.delete_namespaced_config_map.call_args.kwargs["body"] == {
            "preconditions": {"uid": self.RESOURCE_UID}
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drift", ["no-ref", "wrong-ref", "metadata"])
    async def test_legacy_seed_delete_refuses_unproven_incumbent(self, drift):
        p = self._provisioner()
        legacy = self._legacy_seed(
            p,
            pod_uid=(
                None
                if drift == "no-ref"
                else (
                    "99999999-9999-4999-8999-999999999999"
                    if drift == "wrong-ref"
                    else self.POD_UID
                )
            ),
            labels={"legacy": "unexpected"} if drift == "metadata" else None,
        )
        p._core_api.read_namespaced_config_map.return_value = legacy

        assert not await p._delete_seed_configmap(
            self.OWNER.pod_name,
            expected_owner=self.OWNER,
            expected_pod_uid=self.POD_UID,
        )
        p._core_api.delete_namespaced_config_map.assert_not_called()

    @pytest.mark.parametrize(
        "drift",
        [
            "owner",
            "opposite-owner",
            "component",
            "selector",
            "headless",
            "type",
            "udp",
            "uid",
            "deleting",
        ],
    )
    def test_service_identity_rejects_owner_and_exact_spec_drift(self, drift):
        from orchestrator.services.container_provisioner import (
            WorkspaceRuntimeAuthorityError,
        )

        p = self._provisioner()
        service = self._service(p)
        if drift == "owner":
            service.metadata.labels[self.OWNER.label_key] = "foreign"
        elif drift == "opposite-owner":
            service.metadata.labels["srw/job-id"] = self.OWNER.id
        elif drift == "component":
            service.metadata.labels["srw/component"] = "foreign"
        elif drift == "selector":
            service.spec.selector = {"app": "srw-workspace"}
        elif drift == "headless":
            service.spec.cluster_ip = "10.43.0.1"
        elif drift == "type":
            service.spec.type = "ExternalName"
        elif drift == "udp":
            service.spec.ports[0].protocol = "UDP"
        elif drift == "uid":
            service.metadata.uid = "not-a-uuid"
        else:
            service.metadata.deletion_timestamp = "now"

        with pytest.raises(WorkspaceRuntimeAuthorityError):
            p._require_stateless_service_identity(service, owner=self.OWNER)

    @pytest.mark.asyncio
    async def test_service_conflict_and_delete_require_exact_owner_uid(self):
        class _Conflict(Exception):
            status = 409

        not_found = type("ApiErr", (Exception,), {"status": 404})()
        p = self._provisioner()
        service = self._service(p)
        p._core_api.create_namespaced_service.side_effect = _Conflict()
        p._core_api.read_namespaced_service.return_value = service
        assert await p._create_service(self.OWNER, require_exact_owner=True)

        p._core_api.read_namespaced_service.side_effect = [service, not_found]
        assert await p._delete_service(self.OWNER, require_exact_owner=True)
        assert p._core_api.delete_namespaced_service.call_args.kwargs["body"] == {
            "preconditions": {"uid": self.RESOURCE_UID}
        }


class TestIdePodResourceAuthority:
    JOB_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    RUNTIME = "11111111-1111-4111-8111-111111111111"

    @classmethod
    def _owner(cls):
        return WorkspaceOwner.job(cls.JOB_ID)

    @classmethod
    def _provisioner(cls):
        from orchestrator.services.container_provisioner import ContainerProvisioner

        p = ContainerProvisioner()
        p._k8s_available = True
        p._core_api = MagicMock()
        p._resolve_network_tier = AsyncMock(return_value="internet-only")
        p._resolve_ide_seed_files = AsyncMock(return_value={})
        p._resolve_ide_extensions = AsyncMock(return_value=[])
        p._resolve_ide_needs_state = AsyncMock(return_value=False)
        p._create_seed_configmap = AsyncMock(return_value=None)
        p._delete_seed_configmap = AsyncMock(return_value=True)
        p._adopt_configmap = AsyncMock(return_value=True)
        p._wait_for_ready = AsyncMock(return_value="10.42.0.30")
        p._seed_workspace_state = AsyncMock(return_value=None)
        return p

    @classmethod
    def _pod(cls, p, *, seed_name=None):
        owner = cls._owner()
        manifest = p._build_pod_manifest(
            pod_name=f"ide-{cls.JOB_ID[:12]}",
            owner=owner,
            image=p._workspace_image,
            cpu="250m",
            memory="512Mi",
            cpu_limit="1000m",
            memory_limit="2Gi",
            network_tier="internet-only",
            seed_configmap=seed_name,
        )
        manifest["metadata"]["labels"]["srw/component"] = "ide-session"
        return _pod_from_manifest(manifest, uid=cls.RUNTIME)

    @classmethod
    def _seed(cls, p):
        owner = cls._owner()
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=p._seed_configmap_name(f"ide-{cls.JOB_ID[:12]}"),
                namespace=p._namespace,
                uid="22222222-2222-4222-8222-222222222222",
                resource_version="9",
                labels={
                    "app": "srw-workspace",
                    "srw/component": "workspace-seed",
                    "srw.io/component": "agent-workspace",
                    owner.label_key: owner.id,
                },
                annotations={},
                deletion_timestamp=None,
                owner_references=[],
            )
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("incumbent_has_seed", "desired_has_seed"),
        [(True, False), (False, True), (True, True), (False, False)],
    )
    async def test_live_409_uses_incumbent_seed_plan_without_deleting_bound_cm(
        self, incumbent_has_seed, desired_has_seed
    ):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        pod_name = f"ide-{self.JOB_ID[:12]}"
        seed_name = p._seed_configmap_name(pod_name)
        p._create_seed_configmap.return_value = seed_name if desired_has_seed else None
        incumbent = self._pod(
            p,
            seed_name=seed_name if incumbent_has_seed else None,
        )
        p._core_api.create_namespaced_pod.side_effect = _Conflict()
        p._core_api.read_namespaced_pod.return_value = incumbent
        if incumbent_has_seed:
            p._core_api.read_namespaced_config_map.return_value = self._seed(p)

        created_tasks = []

        def close_task(coro):
            coro.close()
            created_tasks.append(coro)
            return MagicMock()

        with patch(
            "orchestrator.services.container_provisioner.asyncio.create_task",
            side_effect=close_task,
        ):
            assert await p.create_ide_pod(self.JOB_ID) == "10.42.0.30"

        p._adopt_configmap.assert_not_awaited()
        if not incumbent_has_seed and desired_has_seed:
            p._delete_seed_configmap.assert_awaited_once()
            cleanup = p._delete_seed_configmap.await_args
            assert cleanup.args == (pod_name,)
            assert cleanup.kwargs["expected_owner"].id == self.JOB_ID
        else:
            p._delete_seed_configmap.assert_not_awaited()
        assert len(created_tasks) == 1

    @pytest.mark.asyncio
    async def test_live_409_malformed_storage_refuses_without_seed_cleanup(self):
        class _Conflict(Exception):
            status = 409

        p = self._provisioner()
        pod = self._pod(p)
        pod.spec.containers.append(
            SimpleNamespace(
                name="sidecar",
                volume_mounts=[SimpleNamespace(name="workspace-data")],
                volume_devices=[],
            )
        )
        p._core_api.create_namespaced_pod.side_effect = _Conflict()
        p._core_api.read_namespaced_pod.return_value = pod

        assert await p.create_ide_pod(self.JOB_ID) is None
        p._delete_seed_configmap.assert_not_awaited()
        p._wait_for_ready.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_initial_404_never_sends_name_only_pod_delete(self):
        class _NotFound(Exception):
            status = 404

        p = self._provisioner()
        p._core_api.read_namespaced_pod.side_effect = _NotFound()

        assert await p.delete_ide_pod(self.JOB_ID)
        p._core_api.delete_namespaced_pod.assert_not_called()
        p._delete_seed_configmap.assert_awaited_once()
        cleanup = p._delete_seed_configmap.await_args
        assert cleanup.args == (f"ide-{self.JOB_ID[:12]}",)
        assert cleanup.kwargs["expected_owner"].id == self.JOB_ID
        assert cleanup.kwargs["expected_pod_uid"] is None
