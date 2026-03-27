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

    Creates per-job pods with SSH server + code-server. Pods are ephemeral
    (emptyDir storage) and deleted when the job completes or is cancelled.
    """

    def __init__(self):
        self._db: Optional[Any] = None
        self._core_api: Optional[Any] = None
        self._k8s_available: bool = False
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

    @property
    def is_available(self) -> bool:
        """True if container provisioning is available."""
        return self._k8s_available

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(self, db: Any) -> None:
        """Initialize the provisioner.

        Args:
            db: PostgresDB instance for job context updates.
        """
        self._db = db
        self._init_k8s()

        if self._k8s_available:
            logger.info(
                "Container provisioner ready (namespace=%s, image=%s)",
                self._namespace,
                self._workspace_image,
            )
        else:
            logger.info(
                "Container provisioner: not available (kubernetes client unavailable)"
            )

    def _init_k8s(self) -> None:
        """Try to initialize the Kubernetes client."""
        if not K8S_AVAILABLE:
            return

        try:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                try:
                    k8s_config.load_kube_config()
                except k8s_config.ConfigException:
                    logger.debug(
                        "Kubernetes not available (no in-cluster or kubeconfig)"
                    )
                    return

            self._core_api = k8s_client.CoreV1Api()
            self._k8s_available = True
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
            logger.info(
                "Workspace container created: %s (job %s)", pod_name, job_id
            )
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
            logger.info(
                "Workspace container deleted: %s (job %s)", pod_name, job_id
            )
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
                ready = all(
                    cs.ready for cs in pod.status.container_statuses
                )

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
            logger.debug(
                "Workspace status query failed for job %s: %s", job_id, e
            )
            return None

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _build_pod_manifest(
        self,
        pod_name: str,
        job_id: str,
        image: str,
        cpu: str,
        memory: str,
        cpu_limit: str,
        memory_limit: str,
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
                "containers": [
                    {
                        "name": "workspace",
                        "image": image,
                        "ports": [
                            {"containerPort": 22, "name": "ssh"},
                            {"containerPort": 8080, "name": "code-server"},
                        ],
                        "resources": {
                            "requests": {"cpu": cpu, "memory": memory},
                            "limits": {
                                "cpu": cpu_limit,
                                "memory": memory_limit,
                            },
                        },
                        "volumeMounts": [
                            {
                                "name": "workspace-data",
                                "mountPath": "/home/agent-host/workspace",
                            },
                            {
                                "name": "ssh-pubkey",
                                "mountPath": "/home/agent-host/.ssh/authorized_keys",
                                "subPath": "ssh-publickey",
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

    async def _wait_for_ready(
        self, pod_name: str, timeout: int = 120
    ) -> Optional[str]:
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


# Module-level singleton
container_provisioner = ContainerProvisioner()
