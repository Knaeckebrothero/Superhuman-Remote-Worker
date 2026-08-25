"""Persistent Agent Provisioner — On-demand pod lifecycle for interactive sessions.

Creates ephemeral K8s Pods per persistent thread. Pods run the same agent
image as worker agents but with ``--mode persistent --thread-id <uuid>``.

Lifecycle:
    create_agent_pod()   — user creates session → orchestrator provisions pod
    delete_agent_pod()   — idle timeout / session end → pod deleted (workspace
                           snapshot handled by WorkspaceSuspensionService)
    get_pod_status()     — check if pod is running for a thread

For local development, persistent agents are started manually via:
    python agent.py --mode persistent --thread-id <uuid> --config session_base
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Dict, Optional

from src.core.loader import canonical_config_name

from .runtime_actor import issue_runtime_actor_bootstrap

logger = logging.getLogger(__name__)


class PersistentPodCreateStatus(StrEnum):
    """Truthful outcomes for the deterministic persistent-pod name."""

    CREATED = "created"
    ALREADY_CURRENT = "already_current"
    TERMINATING = "terminating"
    CONFLICTING = "conflicting"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PersistentPodCreateResult:
    """Result of one create attempt without collapsing a 409 into success."""

    status: PersistentPodCreateStatus
    pod_name: str
    pod_uid: str | None = None
    build_sha: str | None = None
    failure_class: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in {
            PersistentPodCreateStatus.CREATED,
            PersistentPodCreateStatus.ALREADY_CURRENT,
        }


def _normalize_config_name(config_name: str) -> str:
    """A UUID in ``config_name`` means the cockpit put the expert id in the
    wrong slot — it has no on-disk ``<uuid>.yaml`` and ``--config <uuid>``
    crashes startup. Sessions apply the bound expert via ``config_override``,
    so fall back to the session base. See
    knowledge-history/done/global_expert_management.md."""
    if not config_name:
        return canonical_config_name(config_name)
    try:
        uuid.UUID(str(config_name))
    except (ValueError, TypeError, AttributeError):
        return config_name
    logger.warning(
        "session config_name %s is a UUID (expert id in the config slot); "
        "booting session_base — expert applies via config_override.",
        config_name,
    )
    return "session_base"


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
        self._agent_image_pull_policy: str = os.environ.get(
            "PERSISTENT_AGENT_IMAGE_PULL_POLICY", "Always"
        ).strip()
        if self._agent_image_pull_policy not in {"Always", "IfNotPresent", "Never"}:
            raise ValueError(
                "PERSISTENT_AGENT_IMAGE_PULL_POLICY must be one of "
                "Always, IfNotPresent, or Never"
            )
        # Chart labels — without these the database NetworkPolicies (which
        # match app.kubernetes.io/{name,instance} + component=agent) REJECT
        # ingress from these pods: the officer respawn crash-looped on
        # asyncpg ECONNREFUSED to srw-postgres until they were added
        # (2026-07-30). Injected by the chart's orchestrator Deployment,
        # same mechanism as agent_provisioner.
        self._chart_label_name: str = os.environ.get("AGENT_LABEL_NAME", "").strip()
        self._chart_label_instance: str = os.environ.get(
            "AGENT_LABEL_INSTANCE", ""
        ).strip()
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

    @property
    def image_ref(self) -> str:
        """Exact server-configured image used for a new persistent pod."""

        return self._agent_image

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
        config_name: str = "session_base",
        expert_id: str | None = None,
        cpu_request: str = "250m",
        memory_request: str = "512Mi",
        cpu_limit: str = "1000m",
        memory_limit: str = "2Gi",
        lifecycle_generation: str | None = None,
        target_image_ref: str | None = None,
    ) -> PersistentPodCreateResult:
        """Create a K8s pod running a persistent agent for *thread_id*.

        Args:
            thread_id: Thread UUID to bind the agent to.
            config_name: Agent config to use (e.g. ``session_base``).
            cpu_request: CPU request.
            memory_request: Memory request.
            cpu_limit: CPU limit.
            memory_limit: Memory limit.

        Returns a typed result. In particular, an existing terminating pod is
        never reported as successful, and a live 409 is accepted only when
        its server-owned labels describe this exact thread/build/generation.
        """
        config_name = _normalize_config_name(config_name)
        image_ref = target_image_ref or self._agent_image
        target_build_sha = self._build_sha(image_ref)
        if not self._k8s_available:
            logger.info(
                "K8s not available — start agent manually: "
                "python agent.py --mode persistent --thread-id %s "
                "--config %s",
                thread_id,
                config_name,
            )
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                f"persistent-{thread_id[:12]}",
                failure_class="kubernetes_unavailable",
            )

        pod_name = f"persistent-{thread_id[:12]}"
        pvc_name = f"pvc-persistent-{thread_id[:12]}"

        # Avoid minting an unused bootstrap for the ordinary idempotent case.
        # Kubernetes create remains the final concurrency CAS; a race after
        # this read is classified again from the incumbent on 409.
        try:
            incumbent = await self._read_pod(pod_name)
        except Exception as exc:
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                pod_name,
                failure_class=f"observation_{type(exc).__name__}"[:128],
            )
        if incumbent is not None:
            return self._classify_incumbent(
                incumbent,
                thread_id=thread_id,
                pod_name=pod_name,
                lifecycle_generation=lifecycle_generation,
                expected_build_sha=target_build_sha,
            )

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
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                pod_name,
                failure_class="pvc_creation_failed",
            )

        try:
            runtime_actor_bootstrap = await issue_runtime_actor_bootstrap(
                self._db, thread_id
            )
        except Exception:
            logger.exception(
                "Could not issue runtime actor bootstrap for session %s; "
                "refusing to provision an identity-less pod",
                thread_id,
            )
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                pod_name,
                failure_class="runtime_bootstrap_failed",
            )

        manifest = self._build_agent_pod_manifest(
            pod_name=pod_name,
            thread_id=thread_id,
            config_name=config_name,
            expert_id=expert_id,
            cpu_request=cpu_request,
            memory_request=memory_request,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            pvc_name=pvc_name,
            runtime_actor_bootstrap=runtime_actor_bootstrap,
            lifecycle_generation=lifecycle_generation,
            image_ref=image_ref,
        )

        try:
            created_pod = await asyncio.to_thread(
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
                    "expected_build_sha": target_build_sha,
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

            pod_uid = (
                str(getattr(getattr(created_pod, "metadata", None), "uid", "") or "")
                or None
            )
            if pod_uid is None:
                observed = await self._read_pod(pod_name)
                pod_uid = (
                    str(getattr(getattr(observed, "metadata", None), "uid", "") or "")
                    or None
                )
            await self._set_thread_context(
                thread_id,
                {
                    "pod_uid": pod_uid,
                    "observed_build_sha": target_build_sha,
                    "lifecycle_generation": lifecycle_generation,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.CREATED,
                pod_name,
                pod_uid=pod_uid,
                build_sha=target_build_sha,
            )
        except Exception as e:
            if hasattr(e, "status") and e.status == 409:
                try:
                    incumbent = await self._read_pod(pod_name)
                except Exception as exc:
                    return PersistentPodCreateResult(
                        PersistentPodCreateStatus.FAILED,
                        pod_name,
                        failure_class=f"observation_{type(exc).__name__}"[:128],
                    )
                if incumbent is not None:
                    return self._classify_incumbent(
                        incumbent,
                        thread_id=thread_id,
                        pod_name=pod_name,
                        lifecycle_generation=lifecycle_generation,
                        expected_build_sha=target_build_sha,
                    )
                logger.info(
                    "Persistent pod create conflicted but incumbent vanished: %s",
                    pod_name,
                )
                return PersistentPodCreateResult(
                    PersistentPodCreateStatus.CONFLICTING,
                    pod_name,
                    failure_class="conflict_without_incumbent",
                )

            logger.error(
                "Failed to create agent pod for thread %s (%s)",
                thread_id,
                type(e).__name__,
            )
            await self._set_thread_context(
                thread_id,
                {
                    "status": "failed",
                    "error": "persistent pod creation failed",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                pod_name,
                failure_class=type(e).__name__[:128],
            )

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

    async def delete_agent_pod_exact(
        self, thread_id: str, *, expected_pod_uid: str
    ) -> bool:
        """Delete only the exact old pod object, never a same-name successor."""

        if not self._k8s_available or not str(expected_pod_uid).strip():
            return False
        pod_name = f"persistent-{thread_id[:12]}"
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
                grace_period_seconds=30,
                body={"preconditions": {"uid": str(expected_pod_uid)}},
            )
            return True
        except Exception as exc:
            if getattr(exc, "status", None) in {404, 409}:
                # 409 is the UID precondition protecting a replacement.
                return True
            logger.warning("Exact persistent pod deletion failed for %s", pod_name)
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
                "pod_uid": str(getattr(pod.metadata, "uid", "") or "") or None,
                "phase": pod.status.phase,
                "pod_ip": pod.status.pod_ip,
                "ready": ready,
                "terminating": bool(getattr(pod.metadata, "deletion_timestamp", None)),
                "build_sha": (pod.metadata.labels or {}).get("srw/build-sha"),
                "labels": dict(pod.metadata.labels or {}),
            }
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                return None
            logger.error("Failed to query agent pod for thread %s: %s", thread_id, e)
            return None

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @property
    def expected_build_sha(self) -> str | None:
        return self._build_sha(self._agent_image)

    @staticmethod
    def _build_sha(image_ref: str) -> str | None:
        if ":sha-" not in image_ref:
            return None
        return image_ref.rsplit(":sha-", 1)[-1]

    async def _read_pod(self, pod_name: str) -> Any | None:
        try:
            return await asyncio.to_thread(
                self._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=self._namespace,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            logger.warning("Persistent pod observation failed for %s", pod_name)
            raise

    def _classify_incumbent(
        self,
        pod: Any,
        *,
        thread_id: str,
        pod_name: str,
        lifecycle_generation: str | None,
        expected_build_sha: str | None,
    ) -> PersistentPodCreateResult:
        metadata = getattr(pod, "metadata", None)
        labels = dict(getattr(metadata, "labels", None) or {})
        pod_uid = str(getattr(metadata, "uid", "") or "") or None
        build_sha = labels.get("srw/build-sha")
        if getattr(metadata, "deletion_timestamp", None):
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.TERMINATING,
                pod_name,
                pod_uid=pod_uid,
                build_sha=build_sha,
            )
        exact = (
            labels.get("srw/component") == "persistent-agent"
            and labels.get("srw/thread-id") == thread_id
            and (expected_build_sha is None or build_sha == expected_build_sha)
            and (
                lifecycle_generation is None
                or labels.get("srw/recycle-generation") == lifecycle_generation
            )
        )
        return PersistentPodCreateResult(
            (
                PersistentPodCreateStatus.ALREADY_CURRENT
                if exact
                else PersistentPodCreateStatus.CONFLICTING
            ),
            pod_name,
            pod_uid=pod_uid,
            build_sha=build_sha,
            failure_class=None if exact else "incumbent_authority_mismatch",
        )

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
        expert_id: Optional[str] = None,
        runtime_actor_bootstrap: Optional[str] = None,
        lifecycle_generation: Optional[str] = None,
        image_ref: Optional[str] = None,
    ) -> dict:
        """Build the Kubernetes Pod manifest for a persistent agent.

        Uses ``envFrom`` to inject all keys from the shared ConfigMap and
        Secret, avoiding duplication of the 60+ env vars from the static
        Deployment.  Pod-specific overrides (AGENT_CONFIG, AGENT_PORT) are
        set via ``env``.
        """
        labels = {
            "app": "srw-persistent-agent",
            "srw/thread-id": thread_id,
            "srw/component": "persistent-agent",
        }
        # NetworkPolicy admission (see __init__): the Helm-rendered DB
        # policies select component=agent specifically — "persistent-agent"
        # does not match them.
        if self._chart_label_name:
            labels["app.kubernetes.io/name"] = self._chart_label_name
        if self._chart_label_instance:
            labels["app.kubernetes.io/instance"] = self._chart_label_instance
        if self._chart_label_name or self._chart_label_instance:
            labels["app.kubernetes.io/component"] = "agent"
        # Build SHA — lets the lifecycle reconciler enumerate stale pods by
        # selector, same convention as agent_provisioner.
        selected_image = image_ref or self._agent_image
        build_sha = self._build_sha(selected_image)
        if build_sha:
            labels["srw/build-sha"] = build_sha
        if lifecycle_generation:
            labels["srw/recycle-generation"] = str(lifecycle_generation)
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
                        "image": selected_image,
                        "imagePullPolicy": self._agent_image_pull_policy,
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
                        # Kubernetes exposes deletionTimestamp outside the
                        # container before the process can observe it.  preStop
                        # creates a pod-local sentinel first, closing input and
                        # provider admission synchronously inside the runtime,
                        # then holds the grace window while the current tool
                        # batch settles.  Abrupt node loss may skip hooks and is
                        # handled by the persistent transcript/LF-5 recovery
                        # path instead.
                        "lifecycle": {
                            "preStop": {
                                "exec": {
                                    "command": [
                                        "sh",
                                        "-c",
                                        ": > /tmp/srw-persistent-terminating; "
                                        "exec python -m src.api.persistent_termination",
                                    ]
                                }
                            }
                        },
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
                            {
                                "name": "POD_UID",
                                "valueFrom": {
                                    "fieldRef": {
                                        "apiVersion": "v1",
                                        "fieldPath": "metadata.uid",
                                    }
                                },
                            },
                            {
                                "name": "MCP_INTERNAL_KEY",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": self._secret_name,
                                        "key": "MCP_INTERNAL_KEY",
                                        "optional": True,
                                    }
                                },
                            },
                            {
                                "name": "SRW_RUNTIME_ACTOR_BOOTSTRAP",
                                "value": runtime_actor_bootstrap or "",
                            },
                        ]
                        + (
                            [{"name": "AGENT_EXPERT_ID", "value": expert_id}]
                            if expert_id
                            else []
                        ),
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
                        # timeoutSeconds raised from the 1s default: token
                        # counting and restore paths can block the event loop
                        # for seconds at a time, and a 1s probe deadline
                        # SIGKILLed a healthy officer pod mid-turn (exit 137,
                        # k3d smoke). 5s tolerates legitimate loop stalls while
                        # still catching a truly wedged process.
                        "livenessProbe": {
                            "httpGet": {
                                "path": "/health",
                                "port": 8001,
                            },
                            "initialDelaySeconds": 60,
                            "periodSeconds": 30,
                            "timeoutSeconds": 5,
                            "failureThreshold": 4,
                        },
                        # /health, NOT /ready: /ready reports "free to accept
                        # a session" (503 while one is attached), which left
                        # dedicated pods 0/1-Ready while demonstrably serving
                        # turns (k3d smoke, open item 8). Dedicated pods are
                        # addressed by pod IP, never through a Service
                        # selector, so readiness here is operator signal —
                        # and the honest signal is process health.
                        "readinessProbe": {
                            "httpGet": {
                                "path": "/health",
                                "port": 8001,
                            },
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
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
