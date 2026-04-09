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

logger = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config

    K8S_AVAILABLE = True
except ImportError:
    k8s_client = None
    k8s_config = None
    K8S_AVAILABLE = False


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
        job_id: str,
        cpu: str = "500m",
        memory: str = "1Gi",
        cpu_limit: str = "2000m",
        memory_limit: str = "4Gi",
        image: Optional[str] = None,
    ) -> bool:
        """Create a workspace container for a job.

        Args:
            job_id: Job UUID.
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

        pod_name = f"workspace-{job_id[:12]}"
        workspace_image = image or self._workspace_image

        # emptyDir by default — storage dies with the pod, no cleanup needed.
        # Each job gets a fresh container; isolation is the pod boundary.
        pod_manifest = self._build_pod_manifest(
            pod_name=pod_name,
            job_id=job_id,
            image=workspace_image,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
        )

        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
            logger.info("Workspace container created: %s (job %s)", pod_name, job_id)
            await self._set_context(
                job_id,
                {
                    "status": "created",
                    "pod_name": pod_name,
                    "namespace": self._namespace,
                },
            )

            # Wait for pod IP (poll until ready or timeout)
            pod_ip = await self._wait_for_ready(pod_name, timeout=120)
            if pod_ip:
                await self._set_context(
                    job_id,
                    {"status": "ready", "pod_ip": pod_ip},
                )
                logger.info(
                    "Workspace container ready: %s @ %s (job %s)",
                    pod_name,
                    pod_ip,
                    job_id,
                )
            else:
                logger.warning(
                    "Workspace container created but not ready within timeout: %s (job %s)",
                    pod_name,
                    job_id,
                )
                await self._set_context(job_id, {"status": "creating"})

            return True
        except Exception as e:
            logger.error(
                "Failed to create workspace container for job %s: %s", job_id, e
            )
            await self._set_context(
                job_id,
                {"status": "failed", "error": str(e)},
            )
            return False

    async def delete_workspace(self, job_id: str) -> bool:
        """Delete the workspace container for a job.

        Returns:
            True if deleted, False otherwise.
        """
        if not self._k8s_available:
            return False

        pod_name = f"workspace-{job_id[:12]}"

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=10,
            )
            logger.info("Workspace container deleted: %s (job %s)", pod_name, job_id)
            await self._set_context(job_id, {"status": "deleted"})
            return True
        except Exception as e:
            # 404 is fine — pod already gone
            if hasattr(e, "status") and e.status == 404:
                logger.debug(
                    "Workspace container already deleted: %s (job %s)",
                    pod_name,
                    job_id,
                )
                return True
            logger.error(
                "Failed to delete workspace container for job %s: %s", job_id, e
            )
            return False

    async def delete_workspace_pvc(self, job_id: str) -> bool:
        """Delete the PVC for a job workspace if one exists.

        With emptyDir (default), there is no PVC — storage dies with the pod.
        This method is kept for backward compatibility: it cleans up PVCs
        from workspaces created before the emptyDir switch.
        """
        pvc_name = f"pvc-workspace-{job_id[:12]}"
        return await self._delete_pvc(pvc_name)

    async def release_workspace(self, job_id: str) -> bool:
        """Snapshot a job workspace to S3, then delete the pod.

        K8s pods use emptyDir so data dies with the pod. Snapshotting before
        deletion enables resume support (a new pod can restore from S3).

        Returns:
            True if deletion succeeded (snapshot failure is non-fatal).
        """
        # Get pod IP for SSH-based snapshot
        status = await self.get_workspace_status(job_id)
        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and status
            and status.get("pod_ip")
            and status.get("ready")
        ):
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=job_id,
                    ssh_host=status["pod_ip"],
                    ssh_port=22,
                    source_type="pod",
                )
                logger.info(
                    "Workspace snapshot captured for job %s before release", job_id
                )
            except Exception:
                logger.exception(
                    "Workspace snapshot failed for job %s — deleting anyway", job_id
                )

        deleted = await self.delete_workspace(job_id)
        await self.delete_workspace_pvc(job_id)
        return deleted

    async def release_thread_workspace(self, thread_id: str) -> bool:
        """Snapshot a thread workspace to S3, then delete the pod.

        Returns:
            True if deletion succeeded (snapshot failure is non-fatal).
        """
        if not self._k8s_available:
            return False

        pod_name = f"ws-thread-{thread_id[:12]}"

        # Get pod IP for snapshot
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
            pod_ip = pod.status.pod_ip
            ready = pod.status.container_statuses and all(
                cs.ready for cs in pod.status.container_statuses
            )
        except Exception:
            pod_ip = None
            ready = False

        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and pod_ip
            and ready
        ):
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=thread_id,
                    ssh_host=pod_ip,
                    ssh_port=22,
                    source_type="pod",
                    entity_type="threads",
                )
                logger.info(
                    "Workspace snapshot captured for thread %s before release",
                    thread_id,
                )
            except Exception:
                logger.exception(
                    "Workspace snapshot failed for thread %s — deleting anyway",
                    thread_id,
                )

        deleted = await self.delete_thread_workspace(thread_id)
        await self.delete_thread_workspace_pvc(thread_id)
        return deleted

    async def get_workspace_status(self, job_id: str) -> Optional[dict]:
        """Query the workspace container status.

        Returns:
            Status dict or None if not found.
        """
        if not self._k8s_available:
            return None

        pod_name = f"workspace-{job_id[:12]}"

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
                "job_id": job_id,
                "pod_name": pod_name,
                "phase": phase,
                "pod_ip": pod_ip,
                "ready": ready,
            }
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                return None
            logger.debug("Workspace status query failed for job %s: %s", job_id, e)
            return None

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

        pod_manifest = self._build_pod_manifest(
            pod_name=pod_name,
            job_id=job_id,
            image=self._workspace_image,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
        )

        # Override labels to distinguish IDE pods from workspace pods
        pod_manifest["metadata"]["labels"]["srw/component"] = "ide-session"

        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
            logger.info("IDE pod created: %s (job %s)", pod_name, job_id)

            pod_ip = await self._wait_for_ready(pod_name, timeout=90)
            if pod_ip:
                logger.info("IDE pod ready: %s @ %s (job %s)", pod_name, pod_ip, job_id)
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
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
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

        pvc_labels = {"app": "srw-workspace", "srw/component": "workspace-pvc"}
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

    def _build_pod_manifest(
        self,
        pod_name: str,
        job_id: str,
        image: str,
        cpu: str,
        memory: str,
        cpu_limit: str,
        memory_limit: str,
        pvc_name: Optional[str] = None,
    ) -> dict:
        """Build the Kubernetes Pod manifest for a workspace container."""
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self._namespace,
                "labels": {
                    "app": "srw-workspace",
                    "srw/job-id": job_id,
                    "srw/component": "workspace",
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
                # Pod-level security: run SSHD as root (required for port 22
                # and user session management), but restrict everything else.
                "securityContext": {
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "workspace",
                        "image": image,
                        "ports": [
                            {"containerPort": 22, "name": "ssh"},
                            {"containerPort": 8080, "name": "code-server"},
                            {"containerPort": 9222, "name": "cdp"},
                        ],
                        "resources": {
                            "requests": {"cpu": cpu, "memory": memory},
                            "limits": {
                                "cpu": cpu_limit,
                                "memory": memory_limit,
                            },
                        },
                        # Container security hardening:
                        # - Drop all capabilities, add back only what SSHD needs
                        # - SETUID/SETGID: user session switching
                        # - NET_BIND_SERVICE: bind to port 22
                        # - CHOWN/DAC_OVERRIDE/FOWNER: file ownership for sessions
                        # - SYS_CHROOT: SSHD privilege separation
                        # - KILL: signal management
                        # - AUDIT_WRITE: PAM audit logging
                        # - allowPrivilegeEscalation: true (required for SSHD setuid)
                        # - sudo is NOT installed — agent-host cannot escalate
                        "securityContext": {
                            "capabilities": {
                                "drop": ["ALL"],
                                "add": [
                                    "CHOWN",
                                    "DAC_OVERRIDE",
                                    "FOWNER",
                                    "SETGID",
                                    "SETUID",
                                    "NET_BIND_SERVICE",
                                    "SYS_CHROOT",
                                    "KILL",
                                    "AUDIT_WRITE",
                                ],
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
                            "tcpSocket": {"port": 22},
                            "initialDelaySeconds": 3,
                            "periodSeconds": 5,
                        },
                        "livenessProbe": {
                            "tcpSocket": {"port": 22},
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

    async def _set_context(self, job_id: str, updates: dict) -> None:
        """Atomically merge updates into the job's context.workspace_container key."""
        if not self._db:
            return

        try:
            await self._db.merge_workspace_container_context(job_id, updates)
        except Exception:
            logger.exception(
                "Failed to update workspace container context for job %s",
                job_id,
            )

    # =========================================================================
    # Thread workspace (persistent agent sessions)
    # =========================================================================

    async def create_thread_workspace(
        self,
        thread_id: str,
        cpu: str = "500m",
        memory: str = "1Gi",
        cpu_limit: str = "2000m",
        memory_limit: str = "4Gi",
        image: Optional[str] = None,
    ) -> bool:
        """Create a workspace container for a persistent thread.

        Same as create_workspace() but stores context in threads.metadata
        instead of jobs.context.

        Args:
            thread_id: Thread UUID.
            cpu: CPU request.
            memory: Memory request.
            cpu_limit: CPU limit.
            memory_limit: Memory limit.
            image: Workspace image override.

        Returns:
            True if pod creation succeeded, False otherwise.
        """
        if not self._k8s_available:
            return False

        pod_name = f"ws-thread-{thread_id[:12]}"
        workspace_image = image or self._workspace_image

        # emptyDir by default — storage dies with the pod, no cleanup needed.
        # Each session gets a fresh container; isolation is the pod boundary.
        pod_manifest = self._build_pod_manifest(
            pod_name=pod_name,
            job_id=thread_id,  # Reuse job_id label slot for thread_id
            image=workspace_image,
            cpu=cpu,
            memory=memory,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
        )

        # Override labels for thread identification
        pod_manifest["metadata"]["labels"]["srw/thread-id"] = thread_id
        pod_manifest["metadata"]["labels"]["srw/component"] = "thread-workspace"

        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=pod_manifest,
            )
            logger.info("Thread workspace created: %s (thread %s)", pod_name, thread_id)
            await self._set_thread_context(
                thread_id,
                {
                    "status": "created",
                    "pod_name": pod_name,
                    "namespace": self._namespace,
                },
            )

            pod_ip = await self._wait_for_ready(pod_name, timeout=120)
            if pod_ip:
                await self._set_thread_context(
                    thread_id,
                    {"status": "ready", "pod_ip": pod_ip},
                )
                logger.info(
                    "Thread workspace ready: %s @ %s (thread %s)",
                    pod_name,
                    pod_ip,
                    thread_id,
                )
            else:
                logger.warning(
                    "Thread workspace not ready within timeout: %s (thread %s)",
                    pod_name,
                    thread_id,
                )
                await self._set_thread_context(thread_id, {"status": "creating"})

            return True
        except Exception as e:
            logger.error("Failed to create thread workspace for %s: %s", thread_id, e)
            await self._set_thread_context(
                thread_id,
                {"status": "failed", "error": str(e)},
            )
            return False

    async def delete_thread_workspace(self, thread_id: str) -> bool:
        """Delete the workspace container for a persistent thread.

        Returns:
            True if deleted, False otherwise.
        """
        if not self._k8s_available:
            return False

        pod_name = f"ws-thread-{thread_id[:12]}"

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=10,
            )
            logger.info("Thread workspace deleted: %s (thread %s)", pod_name, thread_id)
            await self._set_thread_context(thread_id, {"status": "deleted"})
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug(
                    "Thread workspace already deleted: %s (thread %s)",
                    pod_name,
                    thread_id,
                )
                return True
            logger.error("Failed to delete thread workspace for %s: %s", thread_id, e)
            return False

    async def delete_thread_workspace_pvc(self, thread_id: str) -> bool:
        """Delete the PVC for a thread workspace if one exists.

        With emptyDir (default), there is no PVC — storage dies with the pod.
        Kept for backward compatibility with existing PVCs.
        """
        pvc_name = f"pvc-ws-thread-{thread_id[:12]}"
        return await self._delete_pvc(pvc_name)

    async def _set_thread_context(self, thread_id: str, updates: dict) -> None:
        """Atomically merge updates into thread's metadata.workspace_container."""
        if not self._db:
            return

        try:
            await self._db.merge_thread_workspace_context(thread_id, updates)
        except Exception:
            logger.exception(
                "Failed to update thread workspace context for %s", thread_id
            )


# Module-level singleton
container_provisioner = ContainerProvisioner()
