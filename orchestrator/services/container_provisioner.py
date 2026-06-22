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
from services.workspace_lifecycle import WorkspaceOwner

logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config

    K8S_AVAILABLE = True
except ImportError:
    k8s_client = None
    k8s_config = None
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

    Creates per-job pods with SSH server + code-server. Workspace data is
    stored on PVCs so it survives pod crashes. PVCs are deleted on final
    cleanup (job completion/cancellation) but retained during suspension.
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
    ) -> bool:
        """Create a workspace container for a job or persistent thread.

        Args:
            owner: WorkspaceOwner identifying the job or session.
            cpu: CPU request.
            memory: Memory request.
            cpu_limit: CPU limit.
            memory_limit: Memory limit.
            image: Workspace image override (defaults to WORKSPACE_IMAGE env).

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

        # Seed the user's saved code-server config (theme/keybindings/snippets)
        # into the pod before it starts. Best-effort — never blocks provisioning.
        seed_files = await self._resolve_ide_seed_files(owner)
        seed_exts = await self._resolve_ide_extensions(owner)
        seed_needs_state = await self._resolve_ide_needs_state(owner, seed_exts)
        seed_cm = await self._create_seed_configmap(
            pod_name, seed_files, seed_exts, needs_state=seed_needs_state
        )

        # emptyDir by default — storage dies with the pod, no cleanup needed.
        # Each job/session gets a fresh container; isolation is the pod boundary.
        pod_manifest = self._build_pod_manifest(
            pod_name=pod_name,
            owner=owner,
            image=workspace_image,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            network_tier=network_tier,
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
                },
            )

            # Open a compute-metering interval (Slice 4b) — best-effort, billed on
            # the requested cpu/memory from pod creation to deletion. Idempotent
            # on the live-pod re-create path (one open interval per owner).
            await workspace_metering.open_interval(
                self._db, owner, tier="sandbox", cpu=cpu, memory=memory
            )

            # Wait for pod IP (poll until ready or timeout)
            pod_ip = await self._wait_for_ready(pod_name, timeout=120)
            if pod_ip:
                await self._set_context(
                    owner,
                    {"status": "ready", "pod_ip": pod_ip, "port": 30022},
                )
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
    ) -> bool:
        """Create a PVC for workspace data. Idempotent — 409 treated as success."""
        if not self._k8s_available:
            return False

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
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
                logger.debug("PVC already exists: %s", pvc_name)
                return True
            logger.error("Failed to create PVC %s: %s", pvc_name, e)
            return False

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
