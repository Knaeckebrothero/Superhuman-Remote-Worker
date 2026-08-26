"""Container Provisioner — Per-job workspace container lifecycle management.

Creates lightweight workspace containers as an alternative to KubeVirt VMs.
Workspace containers run on the same cluster as the orchestrator and provide:
  - SSH server (for RemoteBackend file/shell operations)
  - tmux (for persistent shell sessions)
  - code-server (for IDE access)
  - Dev tools (git, build-essential, node, python, etc.)

The agent connects to workspace containers via SSH, identical to VMs.
From the agent's perspective, there is no difference.

Selection logic:
  - kubernetes client available → container provisioning enabled
  - No kubernetes client → disabled (falls back to local backend)
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID, uuid4

from services import resolve_ssh_key_path, workspace_metering
from services.ssh_helpers import (
    SSHPrivateKeyError,
    wait_for_agent_ssh,
    workspace_private_key_fingerprint,
)
from services.workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY
from services.workspace_lifecycle import WorkspaceOwner
from services.managed_repository_process_retirement import (
    retire_managed_repository_processes,
)

logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.stream import stream as k8s_stream

    K8S_AVAILABLE = True
except ImportError:
    k8s_client = None
    k8s_config = None
    k8s_stream = None
    K8S_AVAILABLE = False


# Pod-network egress tier applied to workspaces whose owning project has
# no resolvable tier (no DB, no project, or pre-migration project rows).
# Matches the projects.network_tier column default; the closed CHECK set
# in 0016_project_network_tier.sql is the source of truth for valid names.
DEFAULT_NETWORK_TIER = "internet-only"

# Private control-plane attestation paired with the stable workspace backing
# generation. Unlike the PVC-backed generation, this value changes whenever
# Kubernetes replaces the workspace pod.
WORKSPACE_RUNTIME_INCARNATION_KEY = "_runtime_incarnation"
WORKSPACE_RUNTIME_CREATION_KEY = "_stateless_runtime_creation"
WORKSPACE_RUNTIME_CREATION_ANNOTATION = "srw.io/runtime-creation-generation"
STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER = "lifecycle.srw.dev/stateless-process-zero"
_UNSPECIFIED_RESOURCE_BINDING = object()


class WorkspaceSSHAuthenticationError(RuntimeError):
    """A K8s-ready workspace rejected the configured SSH identity."""


class WorkspaceRuntimeAuthorityError(RuntimeError):
    """A deterministic Pod name no longer identifies the authorized runtime."""


@dataclass(frozen=True)
class WorkspaceRuntimeAttestation:
    """Exact control-plane identity for one live workspace SSH endpoint."""

    backing_id: str
    workspace_generation: str
    runtime_incarnation: str
    ssh_host_key_fingerprint: str
    host: str
    pod_ip: str
    port: int = 30022


@dataclass(frozen=True)
class WorkspaceTeardownIdentity:
    """Immutable Kubernetes identities captured before terminal teardown.

    A deterministic resource name is not authority: Kubernetes may recreate a
    Pod, PVC, or Service with the same name after a finalizer crash.  S36 keeps
    this fixed-cardinality record in the completion effect intent and every
    destructive call later carries the corresponding UID precondition.
    """

    pod_uid: str | None
    pvc_uid: str | None
    service_uid: str | None
    pod_ip: str | None = None
    ssh_host_key_fingerprint: str | None = None
    ssh_port: int = 30022


def _canonical_runtime_uuid(value: Any, *, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{label} is invalid")
    return canonical


def _resource_field(resource: Any, *names: str) -> Any:
    """Read one Kubernetes model/dict field without MagicMock coercion."""

    for name in names:
        if isinstance(resource, dict):
            if name in resource:
                return resource[name]
        elif resource is not None and hasattr(resource, name):
            return getattr(resource, name)
    return None


def _all_pod_container_statuses_terminated(pod: Any) -> bool:
    """Prove every declared/observed Pod container has terminated.

    Kubernetes reports regular, restartable-init, and ephemeral/debug
    containers in separate status arrays. Looking only at
    ``container_statuses`` can therefore declare a Pod quiescent while an init
    sidecar or injected debug container is still running. Missing status for a
    declared container is equally ambiguous. Only a non-empty, complete set of
    exact terminated states is positive proof.
    """

    status_obj = getattr(pod, "status", None)
    spec_obj = getattr(pod, "spec", None)
    observed_any = False
    for status_field, spec_field in (
        ("container_statuses", "containers"),
        ("init_container_statuses", "init_containers"),
        ("ephemeral_container_statuses", "ephemeral_containers"),
    ):
        raw_statuses = getattr(status_obj, status_field, None)
        if raw_statuses is None:
            statuses: list[Any] = []
        elif isinstance(raw_statuses, (list, tuple)):
            statuses = list(raw_statuses)
        else:
            return False

        raw_declared = getattr(spec_obj, spec_field, None)
        if isinstance(raw_declared, (list, tuple)) and raw_declared:
            declared_names = {
                str(getattr(container, "name", "") or "") for container in raw_declared
            }
            observed_names = {
                str(getattr(container, "name", "") or "") for container in statuses
            }
            if "" in declared_names or declared_names != observed_names:
                return False

        if statuses:
            observed_any = True
        if any(
            getattr(getattr(container, "state", None), "terminated", None) is None
            for container in statuses
        ):
            return False
    return observed_any


def _deleting_pod_was_never_scheduled(pod: Any) -> bool:
    """Prove an exact deleting Pending Pod never gained process authority.

    A finalizer is installed before scheduling, so a Pod deleted while the
    scheduler has not assigned ``spec.nodeName`` can never acquire a kubelet
    sandbox or container process.  Keep the exception deliberately narrow:
    the object must still be Pending and deleting, and any contradictory
    observed running state makes the result ambiguous.
    """

    metadata = getattr(pod, "metadata", None)
    spec = getattr(pod, "spec", None)
    status = getattr(pod, "status", None)
    if (
        getattr(metadata, "deletion_timestamp", None) is None
        or getattr(status, "phase", None) != "Pending"
        or _resource_field(spec, "node_name", "nodeName") not in (None, "")
    ):
        return False
    for status_field in (
        "container_statuses",
        "init_container_statuses",
        "ephemeral_container_statuses",
    ):
        raw_statuses = getattr(status, status_field, None)
        if raw_statuses is None:
            continue
        if not isinstance(raw_statuses, (list, tuple)):
            return False
        for container in raw_statuses:
            state = getattr(container, "state", None)
            if state is None:
                return False
            if (
                getattr(state, "running", None) is not None
                or getattr(container, "ready", None) is True
                or getattr(container, "started", None) is True
            ):
                return False
            if (
                _resource_field(container, "container_id", "containerID")
                and getattr(state, "terminated", None) is None
            ):
                return False
    return True


def _pod_has_exact_process_zero(pod: Any) -> bool:
    return _all_pod_container_statuses_terminated(
        pod
    ) or _deleting_pod_was_never_scheduled(pod)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _pvc_name_for(owner: WorkspaceOwner) -> str:
    """Deterministic PVC name for an owner's workspace volume.

    The SINGLE source of truth for both the create side (``create_workspace``)
    and the reclaim side (``delete_workspace_pvc``, and through it the lifecycle
    GC). Neither side may spell the name itself: a create/delete drift is the
    worst failure mode in this file — it silently leaks volumes until the
    storage quota rejects new PVCs with a 403 and provisioning fails closed, or
    it deletes some other owner's data.

    Jobs keep the historical ``pvc-workspace-<id[:12]>``; sessions get
    ``pvc-ws-thread-<id[:12]>``, mirroring the pod-name split in
    ``WorkspaceOwner.pod_name``. Truncation matches the pod name so the two
    stay legible side by side in ``kubectl get pod,pvc``.
    """
    prefix = "pvc-workspace" if owner.kind == "job" else "pvc-ws-thread"
    return f"{prefix}-{owner.id[:12]}"


class ContainerProvisioner:
    """Workspace container provisioner using Kubernetes CoreV1Api.

    Creates per-owner pods (job or session) with SSH server + code-server.
    Workspace storage is ``emptyDir`` by default (dies with the pod). When
    ``WORKSPACE_PVC_ENABLED`` is set, BOTH kinds are PVC-backed (Branch a): the
    volume is named after the owner UUID (``_pvc_name_for``), survives pod
    crashes, and reattaches by that deterministic name on recreate. PVCs are
    reclaimed when the owning work reaches a terminal state (completed/failed/
    cancelled job; a session only once its thread is genuinely done, since an
    ``ended`` thread is still resumable) and retained across idle reaps,
    suspend/restore and crash recovery.
    See knowledge-base/knowledge/features/workspace_pvc_branch_a_implementation.md.
    """

    def __init__(self):
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._core_api: Optional[Any] = None
        self._k8s_available: bool = False
        self._in_cluster: bool = False
        self._namespace: str = os.environ.get(
            "WORKSPACE_NAMESPACE", "superhuman-remote-worker"
        )
        self._workspace_image: str = os.environ.get(
            "WORKSPACE_IMAGE",
            "ghcr.io/knaeckebrothero/superhuman-remote-worker-workspace:latest",
        )
        self._ssh_secret_name: str = os.environ.get(
            "WORKSPACE_SSH_SECRET", "vm-ssh-key"
        )
        self._ssh_auth_ready_timeout: float = float(
            os.environ.get("WORKSPACE_SSH_AUTH_READY_TIMEOUT_S", "30")
        )
        self._ssh_auth_connect_timeout: int = int(
            os.environ.get("WORKSPACE_SSH_AUTH_CONNECT_TIMEOUT_S", "5")
        )
        self._ssh_auth_poll_interval: float = float(
            os.environ.get("WORKSPACE_SSH_AUTH_POLL_INTERVAL_S", "2")
        )
        self._storage_class: str = os.environ.get(
            "WORKSPACE_STORAGE_CLASS", "longhorn-ephemeral"
        )
        # Branch (a): PVC-backed workspaces. Default off → emptyDir (today's
        # behavior). Covers BOTH owner kinds. Sessions were scoped out in v1 on
        # the theory that they rehydrate from Postgres — but Postgres only holds
        # the conversation, not the working tree, and a session's pod is reaped
        # when it goes idle while the thread stays resumable. On emptyDir the
        # user reopens a session whose files silently vanished. The PVC name is
        # deterministic on the owner UUID (``_pvc_name_for``), so a recreated pod
        # reattaches the same volume; GC happens when the owning work is
        # terminal. See knowledge-base/knowledge/features/workspace_pvc_branch_a_implementation.md.
        self._pvc_enabled: bool = _env_flag("WORKSPACE_PVC_ENABLED", False)
        self._pvc_size: str = os.environ.get("WORKSPACE_PVC_SIZE", "10Gi")
        # Single-replica node-loss fallback (Phase 3b). A REATTACH gets this
        # longer ready-wait so a transient node reboot (the replica's node coming
        # back) recovers without discarding data; past it, a still-wedged reattach
        # whose holdup is a volume-attach failure means the lone replica's node is
        # gone — discard the PVC and recover onto a fresh volume (the agent then
        # clones from Gitea + resumes the checkpoint; unpushed files are lost).
        # `_fresh_fallback_enabled` is the kill-switch (the discard is the only
        # data-destructive recovery path, so keep it disablable).
        self._reattach_ready_timeout: int = int(
            os.environ.get("WORKSPACE_REATTACH_READY_TIMEOUT", "180")
        )
        self._fresh_fallback_enabled: bool = _env_flag(
            "WORKSPACE_REATTACH_FRESH_FALLBACK", True
        )
        # rclone-backed cloud workspaces are the default container path. They
        # need FUSE inside the workspace pod because the agent shell runs there.
        self._fuse_enabled: bool = _env_flag("WORKSPACE_FUSE_ENABLED", True)
        # In k3d/containerd and many managed clusters, /dev/fuse + SYS_ADMIN is
        # still not enough because the default runtime profile blocks FUSE
        # mounts. Keep this explicit so restricted deployments can opt out.
        self._fuse_privileged: bool = self._fuse_enabled and _env_flag(
            "WORKSPACE_FUSE_PRIVILEGED", True
        )

    @property
    def is_available(self) -> bool:
        """True if container provisioning is available."""
        return self._k8s_available

    @property
    def in_cluster(self) -> bool:
        """True if connected via in-cluster config (running inside K8s)."""
        return self._in_cluster

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(
        self,
        db: Any,
        snapshot_service: Optional[Any] = None,
    ) -> None:
        """Initialize the provisioner.

        Args:
            db: PostgresDB instance for job context updates.
            snapshot_service: Optional SnapshotService for archival before deletion.
        """
        self._db = db
        self._snapshot_service = snapshot_service
        self._init_k8s()

        if self._k8s_available:
            logger.info(
                "Container provisioner ready (namespace=%s, image=%s, in_cluster=%s)",
                self._namespace,
                self._workspace_image,
                self._in_cluster,
            )
        else:
            logger.info(
                "Container provisioner: not available (kubernetes client unavailable)"
            )

    def _init_k8s(self) -> None:
        """Try to initialize the Kubernetes client.

        Important: ``load_kube_config()`` can succeed even when pointing at a
        dead cluster (stale kubeconfig).  We follow up with an actual API call
        (list namespaces) to confirm connectivity.
        """
        if not K8S_AVAILABLE:
            return

        try:
            in_cluster = False
            try:
                k8s_config.load_incluster_config()
                in_cluster = True
            except k8s_config.ConfigException:
                try:
                    k8s_config.load_kube_config()
                except k8s_config.ConfigException:
                    logger.debug(
                        "Kubernetes not available (no in-cluster or kubeconfig)"
                    )
                    return

            api = k8s_client.CoreV1Api()

            # Verify connectivity with a namespace-scoped call (works with
            # Role-based RBAC; the old list_namespace required ClusterRole)
            api.list_namespaced_pod(self._namespace, limit=1, _request_timeout=5)

            self._core_api = api
            self._k8s_available = True
            self._in_cluster = in_cluster
        except Exception as e:
            logger.debug("Container provisioning not available: %s", e)
            self._core_api = None
            self._k8s_available = False

    # =========================================================================
    # Public API
    # =========================================================================

    async def attest_workspace_runtime(
        self, owner: WorkspaceOwner
    ) -> WorkspaceRuntimeAttestation:
        """Attest the exact live backing, Pod, endpoint, and SSH host identity.

        This is deliberately a fresh control-plane read, not a projection of
        ``jobs.context``.  The immutable Pod/PVC/Service/seed ownership checks
        and the host-key exec are performed by ``_trusted_pod_ssh_identity``;
        its post-exec re-reads close deterministic-name replacement races.
        """

        if not self._k8s_available or self._core_api is None:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Kubernetes authority is unavailable"
            )

        expected_network_tier = await self._resolve_network_tier(
            owner.id, kind=owner.network_tier_kind
        )
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod authority probe failed"
            ) from exc

        runtime_incarnation = self._require_workspace_pod_owner(
            pod,
            owner=owner,
            allow_owner_unlabeled=False,
            expected_network_tier=expected_network_tier,
        )
        status = getattr(pod, "status", None)
        pod_ip = str(getattr(status, "pod_ip", "") or "")
        container_statuses = getattr(status, "container_statuses", None)
        if (
            getattr(status, "phase", None) != "Running"
            or not pod_ip
            or not isinstance(container_statuses, (list, tuple))
            or not container_statuses
            or any(
                getattr(item, "ready", None) is not True for item in container_statuses
            )
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod is not exactly Kubernetes-ready"
            )

        pvc_name = self._workspace_pvc_name_from_pod(pod, owner=owner)
        try:
            (
                backing_id,
                fingerprint,
                confirmed_runtime,
            ) = await self._trusted_pod_ssh_identity(
                owner.pod_name,
                pvc_name=pvc_name,
                expected_owner=owner,
                expected_runtime_incarnation=runtime_incarnation,
                expected_network_tier=expected_network_tier,
            )
        except WorkspaceRuntimeAuthorityError:
            raise
        except Exception as exc:
            raise WorkspaceRuntimeAuthorityError(
                "workspace SSH identity attestation failed"
            ) from exc

        if confirmed_runtime != runtime_incarnation:
            raise WorkspaceRuntimeAuthorityError("workspace Pod UID changed")
        expected_backing_kind = "pvc" if pvc_name else "pod"
        expected_prefix = f"k8s-{expected_backing_kind}:{self._namespace}:"
        if not backing_id.startswith(expected_prefix):
            raise WorkspaceRuntimeAuthorityError(
                "workspace backing identity is malformed"
            )
        try:
            workspace_generation = _canonical_runtime_uuid(
                backing_id.removeprefix(expected_prefix),
                label="workspace backing UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc
        if re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint) is None:
            raise WorkspaceRuntimeAuthorityError(
                "workspace SSH host-key fingerprint is malformed"
            )

        return WorkspaceRuntimeAttestation(
            backing_id=backing_id,
            workspace_generation=workspace_generation,
            runtime_incarnation=runtime_incarnation,
            ssh_host_key_fingerprint=fingerprint,
            host=self._workspace_dns(owner) if pvc_name else pod_ip,
            pod_ip=pod_ip,
        )

    async def create_workspace(
        self,
        owner: WorkspaceOwner,
        cpu: str = "500m",
        memory: str = "1Gi",
        cpu_limit: str = "2000m",
        memory_limit: str = "4Gi",
        image: Optional[str] = None,
        fresh: bool = False,
        stateless_creation_generation: str | None = None,
        allow_stateless_create: bool = False,
    ) -> bool:
        """Create a workspace container for a job or persistent thread.

        Args:
            owner: WorkspaceOwner identifying the job or session.
            cpu: CPU request.
            memory: Memory request.
            cpu_limit: CPU limit.
            memory_limit: Memory limit.
            image: Workspace image override (defaults to WORKSPACE_IMAGE env).
            fresh: Single-replica node-loss recovery (Phase 3b) — force a clean
                empty PVC by deleting any existing (wedged) one first, instead of
                reattaching it. Set only by the internal fallback below.

        Returns:
            True if pod creation succeeded, False otherwise.
        """
        if not self._k8s_available:
            return False

        strict_stateless = stateless_creation_generation is not None
        if not strict_stateless and owner.kind == "session":
            lane_impl = (
                getattr(
                    type(self._db),
                    "stateless_thread_workspace_creation_requires_authority",
                    None,
                )
                if self._db is not None
                else None
            )
            try:
                requires_authority = (
                    await lane_impl(self._db, owner.id) if callable(lane_impl) else None
                )
            except Exception:
                requires_authority = None
            if requires_authority is not False:
                logger.error(
                    "Direct workspace create refused without stateless nonce "
                    "authority for thread %s",
                    owner.id,
                )
                return False
        if strict_stateless:
            if owner.kind != "session" or self._db is None:
                return False
            if fresh:
                return False
            try:
                stateless_creation_generation = _canonical_runtime_uuid(
                    stateless_creation_generation,
                    label="stateless workspace creation generation",
                )
            except ValueError:
                return False
            if type(allow_stateless_create) is not bool:
                return False
            if not allow_stateless_create:
                return await self.continue_stateless_workspace_creation(
                    owner,
                    generation=stateless_creation_generation,
                    expected_runtime_incarnation=None,
                    cpu=cpu,
                    memory=memory,
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    image=image,
                )
            validate_impl = getattr(
                type(self._db),
                "validate_stateless_thread_workspace_creation_attempt",
                None,
            )
            if not callable(validate_impl) or not await validate_impl(
                self._db,
                owner.id,
                generation=stateless_creation_generation,
                attempted=False,
            ):
                return False

        pod_name = owner.pod_name
        workspace_image = image or self._workspace_image
        network_tier = await self._resolve_network_tier(
            owner.id, kind=owner.network_tier_kind
        )

        # Workspace storage (Branch a): PVC-backed for BOTH owner kinds when
        # WORKSPACE_PVC_ENABLED — the volume is named by the owner UUID, survives
        # pod crashes, and reattaches by that deterministic name on recreate
        # (drift recovery, suspend/restore, give_up all funnel back through
        # create_workspace, so 409-reuse here IS the resume path). Sessions need
        # this at least as much as jobs: their pod is reaped on idle while the
        # thread stays resumable, so emptyDir destroys the working tree of state
        # a user can still reopen. Otherwise emptyDir — storage dies with the
        # pod; isolation is the pod boundary. Created BEFORE the seed ConfigMap
        # so a PVC failure leaves nothing to clean up — it is the provisioning
        # prerequisite (and it fails closed: never silently downgrade to
        # emptyDir, that would trade a visible failure for silent data loss).
        pvc_name: Optional[str] = None
        pvc_reattach = False
        if self._pvc_enabled:
            pvc_name = _pvc_name_for(owner)
            # Fresh recovery (Phase 3b single-replica fallback): the prior reattach
            # was wedged because the PVC's only replica is on a dead node. Delete
            # the stuck PVC (and wait for it to release) so the create below makes
            # a brand-new empty volume under the same deterministic name.
            if fresh:
                if not await self._delete_pvc_and_wait(
                    pvc_name,
                    expected_owner=owner,
                ):
                    return False
            pvc_status = await self._create_pvc(
                pvc_name,
                size=self._pvc_size,
                # Owner label lets the backstop reaper resolve PVC → owner and
                # is a belt-and-suspenders identity check before any reattach.
                # lifecycle's reap_orphans() sweep covers BOTH kinds: jobs via
                # `srw/job-id` + the `pvc-workspace-` prefix, sessions via
                # `srw/thread-id` + `pvc-ws-thread-`/`pvc-agent-s-`. The reclaim
                # rules differ, not the coverage — a job PVC goes when the job is
                # terminal or gone, a session's only when the `threads` row is
                # actually absent (an `ended` thread is still resumable). And
                # because a session's pod is usually idle-reaped long before the
                # thread is deleted, that sweep — not the inline terminal path —
                # is the primary reclaim route for sessions.
                labels={owner.label_key: owner.id},
                expected_owner=owner,
            )
            if not pvc_status:
                logger.error(
                    "Workspace PVC create failed for %s %s — aborting provision",
                    owner.kind,
                    owner.id,
                )
                if not strict_stateless:
                    await self._set_context(
                        owner, {"status": "failed", "error": "PVC creation failed"}
                    )
                return False
            # "reused" (409) = an EXISTING volume reattached — the only case where
            # the single-replica node-loss wedge can occur. `fresh` recreates a NEW
            # volume, so it is never itself a reattach (prevents the fallback below
            # from re-firing → no recursion).
            pvc_reattach = pvc_status == "reused" and not fresh

        # Seed the user's saved code-server config (theme/keybindings/snippets)
        # into the pod before it starts. Best-effort — never blocks provisioning.
        seed_files = await self._resolve_ide_seed_files(owner)
        seed_exts = await self._resolve_ide_extensions(owner)
        seed_needs_state = (
            False
            if strict_stateless
            else await self._resolve_ide_needs_state(owner, seed_exts)
        )
        try:
            seed_cm = await self._create_seed_configmap(
                pod_name,
                seed_files,
                seed_exts,
                needs_state=seed_needs_state,
                expected_owner=owner,
                expected_creation_generation=(
                    stateless_creation_generation if strict_stateless else None
                ),
            )
        except WorkspaceRuntimeAuthorityError as error:
            logger.error(
                "Stateless seed ConfigMap preparation failed for %s: %s",
                owner.id,
                error,
            )
            return False

        pod_manifest = self._build_pod_manifest(
            pod_name=pod_name,
            owner=owner,
            image=workspace_image,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            network_tier=network_tier,
            pvc_name=pvc_name,
            seed_configmap=seed_cm,
            stateless_creation_generation=stateless_creation_generation,
        )

        # Once the durable false->true attempt CAS is submitted, its outcome is
        # the authority boundary for the one permitted Kubernetes create. A
        # timeout/cancellation/non-409 error can occur after the apiserver has
        # accepted an exact Pod that already references this ConfigMap. Never
        # delete the seed on that ambiguous side of the edge: all retries are
        # read/adopt-only and cannot recreate it. Exact terminal workspace
        # cleanup owns the deterministic ConfigMap thereafter.
        stateless_attempt_may_have_committed = False
        existing_seed_bound = False
        reused_existing_pod = False
        metered_cpu, metered_memory = cpu, memory
        try:
            if strict_stateless:
                claim_impl = getattr(
                    type(self._db),
                    "claim_stateless_thread_workspace_creation_attempt",
                    None,
                )
                if not callable(claim_impl):
                    return False
                stateless_attempt_may_have_committed = True
                if not await claim_impl(
                    self._db,
                    owner.id,
                    generation=stateless_creation_generation,
                ):
                    return False
                created_pod = await self._create_stateless_pod_once(
                    owner,
                    pod_manifest,
                    generation=stateless_creation_generation,
                    expected_network_tier=network_tier,
                    expected_pvc_name=pvc_name,
                    expected_seed_configmap=seed_cm,
                )
                if created_pod is None:
                    return False
                runtime_incarnation = self._require_stateless_pod_identity(
                    created_pod,
                    owner=owner,
                    generation=stateless_creation_generation,
                    expected_network_tier=network_tier,
                    expected_pvc_name=pvc_name,
                    expected_seed_configmap=seed_cm,
                )
                metered_cpu, metered_memory = (
                    self._workspace_resource_requests_from_pod(created_pod)
                )
                publish_impl = getattr(
                    type(self._db),
                    "publish_stateless_thread_workspace_runtime",
                    None,
                )
                if not callable(publish_impl) or not await publish_impl(
                    self._db,
                    owner.id,
                    generation=stateless_creation_generation,
                    runtime_incarnation=runtime_incarnation,
                    pod_name=pod_name,
                    namespace=self._namespace,
                ):
                    logger.error(
                        "Lost stateless runtime publication authority for thread %s",
                        owner.id,
                    )
                    return False
            else:
                (
                    created_pod,
                    reused_existing_pod,
                ) = await self._create_pod_resolving_teardown(
                    pod_manifest,
                    pod_name,
                    owner=owner,
                )
                if created_pod is None:
                    return False
                runtime_incarnation = self._require_workspace_pod_owner(
                    created_pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    expected_network_tier=network_tier,
                )
                observed_seed = self._require_stateless_pod_storage_binding(
                    created_pod,
                    owner=owner,
                    expected_pvc_name=pvc_name,
                    expected_seed_configmap=(
                        _UNSPECIFIED_RESOURCE_BINDING
                        if reused_existing_pod
                        else seed_cm
                    ),
                )
                if reused_existing_pod:
                    if observed_seed is not None:
                        existing_seed_bound = True
                        seed_configmap = await asyncio.to_thread(
                            self._core_api.read_namespaced_config_map,
                            name=observed_seed,
                            namespace=self._namespace,
                        )
                        try:
                            self._require_stateless_seed_configmap_identity(
                                seed_configmap,
                                owner=owner,
                                pod_name=pod_name,
                            )
                        except WorkspaceRuntimeAuthorityError:
                            await self._require_legacy_seed_configmap_migration(
                                seed_configmap,
                                owner=owner,
                                pod_name=pod_name,
                            )
                        seed_cm = observed_seed
                    elif seed_cm is not None:
                        if not await self._delete_seed_configmap(
                            pod_name,
                            expected_owner=owner,
                        ):
                            return False
                        seed_cm = None
            # Make the pod own the seed ConfigMap so K8s GCs it on teardown.
            # created_pod is None only for the idempotent live-pod case, where
            # the existing pod already owns its (same-named) ConfigMap.
            if created_pod is not None:
                adopted_seed = await self._adopt_configmap(
                    seed_cm,
                    created_pod,
                    expected_owner=owner,
                    expected_creation_generation=(
                        stateless_creation_generation if strict_stateless else None
                    ),
                )
                if adopted_seed is not True:
                    return False
            # PVC-backed workspaces (jobs AND sessions) get a stable headless
            # Service so the agent dials a constant DNS name that survives pod
            # recreates (reattach/recovery) instead of an ephemeral pod IP. Same
            # gate as the PVC; kept across idle reaps, dropped on release.
            if pvc_name:
                service_created = await self._create_service(
                    owner,
                    require_exact_owner=True,
                )
                if not service_created:
                    # Ready publishes this stable DNS name. Preserve the exact
                    # UID+attempt marker until the idempotent Service exists;
                    # continuation retries it without another Pod create.
                    return False
            logger.info(
                "Workspace container created: %s (%s %s)",
                pod_name,
                owner.kind,
                owner.id,
            )
            if not strict_stateless:
                await self._set_context(
                    owner,
                    {
                        "status": "created",
                        "provisioner": "k8s",
                        "pod_name": pod_name,
                        "namespace": self._namespace,
                        WORKSPACE_RUNTIME_INCARNATION_KEY: runtime_incarnation,
                        **(
                            {
                                CANVAS_WORKSPACE_GENERATION_KEY: None,
                            }
                            if owner.kind == "session"
                            else {}
                        ),
                    },
                )

            # Open a compute-metering interval (Slice 4b) — best-effort, billed on
            # the accepted Pod's immutable requests for strict stateless
            # runtimes. Admission may mutate those values, and a later retry
            # must not bill using caller/config drift. Pinned/job behavior keeps
            # its legacy requested-value semantics.
            await workspace_metering.open_interval(
                self._db,
                owner,
                tier="sandbox",
                cpu=metered_cpu,
                memory=metered_memory,
            )

            # Wait for pod IP. A reattach gets the longer window so a transient
            # node reboot recovers without discarding data; a fresh create keeps
            # the standard 120s.
            ready_timeout = self._reattach_ready_timeout if pvc_reattach else 120
            pod_ip = await self._wait_for_ready(
                pod_name,
                timeout=ready_timeout,
                expected_owner=owner,
                expected_runtime_incarnation=runtime_incarnation,
                expected_creation_generation=(
                    stateless_creation_generation if strict_stateless else None
                ),
                expected_network_tier=network_tier,
                expected_pvc_name=pvc_name,
                expected_seed_configmap=seed_cm,
            )
            if pod_ip:
                if seed_cm is not None:
                    ready_seed = await asyncio.to_thread(
                        self._core_api.read_namespaced_config_map,
                        name=seed_cm,
                        namespace=self._namespace,
                    )
                    self._require_stateless_seed_configmap_identity(
                        ready_seed,
                        owner=owner,
                        generation=(
                            stateless_creation_generation if strict_stateless else None
                        ),
                        pod_name=pod_name,
                    )
                    self._require_seed_configmap_pod_owner_reference(
                        ready_seed,
                        pod_name=pod_name,
                        runtime_incarnation=runtime_incarnation,
                    )
                if pvc_name is not None:
                    ready_claim = await asyncio.to_thread(
                        self._core_api.read_namespaced_persistent_volume_claim,
                        name=pvc_name,
                        namespace=self._namespace,
                    )
                    self._require_stateless_pvc_identity(
                        ready_claim,
                        owner=owner,
                        pvc_name=pvc_name,
                    )
                    ready_service = await asyncio.to_thread(
                        self._core_api.read_namespaced_service,
                        name=owner.pod_name,
                        namespace=self._namespace,
                    )
                    self._require_stateless_service_identity(
                        ready_service,
                        owner=owner,
                    )
                canvas_generation = None
                if owner.kind == "session" and self._db:
                    try:
                        # Now that sessions can be PVC-backed, this resolves the
                        # trusted identity to the VOLUME (`k8s-pvc:…`) rather
                        # than the pod — which is the point: the backing_id (and
                        # so the Canvas workspace_generation) stops churning on
                        # every pod recreate. A session that predates the PVC
                        # switch re-binds once, from pod UID to PVC UID.
                        (
                            backing_id,
                            fingerprint,
                            trusted_runtime_incarnation,
                        ) = await self._trusted_pod_ssh_identity(
                            pod_name,
                            pvc_name=pvc_name,
                            expected_owner=owner,
                            expected_runtime_incarnation=runtime_incarnation,
                            expected_creation_generation=(
                                stateless_creation_generation
                                if strict_stateless
                                else None
                            ),
                            expected_network_tier=network_tier,
                            expected_seed_configmap=seed_cm,
                        )
                        if strict_stateless:
                            complete_impl = getattr(
                                type(self._db),
                                "complete_stateless_thread_workspace_creation",
                                None,
                            )
                            completed = (
                                await complete_impl(
                                    self._db,
                                    owner.id,
                                    generation=stateless_creation_generation,
                                    runtime_incarnation=(trusted_runtime_incarnation),
                                    backing_id=backing_id,
                                    ssh_host_key_fingerprint=fingerprint,
                                    pod_ip=pod_ip,
                                    port=30022,
                                    host=(
                                        self._workspace_dns(owner) if pvc_name else None
                                    ),
                                )
                                if callable(complete_impl)
                                else None
                            )
                            if not completed:
                                logger.error(
                                    "Lost exact Ready publication authority for "
                                    "stateless thread %s",
                                    owner.id,
                                )
                                return False
                            canvas_generation = completed.get("workspace_generation")
                            runtime_incarnation = trusted_runtime_incarnation
                        else:
                            binding = await self._db.bind_thread_workspace_backing(
                                owner.id,
                                backing_kind="remote",
                                backing_id=backing_id,
                                ssh_host_key_fingerprint=fingerprint,
                            )
                            if binding:
                                canvas_generation = binding.get("workspace_generation")
                                if canvas_generation:
                                    runtime_incarnation = trusted_runtime_incarnation
                    except Exception:
                        # The workspace remains usable by its agent, but Canvas
                        # file serving fails closed until a trusted binding is
                        # available. Never substitute SSH TOFU here.
                        logger.exception(
                            "Failed to bind trusted Canvas SSH identity for session %s",
                            owner.id,
                        )
                ready_ctx = {
                    "status": "ready",
                    "provisioner": "k8s",
                    "pod_ip": pod_ip,
                    "port": 30022,
                }
                if owner.kind == "job":
                    # Jobs do not use the Canvas backing bind, but their worker
                    # bundle still needs immutable endpoint provenance.  This
                    # is the exact Pod UID already verified above and used by
                    # deletion/reattach fencing.
                    ready_ctx[WORKSPACE_RUNTIME_INCARNATION_KEY] = runtime_incarnation
                if owner.kind == "session":
                    # Pair this endpoint with the exact binding minted above.
                    # A failed/missing bind publishes null and Canvas fails closed.
                    ready_ctx[CANVAS_WORKSPACE_GENERATION_KEY] = canvas_generation
                    ready_ctx[WORKSPACE_RUNTIME_INCARNATION_KEY] = runtime_incarnation
                # Hand the agent the STABLE Service DNS (not the ephemeral IP) so
                # a reattached/recovered pod is reachable at the same address.
                # The dispatch + resume paths prefer this `host` over `pod_ip`.
                if pvc_name:
                    ready_ctx["host"] = self._workspace_dns(owner)
                if not strict_stateless:
                    await self._set_context(owner, ready_ctx)
                logger.info(
                    "Workspace container ready: %s @ %s (%s %s)",
                    pod_name,
                    pod_ip,
                    owner.kind,
                    owner.id,
                )
                # The legacy Phase-B SFTP task is name-based and detached from
                # lifecycle authority. Do not let it outlive a stateless Ready
                # CAS and race a replacement or terminal retirement.
                if not strict_stateless:
                    asyncio.create_task(self._seed_workspace_state(owner, pod_ip))
            elif (
                not strict_stateless
                and pvc_reattach
                and self._fresh_fallback_enabled
                and await self._pod_volume_attach_failing(pod_name)
            ):
                # Single-replica node-loss fallback (Phase 3b): the reattached
                # volume can't attach here and won't until the dead node holding
                # its lone replica returns. Discard the wedged PVC and recover onto
                # a FRESH empty volume — the agent then clones from Gitea and
                # resumes the Postgres checkpoint (unpushed working-tree files are
                # lost: the accepted cost of single-replica + node loss). Recurses
                # once with fresh=True, which creates a NEW volume (not a reattach),
                # so this branch cannot re-fire.
                logger.warning(
                    "Workspace %s reattach wedged — volume unattachable (likely a "
                    "dead node holding the single replica). Discarding the PVC and "
                    "recovering onto a fresh volume; unpushed files are lost (%s %s)",
                    pod_name,
                    owner.kind,
                    owner.id,
                )
                try:
                    await self.delete_workspace(owner)
                except Exception:
                    logger.exception(
                        "Error deleting wedged pod before fresh recovery (%s)",
                        pod_name,
                    )
                fresh_ok = await self.create_workspace(
                    owner,
                    cpu=cpu,
                    memory=memory,
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    image=image,
                    fresh=True,
                )
                # Record that the working tree was reset (observability for the
                # dispatch/UI: this resume starts from the last push, not the disk).
                await self._set_context(owner, {"workspace_reset": True})
                return fresh_ok
            else:
                logger.warning(
                    "Workspace container created but not ready within timeout: %s (%s %s)",
                    pod_name,
                    owner.kind,
                    owner.id,
                )
                if not strict_stateless:
                    await self._set_context(owner, {"status": "creating"})

            return True
        except Exception as e:
            logger.error(
                "Failed to create workspace container for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            # Legacy/job failures can discard an unreferenced seed. For a
            # submitted stateless attempt, API acceptance is ambiguous and the
            # exact Pod may already depend on it; preserve it for read/adopt
            # continuation and let terminal workspace cleanup remove it.
            if not (
                existing_seed_bound
                or reused_existing_pod
                or (strict_stateless and stateless_attempt_may_have_committed)
            ):
                await self._delete_seed_configmap(
                    pod_name,
                    expected_owner=owner,
                )
            if not strict_stateless:
                await self._set_context(
                    owner,
                    {"status": "failed", "error": str(e)},
                )
            return False

    async def prepare_stateless_workspace_recreation(
        self,
        owner: WorkspaceOwner,
        *,
        expected_runtime_incarnation: str,
        mode: str,
    ) -> str | None:
        """Rotate create authority only after the caller proves exact-terminal.

        The marker is persisted before the UID-preconditioned delete.  Its
        attempt bit remains false until create_workspace has completed all
        non-Pod preparation, so a crash before Pod actuation remains safely
        recoverable by a later lifecycle owner.
        """

        if owner.kind != "session" or not self._k8s_available or self._db is None:
            return None
        try:
            expected_runtime_incarnation = _canonical_runtime_uuid(
                expected_runtime_incarnation,
                label="stateless workspace runtime incarnation",
            )
        except ValueError:
            return None
        if mode not in {"create", "restore"}:
            return None
        if (
            await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
            )
            != "exact_terminal"
        ):
            return None
        prepare_impl = getattr(
            type(self._db), "prepare_stateless_thread_workspace_creation", None
        )
        if not callable(prepare_impl):
            return None
        generation = str(uuid4())
        plan = await prepare_impl(
            self._db,
            owner.id,
            proposed_generation=generation,
            mode=mode,
            expected_runtime_incarnation=expected_runtime_incarnation,
        )
        if (
            not isinstance(plan, dict)
            or plan.get("state") != "prepared"
            or not isinstance(plan.get("creation"), dict)
            or plan["creation"].get("generation") != generation
            or plan["creation"].get("attempted") is not False
        ):
            return None
        deleted = await self._delete_prepared_terminal_workspace_runtime(
            owner,
            generation=generation,
            expected_runtime_incarnation=expected_runtime_incarnation,
        )
        return generation if deleted is True else None

    async def finalize_stateless_workspace_recreation_deletion(
        self,
        owner: WorkspaceOwner,
        *,
        generation: str,
        expected_runtime_incarnation: str,
    ) -> bool:
        """Recover the DB edge after a proven-terminal delete reached 404."""

        if owner.kind != "session" or self._db is None:
            return False
        try:
            generation = _canonical_runtime_uuid(
                generation,
                label="stateless workspace creation generation",
            )
            expected_runtime_incarnation = _canonical_runtime_uuid(
                expected_runtime_incarnation,
                label="stateless workspace runtime incarnation",
            )
        except ValueError:
            return False
        if (
            await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
            )
            != "exact_absent"
        ):
            return False
        return await self._clear_prepared_terminal_workspace_runtime(
            owner,
            generation=generation,
            expected_runtime_incarnation=expected_runtime_incarnation,
        )

    async def _delete_prepared_terminal_workspace_runtime(
        self,
        owner: WorkspaceOwner,
        *,
        generation: str,
        expected_runtime_incarnation: str,
    ) -> bool:
        """UID-delete and observe 404 before clearing durable runtime identity."""

        validate_impl = getattr(
            type(self._db),
            "validate_stateless_thread_workspace_creation_attempt",
            None,
        )
        if not callable(validate_impl) or not await validate_impl(
            self._db,
            owner.id,
            generation=generation,
            attempted=False,
            expected_runtime_incarnation=expected_runtime_incarnation,
        ):
            return False
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
            observed = self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
            if observed != expected_runtime_incarnation:
                return False
            retained_for_process_zero = self._has_stateless_process_zero_finalizer(pod)
            if (
                not retained_for_process_zero
                and not await self._ensure_managed_repository_process_zero_before_delete(
                    owner,
                    pod,
                    expected_runtime_incarnation,
                )
            ):
                return False
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                retained_for_process_zero = False
                if (
                    self._db is None
                    or not await self._db.managed_repository_workspace_process_zero_is_current(
                        owner.id,
                        owner_kind=("thread" if owner.kind == "session" else "job"),
                        scope="workspace_container",
                        provisioner="k8s",
                        runtime_incarnation=expected_runtime_incarnation,
                    )
                ):
                    return False
            else:
                return False
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
                grace_period_seconds=10,
                body={"preconditions": {"uid": expected_runtime_incarnation}},
            )
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                return False
        if retained_for_process_zero:
            if not await self._wait_for_exact_workspace_pod_terminal(
                owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
                timeout=30,
            ):
                return False
            if not await self.release_stateless_workspace_process_zero_finalizer(
                owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
            ):
                return False
        if not await self._wait_for_exact_workspace_pod_absent(
            owner,
            expected_runtime_incarnation=expected_runtime_incarnation,
            timeout=30,
        ):
            # Keep both the old UID and the unattempted marker durable. A later
            # exact-absent retry can finish the guarded clear; no caller can
            # consume the one-shot create while the old object remains.
            return False
        return await self._clear_prepared_terminal_workspace_runtime(
            owner,
            generation=generation,
            expected_runtime_incarnation=expected_runtime_incarnation,
        )

    @staticmethod
    def _has_stateless_process_zero_finalizer(pod: Any) -> bool:
        finalizers = getattr(getattr(pod, "metadata", None), "finalizers", None)
        return isinstance(finalizers, (list, tuple)) and (
            STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER in finalizers
        )

    async def _wait_for_exact_workspace_pod_terminal(
        self,
        owner: WorkspaceOwner,
        *,
        expected_runtime_incarnation: str,
        timeout: float,
    ) -> bool:
        """Wait for positive process-zero evidence on one retained Pod UID."""

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            authority = await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
            )
            if authority == "exact_terminal":
                return True
            if authority in {"exact_absent", "replacement"}:
                return False
            await asyncio.sleep(1)
        return False

    async def release_stateless_workspace_process_zero_finalizer(
        self,
        owner: WorkspaceOwner,
        *,
        expected_runtime_incarnation: str,
    ) -> bool:
        """Release the workspace finalizer after exact terminal proof.

        The public name is retained for compatibility with the original
        stateless-only lifecycle owner.  New pinned job/session workspace and
        IDE Pods use the same exact-UID protocol through the private helper.
        """

        return await self._release_process_zero_finalizer(
            owner,
            pod_name=owner.pod_name,
            expected_runtime_incarnation=expected_runtime_incarnation,
            scope="workspace_container",
        )

    async def _release_process_zero_finalizer(
        self,
        owner: WorkspaceOwner,
        *,
        pod_name: str,
        expected_runtime_incarnation: str,
        scope: str,
        expected_component: str | None = None,
    ) -> bool:
        """Remove only our finalizer after exact terminal-container proof.

        The JSON Patch tests both immutable Pod UID and the selected finalizer
        slot.  A reordered list, a same-name replacement, API absence, or a
        non-terminal Pod is a retryable refusal rather than deletion authority.
        """

        if not self._k8s_available or self._db is None:
            return False
        if scope not in {"workspace_container", "ide"}:
            return False
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            observed = self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
                expected_pod_name=pod_name,
                expected_component=expected_component,
            )
            if observed != expected_runtime_incarnation:
                return False
            if not _pod_has_exact_process_zero(pod):
                return False
            if not await self._db.record_managed_repository_workspace_process_zero(
                owner.id,
                owner_kind=("thread" if owner.kind == "session" else "job"),
                scope=scope,
                provisioner="k8s",
                runtime_incarnation=expected_runtime_incarnation,
            ):
                return False
            # Preserve the original stateless convenience receipt used to
            # settle a committed finalizer-patch whose response is lost.  A
            # pinned session simply declines this optional second projection.
            if owner.kind == "session" and scope == "workspace_container":
                record_terminal = getattr(
                    type(self._db),
                    "record_stateless_thread_workspace_process_zero",
                    None,
                )
                if callable(record_terminal):
                    await record_terminal(
                        self._db,
                        owner.id,
                        runtime_incarnation=expected_runtime_incarnation,
                    )
            finalizers = getattr(getattr(pod, "metadata", None), "finalizers", None)
            if finalizers is None:
                return True
            if not isinstance(finalizers, (list, tuple)):
                return False
            matching = [
                index
                for index, value in enumerate(finalizers)
                if value == STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER
            ]
            if not matching:
                return True
            if len(matching) != 1:
                return False
            index = matching[0]
            await asyncio.to_thread(
                self._core_api.patch_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                body=[
                    {
                        "op": "test",
                        "path": "/metadata/uid",
                        "value": expected_runtime_incarnation,
                    },
                    {
                        "op": "test",
                        "path": f"/metadata/finalizers/{index}",
                        "value": STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER,
                    },
                    {
                        "op": "remove",
                        "path": f"/metadata/finalizers/{index}",
                    },
                ],
                _content_type="application/json-patch+json",
            )
            return True
        except Exception:
            return False

    async def _wait_for_exact_workspace_pod_absent(
        self,
        owner: WorkspaceOwner,
        *,
        expected_runtime_incarnation: str,
        timeout: float,
    ) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=owner.pod_name,
                    namespace=self._namespace,
                )
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    return True
                return False
            observed = str(getattr(getattr(pod, "metadata", None), "uid", "") or "")
            if observed != expected_runtime_incarnation:
                return False
            await asyncio.sleep(1)
        return False

    async def _clear_prepared_terminal_workspace_runtime(
        self,
        owner: WorkspaceOwner,
        *,
        generation: str,
        expected_runtime_incarnation: str,
    ) -> bool:
        # The deterministic seed may not have an ownerReference when the
        # original create response was lost. Remove it only after exact Pod
        # absence, but before exposing the replacement attempt.
        if not await self._delete_seed_configmap(
            owner.pod_name,
            expected_owner=owner,
            expected_pod_uid=expected_runtime_incarnation,
        ):
            return False
        clear_impl = getattr(
            type(self._db),
            "clear_stateless_thread_workspace_runtime_for_recreation",
            None,
        )
        if not callable(clear_impl) or not await clear_impl(
            self._db,
            owner.id,
            generation=generation,
            expected_runtime_incarnation=expected_runtime_incarnation,
        ):
            return False
        await workspace_metering.close_interval(self._db, owner)
        return True

    async def continue_stateless_workspace_creation(
        self,
        owner: WorkspaceOwner,
        *,
        generation: str,
        expected_runtime_incarnation: str | None,
        cpu: str = "500m",
        memory: str = "1Gi",
        cpu_limit: str = "2000m",
        memory_limit: str = "4Gi",
        image: Optional[str] = None,
    ) -> bool:
        """Resume one attempted create without ever issuing create-by-name."""

        del cpu_limit, memory_limit, image
        if owner.kind != "session" or not self._k8s_available or self._db is None:
            return False
        try:
            generation = _canonical_runtime_uuid(
                generation,
                label="stateless workspace creation generation",
            )
            expected_runtime = (
                _canonical_runtime_uuid(
                    expected_runtime_incarnation,
                    label="stateless workspace runtime incarnation",
                )
                if expected_runtime_incarnation is not None
                else None
            )
        except ValueError:
            return False
        validate_impl = getattr(
            type(self._db),
            "validate_stateless_thread_workspace_creation_attempt",
            None,
        )
        if not callable(validate_impl) or not await validate_impl(
            self._db,
            owner.id,
            generation=generation,
            attempted=True,
            expected_runtime_incarnation=expected_runtime,
        ):
            return False
        try:
            network_tier = await self._resolve_network_tier(
                owner.id,
                kind=owner.network_tier_kind,
            )
            pod = await self._read_stateless_creation_pod(
                owner,
                generation=generation,
                expected_runtime_incarnation=expected_runtime,
                expected_network_tier=network_tier,
                expected_pvc_name=_UNSPECIFIED_RESOURCE_BINDING,
                expected_seed_configmap=_UNSPECIFIED_RESOURCE_BINDING,
            )
            if pod is None:
                # Once attempted, Kubernetes absence is an absorbing safe hold:
                # the original API call might have reached a partitioned node.
                return False
            runtime_incarnation = self._require_stateless_pod_identity(
                pod,
                owner=owner,
                generation=generation,
                expected_runtime_incarnation=expected_runtime,
                expected_network_tier=network_tier,
                expected_pvc_name=_UNSPECIFIED_RESOURCE_BINDING,
                expected_seed_configmap=_UNSPECIFIED_RESOURCE_BINDING,
            )
            pvc_name = self._workspace_pvc_name_from_pod(pod, owner=owner)
            actual_cpu, actual_memory = self._workspace_resource_requests_from_pod(pod)
            pvc_uid: str | None = None
            pvc_storage_class: str | None = None
            if pvc_name is not None:
                claim = await asyncio.to_thread(
                    self._core_api.read_namespaced_persistent_volume_claim,
                    name=pvc_name,
                    namespace=self._namespace,
                )
                pvc_uid = self._require_stateless_pvc_identity(
                    claim,
                    owner=owner,
                    pvc_name=pvc_name,
                    allow_any_storage_class=True,
                )
                pvc_storage_class = str(
                    getattr(getattr(claim, "spec", None), "storage_class_name", "")
                    or ""
                )
            seed_configmap = self._require_stateless_pod_storage_binding(
                pod,
                owner=owner,
                expected_pvc_name=pvc_name,
                expected_seed_configmap=_UNSPECIFIED_RESOURCE_BINDING,
            )
            publish_impl = getattr(
                type(self._db),
                "publish_stateless_thread_workspace_runtime",
                None,
            )
            if not callable(publish_impl) or not await publish_impl(
                self._db,
                owner.id,
                generation=generation,
                runtime_incarnation=runtime_incarnation,
                pod_name=owner.pod_name,
                namespace=self._namespace,
            ):
                return False

            if (
                await self._adopt_configmap(
                    seed_configmap,
                    pod,
                    expected_owner=owner,
                    expected_creation_generation=generation,
                )
                is not True
            ):
                return False

            if pvc_name and not await self._create_service(
                owner,
                require_exact_owner=True,
            ):
                return False
            await workspace_metering.open_interval(
                self._db,
                owner,
                tier="sandbox",
                cpu=actual_cpu,
                memory=actual_memory,
            )
            ready_timeout = self._reattach_ready_timeout if pvc_name else 120
            pod_ip = await self._wait_for_ready(
                owner.pod_name,
                timeout=ready_timeout,
                expected_owner=owner,
                expected_runtime_incarnation=runtime_incarnation,
                expected_creation_generation=generation,
                expected_network_tier=network_tier,
                expected_pvc_name=pvc_name,
                expected_seed_configmap=seed_configmap,
            )
            if not pod_ip:
                return True
            (
                backing_id,
                fingerprint,
                trusted_runtime,
            ) = await self._trusted_pod_ssh_identity(
                owner.pod_name,
                pvc_name=pvc_name,
                expected_owner=owner,
                expected_runtime_incarnation=runtime_incarnation,
                expected_creation_generation=generation,
                expected_network_tier=network_tier,
                expected_seed_configmap=seed_configmap,
                expected_pvc_uid=pvc_uid,
                expected_pvc_storage_class=pvc_storage_class,
            )
            complete_impl = getattr(
                type(self._db),
                "complete_stateless_thread_workspace_creation",
                None,
            )
            completed = (
                await complete_impl(
                    self._db,
                    owner.id,
                    generation=generation,
                    runtime_incarnation=trusted_runtime,
                    backing_id=backing_id,
                    ssh_host_key_fingerprint=fingerprint,
                    pod_ip=pod_ip,
                    port=30022,
                    host=self._workspace_dns(owner) if pvc_name else None,
                )
                if callable(complete_impl)
                else None
            )
            if not completed:
                return False
            logger.info(
                "Stateless workspace create continued to Ready: %s (%s)",
                owner.pod_name,
                owner.id,
            )
            # Phase-B seeding is intentionally skipped for stateless sessions:
            # its legacy SFTP task is name-based, unpinned, and detached from
            # lifecycle authority. Pinned/job behavior remains unchanged.
            return True
        except (WorkspaceRuntimeAuthorityError, WorkspaceSSHAuthenticationError):
            return False
        except Exception:
            logger.exception(
                "Failed to continue exact stateless workspace %s", owner.id
            )
            return False

    async def _read_stateless_creation_pod(
        self,
        owner: WorkspaceOwner,
        *,
        generation: str,
        expected_runtime_incarnation: str | None,
        expected_network_tier: str,
        expected_pvc_name: str | None,
        expected_seed_configmap: str | None | object,
    ) -> Any | None:
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod authority probe failed"
            ) from exc
        self._require_stateless_pod_identity(
            pod,
            owner=owner,
            generation=generation,
            expected_runtime_incarnation=expected_runtime_incarnation,
            expected_network_tier=expected_network_tier,
            expected_pvc_name=expected_pvc_name,
            expected_seed_configmap=expected_seed_configmap,
        )
        seed_configmap = self._require_stateless_pod_storage_binding(
            pod,
            owner=owner,
            expected_pvc_name=expected_pvc_name,
            expected_seed_configmap=expected_seed_configmap,
        )
        if seed_configmap is not None:
            try:
                configmap = await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=seed_configmap,
                    namespace=self._namespace,
                )
                self._require_stateless_seed_configmap_identity(
                    configmap,
                    owner=owner,
                    generation=generation,
                )
            except Exception as error:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace seed ConfigMap authority changed"
                ) from error
        if _pod_has_exact_process_zero(pod):
            raise WorkspaceRuntimeAuthorityError("authorized workspace Pod is terminal")
        return pod

    async def _create_stateless_pod_once(
        self,
        owner: WorkspaceOwner,
        pod_manifest: dict,
        *,
        generation: str,
        expected_network_tier: str,
        expected_pvc_name: str | None,
        expected_seed_configmap: str | None,
    ) -> Any | None:
        """Issue the one authorized create, adopting only its exact response."""

        try:
            pod = await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
        except Exception as exc:
            if getattr(exc, "status", None) != 409:
                raise
            # A client-side retry may surface 409 after the original create
            # reached the apiserver.  Adopt only the exact annotated object;
            # never wait out or replace an unrelated deterministic-name Pod.
            return await self._read_stateless_creation_pod(
                owner,
                generation=generation,
                expected_runtime_incarnation=None,
                expected_network_tier=expected_network_tier,
                expected_pvc_name=expected_pvc_name,
                expected_seed_configmap=expected_seed_configmap,
            )
        if pod is None:
            # Some Kubernetes mocks/clients omit the response body. Re-read the
            # object, still under the exact annotation/owner fence.
            return await self._read_stateless_creation_pod(
                owner,
                generation=generation,
                expected_runtime_incarnation=None,
                expected_network_tier=expected_network_tier,
                expected_pvc_name=expected_pvc_name,
                expected_seed_configmap=expected_seed_configmap,
            )
        self._require_stateless_pod_identity(
            pod,
            owner=owner,
            generation=generation,
            expected_network_tier=expected_network_tier,
            expected_pvc_name=expected_pvc_name,
            expected_seed_configmap=expected_seed_configmap,
        )
        return pod

    def _require_stateless_pod_identity(
        self,
        pod: Any,
        *,
        owner: WorkspaceOwner,
        generation: str,
        expected_runtime_incarnation: str | None = None,
        expected_network_tier: str | None = None,
        expected_pvc_name: str | None | object = _UNSPECIFIED_RESOURCE_BINDING,
        expected_seed_configmap: str | None | object = (_UNSPECIFIED_RESOURCE_BINDING),
    ) -> str:
        metadata = getattr(pod, "metadata", None)
        labels = getattr(metadata, "labels", None)
        annotations = getattr(metadata, "annotations", None)
        opposite_owner_label = (
            "srw/job-id" if owner.kind == "session" else "srw/thread-id"
        )
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod identity metadata is malformed"
            )
        if (
            str(getattr(metadata, "name", "") or "") != owner.pod_name
            or str(getattr(metadata, "namespace", "") or "") != self._namespace
            or labels.get(owner.label_key) != owner.id
            or labels.get("app") != "srw-workspace"
            or labels.get("srw/component") != owner.component_label
            or labels.get("srw.io/component") != "agent-workspace"
            or opposite_owner_label in labels
            or (
                expected_network_tier is not None
                and labels.get("srw.io/network-tier") != expected_network_tier
            )
            or annotations.get(WORKSPACE_RUNTIME_CREATION_ANNOTATION) != generation
            or getattr(metadata, "deletion_timestamp", None) is not None
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod owner or creation authority changed"
            )
        self._require_stateless_pod_storage_binding(
            pod,
            owner=owner,
            expected_pvc_name=expected_pvc_name,
            expected_seed_configmap=expected_seed_configmap,
        )
        try:
            runtime_incarnation = _canonical_runtime_uuid(
                str(getattr(metadata, "uid", "") or ""),
                label="workspace Pod UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc
        if (
            expected_runtime_incarnation is not None
            and runtime_incarnation != expected_runtime_incarnation
        ):
            raise WorkspaceRuntimeAuthorityError("workspace Pod UID changed")
        return runtime_incarnation

    def _require_workspace_pod_owner(
        self,
        pod: Any,
        *,
        owner: WorkspaceOwner,
        allow_owner_unlabeled: bool,
        allow_terminating: bool = False,
        expected_network_tier: str | None = None,
        expected_pod_name: str | None = None,
        expected_component: str | None = None,
    ) -> str:
        """Fence deterministic-name Pod reuse/deletion across owner prefixes."""

        metadata = getattr(pod, "metadata", None)
        labels = getattr(metadata, "labels", None)
        observed_namespace = getattr(metadata, "namespace", None)
        if (
            str(getattr(metadata, "name", "") or "")
            != (expected_pod_name or owner.pod_name)
            or (
                isinstance(observed_namespace, str)
                and observed_namespace
                and observed_namespace != self._namespace
            )
            or (not allow_owner_unlabeled and observed_namespace != self._namespace)
            or (
                not allow_terminating
                and getattr(metadata, "deletion_timestamp", None) is not None
            )
        ):
            raise WorkspaceRuntimeAuthorityError("workspace Pod identity changed")
        if not isinstance(labels, dict):
            if labels is not None or not allow_owner_unlabeled:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod owner labels are malformed"
                )
        else:
            owner_labels_present = any(
                key in labels for key in ("srw/job-id", "srw/thread-id")
            )
            if not owner_labels_present:
                if not allow_owner_unlabeled:
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace Pod owner labels are missing"
                    )
            elif (
                labels.get(owner.label_key) != owner.id
                or ("srw/job-id" if owner.kind == "session" else "srw/thread-id")
                in labels
                or labels.get("app") != "srw-workspace"
                or labels.get("srw/component")
                != (expected_component or owner.component_label)
                or labels.get("srw.io/component") != "agent-workspace"
                or (
                    expected_network_tier is not None
                    and labels.get("srw.io/network-tier") != expected_network_tier
                )
            ):
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod belongs to another owner"
                )
        try:
            return _canonical_runtime_uuid(
                str(getattr(metadata, "uid", "") or ""),
                label="workspace Pod UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc

    def _require_stateless_pod_storage_binding(
        self,
        pod: Any,
        *,
        owner: WorkspaceOwner,
        expected_pvc_name: str | None | object,
        expected_seed_configmap: str | None | object,
        expected_pod_name: str | None = None,
    ) -> str | None:
        spec = getattr(pod, "spec", None)
        volumes = getattr(spec, "volumes", None)
        containers = getattr(spec, "containers", None)
        if not isinstance(volumes, (list, tuple)) or not isinstance(
            containers, (list, tuple)
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod storage declaration is malformed"
            )
        workspace_volumes = [
            volume
            for volume in volumes
            if _resource_field(volume, "name") == "workspace-data"
        ]
        workspace_containers = [
            container
            for container in containers
            if _resource_field(container, "name") == "workspace"
        ]
        if len(workspace_volumes) != 1 or len(workspace_containers) != 1:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod storage identity changed"
            )
        workspace_volume = workspace_volumes[0]
        pvc_source = _resource_field(
            workspace_volume,
            "persistent_volume_claim",
            "persistentVolumeClaim",
        )
        empty_dir_source = _resource_field(
            workspace_volume,
            "empty_dir",
            "emptyDir",
        )
        if expected_pvc_name is _UNSPECIFIED_RESOURCE_BINDING:
            observed_claim_name = (
                _resource_field(pvc_source, "claim_name", "claimName")
                if pvc_source is not None
                else None
            )
            if (pvc_source is None) == (empty_dir_source is None) or (
                pvc_source is not None
                and (
                    observed_claim_name != _pvc_name_for(owner)
                    or _resource_field(pvc_source, "read_only", "readOnly")
                    not in {None, False}
                )
            ):
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod storage source changed"
                )
        else:
            if expected_pvc_name is None:
                if empty_dir_source is None or pvc_source is not None:
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace Pod emptyDir binding changed"
                    )
            elif (
                pvc_source is None
                or empty_dir_source is not None
                or _resource_field(pvc_source, "claim_name", "claimName")
                != expected_pvc_name
                or _resource_field(pvc_source, "read_only", "readOnly")
                not in {None, False}
            ):
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod PVC binding changed"
                )

        # Volume names are only aliases; the source is the storage authority.
        # Admission or a sidecar must not add a second name for the same PVC and
        # consume it outside the workspace container.
        observed_claim_name = (
            _resource_field(pvc_source, "claim_name", "claimName")
            if pvc_source is not None
            else None
        )
        workspace_source_names = {
            str(_resource_field(volume, "name") or "")
            for volume in volumes
            if observed_claim_name is not None
            and _resource_field(
                _resource_field(
                    volume,
                    "persistent_volume_claim",
                    "persistentVolumeClaim",
                ),
                "claim_name",
                "claimName",
            )
            == observed_claim_name
        }
        if observed_claim_name is not None and workspace_source_names != {
            "workspace-data"
        }:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod PVC source has an alias"
            )
        mounts = _resource_field(
            workspace_containers[0],
            "volume_mounts",
            "volumeMounts",
        )
        if not isinstance(mounts, (list, tuple)):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod volume mounts are malformed"
            )
        workspace_mounts = [
            mount
            for mount in mounts
            if _resource_field(mount, "name") == "workspace-data"
        ]
        if (
            len(workspace_mounts) != 1
            or _resource_field(workspace_mounts[0], "mount_path", "mountPath")
            != "/home/agent-host"
            or _resource_field(workspace_mounts[0], "sub_path", "subPath")
            not in {None, ""}
            or _resource_field(
                workspace_mounts[0],
                "sub_path_expr",
                "subPathExpr",
            )
            not in {None, ""}
            or _resource_field(workspace_mounts[0], "read_only", "readOnly")
            not in {None, False}
            or _resource_field(
                workspace_mounts[0],
                "mount_propagation",
                "mountPropagation",
            )
            is not None
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod workspace mount changed"
            )

        workspace_devices = (
            _resource_field(
                workspace_containers[0],
                "volume_devices",
                "volumeDevices",
            )
            or []
        )
        if not isinstance(workspace_devices, (list, tuple)) or any(
            _resource_field(device, "name") in workspace_source_names
            or _resource_field(device, "name") == "workspace-data"
            for device in workspace_devices
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod workspace volume device changed"
            )

        seed_volumes = [
            volume
            for volume in volumes
            if _resource_field(volume, "name") == "code-server-config"
        ]
        seed_mounts = [
            mount
            for mount in mounts
            if _resource_field(mount, "name") == "code-server-config"
        ]
        deterministic_seed_name = self._seed_configmap_name(
            expected_pod_name or owner.pod_name
        )
        seed_source_names = {
            str(_resource_field(volume, "name") or "")
            for volume in volumes
            if _resource_field(
                _resource_field(volume, "config_map", "configMap"),
                "name",
            )
            == deterministic_seed_name
        }
        if seed_source_names and seed_source_names != {"code-server-config"}:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod seed ConfigMap source has an alias"
            )
        sensitive_volume_names = {
            "workspace-data",
            "code-server-config",
            *workspace_source_names,
            *seed_source_names,
        }
        for container_field in (
            "containers",
            "init_containers",
            "ephemeral_containers",
        ):
            raw_containers = getattr(spec, container_field, None) or []
            if not isinstance(raw_containers, (list, tuple)):
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod container declaration is malformed"
                )
            for container in raw_containers:
                if container is workspace_containers[0]:
                    continue
                for mount in (
                    _resource_field(
                        container,
                        "volume_mounts",
                        "volumeMounts",
                    )
                    or []
                ):
                    if _resource_field(mount, "name") in sensitive_volume_names:
                        raise WorkspaceRuntimeAuthorityError(
                            "workspace Pod storage has another consumer"
                        )
                for device in (
                    _resource_field(
                        container,
                        "volume_devices",
                        "volumeDevices",
                    )
                    or []
                ):
                    if _resource_field(device, "name") in sensitive_volume_names:
                        raise WorkspaceRuntimeAuthorityError(
                            "workspace Pod storage has another consumer"
                        )
        if not seed_volumes and not seed_mounts:
            if (
                expected_seed_configmap is not _UNSPECIFIED_RESOURCE_BINDING
                and expected_seed_configmap is not None
            ):
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod seed ConfigMap binding disappeared"
                )
            return None
        if len(seed_volumes) != 1 or len(seed_mounts) != 1:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod seed ConfigMap binding changed"
            )
        config_map_source = _resource_field(
            seed_volumes[0],
            "config_map",
            "configMap",
        )
        seed_name = _resource_field(config_map_source, "name")
        deterministic_name = deterministic_seed_name
        if (
            config_map_source is None
            or seed_name != deterministic_name
            or _resource_field(seed_mounts[0], "mount_path", "mountPath")
            != "/mnt/code-server-config"
            or _resource_field(seed_mounts[0], "read_only", "readOnly") is not True
            or _resource_field(seed_mounts[0], "sub_path", "subPath") not in {None, ""}
            or _resource_field(seed_mounts[0], "sub_path_expr", "subPathExpr")
            not in {None, ""}
            or (
                expected_seed_configmap is not _UNSPECIFIED_RESOURCE_BINDING
                and expected_seed_configmap != seed_name
            )
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod seed ConfigMap binding changed"
            )

        return str(seed_name)

    def _workspace_pvc_name_from_pod(
        self,
        pod: Any,
        *,
        owner: WorkspaceOwner,
    ) -> str | None:
        """Return an exact Pod's immutable storage plan after validating it."""

        self._require_stateless_pod_storage_binding(
            pod,
            owner=owner,
            expected_pvc_name=_UNSPECIFIED_RESOURCE_BINDING,
            expected_seed_configmap=_UNSPECIFIED_RESOURCE_BINDING,
        )
        spec = getattr(pod, "spec", None)
        volumes = getattr(spec, "volumes", None) or []
        workspace_volume = next(
            volume
            for volume in volumes
            if _resource_field(volume, "name") == "workspace-data"
        )
        pvc_source = _resource_field(
            workspace_volume,
            "persistent_volume_claim",
            "persistentVolumeClaim",
        )
        if pvc_source is None:
            return None
        return str(_resource_field(pvc_source, "claim_name", "claimName"))

    def _workspace_resource_requests_from_pod(self, pod: Any) -> tuple[str, str]:
        containers = getattr(getattr(pod, "spec", None), "containers", None)
        if not isinstance(containers, (list, tuple)):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod resource declaration is malformed"
            )
        workspaces = [
            container
            for container in containers
            if _resource_field(container, "name") == "workspace"
        ]
        if len(workspaces) != 1:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod resource identity changed"
            )
        requests = _resource_field(
            _resource_field(workspaces[0], "resources"),
            "requests",
        )
        if not isinstance(requests, dict):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod resource requests are malformed"
            )
        cpu = requests.get("cpu")
        memory = requests.get("memory")
        if not all(
            isinstance(value, str) and value and "\x00" not in value
            for value in (cpu, memory)
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod resource requests are malformed"
            )
        return cpu, memory

    def _require_workspace_pod_connection_identity(
        self,
        pod: Any,
        *,
        owner: WorkspaceOwner,
        expected_runtime_incarnation: str,
        expected_creation_generation: str | None,
        expected_network_tier: str | None,
        expected_pvc_name: str | None | object,
        expected_seed_configmap: str | None | object,
        expected_pod_name: str | None = None,
        expected_component: str | None = None,
    ) -> str:
        if expected_creation_generation is not None:
            return self._require_stateless_pod_identity(
                pod,
                owner=owner,
                generation=expected_creation_generation,
                expected_runtime_incarnation=expected_runtime_incarnation,
                expected_network_tier=expected_network_tier,
                expected_pvc_name=expected_pvc_name,
                expected_seed_configmap=expected_seed_configmap,
            )
        observed_runtime = self._require_workspace_pod_owner(
            pod,
            owner=owner,
            allow_owner_unlabeled=False,
            expected_network_tier=expected_network_tier,
            expected_pod_name=expected_pod_name,
            expected_component=expected_component,
        )
        if observed_runtime != expected_runtime_incarnation:
            raise WorkspaceRuntimeAuthorityError("workspace Pod UID changed")
        self._require_stateless_pod_storage_binding(
            pod,
            owner=owner,
            expected_pvc_name=expected_pvc_name,
            expected_seed_configmap=expected_seed_configmap,
            expected_pod_name=expected_pod_name,
        )
        return observed_runtime

    async def delete_workspace(
        self,
        owner: WorkspaceOwner,
        *,
        expected_runtime_incarnation: str | None = None,
        wait_for_exact_absence: bool = False,
        exact_absence_timeout_seconds: float = 30.0,
        captured_teardown_uid: str | None = None,
    ) -> bool:
        """Delete the workspace container for a job or persistent thread.

        Returns:
            True if deleted, False otherwise.
        """
        if not self._k8s_available:
            return False
        if wait_for_exact_absence and expected_runtime_incarnation is None:
            return False
        if captured_teardown_uid is not None and (
            expected_runtime_incarnation != captured_teardown_uid
        ):
            return False

        pod_name = owner.pod_name
        already_absent = False
        retained_for_process_zero = False
        observed_runtime_uid: str | None = None
        try:
            observed_pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            observed_runtime_uid = self._require_workspace_pod_owner(
                observed_pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
            retained_for_process_zero = self._has_stateless_process_zero_finalizer(
                observed_pod
            )
            if expected_runtime_incarnation is not None and observed_runtime_uid != str(
                expected_runtime_incarnation
            ):
                return False
            if (
                not retained_for_process_zero
                and not await self._ensure_managed_repository_process_zero_before_delete(
                    owner,
                    observed_pod,
                    observed_runtime_uid,
                )
            ):
                return False
        except Exception as error:
            if getattr(error, "status", None) == 404:
                process_zero_uid: str | None = None
                if owner.kind == "session":
                    read_terminal = getattr(
                        type(self._db),
                        "get_stateless_thread_workspace_process_zero",
                        None,
                    )
                    if callable(read_terminal):
                        process_zero_uid = await read_terminal(
                            self._db,
                            owner.id,
                            expected_runtime_incarnation=(
                                str(expected_runtime_incarnation)
                                if expected_runtime_incarnation is not None
                                else None
                            ),
                        )
                if process_zero_uid is not None:
                    observed_runtime_uid = process_zero_uid
                elif expected_runtime_incarnation is not None and self._db:
                    generic_zero = await self._db.managed_repository_workspace_process_zero_is_current(
                        owner.id,
                        owner_kind=("thread" if owner.kind == "session" else "job"),
                        scope="workspace_container",
                        provisioner="k8s",
                        runtime_incarnation=str(expected_runtime_incarnation),
                    )
                    if generic_zero:
                        observed_runtime_uid = str(expected_runtime_incarnation)
                    else:
                        return False
                else:
                    return False
                already_absent = True
            else:
                logger.error(
                    "Refusing workspace Pod cleanup for %s %s: %s",
                    owner.kind,
                    owner.id,
                    error,
                )
                return False
        try:
            if not already_absent:
                delete_body: dict[str, Any] = {
                    "preconditions": {"uid": observed_runtime_uid}
                }
                if captured_teardown_uid is not None:
                    # Some apiservers do not honor the query parameter alone.
                    # Carry the same bound in DeleteOptions for durable S36 so
                    # it cannot inherit the Pod's ordinary 120-second grace
                    # and outlive the pinned agent's 60-second report timeout.
                    # Legacy/default-off callers retain their exact body.
                    delete_body["gracePeriodSeconds"] = 10
                await asyncio.to_thread(
                    self._core_api.delete_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                    grace_period_seconds=10,
                    body=delete_body,
                )
                logger.info(
                    "Workspace container deletion accepted: %s (%s %s)",
                    pod_name,
                    owner.kind,
                    owner.id,
                )
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                already_absent = True
                logger.debug(
                    "Workspace container already deleted: %s (%s %s)",
                    pod_name,
                    owner.kind,
                    owner.id,
                )
            elif (
                hasattr(e, "status")
                and e.status == 409
                and captured_teardown_uid is not None
            ):
                # The apiserver applied the UID precondition atomically. A
                # conflict means the captured Pod is gone and a same-name
                # replacement now owns the name; never mutate that replacement
                # or its durable endpoint context.
                logger.debug(
                    "Captured workspace Pod already gone; preserving replacement: "
                    "%s (%s %s)",
                    pod_name,
                    owner.kind,
                    owner.id,
                )
                # The captured Pod is gone, but this grouped teardown must not
                # interpret that as authority over the captured PVC/Service:
                # the replacement may already be using those same objects.
                # Returning False makes S36 preserve the entire resource set.
                return False
            else:
                logger.error(
                    "Failed to delete workspace container for %s %s: %s",
                    owner.kind,
                    owner.id,
                    e,
                )
                return False

        if retained_for_process_zero and not already_absent:
            assert observed_runtime_uid is not None
            if not await self._wait_for_exact_workspace_pod_terminal(
                owner,
                expected_runtime_incarnation=observed_runtime_uid,
                timeout=exact_absence_timeout_seconds,
            ):
                return False
            if not await self.release_stateless_workspace_process_zero_finalizer(
                owner,
                expected_runtime_incarnation=observed_runtime_uid,
            ):
                return False
            # A finalizer is a deliberate process-zero retention boundary, so
            # even historical non-strict callers must not publish deletion
            # before its exact object has actually disappeared.
            wait_for_exact_absence = True

        if wait_for_exact_absence and not already_absent:
            absence_runtime_uid = (
                str(expected_runtime_incarnation)
                if expected_runtime_incarnation is not None
                else str(observed_runtime_uid or "")
            )
            if not absence_runtime_uid:
                return False
            if not await self._wait_for_exact_workspace_pod_absent(
                owner,
                expected_runtime_incarnation=absence_runtime_uid,
                timeout=exact_absence_timeout_seconds,
            ):
                # DELETE acceptance is not runtime absence. Keep the immutable
                # UID and endpoint durable so terminal retirement cannot settle
                # or Resume consume a new one-shot create against a terminating
                # same-name object.
                return False

        seed_deleted = await self._delete_seed_configmap(
            pod_name,
            expected_owner=owner,
            expected_pod_uid=observed_runtime_uid,
        )
        if wait_for_exact_absence and not seed_deleted:
            return False
        # A 404 (or the exact wait above) is now authoritative. Clear any stale
        # ready endpoint only after the object is gone; otherwise immediate
        # Resume can race the terminating old name and consume its one attempt.
        await self._set_context(
            owner,
            {
                "status": "deleted",
                "pod_ip": None,
                **(
                    {WORKSPACE_RUNTIME_INCARNATION_KEY: None}
                    if owner.kind == "session"
                    else {}
                ),
            },
        )
        await workspace_metering.close_interval(self._db, owner)
        return True

    async def _ensure_managed_repository_process_zero_before_delete(
        self,
        owner: WorkspaceOwner,
        pod: Any,
        runtime_incarnation: str,
    ) -> bool:
        """Enforce credential-process containment at the lowest Pod boundary."""

        if not self._db:
            return False
        owner_kind = "thread" if owner.kind == "session" else "job"
        if await self._db.managed_repository_workspace_process_zero_is_current(
            owner.id,
            owner_kind=owner_kind,
            scope="workspace_container",
            provisioner="k8s",
            runtime_incarnation=runtime_incarnation,
        ):
            return True

        process_zero = _pod_has_exact_process_zero(pod)
        if not process_zero:
            if not await self._db.claim_managed_repository_workspace_retirement(
                owner.id,
                owner_kind=owner_kind,
                scope="workspace_container",
                provisioner="k8s",
                runtime_incarnation=runtime_incarnation,
            ):
                return False
            try:
                attestation = await self.attest_workspace_runtime(owner)
            except Exception:
                logger.warning(
                    "Workspace delete refused without an exact repository-agent "
                    "retirement endpoint for %s %s",
                    owner.kind,
                    owner.id,
                )
                return False
            if attestation.runtime_incarnation != runtime_incarnation:
                return False
            process_zero = await self._retire_managed_repository_agents(
                WorkspaceTeardownIdentity(
                    pod_uid=runtime_incarnation,
                    pvc_uid=None,
                    service_uid=None,
                    pod_ip=attestation.pod_ip,
                    ssh_host_key_fingerprint=(attestation.ssh_host_key_fingerprint),
                    ssh_port=attestation.port,
                )
            )
            if process_zero:
                process_zero = await self.workspace_pod_authority(
                    owner,
                    expected_runtime_incarnation=runtime_incarnation,
                ) in {"exact_live", "exact_terminal"}
        if not process_zero:
            return False
        return bool(
            await self._db.record_managed_repository_workspace_process_zero(
                owner.id,
                owner_kind=owner_kind,
                scope="workspace_container",
                provisioner="k8s",
                runtime_incarnation=runtime_incarnation,
            )
        )

    async def delete_workspace_pvc(
        self,
        owner: WorkspaceOwner,
        *,
        require_exact_owner: bool = False,
        expected_uid: str | None = None,
    ) -> bool:
        """Reclaim the PVC backing a job or session workspace, if one exists.

        Both names are LIVE under ``WORKSPACE_PVC_ENABLED`` — jobs are
        ``pvc-workspace-*`` and sessions ``pvc-ws-thread-*`` — so this is the
        reclaim half of provisioning, not legacy cleanup. The name comes from
        ``_pvc_name_for``, the same helper ``create_workspace`` uses, so the
        create and delete sides cannot drift onto different volumes.

        Destructive and irreversible: call it only when the owning work is
        genuinely finished. An emptyDir workspace has no PVC, and a PVC already
        gone is a 404 — both are idempotent successes.
        """
        delete_kwargs: dict[str, Any] = {
            "expected_owner": owner if require_exact_owner else None,
        }
        if expected_uid is not None:
            delete_kwargs["expected_uid"] = expected_uid
        return await self._delete_pvc(_pvc_name_for(owner), **delete_kwargs)

    async def capture_workspace_teardown_identity(
        self,
        owner: WorkspaceOwner,
    ) -> WorkspaceTeardownIdentity:
        """Capture S36's exact Pod/PVC/Service identities without SSH.

        A Pod 404 does not imply that its PVC and Service are gone. In that
        case ``pod_uid`` is ``None`` and the residual deterministic resources
        are captured independently under their full owner/spec authority. The
        Pod name is re-read after those captures so a concurrent replacement
        cannot donate its PVC or Service identity to this teardown intent.
        Every ambiguous API failure raises rather than granting cleanup.
        """

        if not self._k8s_available:
            raise WorkspaceRuntimeAuthorityError(
                "Kubernetes workspace identity capture is unavailable"
            )
        pod_absent = False
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                pod_absent = True
                pod_uid = None
                pvc_name = _pvc_name_for(owner)
            else:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod identity capture failed"
                ) from exc
        else:
            pod_uid = self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
            pvc_name = self._workspace_pvc_name_from_pod(pod, owner=owner)

        pvc_uid: str | None = None
        if pvc_name is not None:
            try:
                claim = await asyncio.to_thread(
                    self._core_api.read_namespaced_persistent_volume_claim,
                    name=pvc_name,
                    namespace=self._namespace,
                )
            except Exception as exc:
                if not (pod_absent and getattr(exc, "status", None) == 404):
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace PVC identity capture failed"
                    ) from exc
            else:
                pvc_uid = self._require_stateless_pvc_identity(
                    claim,
                    owner=owner,
                    pvc_name=pvc_name,
                    allow_any_storage_class=True,
                )

        service_uid: str | None
        try:
            service = await asyncio.to_thread(
                self._core_api.read_namespaced_service,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                service_uid = None
            else:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Service identity capture failed"
                ) from exc
        else:
            service_uid = self._require_stateless_service_identity(service, owner=owner)

        if pod_absent:
            # The first 404 and this final 404 bracket the residual captures.
            # Any same-name Pod observed here may already consume those
            # resources, so its appearance makes the whole capture ambiguous.
            try:
                await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=owner.pod_name,
                    namespace=self._namespace,
                )
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace Pod absence recheck failed"
                    ) from exc
            else:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod appeared during teardown identity capture"
                )
        else:
            # Bracket the PVC/Service reads with the exact Pod UID. Without
            # this second read, a same-name replacement could donate its
            # freshly-created named resources to the old teardown intent.
            try:
                confirmed_pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=owner.pod_name,
                    namespace=self._namespace,
                )
                confirmed_uid = self._require_workspace_pod_owner(
                    confirmed_pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    allow_terminating=True,
                )
            except Exception as exc:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod identity recheck failed"
                ) from exc
            if confirmed_uid != pod_uid:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace Pod changed during teardown identity capture"
                )

        return WorkspaceTeardownIdentity(
            pod_uid=pod_uid,
            pvc_uid=pvc_uid,
            service_uid=service_uid,
        )

    async def capture_terminal_workspace_identity(
        self,
        owner: WorkspaceOwner,
    ) -> WorkspaceTeardownIdentity:
        """Capture S36's UID tuple plus the exact SSH snapshot authority."""

        identity = await self.capture_workspace_teardown_identity(owner)
        if identity.pod_uid is None:
            return identity
        authority = await self.workspace_pod_authority(
            owner,
            expected_runtime_incarnation=identity.pod_uid,
        )
        if authority == "exact_terminal":
            return identity
        if authority != "exact_live":
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod is not available for terminal authority capture"
            )
        attestation = await self.attest_workspace_runtime(owner)
        if identity.pod_uid != attestation.runtime_incarnation:
            raise WorkspaceRuntimeAuthorityError(
                "workspace Pod changed between SSH and teardown attestation"
            )
        return WorkspaceTeardownIdentity(
            pod_uid=identity.pod_uid,
            pvc_uid=identity.pvc_uid,
            service_uid=identity.service_uid,
            pod_ip=attestation.pod_ip,
            ssh_host_key_fingerprint=attestation.ssh_host_key_fingerprint,
            ssh_port=attestation.port,
        )

    async def classify_workspace_teardown_identity(
        self,
        owner: WorkspaceOwner,
        identity: WorkspaceTeardownIdentity,
    ) -> str:
        """Classify a failed captured release without adopting stable names.

        ``identity_superseded`` is reserved for a proven Pod/PVC/Service UID
        replacement.  API ambiguity remains ``unknown`` so the S36 journal
        retries; exact captured or absent resources remain ``matched``.
        """

        if not self._k8s_available:
            return "unknown"
        if identity.pod_uid is None:
            pod_authority = (
                "exact_absent"
                if await self._captured_teardown_pod_is_absent(owner)
                else "unknown"
            )
        else:
            pod_authority = await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=identity.pod_uid,
            )
        if pod_authority == "replacement":
            return "identity_superseded"
        if pod_authority == "unknown":
            return "unknown"

        async def _resource_matches(
            *,
            expected_uid: str | None,
            read: Callable[[], Awaitable[Any]],
            authenticate: Callable[[Any], str],
        ) -> str:
            try:
                resource = await read()
            except Exception as exc:
                return "matched" if getattr(exc, "status", None) == 404 else "unknown"
            try:
                observed_uid = authenticate(resource)
            except WorkspaceRuntimeAuthorityError:
                return "identity_superseded"
            if expected_uid is None or observed_uid != expected_uid:
                return "identity_superseded"
            return "matched"

        async def _read_pvc() -> Any:
            return await asyncio.to_thread(
                self._core_api.read_namespaced_persistent_volume_claim,
                name=_pvc_name_for(owner),
                namespace=self._namespace,
            )

        async def _read_service() -> Any:
            return await asyncio.to_thread(
                self._core_api.read_namespaced_service,
                name=owner.pod_name,
                namespace=self._namespace,
            )

        pvc = await _resource_matches(
            expected_uid=identity.pvc_uid,
            read=_read_pvc,
            authenticate=lambda resource: self._require_stateless_pvc_identity(
                resource,
                owner=owner,
                pvc_name=_pvc_name_for(owner),
                allow_any_storage_class=True,
            ),
        )
        if pvc != "matched":
            return pvc
        return await _resource_matches(
            expected_uid=identity.service_uid,
            read=_read_service,
            authenticate=lambda resource: self._require_stateless_service_identity(
                resource,
                owner=owner,
            ),
        )

    async def release_workspace(
        self,
        owner: "WorkspaceOwner",
        *,
        reclaim_volume: bool = True,
        require_snapshot: bool = False,
        expected_runtime_incarnation: str | None = None,
        expected_host_key_fingerprint: str | None = None,
        on_snapshot_captured: Callable[[], Awaitable[bool]] | None = None,
        capture_snapshot: bool = True,
        strict_terminal_snapshot: bool = False,
        terminal_snapshot_generation: str | None = None,
        terminal_snapshot_created_at: str | None = None,
        strict: bool = False,
        teardown_identity: WorkspaceTeardownIdentity | None = None,
        exact_absence_timeout_seconds: float = 30.0,
    ) -> bool:
        """Snapshot a workspace to S3, then delete the pod (and, by default, its PVC).

        Owner-keyed: serves both jobs and sessions. On an emptyDir workspace the
        data dies with the pod, so snapshotting first is what enables resume (a
        fresh pod restores from S3). Snapshot failure is non-fatal.

        Args:
            reclaim_volume: When False, keep the PVC — snapshot and delete the
                pod (and its Service), but leave the volume so a later recreate
                reattaches the real working tree instead of an S3 approximation.
                Required for a PVC-backed session: an ``ended`` thread is still
                RESUMABLE, so unconditionally deleting its volume is data
                destruction on live state a user can legitimately reopen. Pass
                True (the default) only when the owning work is truly terminal.
            exact_absence_timeout_seconds: Maximum time to prove the exact Pod
                UID absent after Kubernetes accepts a strict delete. Existing
                callers retain the historical 30-second bound; captured S36
                supplies 45 seconds around its explicit 10-second grace.

        The headless Service is dropped either way: it is 409-idempotent to
        recreate on the next ``create_workspace``, so unlike the volume it costs
        nothing to lose.

        Returns:
            True if pod deletion succeeded.
        """
        if not self._k8s_available:
            return False

        if teardown_identity is not None:
            return await self._release_captured_workspace(
                owner,
                teardown_identity,
                reclaim_volume=reclaim_volume,
                require_snapshot=require_snapshot,
                expected_runtime_incarnation=expected_runtime_incarnation,
                expected_host_key_fingerprint=expected_host_key_fingerprint,
                on_snapshot_captured=on_snapshot_captured,
                capture_snapshot=capture_snapshot,
                strict_terminal_snapshot=strict_terminal_snapshot,
                terminal_snapshot_generation=terminal_snapshot_generation,
                terminal_snapshot_created_at=terminal_snapshot_created_at,
                strict=strict,
                exact_absence_timeout_seconds=exact_absence_timeout_seconds,
            )

        if expected_runtime_incarnation is not None:
            # Shell retirement and archive are two separate network effects.
            # Re-prove the exact Pod UID at their handoff so a replacement at
            # the stable Service name is never captured or deleted as the old
            # runtime.
            exact_live = await self.workspace_pod_live(
                owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
            )
            if exact_live is not True:
                logger.warning(
                    "Workspace runtime changed before strict release for %s %s",
                    owner.kind,
                    owner.id,
                )
                return False

        status = await self.get_workspace_status(owner)
        if expected_runtime_incarnation is not None and (
            status is None
            or str(status.get("runtime_incarnation") or "")
            != str(expected_runtime_incarnation)
        ):
            return False
        effective_runtime_incarnation = (
            str(expected_runtime_incarnation)
            if expected_runtime_incarnation is not None
            else str((status or {}).get("runtime_incarnation") or "")
        )
        if not effective_runtime_incarnation:
            return False
        if (
            await self.workspace_pod_live(
                owner,
                expected_runtime_incarnation=effective_runtime_incarnation,
            )
            is not True
        ):
            return False
        pod_ip = status.get("pod_ip") if status else None
        ready = status.get("ready") if status else False

        if (
            capture_snapshot
            and self._snapshot_service
            and self._snapshot_service.is_available
            and pod_ip
            and ready
        ):
            try:
                capture_kwargs: dict[str, Any] = {
                    "job_id": owner.id,
                    "ssh_host": pod_ip,
                    "ssh_port": 30022,
                    "source_type": "pod",
                    "entity_type": ("threads" if owner.kind == "session" else "jobs"),
                    "expected_host_key_fingerprint": expected_host_key_fingerprint,
                }
                if strict_terminal_snapshot:
                    capture_kwargs["strict_terminal"] = True
                captured = bool(
                    await self._snapshot_service.capture_vm_snapshot(**capture_kwargs)
                )
                if captured:
                    if (
                        on_snapshot_captured is not None
                        and not await on_snapshot_captured()
                    ):
                        logger.error(
                            "Snapshot acknowledgement lost lifecycle authority "
                            "for %s %s",
                            owner.kind,
                            owner.id,
                        )
                        return False
                    logger.info(
                        "Workspace snapshot captured for %s %s before release",
                        owner.kind,
                        owner.id,
                    )
                elif require_snapshot:
                    logger.error(
                        "Required workspace snapshot was not captured for %s %s",
                        owner.kind,
                        owner.id,
                    )
                    return False
            except Exception:
                if require_snapshot:
                    logger.exception(
                        "Required workspace snapshot failed for %s %s",
                        owner.kind,
                        owner.id,
                    )
                    return False
                logger.exception(
                    "Workspace snapshot failed for %s %s — deleting anyway",
                    owner.kind,
                    owner.id,
                )
        elif require_snapshot:
            logger.error(
                "Required workspace snapshot is unavailable for %s %s",
                owner.kind,
                owner.id,
            )
            return False

        if (
            await self.workspace_pod_live(
                owner,
                expected_runtime_incarnation=effective_runtime_incarnation,
            )
            is not True
        ):
            return False
        delete_kwargs = {
            "expected_runtime_incarnation": effective_runtime_incarnation,
            **(
                {
                    "wait_for_exact_absence": True,
                    "exact_absence_timeout_seconds": exact_absence_timeout_seconds,
                }
                if strict
                else {}
            ),
        }
        deleted = await self.delete_workspace(owner, **delete_kwargs)
        if not deleted:
            return False
        volume_deleted = True
        if reclaim_volume:
            volume_deleted = await self.delete_workspace_pvc(
                owner,
                require_exact_owner=True,
            )
        service_deleted = await self._delete_service(
            owner,
            require_exact_owner=True,
        )
        if strict:
            return bool(volume_deleted and service_deleted)
        return True

    async def _release_captured_workspace(
        self,
        owner: WorkspaceOwner,
        identity: WorkspaceTeardownIdentity,
        *,
        reclaim_volume: bool,
        require_snapshot: bool,
        expected_runtime_incarnation: str | None,
        expected_host_key_fingerprint: str | None,
        on_snapshot_captured: Callable[[], Awaitable[bool]] | None,
        capture_snapshot: bool,
        strict_terminal_snapshot: bool,
        terminal_snapshot_generation: str | None,
        terminal_snapshot_created_at: str | None,
        strict: bool,
        exact_absence_timeout_seconds: float,
    ) -> bool:
        """Release only the Kubernetes objects captured in an S36 intent."""

        if (
            expected_runtime_incarnation is not None
            and expected_runtime_incarnation != identity.pod_uid
        ):
            return False
        if expected_host_key_fingerprint is not None and (
            expected_host_key_fingerprint != identity.ssh_host_key_fingerprint
        ):
            return False

        snapshot_captured = False
        if require_snapshot:
            if (
                not strict_terminal_snapshot
                or identity.pod_uid is None
                or not identity.pod_ip
                or not identity.ssh_host_key_fingerprint
                or terminal_snapshot_generation is None
                or terminal_snapshot_created_at is None
                or not self._snapshot_service
                or not self._snapshot_service.is_available
            ):
                return False
            (
                snapshot_captured,
                _,
            ) = await self._snapshot_service.reconcile_terminal_snapshot_generation(
                owner.id,
                terminal_generation=terminal_snapshot_generation,
                entity_type=("threads" if owner.kind == "session" else "jobs"),
                expected_runtime_incarnation=identity.pod_uid,
                expected_host_key_fingerprint=(identity.ssh_host_key_fingerprint),
            )
        if identity.pod_uid is None:
            if (
                require_snapshot and not snapshot_captured
            ) or not await self._captured_teardown_pod_is_absent(owner):
                return False
            # A Pod 404 cannot prove that a partitioned node stopped the
            # workspace's repo-key ssh-agent, and this intent has no exact UID
            # with which to validate a prior receipt. Never turn absence into
            # credential-process authority.
            return False
        else:
            authority = await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=identity.pod_uid,
            )
            if authority == "unknown":
                return False
            if authority == "replacement":
                # A same-name successor may legitimately reuse the captured
                # PVC and Service.  Preserve the entire resource set; proving
                # only that the old Pod UID is gone is not teardown authority
                # over resources attached to its replacement.
                return False

            if authority == "exact_live":
                status = await self.get_workspace_status(owner)
                if (
                    status is None
                    or str(status.get("runtime_incarnation") or "") != identity.pod_uid
                ):
                    return False
                pod_ip = status.get("pod_ip")
                ready = status.get("ready")
                if identity.pod_ip is not None and pod_ip != identity.pod_ip:
                    return False
                if (
                    not snapshot_captured
                    and capture_snapshot
                    and self._snapshot_service
                    and self._snapshot_service.is_available
                    and pod_ip
                    and ready
                ):
                    capture_kwargs: dict[str, Any] = {
                        "job_id": owner.id,
                        "ssh_host": identity.pod_ip or pod_ip,
                        "ssh_port": identity.ssh_port,
                        "source_type": "pod",
                        "entity_type": (
                            "threads" if owner.kind == "session" else "jobs"
                        ),
                        "expected_host_key_fingerprint": (
                            identity.ssh_host_key_fingerprint
                        ),
                    }
                    if strict_terminal_snapshot:
                        capture_kwargs["strict_terminal"] = True
                    if terminal_snapshot_generation is not None:
                        capture_kwargs.update(
                            {
                                "terminal_generation": terminal_snapshot_generation,
                                "terminal_created_at": terminal_snapshot_created_at,
                                "expected_runtime_incarnation": identity.pod_uid,
                            }
                        )
                    try:
                        captured = bool(
                            await self._snapshot_service.capture_vm_snapshot(
                                **capture_kwargs
                            )
                        )
                    except Exception:
                        if require_snapshot:
                            logger.exception(
                                "Required captured workspace snapshot failed for %s %s",
                                owner.kind,
                                owner.id,
                            )
                            return False
                        logger.exception(
                            "Captured workspace snapshot failed for %s %s",
                            owner.kind,
                            owner.id,
                        )
                        captured = False
                    if captured and on_snapshot_captured is not None:
                        if not await on_snapshot_captured():
                            return False
                    if not captured and require_snapshot:
                        return False
                    snapshot_captured = snapshot_captured or captured
                elif require_snapshot and not snapshot_captured:
                    return False
            elif require_snapshot and not snapshot_captured:
                # A terminal/absent/replacement Pod can no longer produce the
                # required final snapshot. Never call SSH through the stable name.
                return False

        if require_snapshot and not snapshot_captured:
            return False

        # Snapshot first, then contain the exact credential-bearing process
        # namespace before asking Kubernetes to delete it. API acceptance or
        # 404 is not process-zero under a partitioned node. The receipt commits
        # before DELETE so a lost response can be reconciled only against this
        # same Pod UID.
        process_zero = False
        if identity.pod_uid is not None:
            process_zero = bool(
                self._db
                and await self._db.managed_repository_workspace_process_zero_is_current(
                    owner.id,
                    owner_kind=("thread" if owner.kind == "session" else "job"),
                    scope="workspace_container",
                    provisioner="k8s",
                    runtime_incarnation=identity.pod_uid,
                )
            )
            authority = await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=identity.pod_uid,
            )
            if not process_zero and authority == "exact_terminal":
                process_zero = True
            elif not process_zero and authority == "exact_live":
                if not identity.pod_ip or not identity.ssh_host_key_fingerprint:
                    return False
                process_zero = await self._retire_managed_repository_agents(identity)
                if process_zero:
                    authority = await self.workspace_pod_authority(
                        owner,
                        expected_runtime_incarnation=identity.pod_uid,
                    )
                    process_zero = authority in {"exact_live", "exact_terminal"}
            elif authority == "exact_absent":
                process_zero = bool(
                    self._db
                    and await self._db.managed_repository_workspace_process_zero_is_current(
                        owner.id,
                        owner_kind=("thread" if owner.kind == "session" else "job"),
                        scope="workspace_container",
                        provisioner="k8s",
                        runtime_incarnation=identity.pod_uid,
                    )
                )
            if process_zero and authority != "exact_absent":
                process_zero = bool(
                    self._db
                    and await self._db.record_managed_repository_workspace_process_zero(
                        owner.id,
                        owner_kind=("thread" if owner.kind == "session" else "job"),
                        scope="workspace_container",
                        provisioner="k8s",
                        runtime_incarnation=identity.pod_uid,
                    )
                )
        if not process_zero:
            logger.warning(
                "Refusing Kubernetes teardown without exact managed-repository "
                "process-zero for %s %s",
                owner.kind,
                owner.id,
            )
            return False

        # Re-probe at the snapshot/delete handoff. Replacement and 404 prove
        # the captured Pod is gone; neither permits deleting the current name.
        if identity.pod_uid is None:
            if not await self._captured_teardown_pod_is_absent(owner):
                return False
            authority = "exact_absent"
        else:
            authority = await self.workspace_pod_authority(
                owner,
                expected_runtime_incarnation=identity.pod_uid,
            )
            if authority in {"unknown", "replacement"}:
                return False
        pod_deleted = True
        if authority in {"exact_live", "exact_terminal"}:
            pod_deleted = await self.delete_workspace(
                owner,
                expected_runtime_incarnation=identity.pod_uid,
                captured_teardown_uid=identity.pod_uid,
                **(
                    {
                        "wait_for_exact_absence": True,
                        "exact_absence_timeout_seconds": (
                            exact_absence_timeout_seconds
                        ),
                    }
                    if strict
                    else {}
                ),
            )
        if not pod_deleted:
            return False

        volume_deleted = True
        if reclaim_volume and identity.pvc_uid is not None:
            volume_deleted = await self.delete_workspace_pvc(
                owner,
                require_exact_owner=True,
                expected_uid=identity.pvc_uid,
            )
        service_deleted = True
        if identity.service_uid is not None:
            service_deleted = await self._delete_service(
                owner,
                require_exact_owner=True,
                expected_uid=identity.service_uid,
            )
        if strict:
            return bool(volume_deleted and service_deleted)
        return True

    async def _retire_managed_repository_agents(
        self,
        identity: WorkspaceTeardownIdentity,
    ) -> bool:
        """Retire the exact credential-agent namespace on a captured Pod."""

        if not identity.pod_ip or not identity.ssh_host_key_fingerprint:
            return False
        return await retire_managed_repository_processes(
            host=identity.pod_ip,
            port=identity.ssh_port,
            host_key_fingerprint=identity.ssh_host_key_fingerprint,
            operation="Kubernetes managed repository process retirement",
        )

    async def _captured_teardown_pod_is_absent(
        self,
        owner: WorkspaceOwner,
    ) -> bool:
        """Re-prove a Pod-less S36 intent without adopting a same-name Pod."""

        try:
            await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            return getattr(exc, "status", None) == 404
        return False

    async def release_absent_workspace(
        self,
        owner: "WorkspaceOwner",
        *,
        reclaim_volume: bool = False,
        expected_runtime_incarnation: str | None = None,
        strict: bool = False,
    ) -> bool:
        """Finish cleanup only after Kubernetes proves the Pod exactly absent.

        The caller must already hold terminal lifecycle authority preventing a
        same-name successor from being admitted.  This method independently
        requires an ``exact_absent`` Pod-authority result (a Kubernetes 404)
        before it clears stale context or mutates the Service/PVC.  A live or
        terminal object, a replacement UID, and an ambiguous API result all
        fail closed without effects.
        """

        if not self._k8s_available:
            return False
        authority = await self.workspace_pod_authority(
            owner,
            expected_runtime_incarnation=str(expected_runtime_incarnation or ""),
        )
        if authority != "exact_absent":
            logger.warning(
                "Refusing absent-workspace cleanup for %s %s: Pod authority "
                "is %s for retired runtime %r",
                owner.kind,
                owner.id,
                authority,
                expected_runtime_incarnation,
            )
            return False

        # Mirror delete_workspace's 404 branch: absence still needs to clear a
        # stale ready endpoint and close its metering interval.  This happens
        # only after the tri-state probe above has distinguished 404 from API
        # failure; get_workspace_status() deliberately cannot make that claim.
        seed_deleted = await self._delete_seed_configmap(
            owner.pod_name,
            expected_owner=owner,
            expected_pod_uid=expected_runtime_incarnation,
        )
        if strict and not seed_deleted:
            return False

        await self._set_context(
            owner,
            {
                "status": "deleted",
                "pod_ip": None,
                **(
                    {WORKSPACE_RUNTIME_INCARNATION_KEY: None}
                    if owner.kind == "session"
                    else {}
                ),
            },
        )
        await workspace_metering.close_interval(self._db, owner)

        volume_deleted = True
        if reclaim_volume:
            volume_deleted = await self.delete_workspace_pvc(
                owner,
                require_exact_owner=True,
            )
        service_deleted = await self._delete_service(
            owner,
            require_exact_owner=True,
        )
        if strict:
            return bool(seed_deleted and volume_deleted and service_deleted)
        return True

    async def get_workspace_status(self, owner: WorkspaceOwner) -> Optional[dict]:
        """Query the workspace container status.

        Returns:
            Status dict or None if not found.
        """
        if not self._k8s_available:
            return None

        pod_name = owner.pod_name

        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
            phase = pod.status.phase  # Pending, Running, Succeeded, Failed
            pod_ip = pod.status.pod_ip

            ready = False
            if pod.status.container_statuses:
                ready = all(cs.ready for cs in pod.status.container_statuses)

            return {
                "owner_id": owner.id,
                "pod_name": pod_name,
                "phase": phase,
                "pod_ip": pod_ip,
                "ready": ready,
                "runtime_incarnation": str(
                    getattr(getattr(pod, "metadata", None), "uid", "") or ""
                ),
            }
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                return None
            logger.debug(
                "Workspace status query failed for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return None

    async def get_last_termination(self, owner: WorkspaceOwner) -> Optional[dict]:
        """Read the workspace pod's terminated-container cause, for legibility.

        Called on workspace loss BEFORE ``delete_workspace`` (while the Failed
        pod tombstone still exists) so a resource kill surfaces its true cause
        (``OOMKilled`` / ``Evicted``) instead of the opaque downstream SSH error
        the agent happened to hit. Mirrors the agent-pod reap classifier in
        ``agent_provisioner`` — the kill reason lives in ``state.terminated`` or,
        if the container restarted, ``last_state.terminated``.

        Returns ``{phase, pod_reason, container_reason, exit_code, signal}`` or
        ``None`` when the pod is already gone (404) or K8s is unavailable.
        """
        if not self._k8s_available:
            return None
        pod_name = owner.pod_name
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return None
            logger.debug(
                "Termination read failed for %s %s: %s", owner.kind, owner.id, e
            )
            return None
        try:
            self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
        except WorkspaceRuntimeAuthorityError:
            return None

        status = pod.status
        exit_code: Any = None
        container_reason: Any = None
        signal: Any = None
        for cs in getattr(status, "container_statuses", None) or []:
            if cs.name != "workspace":
                continue
            terminated = getattr(getattr(cs, "state", None), "terminated", None)
            if terminated is None:
                # Container restarted (e.g. OOMKilled then restarted): the kill
                # reason lives in last_state.terminated, not state.
                terminated = getattr(
                    getattr(cs, "last_state", None), "terminated", None
                )
            if terminated is not None:
                exit_code = getattr(terminated, "exit_code", None)
                container_reason = getattr(terminated, "reason", None)
                signal = getattr(terminated, "signal", None)
            break
        return {
            "phase": getattr(status, "phase", None),
            # pod-level status.reason is "Evicted" on node-pressure eviction
            "pod_reason": getattr(status, "reason", None),
            "container_reason": container_reason,
            "exit_code": exit_code,
            "signal": signal,
        }

    async def workspace_pod_live(
        self,
        owner: "WorkspaceOwner",
        *,
        expected_runtime_incarnation: str | None = None,
    ) -> Optional[bool]:
        """Drift probe: is the owner's exact workspace pod actually alive?

        Returns:
            ``True``  — pod exists and is ``Running``/``Pending`` (usable or
                        still coming up), and its Kubernetes UID matches
                        ``expected_runtime_incarnation`` when supplied.
            ``False`` — pod is confirmed gone (404), or a terminal tombstone
                        whose containers are all terminated. A same-name
                        replacement is also not the requested incarnation.
            ``None``  — can't tell: no k8s client, or a transient API error.

        Mutation callers MUST treat ``None`` as "assume live" so a probe blip
        (or a non-k8s backend) never triggers a false recreate of a healthy
        workspace. Credential-delivery callers may instead fail closed without
        mutating durable state.
        """
        if not self._k8s_available:
            return None
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return False
            logger.debug(
                "workspace_pod_live probe failed for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return None
        try:
            self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
        except WorkspaceRuntimeAuthorityError:
            return False
        if (
            getattr(getattr(pod, "metadata", None), "deletion_timestamp", None)
            is not None
        ):
            # A terminating Pod may retain running containers throughout its
            # grace period. It is neither stable-live nor quiescence proof.
            return None
        phase = getattr(pod.status, "phase", None)
        if expected_runtime_incarnation is not None:
            runtime_incarnation = str(getattr(pod.metadata, "uid", "") or "")
            if runtime_incarnation != str(expected_runtime_incarnation):
                return False
        if phase in ("Running", "Pending"):
            return True
        if phase in ("Failed", "Succeeded"):
            statuses = getattr(pod.status, "container_statuses", None) or []
            if statuses and all(
                getattr(getattr(status, "state", None), "terminated", None) is not None
                for status in statuses
            ):
                return False
        # Unknown, missing status, or a terminal-looking Pod without complete
        # container termination evidence remains ambiguous under partitions.
        return None

    async def workspace_pod_authority(
        self,
        owner: "WorkspaceOwner",
        *,
        expected_runtime_incarnation: str,
    ) -> str:
        """Classify one exact terminal Pod authority without conflating drift.

        Returns ``exact_live``, ``exact_terminal``, ``exact_absent``,
        ``replacement``, or ``unknown``.  API-object absence is distinct from
        an observed exact UID whose containers are all terminated: only the
        latter proves resident processes have stopped.
        """

        if not self._k8s_available:
            return "unknown"
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=owner.pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return "exact_absent"
            return "unknown"
        observed = str(getattr(getattr(pod, "metadata", None), "uid", "") or "")
        try:
            self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )
        except WorkspaceRuntimeAuthorityError:
            return "replacement"
        if observed != str(expected_runtime_incarnation):
            return "replacement"
        phase = getattr(pod.status, "phase", None)
        if _pod_has_exact_process_zero(pod):
            return "exact_terminal"
        if getattr(pod.metadata, "deletion_timestamp", None) is not None:
            return "unknown"
        if phase in ("Running", "Pending"):
            return "exact_live"
        return "unknown"

    # =========================================================================
    # IDE session pods (on-demand code-server for workspace browsing)
    # =========================================================================

    async def create_ide_pod(
        self,
        job_id: str,
        cpu: str = "250m",
        memory: str = "512Mi",
        cpu_limit: str = "1000m",
        memory_limit: str = "2Gi",
    ) -> Optional[str]:
        """Create a lightweight IDE pod for browsing a job's workspace.

        Unlike ``create_workspace`` (which provisions the agent's execution
        environment), this creates a short-lived pod purely for code-server
        access. The Gitea repo is cloned after the pod is ready via SSH.

        Args:
            job_id: Job UUID.
            cpu/memory: Resource requests (lower than workspace defaults).
            cpu_limit/memory_limit: Resource limits.

        Returns:
            Pod IP if the pod became ready, None on failure.
        """
        if not self._k8s_available:
            return None

        pod_name = f"ide-{job_id[:12]}"
        network_tier = await self._resolve_network_tier(job_id, kind="job")

        owner = WorkspaceOwner.job(job_id)
        seed_files = await self._resolve_ide_seed_files(owner)
        seed_exts = await self._resolve_ide_extensions(owner)
        seed_needs_state = await self._resolve_ide_needs_state(owner, seed_exts)
        seed_cm = await self._create_seed_configmap(
            pod_name,
            seed_files,
            seed_exts,
            needs_state=seed_needs_state,
            expected_owner=owner,
        )

        pod_manifest = self._build_pod_manifest(
            pod_name=pod_name,
            owner=owner,
            image=self._workspace_image,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            network_tier=network_tier,
            seed_configmap=seed_cm,
        )

        # Override labels to distinguish IDE pods from workspace pods
        pod_manifest["metadata"]["labels"]["srw/component"] = "ide-session"

        try:
            reused_existing_pod = False
            try:
                created_pod = await asyncio.to_thread(
                    self._core_api.create_namespaced_pod,
                    namespace=self._namespace,
                    body=pod_manifest,
                )
                if created_pod is None:
                    created_pod = await asyncio.to_thread(
                        self._core_api.read_namespaced_pod,
                        name=pod_name,
                        namespace=self._namespace,
                    )
                runtime_incarnation = self._require_workspace_pod_owner(
                    created_pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    expected_network_tier=network_tier,
                    expected_pod_name=pod_name,
                    expected_component="ide-session",
                )
            except Exception as create_error:
                if getattr(create_error, "status", None) != 409:
                    raise
                reused_existing_pod = True
                created_pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                )
                runtime_incarnation = self._require_workspace_pod_owner(
                    created_pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    expected_network_tier=network_tier,
                    expected_pod_name=pod_name,
                    expected_component="ide-session",
                )
            if self._db is None or not await self._db.merge_ide_session_context(
                job_id,
                {
                    WORKSPACE_RUNTIME_INCARNATION_KEY: runtime_incarnation,
                    "container_name": pod_name,
                    "restore_type": "k8s_container",
                },
            ):
                return None
            observed_seed = self._require_stateless_pod_storage_binding(
                created_pod,
                owner=owner,
                expected_pvc_name=None,
                expected_seed_configmap=(
                    _UNSPECIFIED_RESOURCE_BINDING if reused_existing_pod else seed_cm
                ),
                expected_pod_name=pod_name,
            )
            if reused_existing_pod:
                # Settings can drift while an exact-owner IDE Pod remains
                # live. Its immutable mount plan, not today's desired seed,
                # remains authoritative. Never delete an incumbent-bound CM.
                if observed_seed is not None:
                    existing_seed = await asyncio.to_thread(
                        self._core_api.read_namespaced_config_map,
                        name=observed_seed,
                        namespace=self._namespace,
                    )
                    try:
                        self._require_stateless_seed_configmap_identity(
                            existing_seed,
                            owner=owner,
                            pod_name=pod_name,
                        )
                    except WorkspaceRuntimeAuthorityError:
                        await self._require_legacy_seed_configmap_migration(
                            existing_seed,
                            owner=owner,
                            pod_name=pod_name,
                        )
                elif seed_cm is not None:
                    if not await self._delete_seed_configmap(
                        pod_name,
                        expected_owner=owner,
                    ):
                        return None
                seed_cm = observed_seed
            elif (
                await self._adopt_configmap(
                    seed_cm,
                    created_pod,
                    expected_owner=owner,
                )
                is not True
            ):
                return None
            logger.info("IDE pod created: %s (job %s)", pod_name, job_id)

            pod_ip = await self._wait_for_ready(
                pod_name,
                timeout=90,
                expected_owner=owner,
                expected_runtime_incarnation=runtime_incarnation,
                expected_network_tier=network_tier,
                expected_pvc_name=None,
                expected_seed_configmap=seed_cm,
                expected_pod_name=pod_name,
                expected_component="ide-session",
            )
            if pod_ip:
                if self._db is None:
                    return None
                (
                    backing_id,
                    host_fingerprint,
                    confirmed_runtime,
                ) = await self._trusted_pod_ssh_identity(
                    pod_name,
                    expected_owner=owner,
                    expected_runtime_incarnation=runtime_incarnation,
                    expected_network_tier=network_tier,
                    expected_seed_configmap=seed_cm,
                    expected_pod_name=pod_name,
                    expected_component="ide-session",
                )
                if (
                    confirmed_runtime != runtime_incarnation
                    or not backing_id.startswith("k8s-pod:")
                ):
                    return None
                await self._db.merge_ide_session_context(
                    job_id,
                    {
                        WORKSPACE_RUNTIME_INCARNATION_KEY: runtime_incarnation,
                        "ssh_host_key_fingerprint": host_fingerprint,
                        "pod_ip": pod_ip,
                    },
                )
                logger.info("IDE pod ready: %s @ %s (job %s)", pod_name, pod_ip, job_id)
                # Stream license/globalStorage state in (Phase B); fire-and-forget.
                asyncio.create_task(self._seed_workspace_state(owner, pod_ip))
                return pod_ip

            logger.warning(
                "IDE pod created but not ready within timeout: %s (job %s)",
                pod_name,
                job_id,
            )
            return None
        except Exception as e:
            logger.error("Failed to create IDE pod for job %s: %s", job_id, e)
            return None

    async def delete_ide_pod(self, job_id: str) -> bool:
        """Delete an IDE Pod only after exact repository-agent process-zero."""
        if not self._k8s_available:
            return False

        pod_name = f"ide-{job_id[:12]}"
        owner = WorkspaceOwner.job(job_id)
        runtime_uid: str | None = None
        already_absent = False
        retained_for_process_zero = False

        try:
            try:
                pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                )
                runtime_uid = self._require_workspace_pod_owner(
                    pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    allow_terminating=True,
                    expected_pod_name=pod_name,
                    expected_component="ide-session",
                )
                retained_for_process_zero = self._has_stateless_process_zero_finalizer(
                    pod
                )
                authority = self._classify_exact_ide_pod(pod, runtime_uid)
                if retained_for_process_zero:
                    # The finalizer makes Kubernetes termination—not an SSH
                    # race—the process-zero boundary for all newly created IDE
                    # Pods.  Persist the receipt only after this exact UID is
                    # terminal below.
                    process_zero = False
                elif authority == "exact_terminal":
                    process_zero = True
                elif authority == "exact_live":
                    if (
                        self._db is None
                        or not await self._db.claim_managed_repository_workspace_retirement(
                            job_id,
                            owner_kind="job",
                            scope="ide",
                            provisioner="k8s",
                            runtime_incarnation=runtime_uid,
                        )
                    ):
                        return False
                    attestation = await self.attest_ide_runtime(
                        job_id,
                        expected_runtime_incarnation=runtime_uid,
                    )
                    process_zero = await retire_managed_repository_processes(
                        host=attestation.pod_ip,
                        port=attestation.port,
                        host_key_fingerprint=(attestation.ssh_host_key_fingerprint),
                        operation="IDE managed repository process retirement",
                    )
                    if process_zero:
                        process_zero = await self._ide_pod_authority(
                            job_id,
                            expected_runtime_incarnation=runtime_uid,
                        ) in {"exact_live", "exact_terminal"}
                else:
                    return False
                if not retained_for_process_zero:
                    if (
                        not process_zero
                        or self._db is None
                        or not await self._db.record_managed_repository_workspace_process_zero(
                            job_id,
                            owner_kind="job",
                            scope="ide",
                            provisioner="k8s",
                            runtime_incarnation=runtime_uid,
                        )
                    ):
                        return False
            except Exception as read_error:
                if getattr(read_error, "status", None) != 404:
                    raise
                if self._db is None:
                    return False
                job = await self._db.get_job(job_id)
                context = job.get("context") if isinstance(job, dict) else None
                if isinstance(context, str):
                    context = json.loads(context)
                ide = context.get("ide_session") if isinstance(context, dict) else None
                runtime_uid = (
                    str(ide.get(WORKSPACE_RUNTIME_INCARNATION_KEY) or "")
                    if isinstance(ide, dict)
                    else ""
                )
                if (
                    not runtime_uid
                    or not await self._db.managed_repository_workspace_process_zero_is_current(
                        job_id,
                        owner_kind="job",
                        scope="ide",
                        provisioner="k8s",
                        runtime_incarnation=runtime_uid,
                    )
                ):
                    return False
                already_absent = True
            if not already_absent:
                await asyncio.to_thread(
                    self._core_api.delete_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                    grace_period_seconds=5,
                    body={"preconditions": {"uid": runtime_uid}},
                )
            if retained_for_process_zero and not already_absent:
                deadline = asyncio.get_event_loop().time() + 30
                while asyncio.get_event_loop().time() < deadline:
                    authority = await self._ide_pod_authority(
                        job_id,
                        expected_runtime_incarnation=runtime_uid,
                    )
                    if authority == "exact_terminal":
                        break
                    if authority in {"replacement", "exact_absent"}:
                        return False
                    await asyncio.sleep(1)
                else:
                    return False
                if not await self._release_process_zero_finalizer(
                    owner,
                    pod_name=pod_name,
                    expected_runtime_incarnation=runtime_uid,
                    scope="ide",
                    expected_component="ide-session",
                ):
                    return False
                deadline = asyncio.get_event_loop().time() + 30
                while asyncio.get_event_loop().time() < deadline:
                    if (
                        await self._ide_pod_authority(
                            job_id,
                            expected_runtime_incarnation=runtime_uid,
                        )
                        == "exact_absent"
                    ):
                        break
                    await asyncio.sleep(1)
                else:
                    return False
            logger.info("IDE pod deleted: %s (job %s)", pod_name, job_id)
            return await self._delete_seed_configmap(
                pod_name,
                expected_owner=owner,
                expected_pod_uid=runtime_uid,
            )
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                return await self._delete_seed_configmap(
                    pod_name,
                    expected_owner=owner,
                    expected_pod_uid=runtime_uid,
                )
            logger.error("Failed to delete IDE pod for job %s: %s", job_id, e)
            return False

    @staticmethod
    def _classify_exact_ide_pod(pod: Any, runtime_uid: str) -> str:
        if str(getattr(getattr(pod, "metadata", None), "uid", "") or "") != str(
            runtime_uid
        ):
            return "replacement"
        if _pod_has_exact_process_zero(pod):
            return "exact_terminal"
        if getattr(getattr(pod, "metadata", None), "deletion_timestamp", None):
            return "unknown"
        if getattr(getattr(pod, "status", None), "phase", None) in {
            "Running",
            "Pending",
        }:
            return "exact_live"
        return "unknown"

    async def _ide_pod_authority(
        self,
        job_id: str,
        *,
        expected_runtime_incarnation: str,
    ) -> str:
        if not self._k8s_available:
            return "unknown"
        pod_name = f"ide-{job_id[:12]}"
        owner = WorkspaceOwner.job(job_id)
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            observed = self._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
                expected_pod_name=pod_name,
                expected_component="ide-session",
            )
        except Exception as exc:
            return "exact_absent" if getattr(exc, "status", None) == 404 else "unknown"
        if observed != expected_runtime_incarnation:
            return "replacement"
        return self._classify_exact_ide_pod(pod, observed)

    async def attest_ide_runtime(
        self,
        job_id: str,
        *,
        expected_runtime_incarnation: str,
    ) -> WorkspaceRuntimeAttestation:
        """Attest one live IDE Pod and its pinned SSH identity."""

        pod_name = f"ide-{job_id[:12]}"
        owner = WorkspaceOwner.job(job_id)
        network_tier = await self._resolve_network_tier(job_id, kind="job")
        pod = await asyncio.to_thread(
            self._core_api.read_namespaced_pod,
            name=pod_name,
            namespace=self._namespace,
        )
        observed = self._require_workspace_pod_owner(
            pod,
            owner=owner,
            allow_owner_unlabeled=False,
            expected_network_tier=network_tier,
            expected_pod_name=pod_name,
            expected_component="ide-session",
        )
        if observed != expected_runtime_incarnation:
            raise WorkspaceRuntimeAuthorityError("IDE Pod UID changed")
        status = getattr(pod, "status", None)
        pod_ip = str(getattr(status, "pod_ip", "") or "")
        statuses = getattr(status, "container_statuses", None)
        if (
            getattr(status, "phase", None) != "Running"
            or not pod_ip
            or not statuses
            or any(getattr(item, "ready", None) is not True for item in statuses)
        ):
            raise WorkspaceRuntimeAuthorityError("IDE Pod is not ready")
        backing_id, fingerprint, confirmed = await self._trusted_pod_ssh_identity(
            pod_name,
            expected_owner=owner,
            expected_runtime_incarnation=observed,
            expected_network_tier=network_tier,
            expected_pvc_name=None,
            expected_pod_name=pod_name,
            expected_component="ide-session",
        )
        if confirmed != observed or not backing_id.startswith("k8s-pod:"):
            raise WorkspaceRuntimeAuthorityError("IDE Pod identity changed")
        return WorkspaceRuntimeAttestation(
            backing_id=backing_id,
            workspace_generation=observed,
            runtime_incarnation=observed,
            ssh_host_key_fingerprint=fingerprint,
            host=pod_ip,
            pod_ip=pod_ip,
        )

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _create_pvc(
        self,
        pvc_name: str,
        size: str = "10Gi",
        labels: Optional[dict] = None,
        *,
        expected_owner: WorkspaceOwner | None = None,
    ) -> Optional[str]:
        """Create a PVC for workspace data. Idempotent.

        Returns ``"created"`` for a new volume, ``"reused"`` if the PVC already
        existed (409 — i.e. a reattach), or ``None`` on failure.
        """
        if not self._k8s_available:
            return None

        pvc_labels = {
            "app": "srw-workspace",
            "srw/component": "workspace-pvc",
            "srw.io/component": "agent-workspace",
        }
        if labels:
            pvc_labels.update(labels)

        pvc_manifest = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc_name,
                "namespace": self._namespace,
                "labels": pvc_labels,
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": self._storage_class,
                "resources": {"requests": {"storage": size}},
            },
        }

        try:
            created = await asyncio.to_thread(
                self._core_api.create_namespaced_persistent_volume_claim,
                namespace=self._namespace,
                body=pvc_manifest,
            )
            if expected_owner is not None:
                try:
                    claim = created or await asyncio.to_thread(
                        self._core_api.read_namespaced_persistent_volume_claim,
                        name=pvc_name,
                        namespace=self._namespace,
                    )
                    self._require_stateless_pvc_identity(
                        claim,
                        owner=expected_owner,
                        pvc_name=pvc_name,
                    )
                except Exception as authority_error:
                    logger.error(
                        "Stateless workspace PVC authority failed for %s %s: %s",
                        expected_owner.kind,
                        expected_owner.id,
                        authority_error,
                    )
                    return None
            logger.info(
                "PVC created: %s (storageClass=%s)", pvc_name, self._storage_class
            )
            return "created"
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
                if expected_owner is not None:
                    try:
                        existing = await asyncio.to_thread(
                            self._core_api.read_namespaced_persistent_volume_claim,
                            name=pvc_name,
                            namespace=self._namespace,
                        )
                        self._require_stateless_pvc_identity(
                            existing,
                            owner=expected_owner,
                            pvc_name=pvc_name,
                        )
                    except Exception as authority_error:
                        logger.error(
                            "Refusing stateless PVC reuse for %s %s: %s",
                            expected_owner.kind,
                            expected_owner.id,
                            authority_error,
                        )
                        return None
                logger.debug("PVC already exists: %s", pvc_name)
                return "reused"
            # A 403 here is the workspace capacity guard (Phase 3a): the
            # orchestrator SA is allowed to create PVCs, so the only Forbidden
            # it hits is a ResourceQuota "exceeded quota" rejection. Surface it
            # distinctly so an operator/alert can tell "fleet at capacity" from a
            # genuine infra failure. The caller still fails closed (no emptyDir
            # fallback) — capacity exhaustion must not silently drop durability.
            if hasattr(e, "status") and e.status == 403:
                logger.error(
                    "Workspace capacity quota exceeded — PVC %s rejected by "
                    "ResourceQuota; raise workspace.resourceQuota.maxStorage/"
                    "maxCount or wait for jobs to free PVCs: %s",
                    pvc_name,
                    getattr(e, "body", e),
                )
                return None
            logger.error("Failed to create PVC %s: %s", pvc_name, e)
            return None

    def _require_stateless_pvc_identity(
        self,
        claim: Any,
        *,
        owner: WorkspaceOwner,
        pvc_name: str,
        expected_storage_class: str | None = None,
        allow_any_storage_class: bool = False,
    ) -> str:
        """Bind a reused PVC to the full stateless owner, not its name prefix."""

        metadata = getattr(claim, "metadata", None)
        labels = getattr(metadata, "labels", None)
        opposite_owner_label = (
            "srw/job-id" if owner.kind == "session" else "srw/thread-id"
        )
        agent_claim = pvc_name.startswith("pvc-agent-s-")
        expected_app = "srw-agent" if agent_claim else "srw-workspace"
        expected_component = "agent-workspace-pvc" if agent_claim else "workspace-pvc"
        if (
            str(getattr(metadata, "name", "") or "") != pvc_name
            or str(getattr(metadata, "namespace", "") or "") != self._namespace
            or getattr(metadata, "deletion_timestamp", None) is not None
            or not isinstance(labels, dict)
            or labels.get(owner.label_key) != owner.id
            or labels.get("app") != expected_app
            or labels.get("srw/component") != expected_component
            or labels.get("srw.io/component") != "agent-workspace"
            or opposite_owner_label in labels
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace PVC owner authority changed"
            )
        spec = getattr(claim, "spec", None)
        access_modes = getattr(spec, "access_modes", None)
        observed_storage_class = getattr(spec, "storage_class_name", None)
        required_storage_class = expected_storage_class or self._storage_class
        if (
            not isinstance(access_modes, (list, tuple))
            or list(access_modes) != ["ReadWriteOnce"]
            or (
                not allow_any_storage_class
                and observed_storage_class != required_storage_class
            )
            or (
                allow_any_storage_class
                and (
                    not isinstance(observed_storage_class, str)
                    or not observed_storage_class
                    or "\x00" in observed_storage_class
                )
            )
            or getattr(spec, "volume_mode", None) not in {None, "Filesystem"}
            or getattr(spec, "selector", None) is not None
            or getattr(spec, "data_source", None) is not None
            or getattr(spec, "data_source_ref", None) is not None
        ):
            raise WorkspaceRuntimeAuthorityError("workspace PVC spec changed")
        try:
            return _canonical_runtime_uuid(
                str(getattr(metadata, "uid", "") or ""),
                label="workspace PVC UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc

    async def _delete_pvc(
        self,
        pvc_name: str,
        *,
        expected_owner: WorkspaceOwner | None = None,
        expected_uid: str | None = None,
    ) -> bool:
        """Delete a PVC. Idempotent — 404 treated as success."""
        if not self._k8s_available:
            return False

        claim_uid: str | None = None
        if expected_owner is not None:
            try:
                claim = await asyncio.to_thread(
                    self._core_api.read_namespaced_persistent_volume_claim,
                    name=pvc_name,
                    namespace=self._namespace,
                )
                claim_uid = self._require_stateless_pvc_identity(
                    claim,
                    owner=expected_owner,
                    pvc_name=pvc_name,
                )
                if expected_uid is not None and claim_uid != expected_uid:
                    logger.info(
                        "Captured workspace PVC %s is already gone; refusing "
                        "same-name replacement UID %s",
                        expected_uid,
                        claim_uid,
                    )
                    return True
            except Exception as error:
                if getattr(error, "status", None) == 404:
                    return True
                logger.error(
                    "Refusing stateless PVC cleanup for %s %s: %s",
                    expected_owner.kind,
                    expected_owner.id,
                    error,
                )
                return False

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_persistent_volume_claim,
                name=pvc_name,
                namespace=self._namespace,
                **(
                    {"body": {"preconditions": {"uid": claim_uid}}}
                    if claim_uid is not None
                    else {}
                ),
            )
            if expected_owner is not None:
                try:
                    current_claim = await asyncio.to_thread(
                        self._core_api.read_namespaced_persistent_volume_claim,
                        name=pvc_name,
                        namespace=self._namespace,
                    )
                except Exception as error:
                    if getattr(error, "status", None) == 404:
                        logger.info("PVC deleted: %s", pvc_name)
                        return True
                else:
                    try:
                        current_uid = self._require_stateless_pvc_identity(
                            current_claim,
                            owner=expected_owner,
                            pvc_name=pvc_name,
                        )
                    except Exception:
                        current_uid = None
                    if expected_uid is not None and current_uid != expected_uid:
                        return True
                return False
            logger.info("PVC deleted: %s", pvc_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status in (
                {404, 409} if expected_uid is not None else {404}
            ):
                logger.debug("PVC already deleted: %s", pvc_name)
                return True
            logger.error("Failed to delete PVC %s: %s", pvc_name, e)
            return False

    async def _delete_pvc_and_wait(
        self,
        pvc_name: str,
        timeout: int = 90,
        *,
        expected_owner: WorkspaceOwner | None = None,
    ) -> bool:
        """Delete a PVC and wait (bounded) for it to fully release.

        The single-replica fallback recreates a fresh PVC under the SAME
        deterministic name, so the old (wedged) one must be gone first — else the
        create 409-reuses the dead volume. Best-effort: on timeout we proceed
        anyway (the recreate's 409 path is still safe, just not fresh).
        """
        if not self._k8s_available:
            return False
        if not await self._delete_pvc(pvc_name, expected_owner=expected_owner):
            return False
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.to_thread(
                    self._core_api.read_namespaced_persistent_volume_claim,
                    name=pvc_name,
                    namespace=self._namespace,
                )
            except Exception as e:
                if hasattr(e, "status") and e.status == 404:
                    return True  # fully released
            await asyncio.sleep(2)
        logger.warning(
            "PVC %s still present after %ss — refusing fresh recovery",
            pvc_name,
            timeout,
        )
        return False

    async def _pod_volume_attach_failing(self, pod_name: str) -> bool:
        """True if the pod is wedged on a PVC volume that won't attach/mount.

        The single-replica node-loss fallback uses this to CONFIRM — before
        discarding a PVC (the only data-destructive recovery path) — that the
        holdup really is the volume (its lone replica on a dead node), not an
        unrelated stall (image pull, resource pressure, scheduling). Substrate-
        generic: matches the Kubernetes attach/mount failure events rather than
        any Longhorn-specific state, so it also covers Ceph / EBS / etc.
        """
        if not self._k8s_available:
            return False
        try:
            events = await asyncio.to_thread(
                self._core_api.list_namespaced_event,
                namespace=self._namespace,
                field_selector=f"involvedObject.name={pod_name}",
            )
        except Exception:
            logger.exception("Could not read events for pod %s", pod_name)
            return False
        for ev in getattr(events, "items", None) or []:
            reason = getattr(ev, "reason", "") or ""
            message = (getattr(ev, "message", "") or "").lower()
            if (
                reason in ("FailedAttachVolume", "FailedMount")
                or "multi-attach" in message
            ):
                return True
        return False

    async def _create_service(
        self,
        owner: WorkspaceOwner,
        *,
        require_exact_owner: bool = False,
    ) -> bool:
        """Create a headless Service giving the workspace a STABLE DNS name.

        The pod IP changes on every recreate; the agent caches its SSH dial
        target, so a recreated pod (PVC reattach / crash recovery) leaves the
        agent dialing a dead IP and the work churns to fail-loud (see
        knowledge-base/knowledge/issues/workspace_reattach_ephemeral_ip_reconnect_churn.md). A
        headless Service named after the pod gives a stable address
        ``<pod_name>.<ns>.svc:30022`` that always resolves (selector-matched) to
        the *current* pod — so reattach/recovery reconnects with no IP
        propagation. Headless (clusterIP=None) keeps traffic pod->pod (no DNAT),
        so workspace NetworkPolicies are unaffected, and DNS only resolves to a
        Ready pod (closes the sshd-readiness gap). Idempotent — 409 = success.
        Created with the PVC (both owner kinds) and kept across idle reaps;
        dropped on release/terminal. Cheap to lose — the next create_workspace
        recreates it — which is why ``release_workspace`` may drop the Service
        while KEEPING a still-resumable owner's PVC.
        """
        if not self._k8s_available:
            return False
        svc_name = owner.pod_name  # DNS: <svc_name>.<ns>.svc.cluster.local
        manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": self._namespace,
                "labels": {
                    "app": "srw-workspace",
                    "srw/component": "workspace-svc",
                    "srw.io/component": "agent-workspace",
                    owner.label_key: owner.id,
                },
            },
            "spec": {
                "clusterIP": "None",  # headless → A-record to the current pod IP
                "selector": {"app": "srw-workspace", owner.label_key: owner.id},
                "ports": [
                    {
                        "name": "ssh",
                        "port": 30022,
                        "targetPort": 30022,
                        "protocol": "TCP",
                    },
                    {
                        "name": "code-server",
                        "port": 38080,
                        "targetPort": 38080,
                        "protocol": "TCP",
                    },
                    {
                        "name": "cdp",
                        "port": 9222,
                        "targetPort": 9222,
                        "protocol": "TCP",
                    },
                ],
            },
        }
        try:
            created = await asyncio.to_thread(
                self._core_api.create_namespaced_service,
                namespace=self._namespace,
                body=manifest,
            )
            if require_exact_owner:
                try:
                    service = created or await asyncio.to_thread(
                        self._core_api.read_namespaced_service,
                        name=svc_name,
                        namespace=self._namespace,
                    )
                    self._require_stateless_service_identity(service, owner=owner)
                except Exception as authority_error:
                    logger.error(
                        "Stateless workspace Service authority failed for %s: %s",
                        owner.id,
                        authority_error,
                    )
                    return False
            logger.info("Workspace Service created: %s", svc_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
                if require_exact_owner:
                    try:
                        service = await asyncio.to_thread(
                            self._core_api.read_namespaced_service,
                            name=svc_name,
                            namespace=self._namespace,
                        )
                        self._require_stateless_service_identity(service, owner=owner)
                    except Exception as authority_error:
                        logger.error(
                            "Refusing stateless Service reuse for %s: %s",
                            owner.id,
                            authority_error,
                        )
                        return False
                logger.debug("Workspace Service already exists: %s", svc_name)
                return True
            logger.error("Failed to create workspace Service %s: %s", svc_name, e)
            return False

    async def _delete_service(
        self,
        owner: WorkspaceOwner,
        *,
        require_exact_owner: bool = False,
        expected_uid: str | None = None,
    ) -> bool:
        """Delete the workspace's headless Service. Idempotent — 404 = success."""
        if not self._k8s_available:
            return False
        svc_name = owner.pod_name
        service_uid: str | None = None
        if require_exact_owner:
            try:
                service = await asyncio.to_thread(
                    self._core_api.read_namespaced_service,
                    name=svc_name,
                    namespace=self._namespace,
                )
                service_uid = self._require_stateless_service_identity(
                    service, owner=owner
                )
                if expected_uid is not None and service_uid != expected_uid:
                    logger.info(
                        "Captured workspace Service %s is already gone; refusing "
                        "same-name replacement UID %s",
                        expected_uid,
                        service_uid,
                    )
                    return True
            except Exception as e:
                if getattr(e, "status", None) == 404:
                    return True
                logger.error(
                    "Refusing stateless Service cleanup for %s: %s", owner.id, e
                )
                return False
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_service,
                name=svc_name,
                namespace=self._namespace,
                **(
                    {"body": {"preconditions": {"uid": service_uid}}}
                    if service_uid is not None
                    else {}
                ),
            )
            if require_exact_owner:
                try:
                    current_service = await asyncio.to_thread(
                        self._core_api.read_namespaced_service,
                        name=svc_name,
                        namespace=self._namespace,
                    )
                except Exception as e:
                    if getattr(e, "status", None) == 404:
                        logger.info("Workspace Service deleted: %s", svc_name)
                        return True
                else:
                    try:
                        current_uid = self._require_stateless_service_identity(
                            current_service, owner=owner
                        )
                    except Exception:
                        current_uid = None
                    if expected_uid is not None and current_uid != expected_uid:
                        return True
                return False
            logger.info("Workspace Service deleted: %s", svc_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status in (
                {404, 409} if expected_uid is not None else {404}
            ):
                logger.debug("Workspace Service already deleted: %s", svc_name)
                return True
            logger.error("Failed to delete workspace Service %s: %s", svc_name, e)
            return False

    def _require_stateless_service_identity(
        self,
        service: Any,
        *,
        owner: WorkspaceOwner,
    ) -> str:
        metadata = getattr(service, "metadata", None)
        labels = getattr(metadata, "labels", None)
        opposite_owner_label = (
            "srw/job-id" if owner.kind == "session" else "srw/thread-id"
        )
        if (
            str(getattr(metadata, "name", "") or "") != owner.pod_name
            or str(getattr(metadata, "namespace", "") or "") != self._namespace
            or getattr(metadata, "deletion_timestamp", None) is not None
            or not isinstance(labels, dict)
            or labels.get(owner.label_key) != owner.id
            or labels.get("app") != "srw-workspace"
            or labels.get("srw/component") != "workspace-svc"
            or labels.get("srw.io/component") != "agent-workspace"
            or opposite_owner_label in labels
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace Service owner authority changed"
            )
        spec = getattr(service, "spec", None)
        selector = getattr(spec, "selector", None)
        ports = getattr(spec, "ports", None)
        if not isinstance(ports, (list, tuple)):
            raise WorkspaceRuntimeAuthorityError("workspace Service spec changed")
        observed_ports = {
            (
                str(getattr(port, "name", "") or ""),
                getattr(port, "port", None),
                getattr(port, "target_port", None),
                getattr(port, "protocol", None),
            )
            for port in ports
        }
        expected_ports = {
            ("ssh", 30022, 30022, "TCP"),
            ("code-server", 38080, 38080, "TCP"),
            ("cdp", 9222, 9222, "TCP"),
        }
        if (
            getattr(spec, "cluster_ip", None) != "None"
            or selector != {"app": "srw-workspace", owner.label_key: owner.id}
            or observed_ports != expected_ports
            or getattr(spec, "type", None) not in {None, "ClusterIP"}
        ):
            raise WorkspaceRuntimeAuthorityError("workspace Service spec changed")
        try:
            return _canonical_runtime_uuid(
                str(getattr(metadata, "uid", "") or ""),
                label="workspace Service UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc

    def _workspace_dns(self, owner: WorkspaceOwner) -> str:
        """Stable in-cluster DNS for the workspace's headless Service."""
        return f"{owner.pod_name}.{self._namespace}.svc.cluster.local"

    def _build_workspace_labels(
        self, owner: WorkspaceOwner, network_tier: str = DEFAULT_NETWORK_TIER
    ) -> dict[str, str]:
        labels = {
            "app": "srw-workspace",
            owner.label_key: owner.id,
            "srw/component": owner.component_label,
            # Fleet-wide selector shared with KubeVirt VM workspaces.
            # See knowledge-base/knowledge/features/workspace_network_policy_unification.md
            "srw.io/component": "agent-workspace",
            # Per-project egress tier — selected by one NetworkPolicy per
            # tier in helm. See knowledge-base/knowledge/features/workspace_network_isolation.md §3.
            "srw.io/network-tier": network_tier,
        }
        # Phase 2a: build SHA label parity with agent pods. Lets the
        # lifecycle reconciler enumerate stale workspaces by selector
        # without joining to the jobs table.
        if ":sha-" in self._workspace_image:
            labels["srw/build-sha"] = self._workspace_image.rsplit(":sha-", 1)[-1]
        return labels

    async def _resolve_network_tier(self, work_id: str, kind: str) -> str:
        """Resolve the egress tier for a job/thread, falling back to the default.

        DB-less environments (tests, local dev without a project linkage)
        and pre-migration deployments both surface as a ``None`` row and
        get the default tier. The helm chart always emits the default-tier
        policy, so an unlabeled pod is never reachability-broken — it just
        inherits the strictest policy.
        """
        if self._db is None:
            return DEFAULT_NETWORK_TIER
        try:
            tier = await self._db.get_workspace_network_tier(work_id, kind)
        except Exception:
            logger.exception(
                "Failed to resolve network_tier for %s=%s; using default",
                kind,
                work_id,
            )
            return DEFAULT_NETWORK_TIER
        return tier or DEFAULT_NETWORK_TIER

    # =========================================================================
    # code-server settings seeding
    # =========================================================================

    async def _resolve_ide_seed_files(self, owner: WorkspaceOwner) -> dict:
        """Fetch the owner-user's stored code-server config. ``{}`` on any miss."""
        if not self._db:
            return {}
        try:
            if owner.kind == "job":
                row = await self._db.get_job(owner.id)
            else:
                row = await self._db.get_thread(owner.id)
            if not isinstance(row, dict):
                return {}
            user_id = row.get("user_id")
            if not user_id:
                return {}
            from services.ide_settings import IdeSettingsStore

            return await IdeSettingsStore(self._db).get_ide_files(str(user_id))
        except Exception as e:
            logger.warning(
                "ide seed: failed to resolve config for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return {}

    async def _resolve_ide_extensions(self, owner: WorkspaceOwner) -> dict:
        """Fetch the owner-user's stored extension manifest items. ``{}`` on miss."""
        if not self._db:
            return {}
        try:
            if owner.kind == "job":
                row = await self._db.get_job(owner.id)
            else:
                row = await self._db.get_thread(owner.id)
            if not isinstance(row, dict):
                return {}
            user_id = row.get("user_id")
            if not user_id:
                return {}
            from services.ide_settings import IdeSettingsStore

            return await IdeSettingsStore(self._db).get_extensions(str(user_id))
        except Exception as e:
            logger.warning(
                "ide seed: failed to resolve extensions for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return {}

    async def _owner_user_id(self, owner: WorkspaceOwner) -> Optional[str]:
        """Resolve the owning user_id for a job/thread workspace. None on miss."""
        if not self._db:
            return None
        try:
            if owner.kind == "job":
                row = await self._db.get_job(owner.id)
            else:
                row = await self._db.get_thread(owner.id)
            user_id = row.get("user_id") if isinstance(row, dict) else None
            return str(user_id) if user_id else None
        except Exception as e:
            logger.warning(
                "ide seed: failed to resolve user for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return None

    async def _resolve_ide_needs_state(
        self, owner: WorkspaceOwner, extensions: dict
    ) -> bool:
        """True when the orchestrator should stream license/globalStorage state
        into this pod once Ready (and thus the entrypoint should wait on the
        sentinel). True if any extension needs byte-copy, or the user has a
        captured globalStorage signature (e.g. a paid theme's license)."""
        if not extensions:
            return False
        if any(v.get("source") == "bytes" for v in extensions.values()):
            return True
        user_id = await self._owner_user_id(owner)
        if not user_id:
            return False
        try:
            from services.ide_settings import IdeSettingsStore

            return bool(await IdeSettingsStore(self._db).get_ext_signature(user_id))
        except Exception as e:
            logger.warning(
                "ide seed: needs-state resolve failed for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return False

    async def _seed_workspace_state(self, owner: WorkspaceOwner, pod_ip: str) -> None:
        """Stream globalStorage + bytes extensions into a freshly-Ready container
        and touch the sentinel. Fire-and-forget; failure leaves the IDE usable
        (extension binaries still arrived via Open VSX at boot)."""
        if not self._db or not pod_ip:
            return
        snap = self._snapshot_service
        if not snap or not snap.is_available:
            return
        user_id = await self._owner_user_id(owner)
        if not user_id:
            return
        try:
            from services.ide_profile_store import IdeProfileStore
            from services.ide_settings import IdeSettingsStore, seed_ide_profile

            items = await IdeSettingsStore(self._db).get_extensions(user_id)
            profile = IdeProfileStore(snap._s3, snap._bucket)
            await seed_ide_profile(
                user_id=user_id,
                ssh_host=pod_ip,
                ssh_port=30022,
                profile_store=profile,
                ext_items=items,
            )
        except Exception as e:
            logger.warning(
                "ide seed: stream state failed for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )

    def _seed_configmap_name(self, pod_name: str) -> str:
        return f"code-server-config-{pod_name}"

    async def _create_seed_configmap(
        self,
        pod_name: str,
        files: dict,
        extensions: Optional[dict] = None,
        needs_state: bool = False,
        *,
        expected_owner: WorkspaceOwner | None = None,
        expected_creation_generation: str | None = None,
    ) -> Optional[str]:
        """Create a ConfigMap carrying a self-contained ``seed.sh`` for the pod.

        ``seed.sh`` writes the user's config files and installs their Open-VSX
        extensions (theme-first). When ``needs_state`` is set, an ``expect-state``
        marker is added so the entrypoint waits (bounded) for the orchestrator to
        stream license/globalStorage state in (Phase B). Returns the ConfigMap
        name, or None when there is nothing to seed (or on failure — seeding is
        best-effort and must never block provisioning).
        """
        if (not files and not extensions) or not self._core_api:
            return None
        if expected_owner is None and expected_creation_generation is not None:
            raise WorkspaceRuntimeAuthorityError(
                "stateless seed ConfigMap authority is incomplete"
            )
        if expected_creation_generation is not None:
            try:
                expected_creation_generation = _canonical_runtime_uuid(
                    expected_creation_generation,
                    label="stateless workspace creation generation",
                )
            except ValueError as exc:
                raise WorkspaceRuntimeAuthorityError(str(exc)) from exc
        from services.ide_settings import (
            build_extension_install_script,
            build_seed_script,
        )

        cm_name = self._seed_configmap_name(pod_name)
        seed_sh = (
            build_seed_script(files)
            + "\n"
            + build_extension_install_script(extensions or {})
        )
        data = {"seed.sh": seed_sh}
        if needs_state:
            data["expect-state"] = "1"
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": cm_name,
                "namespace": self._namespace,
                **(
                    {
                        "labels": {
                            "app": "srw-workspace",
                            "srw/component": "workspace-seed",
                            "srw.io/component": "agent-workspace",
                            expected_owner.label_key: expected_owner.id,
                        },
                        **(
                            {
                                "annotations": {
                                    WORKSPACE_RUNTIME_CREATION_ANNOTATION: (
                                        expected_creation_generation
                                    )
                                }
                            }
                            if expected_creation_generation is not None
                            else {}
                        ),
                    }
                    if expected_owner is not None
                    else {}
                ),
            },
            "data": data,
        }
        try:
            created = await asyncio.to_thread(
                self._core_api.create_namespaced_config_map,
                namespace=self._namespace,
                body=body,
            )
            if expected_owner is not None:
                observed = created or await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=cm_name,
                    namespace=self._namespace,
                )
                self._require_stateless_seed_configmap_identity(
                    observed,
                    owner=expected_owner,
                    generation=expected_creation_generation,
                    pod_name=pod_name,
                )
            return cm_name
        except Exception as e:
            if getattr(e, "status", None) == 409:
                # Stale ConfigMap from a prior attempt — refresh its contents.
                try:
                    if expected_owner is not None:
                        existing = await asyncio.to_thread(
                            self._core_api.read_namespaced_config_map,
                            name=cm_name,
                            namespace=self._namespace,
                        )
                        try:
                            self._require_stateless_seed_configmap_identity(
                                existing,
                                owner=expected_owner,
                                generation=expected_creation_generation,
                                pod_name=pod_name,
                            )
                        except WorkspaceRuntimeAuthorityError:
                            if expected_creation_generation is not None:
                                raise
                            await self._require_legacy_seed_configmap_migration(
                                existing,
                                owner=expected_owner,
                                pod_name=pod_name,
                            )
                        resource_version = str(
                            getattr(
                                getattr(existing, "metadata", None),
                                "resource_version",
                                "",
                            )
                            or ""
                        )
                        if not resource_version:
                            raise WorkspaceRuntimeAuthorityError(
                                "workspace seed ConfigMap resource version is missing"
                            )
                        body["metadata"]["resourceVersion"] = resource_version
                    replaced = await asyncio.to_thread(
                        self._core_api.replace_namespaced_config_map,
                        name=cm_name,
                        namespace=self._namespace,
                        body=body,
                    )
                    if expected_owner is not None:
                        observed = replaced or await asyncio.to_thread(
                            self._core_api.read_namespaced_config_map,
                            name=cm_name,
                            namespace=self._namespace,
                        )
                        self._require_stateless_seed_configmap_identity(
                            observed,
                            owner=expected_owner,
                            generation=expected_creation_generation,
                            pod_name=pod_name,
                        )
                    return cm_name
                except Exception as e2:
                    if expected_owner is not None:
                        raise WorkspaceRuntimeAuthorityError(
                            "workspace seed ConfigMap authority changed"
                        ) from e2
                    logger.warning(
                        "ide seed: replace configmap %s failed: %s", cm_name, e2
                    )
                    return None
            if isinstance(e, WorkspaceRuntimeAuthorityError):
                raise
            if expected_creation_generation is not None:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace seed ConfigMap create failed"
                ) from e
            logger.warning("ide seed: create configmap %s failed: %s", cm_name, e)
            return None

    async def _adopt_configmap(
        self,
        cm_name: Optional[str],
        pod_obj: Any,
        *,
        expected_owner: WorkspaceOwner | None = None,
        expected_creation_generation: str | None = None,
    ) -> bool:
        """Set the pod as the ConfigMap's owner so K8s GCs it on teardown.

        Best-effort: a failure here just means we fall back to the explicit
        delete in ``delete_workspace``/``delete_ide_pod``.
        """
        if not cm_name:
            return True
        if not self._core_api or pod_obj is None:
            return False
        try:
            uid = pod_obj.metadata.uid
            name = pod_obj.metadata.name
        except Exception:
            return False
        if not uid:
            return False
        configmap_uid: str | None = None
        resource_version: str | None = None
        if expected_owner is not None:
            try:
                pod_labels = getattr(getattr(pod_obj, "metadata", None), "labels", None)
                opposite_owner_label = (
                    "srw/job-id"
                    if expected_owner.kind == "session"
                    else "srw/thread-id"
                )
                if (
                    str(name or "")
                    not in {expected_owner.pod_name, f"ide-{expected_owner.id[:12]}"}
                    or not isinstance(pod_labels, dict)
                    or pod_labels.get(expected_owner.label_key) != expected_owner.id
                    or opposite_owner_label in pod_labels
                ):
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace Pod owner changed before ConfigMap adoption"
                    )
                configmap = await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=cm_name,
                    namespace=self._namespace,
                )
                try:
                    configmap_uid = self._require_stateless_seed_configmap_identity(
                        configmap,
                        owner=expected_owner,
                        generation=expected_creation_generation,
                        pod_name=str(name),
                    )
                except WorkspaceRuntimeAuthorityError:
                    if expected_creation_generation is not None:
                        raise
                    await self._require_legacy_seed_configmap_migration(
                        configmap,
                        owner=expected_owner,
                        pod_name=str(name),
                    )
                    configmap_uid = _canonical_runtime_uuid(
                        str(
                            getattr(
                                getattr(configmap, "metadata", None),
                                "uid",
                                "",
                            )
                            or ""
                        ),
                        label="workspace seed ConfigMap UID",
                    )
                resource_version = str(
                    getattr(
                        getattr(configmap, "metadata", None),
                        "resource_version",
                        "",
                    )
                    or ""
                )
                if not resource_version:
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace seed ConfigMap resource version is missing"
                    )
            except Exception as error:
                logger.error(
                    "Refusing workspace seed ConfigMap adoption for %s: %s",
                    expected_owner.id,
                    error,
                )
                return False
        patch = {
            "metadata": {
                **(
                    {"resourceVersion": resource_version}
                    if resource_version is not None
                    else {}
                ),
                **(
                    {
                        "labels": {
                            "app": "srw-workspace",
                            "srw/component": "workspace-seed",
                            "srw.io/component": "agent-workspace",
                            expected_owner.label_key: expected_owner.id,
                        }
                    }
                    if expected_owner is not None
                    else {}
                ),
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": name,
                        "uid": uid,
                        "controller": True,
                        "blockOwnerDeletion": False,
                    }
                ],
            }
        }
        try:
            await asyncio.to_thread(
                self._core_api.patch_namespaced_config_map,
                name=cm_name,
                namespace=self._namespace,
                body=patch,
            )
            if expected_owner is not None:
                confirmed = await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=cm_name,
                    namespace=self._namespace,
                )
                if (
                    self._require_stateless_seed_configmap_identity(
                        confirmed,
                        owner=expected_owner,
                        generation=expected_creation_generation,
                        pod_name=str(name),
                    )
                    != configmap_uid
                ):
                    return False
                owner_references = getattr(
                    getattr(confirmed, "metadata", None),
                    "owner_references",
                    None,
                )
                if not isinstance(owner_references, (list, tuple)) or not any(
                    str(_resource_field(reference, "uid") or "") == str(uid)
                    and str(_resource_field(reference, "name") or "") == str(name)
                    and _resource_field(reference, "controller") is True
                    for reference in owner_references
                ):
                    return False
            return True
        except Exception as e:
            logger.debug("ide seed: adopt configmap %s failed: %s", cm_name, e)
            return False

    async def _delete_seed_configmap(
        self,
        pod_name: str,
        *,
        expected_owner: WorkspaceOwner | None = None,
        expected_pod_uid: str | None = None,
    ) -> bool:
        """Delete a pod's seed ConfigMap with an explicit residue result."""
        if not self._core_api:
            return False
        cm_name = self._seed_configmap_name(pod_name)
        configmap_uid: str | None = None
        if expected_owner is not None:
            try:
                configmap = await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=cm_name,
                    namespace=self._namespace,
                )
                try:
                    configmap_uid = self._require_stateless_seed_configmap_identity(
                        configmap,
                        owner=expected_owner,
                        pod_name=pod_name,
                    )
                except WorkspaceRuntimeAuthorityError:
                    if expected_pod_uid is None:
                        raise
                    configmap_uid = (
                        self._require_legacy_seed_configmap_delete_authority(
                            configmap,
                            pod_name=pod_name,
                            expected_pod_uid=expected_pod_uid,
                        )
                    )
            except Exception as error:
                if getattr(error, "status", None) == 404:
                    return True
                logger.error(
                    "Refusing stateless seed ConfigMap cleanup for %s: %s",
                    expected_owner.id,
                    error,
                )
                return False
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_config_map,
                name=cm_name,
                namespace=self._namespace,
                **(
                    {"body": {"preconditions": {"uid": configmap_uid}}}
                    if configmap_uid is not None
                    else {}
                ),
            )
            if expected_owner is not None:
                try:
                    await asyncio.to_thread(
                        self._core_api.read_namespaced_config_map,
                        name=cm_name,
                        namespace=self._namespace,
                    )
                except Exception as error:
                    if getattr(error, "status", None) == 404:
                        return True
                return False
            return True
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return True
            logger.debug("ide seed: delete configmap %s failed: %s", cm_name, e)
            return False

    def _require_stateless_seed_configmap_identity(
        self,
        configmap: Any,
        *,
        owner: WorkspaceOwner,
        generation: str | None = None,
        pod_name: str | None = None,
    ) -> str:
        metadata = getattr(configmap, "metadata", None)
        labels = getattr(metadata, "labels", None)
        annotations = getattr(metadata, "annotations", None)
        opposite_owner_label = (
            "srw/job-id" if owner.kind == "session" else "srw/thread-id"
        )
        observed_generation: Any = (
            annotations.get(WORKSPACE_RUNTIME_CREATION_ANNOTATION)
            if isinstance(annotations, dict)
            else None
        )
        if observed_generation is not None or generation is not None:
            try:
                observed_generation = _canonical_runtime_uuid(
                    observed_generation,
                    label="workspace seed ConfigMap creation generation",
                )
            except ValueError as exc:
                raise WorkspaceRuntimeAuthorityError(str(exc)) from exc
        if (
            str(getattr(metadata, "name", "") or "")
            != self._seed_configmap_name(pod_name or owner.pod_name)
            or str(getattr(metadata, "namespace", "") or "") != self._namespace
            or getattr(metadata, "deletion_timestamp", None) is not None
            or not isinstance(labels, dict)
            or labels.get(owner.label_key) != owner.id
            or labels.get("app") != "srw-workspace"
            or labels.get("srw/component") != "workspace-seed"
            or labels.get("srw.io/component") != "agent-workspace"
            or opposite_owner_label in labels
            or (generation is not None and observed_generation != generation)
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace seed ConfigMap owner authority changed"
            )
        try:
            return _canonical_runtime_uuid(
                str(getattr(metadata, "uid", "") or ""),
                label="workspace seed ConfigMap UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc

    async def _require_legacy_seed_configmap_migration(
        self,
        configmap: Any,
        *,
        owner: WorkspaceOwner,
        pod_name: str,
    ) -> None:
        """Prove the one supported unlabeled HEAD seed before relabeling it."""

        metadata = getattr(configmap, "metadata", None)
        labels = getattr(metadata, "labels", None)
        annotations = getattr(metadata, "annotations", None)
        if labels not in (None, {}) or annotations not in (None, {}):
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap is not migration-safe"
            )
        expected_pod_uid = self._legacy_seed_configmap_controller_uid(
            configmap,
            pod_name=pod_name,
        )
        pod = await asyncio.to_thread(
            self._core_api.read_namespaced_pod,
            name=pod_name,
            namespace=self._namespace,
        )
        component = "ide-session" if pod_name.startswith("ide-") else None
        observed_pod_uid = self._require_workspace_pod_owner(
            pod,
            owner=owner,
            allow_owner_unlabeled=False,
            expected_pod_name=pod_name,
            expected_component=component,
        )
        if observed_pod_uid != expected_pod_uid:
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap Pod UID changed"
            )
        if not str(getattr(metadata, "resource_version", "") or ""):
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap resource version is missing"
            )

    def _require_legacy_seed_configmap_delete_authority(
        self,
        configmap: Any,
        *,
        pod_name: str,
        expected_pod_uid: str,
    ) -> str:
        metadata = getattr(configmap, "metadata", None)
        labels = getattr(metadata, "labels", None)
        annotations = getattr(metadata, "annotations", None)
        if labels not in (None, {}) or annotations not in (None, {}):
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap is not deletion-safe"
            )
        if (
            self._legacy_seed_configmap_controller_uid(
                configmap,
                pod_name=pod_name,
            )
            != expected_pod_uid
        ):
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap Pod UID changed"
            )
        try:
            return _canonical_runtime_uuid(
                str(getattr(metadata, "uid", "") or ""),
                label="workspace seed ConfigMap UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc

    def _legacy_seed_configmap_controller_uid(
        self,
        configmap: Any,
        *,
        pod_name: str,
    ) -> str:
        metadata = getattr(configmap, "metadata", None)
        if (
            str(getattr(metadata, "name", "") or "")
            != self._seed_configmap_name(pod_name)
            or str(getattr(metadata, "namespace", "") or "") != self._namespace
            or getattr(metadata, "deletion_timestamp", None) is not None
        ):
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap identity changed"
            )
        references = getattr(metadata, "owner_references", None)
        matching = [
            reference
            for reference in references or []
            if str(_resource_field(reference, "name") or "") == pod_name
            and _resource_field(reference, "controller") is True
        ]
        if len(matching) != 1:
            raise WorkspaceRuntimeAuthorityError(
                "legacy workspace seed ConfigMap owner reference is invalid"
            )
        try:
            return _canonical_runtime_uuid(
                str(_resource_field(matching[0], "uid") or ""),
                label="workspace seed ConfigMap Pod UID",
            )
        except ValueError as exc:
            raise WorkspaceRuntimeAuthorityError(str(exc)) from exc

    def _require_seed_configmap_pod_owner_reference(
        self,
        configmap: Any,
        *,
        pod_name: str,
        runtime_incarnation: str,
    ) -> None:
        references = getattr(
            getattr(configmap, "metadata", None),
            "owner_references",
            None,
        )
        if not isinstance(references, (list, tuple)) or not any(
            str(_resource_field(reference, "uid") or "") == runtime_incarnation
            and str(_resource_field(reference, "name") or "") == pod_name
            and _resource_field(reference, "controller") is True
            for reference in references
        ):
            raise WorkspaceRuntimeAuthorityError(
                "workspace seed ConfigMap Pod ownership changed"
            )

    def _build_pod_manifest(
        self,
        pod_name: str,
        owner: WorkspaceOwner,
        image: str,
        cpu: str,
        memory: str,
        cpu_limit: str,
        memory_limit: str,
        network_tier: str = DEFAULT_NETWORK_TIER,
        pvc_name: Optional[str] = None,
        seed_configmap: Optional[str] = None,
        stateless_creation_generation: str | None = None,
    ) -> dict:
        """Build the Kubernetes Pod manifest for a workspace container.

        When ``seed_configmap`` is given, the named ConfigMap (carrying a
        ``seed.sh`` that writes the user's code-server config) is mounted at
        ``/mnt/code-server-config`` so the entrypoint can apply it before
        code-server starts.
        """
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self._namespace,
                "labels": self._build_workspace_labels(owner, network_tier),
                # GC backstop hook: marks the pod as owned by the lifecycle
                # reconciler so a future K8s TTL/GC controller (or an age
                # sweep) can reclaim a tail the reconciler missed. Bare pods
                # have no ownerReference, so without this nothing external can
                # ever clean them up.
                "annotations": {
                    "srw.io/managed-by": "lifecycle-reconciler",
                    **(
                        {
                            WORKSPACE_RUNTIME_CREATION_ANNOTATION: (
                                stateless_creation_generation
                            )
                        }
                        if stateless_creation_generation is not None
                        else {}
                    ),
                },
                # Every credential-capable workspace Pod retains its exact API
                # object until this controller has observed every container
                # terminal and persisted process-zero for the immutable UID.
                # Stateless Pods were the first users of this finalizer; the
                # literal stays stable for rolling compatibility.
                "finalizers": [STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER],
            },
            "spec": {
                "restartPolicy": "Never",
                # Grace period for agent to checkpoint and push artifacts
                # before the pod is killed on deletion.
                "terminationGracePeriodSeconds": 120,
                # Explicit ClusterFirst DNS so workspace pods can resolve
                # in-cluster services (e.g. srw-gitea) even if
                # WORKSPACE_NAMESPACE differs from the service namespace.
                "dnsPolicy": "ClusterFirst",
                "dnsConfig": {
                    "searches": [
                        "superhuman-remote-worker.svc.cluster.local",
                    ],
                },
                # Pod-level security: run SSHD as root for user session
                # management (su to agent-host), but restrict everything else.
                "securityContext": {
                    "seccompProfile": {
                        "type": "Unconfined"
                        if self._fuse_privileged
                        else "RuntimeDefault"
                    },
                },
                "containers": [
                    {
                        "name": "workspace",
                        "image": image,
                        "ports": [
                            {"containerPort": 30022, "name": "ssh"},
                            {"containerPort": 38080, "name": "code-server"},
                            {"containerPort": 9222, "name": "cdp"},
                        ],
                        "env": [
                            {
                                "name": "SRW_WORKSPACE_OWNER_KIND",
                                "value": owner.kind,
                            },
                            {
                                "name": "SRW_WORKSPACE_OWNER_ID",
                                "value": owner.id,
                            },
                            {
                                "name": "SRW_WORKSPACE_RUNTIME_UID",
                                "valueFrom": {
                                    "fieldRef": {
                                        "fieldPath": "metadata.uid",
                                    }
                                },
                            },
                        ],
                        "resources": {
                            "requests": {"cpu": cpu, "memory": memory},
                            "limits": {
                                "cpu": cpu_limit,
                                "memory": memory_limit,
                            },
                        },
                        # Container security profile:
                        # - Drop all capabilities, add back only what SSHD needs
                        #   plus SYS_ADMIN for rclone/FUSE mounts when enabled.
                        # - SETUID/SETGID: user session switching
                        # - NET_BIND_SERVICE: bind to privileged ports (<1024)
                        # - CHOWN/DAC_OVERRIDE/FOWNER: file ownership for sessions
                        # - SYS_CHROOT: SSHD privilege separation
                        # - SYS_ADMIN: required for container FUSE mounts
                        # - KILL: signal management
                        # - AUDIT_WRITE: PAM audit logging
                        # - allowPrivilegeEscalation: true (required for SSHD setuid)
                        # - sudo is NOT installed — agent-host cannot escalate
                        "securityContext": {
                            "capabilities": {
                                "drop": ["ALL"],
                                "add": self._workspace_capabilities(),
                            },
                            "allowPrivilegeEscalation": True,
                        },
                        "volumeMounts": [
                            {
                                "name": "workspace-data",
                                "mountPath": "/home/agent-host",
                            },
                            {
                                "name": "ssh-pubkey",
                                "mountPath": "/tmp/ssh-pubkey",
                                "readOnly": True,
                            },
                            {
                                "name": "workspace-identity",
                                "mountPath": "/var/lib/srw-system",
                            },
                        ],
                        "readinessProbe": {
                            "tcpSocket": {"port": 30022},
                            "initialDelaySeconds": 3,
                            "periodSeconds": 5,
                        },
                        "livenessProbe": {
                            "tcpSocket": {"port": 30022},
                            "initialDelaySeconds": 10,
                            "periodSeconds": 30,
                        },
                    }
                ],
                "volumes": [
                    {
                        "name": "workspace-data",
                        "persistentVolumeClaim": {"claimName": pvc_name},
                    }
                    if pvc_name
                    else {
                        "name": "workspace-data",
                        "emptyDir": {"sizeLimit": "10Gi"},
                    },
                    {
                        "name": "ssh-pubkey",
                        "secret": {
                            "secretName": self._ssh_secret_name,
                            "items": [
                                {
                                    "key": "ssh-publickey",
                                    "path": "ssh-publickey",
                                    "mode": 0o600,
                                }
                            ],
                            "defaultMode": 0o600,
                        },
                    },
                    {
                        "name": "workspace-identity",
                        "emptyDir": {},
                    },
                ],
            },
        }
        if self._fuse_enabled:
            if self._fuse_privileged:
                manifest["spec"]["containers"][0]["securityContext"]["privileged"] = (
                    True
                )
            manifest["spec"]["containers"][0]["volumeMounts"].append(
                {
                    "name": "dev-fuse",
                    "mountPath": "/dev/fuse",
                }
            )
            manifest["spec"]["volumes"].append(
                {
                    "name": "dev-fuse",
                    "hostPath": {"path": "/dev/fuse", "type": "CharDevice"},
                }
            )
        if seed_configmap:
            manifest["spec"]["containers"][0]["volumeMounts"].append(
                {
                    "name": "code-server-config",
                    "mountPath": "/mnt/code-server-config",
                    "readOnly": True,
                }
            )
            manifest["spec"]["volumes"].append(
                {
                    "name": "code-server-config",
                    "configMap": {"name": seed_configmap, "defaultMode": 0o644},
                }
            )
        return manifest

    def _workspace_capabilities(self) -> list[str]:
        capabilities = [
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "SETGID",
            "SETUID",
            "NET_BIND_SERVICE",
            "SYS_CHROOT",
            "KILL",
            "AUDIT_WRITE",
        ]
        if self._fuse_enabled:
            capabilities.append("SYS_ADMIN")
        return capabilities

    async def _create_pod_resolving_teardown(
        self,
        pod_manifest: dict,
        pod_name: str,
        *,
        owner: WorkspaceOwner,
    ) -> tuple[Any, bool]:
        """Create the workspace pod, resolving a suspend/resume teardown race.

        Returns ``(pod, reused_existing)`` so the caller retains the incumbent
        UID and its actual seed-volume binding across a 409 retry.

        The race this fixes (issue
        ``session_agent_drift_drain_kills_idle_sessions``, option c): a
        just-suspended session leaves its old workspace pod — same
        deterministic name (``ws-thread-<tid>``) — ``Terminating`` inside its
        delete grace window. A fast resume (drift-drain → suspend → user
        sends a message seconds later) then 409s on create, which previously
        bubbled to the dispatcher as a failed restore and surfaced to the
        user as a 503 "session ended". We distinguish:

          * incumbent pod ``Terminating`` (``deletion_timestamp`` set, or
            already 404 by the time we look) → wait for it to fully
            disappear, then create the fresh pod;
          * incumbent pod live (no deletion timestamp) → genuine idempotent
            hit, adopt it (mirrors the IDE-pod path).
        """
        try:
            created = await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
            if created is None:
                created = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                )
            self._require_workspace_pod_owner(
                created,
                owner=owner,
                allow_owner_unlabeled=False,
                expected_network_tier=(
                    pod_manifest.get("metadata", {})
                    .get("labels", {})
                    .get("srw.io/network-tier")
                ),
            )
            return created, False
        except Exception as e:
            if getattr(e, "status", None) != 409:
                raise

        # 409 — inspect the incumbent pod sharing this deterministic name.
        try:
            existing = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
        except Exception as e:
            if getattr(e, "status", None) != 404:
                raise
            existing = None  # vanished between create and read — recreate below

        if existing is not None:
            self._require_workspace_pod_owner(
                existing,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=True,
            )

        terminating = (
            existing is None or existing.metadata.deletion_timestamp is not None
        )
        if not terminating:
            # Live pod already present (two creates raced for one owner) —
            # treat as idempotent; the existing pod owns its ConfigMap.
            logger.info(
                "Workspace pod %s already exists and is live — adopting", pod_name
            )
            return existing, True

        # Old pod still draining its delete grace (suspend→resume race).
        # Wait it out, then create the fresh pod on the freed name.
        logger.info(
            "Workspace pod %s is terminating (suspend/resume race) — waiting "
            "for teardown before recreate",
            pod_name,
        )
        if not await self._wait_for_pod_gone(pod_name, timeout=30):
            raise RuntimeError(
                f"workspace pod {pod_name} still terminating after 30s; "
                "cannot recreate for resume"
            )
        created = await asyncio.to_thread(
            self._core_api.create_namespaced_pod,
            namespace=self._namespace,
            body=pod_manifest,
        )
        if created is None:
            created = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
        self._require_workspace_pod_owner(
            created,
            owner=owner,
            allow_owner_unlabeled=False,
            expected_network_tier=(
                pod_manifest.get("metadata", {})
                .get("labels", {})
                .get("srw.io/network-tier")
            ),
        )
        return created, False

    async def _wait_for_pod_gone(self, pod_name: str, timeout: int = 30) -> bool:
        """Poll until the named pod no longer exists (404). True if gone."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                )
            except Exception as e:
                if getattr(e, "status", None) == 404:
                    return True
                # Transient read error — keep polling until the deadline.
            await asyncio.sleep(1)
        return False

    async def _wait_for_ready(
        self,
        pod_name: str,
        timeout: int = 120,
        *,
        expected_owner: WorkspaceOwner | None = None,
        expected_runtime_incarnation: str | None = None,
        expected_creation_generation: str | None = None,
        expected_network_tier: str | None = None,
        expected_pvc_name: str | None | object = _UNSPECIFIED_RESOURCE_BINDING,
        expected_seed_configmap: str | None | object = (_UNSPECIFIED_RESOURCE_BINDING),
        expected_pod_name: str | None = None,
        expected_component: str | None = None,
    ) -> Optional[str]:
        """Poll until the pod is ready and accepts the configured SSH key.

        Returns:
            Pod IP after authenticated readiness, None on Kubernetes timeout.

        Raises:
            WorkspaceSSHAuthenticationError: the pod is Kubernetes-ready but
                the configured private key is unusable or authentication does
                not succeed within its bounded readiness window.
        """
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            try:
                pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                )
                if expected_owner is not None:
                    if expected_runtime_incarnation is None:
                        raise WorkspaceRuntimeAuthorityError(
                            "workspace runtime incarnation is missing"
                        )
                    self._require_workspace_pod_connection_identity(
                        pod,
                        owner=expected_owner,
                        expected_runtime_incarnation=expected_runtime_incarnation,
                        expected_creation_generation=expected_creation_generation,
                        expected_network_tier=expected_network_tier,
                        expected_pvc_name=expected_pvc_name,
                        expected_seed_configmap=expected_seed_configmap,
                        expected_pod_name=expected_pod_name,
                        expected_component=expected_component,
                    )
                if pod.status.phase == "Running" and pod.status.pod_ip:
                    # Check container readiness
                    if pod.status.container_statuses and all(
                        cs.ready for cs in pod.status.container_statuses
                    ):
                        key_path = resolve_ssh_key_path()
                        try:
                            fingerprint = workspace_private_key_fingerprint(key_path)
                        except SSHPrivateKeyError as exc:
                            raise WorkspaceSSHAuthenticationError(str(exc)) from exc

                        ready, attempts, last_error = await wait_for_agent_ssh(
                            pod.status.pod_ip,
                            30022,
                            deadline_s=self._ssh_auth_ready_timeout,
                            connect_timeout_s=self._ssh_auth_connect_timeout,
                            interval_s=self._ssh_auth_poll_interval,
                            key_path=key_path,
                        )
                        if ready:
                            if expected_owner is not None:
                                confirmed = await asyncio.to_thread(
                                    self._core_api.read_namespaced_pod,
                                    name=pod_name,
                                    namespace=self._namespace,
                                )
                                self._require_workspace_pod_connection_identity(
                                    confirmed,
                                    owner=expected_owner,
                                    expected_runtime_incarnation=(
                                        expected_runtime_incarnation
                                    ),
                                    expected_creation_generation=(
                                        expected_creation_generation
                                    ),
                                    expected_network_tier=expected_network_tier,
                                    expected_pvc_name=expected_pvc_name,
                                    expected_seed_configmap=(expected_seed_configmap),
                                    expected_pod_name=expected_pod_name,
                                    expected_component=expected_component,
                                )
                            logger.info(
                                "Workspace SSH authenticated: %s @ %s:30022 "
                                "(attempts=%d, key=%s)",
                                pod_name,
                                pod.status.pod_ip,
                                attempts,
                                fingerprint,
                            )
                            return pod.status.pod_ip
                        raise WorkspaceSSHAuthenticationError(
                            "Workspace pod became Kubernetes-ready but rejected "
                            f"the configured SSH key after {attempts} attempt(s) "
                            f"(key={fingerprint}): {last_error or 'authentication failed'}"
                        )
            except WorkspaceSSHAuthenticationError:
                raise
            except WorkspaceRuntimeAuthorityError:
                raise
            except Exception:
                pass

            await asyncio.sleep(2)

        return None

    async def _trusted_pod_ssh_identity(
        self,
        pod_name: str,
        *,
        pvc_name: str | None = None,
        expected_owner: WorkspaceOwner | None = None,
        expected_runtime_incarnation: str | None = None,
        expected_creation_generation: str | None = None,
        expected_network_tier: str | None = None,
        expected_seed_configmap: str | None | object = (_UNSPECIFIED_RESOURCE_BINDING),
        expected_pvc_uid: str | None = None,
        expected_pvc_storage_class: str | None = None,
        expected_pod_name: str | None = None,
        expected_component: str | None = None,
    ) -> tuple[str, str, str]:
        """Read backing identity, host key, and Pod UID from the control plane."""

        if self._core_api is None or k8s_stream is None:
            raise RuntimeError("Kubernetes exec transport is unavailable")
        pod = await asyncio.to_thread(
            self._core_api.read_namespaced_pod,
            name=pod_name,
            namespace=self._namespace,
        )
        if expected_owner is not None:
            if expected_runtime_incarnation is None:
                raise WorkspaceRuntimeAuthorityError(
                    "workspace runtime incarnation is missing"
                )
            runtime_incarnation = self._require_workspace_pod_connection_identity(
                pod,
                owner=expected_owner,
                expected_runtime_incarnation=expected_runtime_incarnation,
                expected_creation_generation=expected_creation_generation,
                expected_network_tier=expected_network_tier,
                expected_pvc_name=pvc_name,
                expected_seed_configmap=expected_seed_configmap,
                expected_pod_name=expected_pod_name,
                expected_component=expected_component,
            )
        else:
            runtime_incarnation = str(getattr(pod.metadata, "uid", "") or "")
        if not runtime_incarnation:
            raise RuntimeError("workspace pod has no Kubernetes UID")
        trusted_seed_uid: str | None = None
        seed_configmap = None
        if expected_owner is not None:
            seed_configmap = self._require_stateless_pod_storage_binding(
                pod,
                owner=expected_owner,
                expected_pvc_name=pvc_name,
                expected_seed_configmap=expected_seed_configmap,
                expected_pod_name=expected_pod_name,
            )
            if seed_configmap is not None:
                seed = await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=seed_configmap,
                    namespace=self._namespace,
                )
                trusted_seed_uid = self._require_stateless_seed_configmap_identity(
                    seed,
                    owner=expected_owner,
                    generation=expected_creation_generation,
                )
                self._require_seed_configmap_pod_owner_reference(
                    seed,
                    pod_name=pod_name,
                    runtime_incarnation=runtime_incarnation,
                )
        backing_kind = "pod"
        backing_uid = runtime_incarnation
        trusted_claim_uid: str | None = None
        trusted_service_uid: str | None = None
        if pvc_name:
            claim = await asyncio.to_thread(
                self._core_api.read_namespaced_persistent_volume_claim,
                name=pvc_name,
                namespace=self._namespace,
            )
            backing_kind = "pvc"
            if expected_owner is not None:
                trusted_claim_uid = self._require_stateless_pvc_identity(
                    claim,
                    owner=expected_owner,
                    pvc_name=pvc_name,
                    expected_storage_class=expected_pvc_storage_class,
                )
                if (
                    expected_pvc_uid is not None
                    and trusted_claim_uid != expected_pvc_uid
                ):
                    raise WorkspaceRuntimeAuthorityError("workspace PVC UID changed")
                backing_uid = trusted_claim_uid
            else:
                backing_uid = str(getattr(claim.metadata, "uid", "") or "")
            if expected_owner is not None:
                service = await asyncio.to_thread(
                    self._core_api.read_namespaced_service,
                    name=expected_owner.pod_name,
                    namespace=self._namespace,
                )
                trusted_service_uid = self._require_stateless_service_identity(
                    service,
                    owner=expected_owner,
                )
        if not backing_uid:
            raise RuntimeError("workspace pod has no Kubernetes UID")
        output = await asyncio.to_thread(
            k8s_stream,
            self._core_api.connect_get_namespaced_pod_exec,
            pod_name,
            self._namespace,
            command=[
                "ssh-keygen",
                "-lf",
                "/var/lib/srw-system/ssh/ssh_host_ed25519_key.pub",
                "-E",
                "sha256",
            ],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=10,
        )
        fields = str(output).strip().split()
        fingerprint = next(
            (field for field in fields if field.startswith("SHA256:")), None
        )
        if not fingerprint:
            raise RuntimeError("workspace host-key fingerprint was not reported")
        if expected_owner is not None:
            confirmed = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            self._require_workspace_pod_connection_identity(
                confirmed,
                owner=expected_owner,
                expected_runtime_incarnation=runtime_incarnation,
                expected_creation_generation=expected_creation_generation,
                expected_network_tier=expected_network_tier,
                expected_pvc_name=pvc_name,
                expected_seed_configmap=expected_seed_configmap,
                expected_pod_name=expected_pod_name,
                expected_component=expected_component,
            )
            if pvc_name:
                confirmed_claim = await asyncio.to_thread(
                    self._core_api.read_namespaced_persistent_volume_claim,
                    name=pvc_name,
                    namespace=self._namespace,
                )
                if (
                    self._require_stateless_pvc_identity(
                        confirmed_claim,
                        owner=expected_owner,
                        pvc_name=pvc_name,
                        expected_storage_class=expected_pvc_storage_class,
                    )
                    != trusted_claim_uid
                ):
                    raise WorkspaceRuntimeAuthorityError("workspace PVC UID changed")
                confirmed_service = await asyncio.to_thread(
                    self._core_api.read_namespaced_service,
                    name=expected_owner.pod_name,
                    namespace=self._namespace,
                )
                if (
                    self._require_stateless_service_identity(
                        confirmed_service,
                        owner=expected_owner,
                    )
                    != trusted_service_uid
                ):
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace Service UID changed"
                    )
            if seed_configmap is not None:
                confirmed_seed = await asyncio.to_thread(
                    self._core_api.read_namespaced_config_map,
                    name=seed_configmap,
                    namespace=self._namespace,
                )
                if (
                    self._require_stateless_seed_configmap_identity(
                        confirmed_seed,
                        owner=expected_owner,
                        generation=expected_creation_generation,
                    )
                    != trusted_seed_uid
                ):
                    raise WorkspaceRuntimeAuthorityError(
                        "workspace seed ConfigMap UID changed"
                    )
                self._require_seed_configmap_pod_owner_reference(
                    confirmed_seed,
                    pod_name=pod_name,
                    runtime_incarnation=runtime_incarnation,
                )
        return (
            f"k8s-{backing_kind}:{self._namespace}:{backing_uid}",
            fingerprint,
            runtime_incarnation,
        )

    async def _set_context(self, owner: WorkspaceOwner, updates: dict) -> None:
        """Atomically merge updates into the workspace context for a job or session."""
        if not self._db:
            return

        try:
            if owner.kind == "job":
                await self._db.merge_workspace_container_context(owner.id, updates)
            else:
                await self._db.merge_thread_workspace_context(owner.id, updates)
        except Exception:
            logger.exception(
                "Failed to update workspace container context for %s %s",
                owner.kind,
                owner.id,
            )


# Module-level singleton
container_provisioner = ContainerProvisioner()
