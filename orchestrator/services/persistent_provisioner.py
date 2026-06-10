"""Persistent Agent Provisioner — On-demand pod lifecycle for interactive sessions.

Creates ephemeral K8s Pods per persistent thread. Pods run the same agent
image as worker agents but with ``--mode persistent --thread-id <uuid>``.

Lifecycle:
    create_agent_pod()   — user creates session → orchestrator provisions pod
    delete_agent_pod()   — idle timeout / session end → pod deleted (workspace
                           snapshot handled by WorkspaceSuspensionService)
    get_pod_status()     — check if pod is running for a thread

For local development, persistent agents are started manually via:
    python agent.py --mode persistent --thread-id <uuid> --config persistent_defaults
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PersistentProvisioner:
    """Provisions persistent agent pods on demand via Kubernetes API.

    Follows the ContainerProvisioner pattern: direct K8s, graceful
    degradation when K8s is not available.
    """

    def __init__(self) -> None:
        self._db: Optional[Any] = None
        self._core_api: Optional[Any] = None
        self._k8s_available: bool = False
        self._in_cluster: bool = False
        self._namespace: str = os.environ.get(
            "AGENT_NAMESPACE",
            os.environ.get("WORKSPACE_NAMESPACE", "superhuman-remote-worker"),
        )
        self._agent_image: str = os.environ.get(
            "PERSISTENT_AGENT_IMAGE",
            "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest",
        )
        self._configmap_name: str = os.environ.get("AGENT_CONFIGMAP", "srw-config")
        self._secret_name: str = os.environ.get("AGENT_SECRET", "srw")
        self._ssh_secret_name: str = os.environ.get(
            "WORKSPACE_SSH_SECRET", "vm-ssh-key"
        )
        self._storage_class: str = os.environ.get(
            "WORKSPACE_STORAGE_CLASS", "longhorn-ephemeral"
        )
        # host/port for the agent's `wait-for-orchestrator` init container,
        # derived from the chart-injected ORCHESTRATOR_URL (default tracks
        # the dev release name).
        from urllib.parse import urlparse

        _orch = urlparse(
            os.environ.get("ORCHESTRATOR_URL", "http://srw-orchestrator:8085")
        )
        self._orchestrator_host: str = _orch.hostname or "srw-orchestrator"
        self._orchestrator_port: int = _orch.port or 8085

    @property
    def is_available(self) -> bool:
        """Whether K8s provisioning is available."""
        return self._k8s_available

    @property
    def in_cluster(self) -> bool:
        """True if connected via in-cluster config (running inside K8s)."""
        return self._in_cluster

    @property
    def mode(self) -> Optional[str]:
        """Current provisioning mode."""
        if self._k8s_available:
            return "k8s"
        return None

    def connect(self, db: Any) -> None:
        """Initialize provisioner with database connection.

        Args:
            db: PostgresDB instance for thread/agent tracking.
        """
        self._db = db
        self._init_k8s()

        if self._k8s_available:
            logger.info(
                "PersistentProvisioner ready (namespace=%s, image=%s)",
                self._namespace,
                self._agent_image,
            )
        else:
            logger.info(
                "PersistentProvisioner: not available "
                "(persistent agents must be started manually)"
            )

    def _init_k8s(self) -> None:
        """Try to initialize K8s client."""
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            in_cluster = False
            try:
                k8s_config.load_incluster_config()
                in_cluster = True
            except k8s_config.ConfigException:
                try:
                    k8s_config.load_kube_config()
                except k8s_config.ConfigException:
                    logger.info(
                        "K8s not available — persistent agents must be started manually"
                    )
                    return

            self._core_api = k8s_client.CoreV1Api()
            self._k8s_available = True
            self._in_cluster = in_cluster
        except ImportError:
            logger.info(
                "kubernetes package not installed — persistent agents must "
                "be started manually"
            )

    # =========================================================================
    # Pod lifecycle
    # =========================================================================

    async def create_agent_pod(
        self,
        thread_id: str,
        config_name: str = "persistent_defaults",
        cpu_request: str = "250m",
        memory_request: str = "512Mi",
        cpu_limit: str = "1000m",
        memory_limit: str = "2Gi",
    ) -> bool:
        """Create a K8s pod running a persistent agent for *thread_id*.

        Args:
            thread_id: Thread UUID to bind the agent to.
            config_name: Agent config to use (e.g. ``persistent_defaults``).
            cpu_request: CPU request.
            memory_request: Memory request.
            cpu_limit: CPU limit.
            memory_limit: Memory limit.

        Returns:
            True if pod was created (or already existed), False on error.
        """
        if not self._k8s_available:
            logger.info(
                "K8s not available — start agent manually: "
                "python agent.py --mode persistent --thread-id %s "
                "--config %s",
                thread_id,
                config_name,
            )
            return False

        pod_name = f"persistent-{thread_id[:12]}"
        pvc_name = f"pvc-persistent-{thread_id[:12]}"

        # Create PVC for agent workspace (idempotent — reuses existing on restore)
        pvc_ok = await self._create_pvc(
            pvc_name, size="10Gi", labels={"srw/thread-id": thread_id}
        )
        if not pvc_ok:
            now_iso = datetime.now(timezone.utc).isoformat()
            await self._set_thread_context(
                thread_id,
                {
                    "status": "failed",
                    "error": "PVC creation failed",
                    "updated_at": now_iso,
                },
            )
            return False

        manifest = self._build_agent_pod_manifest(
            pod_name=pod_name,
            thread_id=thread_id,
            config_name=config_name,
            cpu_request=cpu_request,
            memory_request=memory_request,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            pvc_name=pvc_name,
        )

        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=manifest,
            )
            logger.info("Agent pod created: %s (thread %s)", pod_name, thread_id)
            now_iso = datetime.now(timezone.utc).isoformat()
            await self._set_thread_context(
                thread_id,
                {
                    "status": "created",
                    "pod_name": pod_name,
                    "namespace": self._namespace,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                },
            )

            # Wait for pod to become ready
            pod_ip = await self._wait_for_ready(pod_name, timeout=120)
            if pod_ip:
                await self._set_thread_context(
                    thread_id,
                    {
                        "status": "ready",
                        "pod_ip": pod_ip,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                logger.info(
                    "Agent pod ready: %s @ %s (thread %s)",
                    pod_name,
                    pod_ip,
                    thread_id,
                )
            else:
                logger.warning(
                    "Agent pod not ready within timeout: %s (thread %s)",
                    pod_name,
                    thread_id,
                )
                await self._set_thread_context(
                    thread_id,
                    {
                        "status": "creating",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            return True
        except Exception as e:
            # Handle pod already exists (409 Conflict)
            if hasattr(e, "status") and e.status == 409:
                logger.info(
                    "Agent pod already exists: %s (thread %s)",
                    pod_name,
                    thread_id,
                )
                return True

            logger.error("Failed to create agent pod for thread %s: %s", thread_id, e)
            await self._set_thread_context(
                thread_id,
                {
                    "status": "failed",
                    "error": str(e),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return False

    async def delete_agent_pod(self, thread_id: str) -> bool:
        """Delete the agent pod for a persistent session.

        Args:
            thread_id: Thread UUID.

        Returns:
            True if deleted (or already gone), False on error.
        """
        if not self._k8s_available:
            return False

        pod_name = f"persistent-{thread_id[:12]}"

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=30,
            )
            logger.info("Agent pod deleted: %s (thread %s)", pod_name, thread_id)
            await self._set_thread_context(
                thread_id,
                {
                    "status": "deleted",
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug(
                    "Agent pod already deleted: %s (thread %s)",
                    pod_name,
                    thread_id,
                )
                await self._set_thread_context(
                    thread_id,
                    {
                        "status": "deleted",
                        "deleted_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                return True
            logger.error("Failed to delete agent pod for thread %s: %s", thread_id, e)
            return False

    async def delete_agent_pvc(self, thread_id: str) -> bool:
        """Delete the PVC for an agent pod (final cleanup only).

        Called on thread end/deletion — NOT during suspension.
        """
        pvc_name = f"pvc-persistent-{thread_id[:12]}"
        return await self._delete_pvc(pvc_name)

    async def get_pod_status(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Query pod status for a thread.

        Args:
            thread_id: Thread UUID.

        Returns:
            Status dict with pod_name, phase, pod_ip, ready; or None.
        """
        if not self._k8s_available:
            return None

        pod_name = f"persistent-{thread_id[:12]}"

        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )

            ready = False
            if pod.status.container_statuses:
                ready = all(cs.ready for cs in pod.status.container_statuses)

            return {
                "thread_id": thread_id,
                "pod_name": pod_name,
                "phase": pod.status.phase,
                "pod_ip": pod.status.pod_ip,
                "ready": ready,
            }
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                return None
            logger.error("Failed to query agent pod for thread %s: %s", thread_id, e)
            return None

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _create_pvc(
        self, pvc_name: str, size: str = "10Gi", labels: Optional[dict] = None
    ) -> bool:
        """Create a PVC for agent workspace data. Idempotent — 409 treated as success."""
        if not self._k8s_available:
            return False

        pvc_labels = {
            "app": "srw-persistent-agent",
            "srw/component": "agent-workspace-pvc",
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

    def _build_agent_pod_manifest(
        self,
        pod_name: str,
        thread_id: str,
        config_name: str,
        cpu_request: str,
        memory_request: str,
        cpu_limit: str,
        memory_limit: str,
        pvc_name: Optional[str] = None,
    ) -> dict:
        """Build the Kubernetes Pod manifest for a persistent agent.

        Uses ``envFrom`` to inject all keys from the shared ConfigMap and
        Secret, avoiding duplication of the 60+ env vars from the static
        Deployment.  Pod-specific overrides (AGENT_CONFIG, AGENT_PORT) are
        set via ``env``.
        """
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self._namespace,
                "labels": {
                    "app": "srw-persistent-agent",
                    "srw/thread-id": thread_id,
                    "srw/component": "persistent-agent",
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 180,
                "securityContext": {
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                # Wait for orchestrator before starting agent. The host:port
                # comes from ORCHESTRATOR_URL (chart-injected, defaults to
                # `http://srw-orchestrator:8085`) so a non-default
                # fullnameOverride doesn't desync agent init from the actual
                # orchestrator Service name.
                "initContainers": [
                    {
                        "name": "wait-for-orchestrator",
                        "image": "busybox:1.36",
                        "command": [
                            "sh",
                            "-c",
                            f"until nc -z {self._orchestrator_host} "
                            f"{self._orchestrator_port}; do sleep 2; done",
                        ],
                    }
                ],
                "containers": [
                    {
                        "name": "agent",
                        "image": self._agent_image,
                        "imagePullPolicy": "Always",
                        "command": [
                            "sh",
                            "-c",
                            f"python agent.py"
                            f" --mode persistent"
                            f" --thread-id {thread_id}"
                            f" --config {config_name}"
                            f" --port 8001"
                            f" --host 0.0.0.0",
                        ],
                        "ports": [{"containerPort": 8001}],
                        # Inject all env from shared ConfigMap + Secret
                        "envFrom": [
                            {
                                "configMapRef": {
                                    "name": self._configmap_name,
                                },
                            },
                            {
                                "secretRef": {
                                    "name": self._secret_name,
                                },
                            },
                        ],
                        # Pod-specific overrides
                        "env": [
                            {"name": "AGENT_CONFIG", "value": config_name},
                            {"name": "AGENT_PORT", "value": "8001"},
                        ],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 999,
                            "runAsGroup": 999,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [
                            {
                                "name": "workspace",
                                "mountPath": "/workspace",
                            },
                            {
                                "name": "vm-ssh-key",
                                "mountPath": "/run/secrets/vm-ssh-key",
                                "subPath": "ssh-privatekey",
                                "readOnly": True,
                            },
                            {"name": "tmp", "mountPath": "/tmp"},
                            {"name": "run", "mountPath": "/run"},
                            {
                                "name": "home-srw",
                                "mountPath": "/home/srw",
                            },
                        ],
                        "livenessProbe": {
                            "httpGet": {
                                "path": "/health",
                                "port": 8001,
                            },
                            "initialDelaySeconds": 60,
                            "periodSeconds": 30,
                        },
                        "readinessProbe": {
                            "httpGet": {
                                "path": "/ready",
                                "port": 8001,
                            },
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                        },
                        "startupProbe": {
                            "httpGet": {
                                "path": "/health",
                                "port": 8001,
                            },
                            "failureThreshold": 10,
                            "periodSeconds": 10,
                        },
                        "resources": {
                            "requests": {
                                "memory": memory_request,
                                "cpu": cpu_request,
                            },
                            "limits": {
                                "memory": memory_limit,
                                "cpu": cpu_limit,
                            },
                        },
                    }
                ],
                "volumes": [
                    {
                        "name": "workspace",
                        "persistentVolumeClaim": {"claimName": pvc_name},
                    }
                    if pvc_name
                    else {
                        "name": "workspace",
                        "emptyDir": {"sizeLimit": "10Gi"},
                    },
                    {
                        "name": "vm-ssh-key",
                        "secret": {
                            "secretName": self._ssh_secret_name,
                            "defaultMode": 0o444,
                        },
                    },
                    {
                        "name": "tmp",
                        "emptyDir": {
                            "medium": "Memory",
                            "sizeLimit": "256Mi",
                        },
                    },
                    {
                        "name": "run",
                        "emptyDir": {
                            "medium": "Memory",
                            "sizeLimit": "16Mi",
                        },
                    },
                    {
                        "name": "home-srw",
                        "emptyDir": {"sizeLimit": "512Mi"},
                    },
                ],
            },
        }

    async def _wait_for_ready(self, pod_name: str, timeout: int = 120) -> Optional[str]:
        """Poll until the agent pod is Running and has an IP.

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
                    if pod.status.container_statuses and all(
                        cs.ready for cs in pod.status.container_statuses
                    ):
                        return pod.status.pod_ip
            except Exception:
                pass

            await asyncio.sleep(2)

        return None

    async def _set_thread_context(self, thread_id: str, updates: dict) -> None:
        """Store agent pod status in thread metadata.

        Uses the existing ``merge_thread_workspace_context`` with an
        ``agent_pod`` wrapper key so it doesn't collide with workspace
        container context.
        """
        if not self._db:
            return

        try:
            # We store under metadata.agent_pod by wrapping the merge.
            # The existing merge_thread_workspace_context merges into
            # metadata.workspace_container — we need a custom approach.
            # For simplicity, do a direct JSONB merge on metadata.agent_pod.
            import json

            async with self._db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE threads
                    SET metadata      = jsonb_set(
                            COALESCE(metadata, '{}'),
                            '{agent_pod}',
                            COALESCE(metadata->'agent_pod', '{}'::jsonb) || $2::jsonb
                                        ),
                        last_activity = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    thread_id,
                    json.dumps(updates),
                )
        except Exception:
            logger.exception(
                "Failed to update agent pod context for thread %s", thread_id
            )


# Module-level singleton
persistent_provisioner = PersistentProvisioner()
