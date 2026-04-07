"""Unified Agent Provisioner — On-demand pod lifecycle for jobs and sessions.

Creates ephemeral K8s Pods for dual-mode agents. Each pod handles exactly one
task (job or session), then exits (restartPolicy: Never). Replaces both the
static agent Deployment and the PersistentProvisioner.

Lifecycle:
    provision_agent()        — pending job or new session → create agent pod
    delete_agent_pod()       — explicit cleanup by pod name
    delete_agent_pod_by_thread() — cleanup by thread_id label
    cleanup_completed_pods() — periodic GC of Succeeded/Failed pods
    ensure_warm_pool()       — maintain MIN_AGENTS idle pods for responsiveness

Docker Compose mode is unaffected — agents use the static container pool.
"""

import asyncio
import logging
import os
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class AgentProvisioner:
    """Provisions agent pods on demand via Kubernetes API.

    Follows the same singleton + ``connect(db)`` pattern as
    ContainerProvisioner and PersistentProvisioner.
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
            "AGENT_IMAGE",
            os.environ.get(
                "PERSISTENT_AGENT_IMAGE",
                "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent:latest",
            ),
        )
        self._configmap_name: str = os.environ.get("AGENT_CONFIGMAP", "srw-config")
        self._secret_name: str = os.environ.get("AGENT_SECRET", "srw-secrets")
        self._max_agents: int = int(os.environ.get("MAX_AGENTS", "10"))
        self._min_agents: int = int(os.environ.get("MIN_AGENTS", "0"))
        self._agent_buffer: int = int(os.environ.get("AGENT_BUFFER", "0"))
        self._reserved_session_slots: int = int(
            os.environ.get("RESERVED_SESSION_SLOTS", "0")
        )
        self._reserved_job_slots: int = int(os.environ.get("RESERVED_JOB_SLOTS", "0"))
        self._label_selector: str = "srw/managed-by=agent-provisioner"

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def is_available(self) -> bool:
        """Whether K8s provisioning is available."""
        return self._k8s_available

    @property
    def in_cluster(self) -> bool:
        """True if connected via in-cluster config (running inside K8s)."""
        return self._in_cluster

    @property
    def max_agents(self) -> int:
        return self._max_agents

    @property
    def min_agents(self) -> int:
        return self._min_agents

    # =========================================================================
    # Initialization
    # =========================================================================

    def connect(self, db: Any) -> None:
        """Initialize provisioner with database connection."""
        self._db = db
        self._init_k8s()

        if self._k8s_available:
            logger.info(
                "AgentProvisioner ready (namespace=%s, image=%s, "
                "max=%d, min=%d, buffer=%d, "
                "reserved_session=%d, reserved_job=%d)",
                self._namespace,
                self._agent_image,
                self._max_agents,
                self._min_agents,
                self._agent_buffer,
                self._reserved_session_slots,
                self._reserved_job_slots,
            )
        else:
            logger.info(
                "AgentProvisioner: K8s not available — "
                "agents must be started manually or via Docker pool"
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
                    logger.info("K8s not available for agent provisioning")
                    return

            self._core_api = k8s_client.CoreV1Api()
            self._k8s_available = True
            self._in_cluster = in_cluster
        except ImportError:
            logger.info(
                "kubernetes package not installed — agent provisioning disabled"
            )

    # =========================================================================
    # Pod lifecycle
    # =========================================================================

    async def provision_agent(
        self,
        purpose: str,
        thread_id: Optional[str] = None,
        config_name: str = "defaults",
        cpu_request: str = "250m",
        memory_request: str = "512Mi",
        cpu_limit: str = "1000m",
        memory_limit: str = "2Gi",
    ) -> Optional[str]:
        """Create an on-demand agent pod.

        Args:
            purpose: ``"job"`` or ``"session"``.
            thread_id: Thread UUID (required for session purpose).
            config_name: Agent config to use.
            cpu_request/memory_request: Resource requests.
            cpu_limit/memory_limit: Resource limits.

        Returns:
            Pod name if created, None if at capacity or on error.
        """
        if not self._k8s_available:
            logger.info(
                "K8s not available — start agent manually: "
                "python agent.py --dev --port 8001"
            )
            return None

        # Capacity check with reservation awareness
        counts = await self.active_counts_by_purpose()
        total = counts["total"]
        if total >= self._max_agents:
            logger.warning(
                "Agent pool at capacity (%d/%d) — cannot provision new %s agent",
                total,
                self._max_agents,
                purpose,
            )
            return None

        # Reservation check: ensure this purpose doesn't starve the other
        if purpose == "job" and self._reserved_session_slots > 0:
            job_ceiling = self._max_agents - self._reserved_session_slots
            if counts["job"] >= job_ceiling:
                logger.warning(
                    "Job agents at reservation ceiling (%d/%d, "
                    "%d slots reserved for sessions)",
                    counts["job"],
                    job_ceiling,
                    self._reserved_session_slots,
                )
                return None
        elif purpose == "session" and self._reserved_job_slots > 0:
            session_ceiling = self._max_agents - self._reserved_job_slots
            if counts["session"] >= session_ceiling:
                logger.warning(
                    "Session agents at reservation ceiling (%d/%d, "
                    "%d slots reserved for jobs)",
                    counts["session"],
                    session_ceiling,
                    self._reserved_job_slots,
                )
                return None

        short_id = uuid4().hex[:8]
        pod_name = f"srw-agent-{purpose[0]}-{short_id}"

        manifest = self._build_pod_manifest(
            pod_name=pod_name,
            purpose=purpose,
            thread_id=thread_id,
            config_name=config_name,
            cpu_request=cpu_request,
            memory_request=memory_request,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
        )

        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_pod,
                namespace=self._namespace,
                body=manifest,
            )
            logger.info(
                "Agent pod created: %s (purpose=%s, config=%s)",
                pod_name,
                purpose,
                config_name,
            )

            # For sessions, store pod info in thread metadata
            if purpose == "session" and thread_id:
                await self._set_thread_context(
                    thread_id,
                    {
                        "status": "created",
                        "pod_name": pod_name,
                        "namespace": self._namespace,
                    },
                )

            return pod_name
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
                logger.info("Agent pod already exists: %s", pod_name)
                return pod_name

            logger.error("Failed to create agent pod %s: %s", pod_name, e)
            if purpose == "session" and thread_id:
                await self._set_thread_context(
                    thread_id,
                    {"status": "failed", "error": str(e)},
                )
            return None

    async def delete_agent_pod(self, pod_name: str) -> bool:
        """Delete a specific agent pod by name.

        Returns:
            True if deleted (or already gone), False on error.
        """
        if not self._k8s_available:
            return False

        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=30,
            )
            logger.info("Agent pod deleted: %s", pod_name)
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug("Agent pod already gone: %s", pod_name)
                return True
            logger.error("Failed to delete agent pod %s: %s", pod_name, e)
            return False

    async def delete_agent_pod_by_thread(self, thread_id: str) -> bool:
        """Delete agent pod(s) matching a thread_id label.

        Returns:
            True if at least one pod was deleted or none existed.
        """
        if not self._k8s_available:
            return False

        try:
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=f"srw/thread-id={thread_id[:12]}",
            )
            if not pods.items:
                logger.debug("No agent pods found for thread %s", thread_id)
                return True

            for pod in pods.items:
                await self.delete_agent_pod(pod.metadata.name)
            return True
        except Exception as e:
            logger.error(
                "Failed to find/delete agent pods for thread %s: %s",
                thread_id,
                e,
            )
            return False

    async def get_pod_status(self, thread_id: str) -> Optional[dict]:
        """Query pod status by thread_id label (for session pods).

        Returns:
            Status dict with pod_name, phase, pod_ip, ready; or None.
        """
        if not self._k8s_available:
            return None

        try:
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=f"srw/thread-id={thread_id[:12]}",
            )
            if not pods.items:
                return None

            pod = pods.items[0]
            ready = False
            if pod.status.container_statuses:
                ready = all(cs.ready for cs in pod.status.container_statuses)

            return {
                "thread_id": thread_id,
                "pod_name": pod.metadata.name,
                "phase": pod.status.phase,
                "pod_ip": pod.status.pod_ip,
                "ready": ready,
            }
        except Exception as e:
            logger.error("Failed to query agent pod for thread %s: %s", thread_id, e)
            return None

    # =========================================================================
    # Pool management
    # =========================================================================

    async def active_count(self) -> int:
        """Count managed agent pods that are NOT in Succeeded/Failed phase."""
        counts = await self.active_counts_by_purpose()
        return counts["total"]

    async def active_counts_by_purpose(self) -> dict[str, int]:
        """Count managed agent pods by purpose (job/session).

        Returns:
            Dict with keys ``job``, ``session``, ``total``.
        """
        result = {"job": 0, "session": 0, "total": 0}
        if not self._k8s_available:
            return result

        try:
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=self._label_selector,
            )
            for pod in pods.items:
                if pod.status.phase in ("Succeeded", "Failed"):
                    continue
                purpose = (pod.metadata.labels or {}).get("srw/purpose", "job")
                if purpose in result:
                    result[purpose] += 1
                result["total"] += 1
            return result
        except Exception as e:
            logger.error("Failed to count active agent pods: %s", e)
            return result

    async def _count_idle_agents(self) -> int:
        """Count agents registered as ready (idle, waiting for work) in the DB."""
        if not self._db:
            return 0
        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS cnt FROM agents "
                    "WHERE status = 'ready' "
                    "AND COALESCE(agent_mode, 'worker') IN ('worker', 'dual')"
                )
                return row["cnt"] if row else 0
        except Exception as e:
            logger.error("Failed to count idle agents: %s", e)
            return 0

    async def ensure_warm_pool(self) -> int:
        """Maintain agent pool floor and buffer.

        Two complementary mechanisms:
        - **MIN_AGENTS**: absolute floor — never fewer than this many pods.
        - **AGENT_BUFFER**: headroom — try to keep this many idle agents
          ready for instant dispatch, scaling up to MAX_AGENTS.

        Returns:
            Number of pods created.
        """
        if not self._k8s_available:
            return 0
        if self._min_agents <= 0 and self._agent_buffer <= 0:
            return 0

        current = await self.active_count()
        idle = await self._count_idle_agents()

        # Target: whichever is higher — the absolute floor or the buffer need
        needed_for_floor = max(0, self._min_agents - current)
        needed_for_buffer = max(0, self._agent_buffer - idle)
        needed = max(needed_for_floor, needed_for_buffer)

        # Don't exceed MAX_AGENTS
        needed = min(needed, self._max_agents - current)

        if needed <= 0:
            return 0

        created = 0
        for _ in range(needed):
            pod_name = await self.provision_agent(purpose="job")
            if pod_name:
                created += 1
            else:
                break  # At capacity or error

        if created > 0:
            logger.info(
                "Warm pool: created %d agent pod(s) "
                "(active=%d, idle=%d, min=%d, buffer=%d)",
                created,
                current + created,
                idle,
                self._min_agents,
                self._agent_buffer,
            )
        return created

    async def cleanup_completed_pods(self) -> int:
        """Delete Succeeded/Failed agent pods and clean stale DB rows.

        Returns:
            Number of pods cleaned up.
        """
        if not self._k8s_available:
            return 0

        cleaned = 0
        try:
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=self._label_selector,
            )

            for pod in pods.items:
                if pod.status.phase in ("Succeeded", "Failed"):
                    try:
                        await asyncio.to_thread(
                            self._core_api.delete_namespaced_pod,
                            name=pod.metadata.name,
                            namespace=self._namespace,
                            grace_period_seconds=0,
                        )
                        cleaned += 1
                        logger.debug(
                            "Cleaned up %s agent pod: %s",
                            pod.status.phase,
                            pod.metadata.name,
                        )
                    except Exception as e:
                        if not (hasattr(e, "status") and e.status == 404):
                            logger.warning(
                                "Failed to clean up pod %s: %s",
                                pod.metadata.name,
                                e,
                            )
        except Exception as e:
            logger.error("Failed to list agent pods for cleanup: %s", e)

        if cleaned > 0:
            logger.info("Cleaned up %d completed agent pod(s)", cleaned)
        return cleaned

    # =========================================================================
    # Pod manifest
    # =========================================================================

    def _build_pod_manifest(
        self,
        pod_name: str,
        purpose: str,
        thread_id: Optional[str],
        config_name: str,
        cpu_request: str,
        memory_request: str,
        cpu_limit: str,
        memory_limit: str,
    ) -> dict:
        """Build the Kubernetes Pod manifest for an agent.

        Uses ``envFrom`` to inject all keys from the shared ConfigMap and
        Secret, avoiding duplication of the 60+ env vars.
        """
        # Build agent command based on purpose
        if purpose == "session" and thread_id:
            command = (
                f"python agent.py"
                f" --mode persistent"
                f" --thread-id {thread_id}"
                f" --config {config_name}"
                f" --port 8001"
                f" --host 0.0.0.0"
            )
        else:
            command = (
                f"python agent.py --config {config_name} --port 8001 --host 0.0.0.0"
            )

        # Labels
        labels = {
            "app": "srw-agent",
            "srw/component": "agent",
            "srw/managed-by": "agent-provisioner",
            "srw/purpose": purpose,
        }
        if thread_id:
            labels["srw/thread-id"] = thread_id[:12]

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self._namespace,
                "labels": labels,
            },
            "spec": {
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 180,
                "securityContext": {
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                # Wait for orchestrator before starting agent
                "initContainers": [
                    {
                        "name": "wait-for-orchestrator",
                        "image": "busybox:1.36",
                        "command": [
                            "sh",
                            "-c",
                            "until nc -z srw-orchestrator 8085; do sleep 2; done",
                        ],
                    }
                ],
                "containers": [
                    {
                        "name": "agent",
                        "image": self._agent_image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c", command],
                        "ports": [{"containerPort": 8001}],
                        # Inject all env from shared ConfigMap + Secret
                        "envFrom": [
                            {"configMapRef": {"name": self._configmap_name}},
                            {"secretRef": {"name": self._secret_name}},
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
                            {"name": "workspace", "mountPath": "/workspace"},
                            {
                                "name": "vm-ssh-key",
                                "mountPath": "/run/secrets/vm-ssh-key",
                                "subPath": "ssh-privatekey",
                                "readOnly": True,
                            },
                            {"name": "tmp", "mountPath": "/tmp"},
                            {"name": "run", "mountPath": "/run"},
                            {"name": "home-srw", "mountPath": "/home/srw"},
                        ],
                        "livenessProbe": {
                            "httpGet": {"path": "/health", "port": 8001},
                            "initialDelaySeconds": 60,
                            "periodSeconds": 30,
                        },
                        "readinessProbe": {
                            "httpGet": {"path": "/ready", "port": 8001},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                        },
                        "startupProbe": {
                            "httpGet": {"path": "/health", "port": 8001},
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
                    },
                    # Tailscale sidecar — mesh VPN for SSH access to
                    # KubeVirt VMs. Creates tailscale0 tun in shared pod
                    # network namespace so the agent container can route
                    # directly to 100.64.x.y addresses.
                    {
                        "name": "tailscale",
                        "image": "ghcr.io/tailscale/tailscale:v1.82.5",
                        "securityContext": {
                            "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
                        },
                        "command": ["/bin/sh", "-c"],
                        "args": [
                            "mkdir -p /dev/net; "
                            "[ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200; "
                            "tailscaled --state=mem: --tun=tailscale0 "
                            "--no-logs-no-support & "
                            "TSPID=$!; "
                            "for i in $(seq 1 30); do "
                            "[ -S /var/run/tailscale/tailscaled.sock ] && break; "
                            "sleep 1; done; "
                            "while true; do "
                            "if tailscale up "
                            '--auth-key="${TS_AUTHKEY}" '
                            "--login-server=https://headscale.superhuman-remote-worker.com "
                            '--hostname="${POD_NAME}" '
                            "--accept-dns=false "
                            "--timeout=60s 2>&1; then "
                            'echo "Tailscale authenticated"; break; fi; '
                            'echo "Auth retry in 15s..."; sleep 15; done; '
                            "wait $TSPID"
                        ],
                        "env": [
                            {
                                "name": "TS_AUTHKEY",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": self._secret_name,
                                        "key": "TAILSCALE_AUTH_KEY",
                                        "optional": True,
                                    }
                                },
                            },
                            {
                                "name": "POD_NAME",
                                "valueFrom": {
                                    "fieldRef": {"fieldPath": "metadata.name"}
                                },
                            },
                        ],
                        "volumeMounts": [
                            {
                                "name": "tailscale-state",
                                "mountPath": "/var/lib/tailscale",
                            }
                        ],
                        "resources": {
                            "requests": {"memory": "64Mi", "cpu": "50m"},
                            "limits": {"memory": "128Mi", "cpu": "200m"},
                        },
                    },
                ],
                "volumes": [
                    # Scratch workspace (agent connects to real workspace
                    # via SSH — this is just local temp storage)
                    {
                        "name": "workspace",
                        "emptyDir": {"sizeLimit": "10Gi"},
                    },
                    {
                        "name": "vm-ssh-key",
                        "secret": {
                            "secretName": "vm-ssh-key",
                            "defaultMode": 0o444,
                        },
                    },
                    {
                        "name": "tmp",
                        "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"},
                    },
                    {
                        "name": "run",
                        "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"},
                    },
                    {"name": "home-srw", "emptyDir": {"sizeLimit": "512Mi"}},
                    {
                        "name": "tailscale-state",
                        "emptyDir": {"sizeLimit": "16Mi"},
                    },
                ],
            },
        }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _set_thread_context(self, thread_id: str, updates: dict) -> None:
        """Store agent pod status in thread metadata under ``agent_pod``."""
        if not self._db:
            return

        try:
            import json

            async with self._db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE threads
                    SET metadata      = jsonb_set(
                            COALESCE(metadata, '{}'),
                            '{agent_pod}',
                            COALESCE(metadata->'agent_pod', '{}'::jsonb)
                                || $2::jsonb
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
agent_provisioner = AgentProvisioner()
