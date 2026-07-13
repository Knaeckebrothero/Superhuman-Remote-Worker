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
import logging
import os
from typing import Any, Optional

from services import workspace_metering
from services.workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY
from services.workspace_lifecycle import WorkspaceOwner

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


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class ContainerProvisioner:
    """Workspace container provisioner using Kubernetes CoreV1Api.

    Creates per-job pods with SSH server + code-server. Workspace storage is
    ``emptyDir`` by default (dies with the pod). When ``WORKSPACE_PVC_ENABLED``
    is set, job workspaces are PVC-backed (Branch a): the volume is named after
    the job UUID, survives pod crashes, and reattaches by that deterministic
    name on recreate. PVCs are reclaimed when the job reaches a terminal state
    (completed/failed/cancelled) and retained across suspend/restore and crash
    recovery. See docs/features/workspace_pvc_branch_a_implementation.md.
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
        self._storage_class: str = os.environ.get(
            "WORKSPACE_STORAGE_CLASS", "longhorn-ephemeral"
        )
        # Branch (a): PVC-backed job workspaces. Default off → emptyDir (today's
        # behavior). Scoped to jobs in v1; sessions rehydrate from Postgres and
        # stay emptyDir. The PVC name is deterministic on the job UUID, so a
        # recreated pod reattaches the same volume; GC happens on terminal job
        # states. See docs/features/workspace_pvc_branch_a_implementation.md.
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

    async def create_workspace(
        self,
        owner: WorkspaceOwner,
        cpu: str = "500m",
        memory: str = "1Gi",
        cpu_limit: str = "2000m",
        memory_limit: str = "4Gi",
        image: Optional[str] = None,
        fresh: bool = False,
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

        pod_name = owner.pod_name
        workspace_image = image or self._workspace_image
        network_tier = await self._resolve_network_tier(
            owner.id, kind=owner.network_tier_kind
        )

        # Workspace storage (Branch a): PVC-backed for jobs when
        # WORKSPACE_PVC_ENABLED — the volume is named by the job UUID, survives
        # pod crashes, and reattaches by that deterministic name on recreate
        # (drift recovery, suspend/restore, give_up all funnel back through
        # create_workspace, so 409-reuse here IS the resume path). Otherwise
        # emptyDir — storage dies with the pod; isolation is the pod boundary.
        # Created BEFORE the seed ConfigMap so a PVC failure leaves nothing to
        # clean up — it is the provisioning prerequisite.
        pvc_name: Optional[str] = None
        pvc_reattach = False
        if self._pvc_enabled and owner.kind == "job":
            pvc_name = f"pvc-workspace-{owner.id[:12]}"
            # Fresh recovery (Phase 3b single-replica fallback): the prior reattach
            # was wedged because the PVC's only replica is on a dead node. Delete
            # the stuck PVC (and wait for it to release) so the create below makes
            # a brand-new empty volume under the same deterministic name.
            if fresh:
                await self._delete_pvc_and_wait(pvc_name)
            pvc_status = await self._create_pvc(
                pvc_name,
                size=self._pvc_size,
                # Owner label lets the backstop reaper resolve PVC → job and
                # is a belt-and-suspenders identity check before any reattach.
                labels={owner.label_key: owner.id},
            )
            if not pvc_status:
                logger.error(
                    "Workspace PVC create failed for %s %s — aborting provision",
                    owner.kind,
                    owner.id,
                )
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
        seed_needs_state = await self._resolve_ide_needs_state(owner, seed_exts)
        seed_cm = await self._create_seed_configmap(
            pod_name, seed_files, seed_exts, needs_state=seed_needs_state
        )

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
        )

        try:
            created_pod = await self._create_pod_resolving_teardown(
                pod_manifest, pod_name
            )
            # Make the pod own the seed ConfigMap so K8s GCs it on teardown.
            # created_pod is None only for the idempotent live-pod case, where
            # the existing pod already owns its (same-named) ConfigMap.
            if created_pod is not None:
                await self._adopt_configmap(seed_cm, created_pod)
            # PVC-backed jobs get a stable headless Service so the agent dials a
            # constant DNS name that survives pod recreates (reattach/recovery)
            # instead of an ephemeral pod IP. Same gate as the PVC; lifecycle
            # mirrors it (kept across idle reaps, deleted on terminal).
            if pvc_name:
                await self._create_service(owner)
            logger.info(
                "Workspace container created: %s (%s %s)",
                pod_name,
                owner.kind,
                owner.id,
            )
            await self._set_context(
                owner,
                {
                    "status": "created",
                    "pod_name": pod_name,
                    "namespace": self._namespace,
                    **(
                        {CANVAS_WORKSPACE_GENERATION_KEY: None}
                        if owner.kind == "session"
                        else {}
                    ),
                },
            )

            # Open a compute-metering interval (Slice 4b) — best-effort, billed on
            # the requested cpu/memory from pod creation to deletion. Idempotent
            # on the live-pod re-create path (one open interval per owner).
            await workspace_metering.open_interval(
                self._db, owner, tier="sandbox", cpu=cpu, memory=memory
            )

            # Wait for pod IP. A reattach gets the longer window so a transient
            # node reboot recovers without discarding data; a fresh create keeps
            # the standard 120s.
            ready_timeout = self._reattach_ready_timeout if pvc_reattach else 120
            pod_ip = await self._wait_for_ready(pod_name, timeout=ready_timeout)
            if pod_ip:
                canvas_generation = None
                if owner.kind == "session" and self._db:
                    try:
                        backing_id, fingerprint = await self._trusted_pod_ssh_identity(
                            pod_name, pvc_name=pvc_name
                        )
                        binding = await self._db.bind_thread_workspace_backing(
                            owner.id,
                            backing_kind="remote",
                            backing_id=backing_id,
                            ssh_host_key_fingerprint=fingerprint,
                        )
                        if binding:
                            canvas_generation = binding.get("workspace_generation")
                    except Exception:
                        # The workspace remains usable by its agent, but Canvas
                        # file serving fails closed until a trusted binding is
                        # available. Never substitute SSH TOFU here.
                        logger.exception(
                            "Failed to bind trusted Canvas SSH identity for session %s",
                            owner.id,
                        )
                ready_ctx = {"status": "ready", "pod_ip": pod_ip, "port": 30022}
                if owner.kind == "session":
                    # Pair this endpoint with the exact binding minted above.
                    # A failed/missing bind publishes null and Canvas fails closed.
                    ready_ctx[CANVAS_WORKSPACE_GENERATION_KEY] = canvas_generation
                # Hand the agent the STABLE Service DNS (not the ephemeral IP) so
                # a reattached/recovered pod is reachable at the same address.
                # The dispatch + resume paths prefer this `host` over `pod_ip`.
                if pvc_name:
                    ready_ctx["host"] = self._workspace_dns(owner)
                await self._set_context(owner, ready_ctx)
                logger.info(
                    "Workspace container ready: %s @ %s (%s %s)",
                    pod_name,
                    pod_ip,
                    owner.kind,
                    owner.id,
                )
                # Stream license/globalStorage state in (Phase B); fire-and-forget
                # so it never blocks provisioning. No-ops when S3 is unavailable.
                asyncio.create_task(self._seed_workspace_state(owner, pod_ip))
            elif (
                pvc_reattach
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
                await self._set_context(owner, {"status": "creating"})

            return True
        except Exception as e:
            logger.error(
                "Failed to create workspace container for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            # Don't leave the seed ConfigMap orphaned if the pod never came up.
            await self._delete_seed_configmap(pod_name)
            await self._set_context(
                owner,
                {"status": "failed", "error": str(e)},
            )
            return False

    async def delete_workspace(self, owner: WorkspaceOwner) -> bool:
        """Delete the workspace container for a job or persistent thread.

        Returns:
            True if deleted, False otherwise.
        """
        if not self._k8s_available:
            return False

        pod_name = owner.pod_name

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=10,
            )
            logger.info(
                "Workspace container deleted: %s (%s %s)",
                pod_name,
                owner.kind,
                owner.id,
            )
            await self._delete_seed_configmap(pod_name)
            await self._set_context(owner, {"status": "deleted"})
            # Close the compute-metering interval (Slice 4b) — pod gone, billing
            # stops. Best-effort; the loop's reconciler bounds any missed close.
            await workspace_metering.close_interval(self._db, owner)
            return True
        except Exception as e:
            # 404 is fine — pod already gone
            if hasattr(e, "status") and e.status == 404:
                logger.debug(
                    "Workspace container already deleted: %s (%s %s)",
                    pod_name,
                    owner.kind,
                    owner.id,
                )
                await workspace_metering.close_interval(self._db, owner)
                return True
            logger.error(
                "Failed to delete workspace container for %s %s: %s",
                owner.kind,
                owner.id,
                e,
            )
            return False

    async def delete_workspace_pvc(self, owner: WorkspaceOwner) -> bool:
        """Delete the PVC for a job or thread workspace if one exists.

        With emptyDir (default), there is no PVC — storage dies with the pod.
        This method is kept for backward compatibility: it cleans up PVCs
        from workspaces created before the emptyDir switch.
        """
        if owner.kind == "job":
            pvc_name = f"pvc-workspace-{owner.id[:12]}"
        else:
            pvc_name = f"pvc-ws-thread-{owner.id[:12]}"
        return await self._delete_pvc(pvc_name)

    async def release_workspace(self, owner: "WorkspaceOwner") -> bool:
        """Snapshot a workspace to S3, then delete the pod (and its PVC).

        Owner-keyed: serves both jobs and sessions. K8s pods use emptyDir, so
        data dies with the pod — snapshotting first enables resume (a fresh pod
        restores from S3). Snapshot failure is non-fatal.

        Returns:
            True if deletion succeeded.
        """
        if not self._k8s_available:
            return False

        status = await self.get_workspace_status(owner)
        pod_ip = status.get("pod_ip") if status else None
        ready = status.get("ready") if status else False

        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and pod_ip
            and ready
        ):
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=owner.id,
                    ssh_host=pod_ip,
                    ssh_port=30022,
                    source_type="pod",
                    entity_type="threads" if owner.kind == "session" else "jobs",
                )
                logger.info(
                    "Workspace snapshot captured for %s %s before release",
                    owner.kind,
                    owner.id,
                )
            except Exception:
                logger.exception(
                    "Workspace snapshot failed for %s %s — deleting anyway",
                    owner.kind,
                    owner.id,
                )

        deleted = await self.delete_workspace(owner)
        await self.delete_workspace_pvc(owner)
        await self._delete_service(owner)
        return deleted

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

    async def workspace_pod_live(self, owner: "WorkspaceOwner") -> Optional[bool]:
        """Drift probe: is the owner's workspace pod actually alive?

        Returns:
            ``True``  — pod exists and is ``Running``/``Pending`` (usable or
                        still coming up).
            ``False`` — pod is confirmed gone (404) or a dead tombstone
                        (``Failed``/``Succeeded``/``Unknown`` phase).
            ``None``  — can't tell: no k8s client, or a transient API error.

        Callers MUST treat ``None`` as "assume live" so a probe blip (or a
        non-k8s backend) never triggers a false recreate of a healthy
        workspace.
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
        phase = getattr(pod.status, "phase", None)
        return phase in ("Running", "Pending")

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
            pod_name, seed_files, seed_exts, needs_state=seed_needs_state
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
            created_pod = await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
            await self._adopt_configmap(seed_cm, created_pod)
            logger.info("IDE pod created: %s (job %s)", pod_name, job_id)

            pod_ip = await self._wait_for_ready(pod_name, timeout=90)
            if pod_ip:
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
            # 409 Conflict = pod already exists (idempotent retry)
            if hasattr(e, "status") and e.status == 409:
                logger.info("IDE pod already exists: %s (job %s)", pod_name, job_id)
                pod_ip = await self._wait_for_ready(pod_name, timeout=90)
                return pod_ip
            logger.error("Failed to create IDE pod for job %s: %s", job_id, e)
            return None

    async def delete_ide_pod(self, job_id: str) -> bool:
        """Delete an IDE session pod.

        Returns:
            True if deleted (or already gone), False on error.
        """
        if not self._k8s_available:
            return False

        pod_name = f"ide-{job_id[:12]}"

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=5,
            )
            logger.info("IDE pod deleted: %s (job %s)", pod_name, job_id)
            await self._delete_seed_configmap(pod_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                await self._delete_seed_configmap(pod_name)
                return True
            logger.error("Failed to delete IDE pod for job %s: %s", job_id, e)
            return False

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _create_pvc(
        self, pvc_name: str, size: str = "10Gi", labels: Optional[dict] = None
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
            await asyncio.to_thread(
                self._core_api.create_namespaced_persistent_volume_claim,
                namespace=self._namespace,
                body=pvc_manifest,
            )
            logger.info(
                "PVC created: %s (storageClass=%s)", pvc_name, self._storage_class
            )
            return "created"
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
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

    async def _delete_pvc(self, pvc_name: str) -> bool:
        """Delete a PVC. Idempotent — 404 treated as success."""
        if not self._k8s_available:
            return False

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_persistent_volume_claim,
                name=pvc_name,
                namespace=self._namespace,
            )
            logger.info("PVC deleted: %s", pvc_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug("PVC already deleted: %s", pvc_name)
                return True
            logger.error("Failed to delete PVC %s: %s", pvc_name, e)
            return False

    async def _delete_pvc_and_wait(self, pvc_name: str, timeout: int = 90) -> None:
        """Delete a PVC and wait (bounded) for it to fully release.

        The single-replica fallback recreates a fresh PVC under the SAME
        deterministic name, so the old (wedged) one must be gone first — else the
        create 409-reuses the dead volume. Best-effort: on timeout we proceed
        anyway (the recreate's 409 path is still safe, just not fresh).
        """
        if not self._k8s_available:
            return
        await self._delete_pvc(pvc_name)
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
                    return  # fully released
            await asyncio.sleep(2)
        logger.warning(
            "PVC %s still present after %ss — proceeding with fresh recovery anyway",
            pvc_name,
            timeout,
        )

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

    async def _create_service(self, owner: WorkspaceOwner) -> bool:
        """Create a headless Service giving the workspace a STABLE DNS name.

        The pod IP changes on every recreate; the agent caches its SSH dial
        target, so a recreated pod (PVC reattach / crash recovery) leaves the
        agent dialing a dead IP and the job churns to fail-loud (see
        docs/issues/workspace_reattach_ephemeral_ip_reconnect_churn.md). A
        headless Service named after the pod gives a stable address
        ``<pod_name>.<ns>.svc:30022`` that always resolves (selector-matched) to
        the *current* pod — so reattach/recovery reconnects with no IP
        propagation. Headless (clusterIP=None) keeps traffic pod->pod (no DNAT),
        so workspace NetworkPolicies are unaffected, and DNS only resolves to a
        Ready pod (closes the sshd-readiness gap). Idempotent — 409 = success.
        Lifecycle mirrors the PVC: kept across idle reaps, deleted on terminal.
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
                    {"name": "ssh", "port": 30022, "targetPort": 30022},
                    {"name": "code-server", "port": 38080, "targetPort": 38080},
                    {"name": "cdp", "port": 9222, "targetPort": 9222},
                ],
            },
        }
        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_service,
                namespace=self._namespace,
                body=manifest,
            )
            logger.info("Workspace Service created: %s", svc_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
                logger.debug("Workspace Service already exists: %s", svc_name)
                return True
            logger.error("Failed to create workspace Service %s: %s", svc_name, e)
            return False

    async def _delete_service(self, owner: WorkspaceOwner) -> bool:
        """Delete the workspace's headless Service. Idempotent — 404 = success."""
        if not self._k8s_available:
            return False
        svc_name = owner.pod_name
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_service,
                name=svc_name,
                namespace=self._namespace,
            )
            logger.info("Workspace Service deleted: %s", svc_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug("Workspace Service already deleted: %s", svc_name)
                return True
            logger.error("Failed to delete workspace Service %s: %s", svc_name, e)
            return False

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
            # See docs/features/workspace_network_policy_unification.md
            "srw.io/component": "agent-workspace",
            # Per-project egress tier — selected by one NetworkPolicy per
            # tier in helm. See docs/features/workspace_network_isolation.md §3.
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
            "metadata": {"name": cm_name, "namespace": self._namespace},
            "data": data,
        }
        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_config_map,
                namespace=self._namespace,
                body=body,
            )
            return cm_name
        except Exception as e:
            if getattr(e, "status", None) == 409:
                # Stale ConfigMap from a prior attempt — refresh its contents.
                try:
                    await asyncio.to_thread(
                        self._core_api.replace_namespaced_config_map,
                        name=cm_name,
                        namespace=self._namespace,
                        body=body,
                    )
                    return cm_name
                except Exception as e2:
                    logger.warning(
                        "ide seed: replace configmap %s failed: %s", cm_name, e2
                    )
                    return None
            logger.warning("ide seed: create configmap %s failed: %s", cm_name, e)
            return None

    async def _adopt_configmap(self, cm_name: Optional[str], pod_obj: Any) -> None:
        """Set the pod as the ConfigMap's owner so K8s GCs it on teardown.

        Best-effort: a failure here just means we fall back to the explicit
        delete in ``delete_workspace``/``delete_ide_pod``.
        """
        if not cm_name or not self._core_api or pod_obj is None:
            return
        try:
            uid = pod_obj.metadata.uid
            name = pod_obj.metadata.name
        except Exception:
            return
        if not uid:
            return
        patch = {
            "metadata": {
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": name,
                        "uid": uid,
                        "controller": True,
                        "blockOwnerDeletion": False,
                    }
                ]
            }
        }
        try:
            await asyncio.to_thread(
                self._core_api.patch_namespaced_config_map,
                name=cm_name,
                namespace=self._namespace,
                body=patch,
            )
        except Exception as e:
            logger.debug("ide seed: adopt configmap %s failed: %s", cm_name, e)

    async def _delete_seed_configmap(self, pod_name: str) -> None:
        """Delete a pod's seed ConfigMap (idempotent; ignores 404)."""
        if not self._core_api:
            return
        cm_name = self._seed_configmap_name(pod_name)
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_config_map,
                name=cm_name,
                namespace=self._namespace,
            )
        except Exception as e:
            if getattr(e, "status", None) != 404:
                logger.debug("ide seed: delete configmap %s failed: %s", cm_name, e)

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
                },
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
        self, pod_manifest: dict, pod_name: str
    ) -> Optional[Any]:
        """Create the workspace pod, resolving a suspend/resume teardown race.

        Returns the created pod object, or ``None`` when a live pod with this
        name already exists (idempotent double-create — the caller skips
        ConfigMap adoption and just waits for readiness). Re-raises any
        non-409 API error to the caller's failure path.

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
            return await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
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

        terminating = (
            existing is None or existing.metadata.deletion_timestamp is not None
        )
        if not terminating:
            # Live pod already present (two creates raced for one owner) —
            # treat as idempotent; the existing pod owns its ConfigMap.
            logger.info(
                "Workspace pod %s already exists and is live — adopting", pod_name
            )
            return None

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
        return await asyncio.to_thread(
            self._core_api.create_namespaced_pod,
            namespace=self._namespace,
            body=pod_manifest,
        )

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

    async def _wait_for_ready(self, pod_name: str, timeout: int = 120) -> Optional[str]:
        """Poll until the workspace pod is Running and has an IP.

        Returns:
            Pod IP if ready, None if timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            try:
                pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=self._namespace,
                )
                if pod.status.phase == "Running" and pod.status.pod_ip:
                    # Check container readiness
                    if pod.status.container_statuses and all(
                        cs.ready for cs in pod.status.container_statuses
                    ):
                        return pod.status.pod_ip
            except Exception:
                pass

            await asyncio.sleep(2)

        return None

    async def _trusted_pod_ssh_identity(
        self, pod_name: str, *, pvc_name: str | None = None
    ) -> tuple[str, str]:
        """Read pod UID + host-key fingerprint through the K8s control plane."""

        if self._core_api is None or k8s_stream is None:
            raise RuntimeError("Kubernetes exec transport is unavailable")
        pod = await asyncio.to_thread(
            self._core_api.read_namespaced_pod,
            name=pod_name,
            namespace=self._namespace,
        )
        backing_kind = "pod"
        backing_uid = str(getattr(pod.metadata, "uid", "") or "")
        if pvc_name:
            claim = await asyncio.to_thread(
                self._core_api.read_namespaced_persistent_volume_claim,
                name=pvc_name,
                namespace=self._namespace,
            )
            backing_kind = "pvc"
            backing_uid = str(getattr(claim.metadata, "uid", "") or "")
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
        return f"k8s-{backing_kind}:{self._namespace}:{backing_uid}", fingerprint

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
