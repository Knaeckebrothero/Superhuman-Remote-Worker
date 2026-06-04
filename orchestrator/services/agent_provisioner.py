"""Unified Agent Provisioner — On-demand pod lifecycle for jobs and sessions.

Creates ephemeral K8s Pods for dual-mode agents. Each pod handles exactly one
task (job or session), then exits (restartPolicy: Never). Replaces both the
static agent Deployment and the PersistentProvisioner.

Lifecycle:
    provision_agent()        — pending job or new session → create agent pod
    delete_agent_pod()       — explicit cleanup by pod name
    delete_agent_pod_by_thread() — cleanup by thread_id label
    reap_pods()              — periodic GC (completed / stale / unstartable)
    ensure_warm_pool()       — maintain MIN_AGENTS idle pods for responsiveness

Docker Compose mode is unaffected — agents use the static container pool.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# Pending-phase container waiting reasons we treat as unrecoverable past grace.
# All reflect bad configuration or missing dependencies that the kubelet will
# retry forever without making progress — deleting the pod lets the scaler
# recreate it with the current config.
_TERMINAL_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "CreateContainerConfigError",
        "CreateContainerError",
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "RunContainerError",
    }
)


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
        self._secret_name: str = os.environ.get("AGENT_SECRET", "srw")
        self._ssh_secret_name: str = os.environ.get(
            "WORKSPACE_SSH_SECRET", "vm-ssh-key"
        )
        self._max_agents: int = int(os.environ.get("MAX_AGENTS", "10"))
        self._min_agents: int = int(os.environ.get("MIN_AGENTS", "0"))
        self._agent_buffer: int = int(os.environ.get("AGENT_BUFFER", "0"))
        self._reserved_session_slots: int = int(
            os.environ.get("RESERVED_SESSION_SLOTS", "0")
        )
        self._reserved_job_slots: int = int(os.environ.get("RESERVED_JOB_SLOTS", "0"))
        self._label_selector: str = "srw/managed-by=agent-provisioner"
        # Standard Helm chart labels for chart-managed NetworkPolicies.
        # Without these, the database NetworkPolicies (which match on
        # app.kubernetes.io/{name,instance,component}=agent) reject ingress
        # from dynamically-provisioned agent pods. Injected by the chart's
        # orchestrator Deployment; defaults match the homelab values.
        self._chart_label_name: str = os.environ.get("AGENT_LABEL_NAME", "").strip()
        self._chart_label_instance: str = os.environ.get(
            "AGENT_LABEL_INSTANCE", ""
        ).strip()
        self._tailscale_enabled: bool = os.environ.get(
            "AGENT_TAILSCALE_ENABLED", "false"
        ).strip().lower() in ("true", "1", "yes")
        self._headscale_url: str = os.environ.get("HEADSCALE_URL", "").strip()
        # Sustained-dark window for the tailscale sidecar liveness probe.
        # After this long without a Running backend, the kubelet kills the
        # sidecar and the tunnel_dark reaper recycles the pod. The self-heal
        # loop recovers transient loss well inside this window.
        self._tailscale_dark_timeout: int = int(
            os.environ.get("AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS", "600")
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
                "python agent.py --port 8001 --loop"
            )
            return None

        # Capacity check with reservation-aware eviction
        counts = await self.active_counts_by_purpose()
        total = counts["total"]
        if total >= self._max_agents:
            # At capacity — try to evict an idle agent of the OTHER purpose
            # if this purpose has reserved slots configured.
            evicted = await self._try_evict_for_reservation(purpose, counts)
            if not evicted:
                logger.warning(
                    "Agent pool at capacity (%d/%d) — cannot provision new %s agent",
                    total,
                    self._max_agents,
                    purpose,
                )
                return None
            # One slot freed via eviction — fall through to create pod

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
                # With restartPolicy: Never and a tailscale sidecar, a crashed
                # agent container leaves the pod in phase=Running indefinitely
                # (the sidecar is still up). Skip these so they don't pin the
                # MAX_AGENTS ceiling — the reaper will delete them.
                if self._has_dead_agent_container(pod):
                    continue
                # A live agent with a kubelet-killed tailscale sidecar is a
                # tunnel_dark zombie awaiting reap — don't pin the ceiling.
                if self._has_dead_tunnel_sidecar(pod):
                    continue
                purpose = (pod.metadata.labels or {}).get("srw/purpose", "job")
                if purpose in result:
                    result[purpose] += 1
                result["total"] += 1
            return result
        except Exception as e:
            logger.error("Failed to count active agent pods: %s", e)
            return result

    @staticmethod
    def _has_dead_agent_container(pod) -> bool:
        """True if the primary "agent" container has terminated.

        Catches both crashes (non-zero exit) and clean exits that didn't
        propagate to pod phase because a sidecar is still running.
        """
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != "agent":
                continue
            state = getattr(cs, "state", None)
            if state and getattr(state, "terminated", None) is not None:
                return True
        return False

    @staticmethod
    def _has_dead_tunnel_sidecar(pod) -> bool:
        """True if the "tailscale" sidecar container has terminated.

        The kubelet kills the sidecar via its liveness probe after a sustained
        dark window; with restartPolicy=Never it stays terminated. The agent
        container may still be Running, so this is checked independently of
        _has_dead_agent_container.
        """
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != "tailscale":
                continue
            state = getattr(cs, "state", None)
            if state and getattr(state, "terminated", None) is not None:
                return True
        return False

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

    async def reap_pods(
        self,
        offline_threshold_minutes: int = 10,
        unstartable_grace_seconds: int = 300,
        crashed_grace_seconds: int = 60,
        tunnel_dark_grace_seconds: int = 60,
    ) -> dict[str, int]:
        """Single-pass GC over managed agent pods.

        Lists the pod set once and dispatches each pod to one of four
        policies with different SLOs:

          - ``completed``: phase in {Succeeded, Failed} → delete immediately.
          - ``crashed``: phase == Running but the ``agent`` container has
            terminated (any exit code) for at least ``crashed_grace_seconds``.
            Catches sidecar-pinned pods that never propagate to phase=Failed,
            including agents that crash before their first heartbeat.
          - ``tunnel_dark``: phase == Running but the ``tailscale`` sidecar
            terminated (kubelet killed it after a sustained dark window) for at
            least ``tunnel_dark_grace_seconds``. The agent may still be up, but
            with no tunnel it cannot reach its workspace — recycle it.
          - ``stale``: phase == Running but the agent's heartbeat has been
            offline in the DB for ``offline_threshold_minutes``.
          - ``unstartable``: phase == Pending with a terminal
            ``state.waiting.reason`` (e.g. CreateContainerConfigError,
            ImagePullBackOff) older than ``unstartable_grace_seconds``.

        Returns a per-category count dict.
        """
        stats = {
            "completed": 0,
            "crashed": 0,
            "tunnel_dark": 0,
            "stale": 0,
            "drained": 0,
            "unstartable": 0,
        }
        if not self._k8s_available:
            return stats

        try:
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=self._label_selector,
            )
        except Exception:
            logger.exception("Failed to list agent pods for reaping")
            return stats

        offline_hostnames = await self._fetch_offline_hostnames(
            offline_threshold_minutes
        )
        draining_hostnames = await self._fetch_draining_hostnames()

        for pod in pods.items:
            if self._is_completed(pod):
                category = "completed"
            elif self._is_crashed(pod, crashed_grace_seconds):
                category = "crashed"
            elif self._is_tunnel_dark(pod, tunnel_dark_grace_seconds):
                category = "tunnel_dark"
            elif self._is_stale_running(pod, offline_hostnames):
                category = "stale"
            elif self._is_drained_running(pod, draining_hostnames):
                category = "drained"
            elif self._is_unstartable(pod, unstartable_grace_seconds):
                category = "unstartable"
            else:
                continue
            # Capture agent stderr before delete so unexpected exits don't
            # vanish with the pod. Diagnostic for the
            # persistent_session_permission_check_race incident — see
            # docs/issues/persistent_session_permission_check_race.md.
            await self._capture_agent_logs_before_reap(pod, category)
            if await self.delete_agent_pod(pod.metadata.name):
                stats[category] += 1

        if sum(stats.values()) > 0:
            logger.info("Reaped agent pod(s): %s", stats)
        return stats

    async def _capture_agent_logs_before_reap(self, pod, category: str) -> None:
        """Log the agent container's tail before reap deletes the pod.

        Diagnostic for the persistent_session_permission_check_race incident:
        when an agent exits unexpectedly (or is reaped while a thread is bound),
        the pod is deleted by ``delete_agent_pod`` and its stderr is gone. We
        emit the last 500 lines to the orchestrator's stderr at WARNING so they
        survive in the cluster's log aggregation.

        Always exception-safe: a capture failure must never block the reap.
        """
        pod_name = pod.metadata.name
        # tunnel_dark reaps the pod for a dead *sidecar*; capture that
        # container's logs, else the agent's.
        target = "tailscale" if category == "tunnel_dark" else "agent"
        exit_code: Any = None
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != target:
                continue
            terminated = getattr(getattr(cs, "state", None), "terminated", None)
            if terminated is not None:
                exit_code = getattr(terminated, "exit_code", None)
            break

        try:
            log_tail = await asyncio.to_thread(
                self._core_api.read_namespaced_pod_log,
                name=pod_name,
                namespace=self._namespace,
                container=target,
                tail_lines=500,
                timestamps=True,
            )
        except Exception as e:
            logger.warning(
                "Reap log capture: failed to fetch logs for pod=%s "
                "(category=%s, exit_code=%s): %s",
                pod_name,
                category,
                exit_code,
                e,
            )
            return

        logger.warning(
            "Reap log capture: pod=%s category=%s phase=%s exit_code=%s "
            "logs_below_marker_BEGIN\n%s\nlogs_below_marker_END pod=%s",
            pod_name,
            category,
            pod.status.phase,
            exit_code,
            log_tail or "(empty)",
            pod_name,
        )

    @staticmethod
    def _is_completed(pod) -> bool:
        return pod.status.phase in ("Succeeded", "Failed")

    @staticmethod
    def _is_crashed(pod, grace_seconds: int) -> bool:
        """Pod is Running but the agent container terminated past the grace.

        Brief grace lets in-flight DB writes (final heartbeat, audit) land
        before we delete the pod, which preserves debuggability without
        meaningfully delaying capacity reclaim.
        """
        if pod.status.phase != "Running":
            return False
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != "agent":
                continue
            state = getattr(cs, "state", None)
            terminated = getattr(state, "terminated", None) if state else None
            if terminated is None:
                return False
            finished_at = getattr(terminated, "finished_at", None)
            if finished_at is None:
                # Terminated but no timestamp yet — be conservative, wait.
                return False
            age = (datetime.now(timezone.utc) - finished_at).total_seconds()
            return age >= grace_seconds
        return False

    @staticmethod
    def _is_tunnel_dark(pod, grace_seconds: int) -> bool:
        """Pod is Running but the tailscale sidecar terminated past the grace.

        Brief grace mirrors _is_crashed (let final writes land). The long
        hysteresis already happened in the kubelet liveness probe.
        """
        if pod.status.phase != "Running":
            return False
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != "tailscale":
                continue
            state = getattr(cs, "state", None)
            terminated = getattr(state, "terminated", None) if state else None
            if terminated is None:
                return False
            finished_at = getattr(terminated, "finished_at", None)
            if finished_at is None:
                return False
            age = (datetime.now(timezone.utc) - finished_at).total_seconds()
            return age >= grace_seconds
        return False

    @staticmethod
    def _is_stale_running(pod, offline_hostnames: set[str]) -> bool:
        return pod.status.phase == "Running" and pod.metadata.name in offline_hostnames

    @staticmethod
    def _is_drained_running(pod, draining_hostnames: set[str]) -> bool:
        """Pod is Running and the orchestrator marked the agent as draining.

        Closes the actuation gap in `_drain_stale_image_agents`: that path
        flips the DB row to ``draining`` but never deletes the pod, so the
        agent kept heartbeating until something else terminated it. Now
        ``draining`` triggers a force delete on the next reconciler tick.
        """
        return pod.status.phase == "Running" and pod.metadata.name in draining_hostnames

    @staticmethod
    def _is_unstartable(pod, grace_seconds: int) -> bool:
        if pod.status.phase != "Pending":
            return False
        created = pod.metadata.creation_timestamp
        if created is None:
            return False
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age < grace_seconds:
            return False
        for attr in ("container_statuses", "init_container_statuses"):
            for cs in getattr(pod.status, attr, None) or []:
                state = getattr(cs, "state", None)
                waiting = getattr(state, "waiting", None) if state else None
                reason = getattr(waiting, "reason", None) if waiting else None
                if reason in _TERMINAL_WAITING_REASONS:
                    return True
        return False

    async def _fetch_offline_hostnames(self, threshold_minutes: int) -> set[str]:
        """Return hostnames of agents whose heartbeat has been stale past threshold."""
        if not self._db:
            return set()
        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT hostname FROM agents
                    WHERE status = 'offline'
                      AND hostname IS NOT NULL
                      AND last_heartbeat < NOW() - make_interval(mins => $1)
                    """,
                    threshold_minutes,
                )
            return {r["hostname"] for r in rows}
        except Exception:
            logger.exception("Failed to query offline agents for reaping")
            return set()

    async def _fetch_draining_hostnames(self) -> set[str]:
        """Return hostnames of agents the orchestrator has marked draining."""
        if not self._db:
            return set()
        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT hostname FROM agents
                    WHERE status = 'draining'
                      AND hostname IS NOT NULL
                    """
                )
            return {r["hostname"] for r in rows}
        except Exception:
            logger.exception("Failed to query draining agents for reaping")
            return set()

    async def scale_down_idle(self, max_terminate: int = 2) -> int:
        """Terminate excess idle agent pods above MIN_AGENTS floor.

        Runs each reconciler cycle but only removes up to *max_terminate* pods
        per invocation for gradual scale-down (avoids thundering herd).

        Returns:
            Number of pods terminated.
        """
        if not self._k8s_available or not self._db:
            return 0
        if self._min_agents <= 0:
            return 0  # No floor configured — nothing to scale down to

        active = await self.active_count()
        if active <= self._min_agents:
            return 0

        # Find truly idle agents (no job, no thread, not session-bound)
        try:
            async with self._db.acquire() as conn:
                idle_rows = await conn.fetch(
                    """
                    SELECT id, hostname FROM agents
                    WHERE status = 'ready'
                      AND current_job_id IS NULL
                      AND thread_id IS NULL
                      AND hostname IS NOT NULL
                    ORDER BY last_heartbeat ASC
                    LIMIT $1
                    """,
                    max_terminate + 5,  # fetch a few extra for filtering
                )
        except Exception:
            logger.exception("Failed to query idle agents for scale-down")
            return 0

        if not idle_rows:
            return 0

        excess = active - self._min_agents
        to_terminate = min(excess, len(idle_rows), max_terminate)
        terminated = 0

        for row in idle_rows[:to_terminate]:
            hostname = row["hostname"]
            agent_id = str(row["id"])
            if await self.delete_agent_pod(hostname):
                # Mark agent offline so it doesn't get re-counted as idle
                try:
                    async with self._db.acquire() as conn:
                        await conn.execute(
                            "UPDATE agents SET status = 'offline' WHERE id = $1",
                            agent_id,
                        )
                except Exception:
                    pass  # Heartbeat timeout will handle it
                terminated += 1

        if terminated:
            logger.info(
                "Scale-down: terminated %d idle agent pod(s) (active=%d, min=%d)",
                terminated,
                active - terminated,
                self._min_agents,
            )
        return terminated

    async def _try_evict_for_reservation(self, purpose: str, counts: dict) -> bool:
        """Evict an idle agent of the OTHER purpose to honour reserved slots.

        Called when the pool is at capacity. If the requesting *purpose* has
        reserved slots configured, finds an idle agent pod of the opposing type
        and deletes it to free one slot.

        Returns:
            True if a pod was successfully evicted.
        """
        # Determine which reservation applies
        if purpose == "session" and self._reserved_session_slots > 0:
            other_purpose = "job"
        elif purpose == "job" and self._reserved_job_slots > 0:
            other_purpose = "session"
        else:
            return False  # No reservation configured for this purpose

        if not self._db:
            return False

        try:
            # Find idle agents (no job, no thread) whose hostname matches a
            # Running pod of the OTHER purpose.
            async with self._db.acquire() as conn:
                idle_rows = await conn.fetch(
                    """
                    SELECT id, hostname FROM agents
                    WHERE status = 'ready'
                      AND current_job_id IS NULL
                      AND thread_id IS NULL
                      AND hostname IS NOT NULL
                    ORDER BY last_heartbeat ASC
                    """,
                )
            if not idle_rows:
                return False

            idle_hostnames = {r["hostname"]: r for r in idle_rows}

            # List pods of the other purpose
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=(f"{self._label_selector},srw/purpose={other_purpose}"),
            )
            for pod in pods.items:
                if pod.status.phase in ("Succeeded", "Failed"):
                    continue
                if pod.metadata.name in idle_hostnames:
                    agent_id = str(idle_hostnames[pod.metadata.name]["id"])
                    if await self.delete_agent_pod(pod.metadata.name):
                        # Mark evicted agent offline
                        try:
                            async with self._db.acquire() as conn:
                                await conn.execute(
                                    "UPDATE agents SET status = 'offline' "
                                    "WHERE id = $1",
                                    agent_id,
                                )
                        except Exception:
                            pass
                        logger.info(
                            "Evicted idle %s agent %s to free slot for %s",
                            other_purpose,
                            pod.metadata.name,
                            purpose,
                        )
                        return True
        except Exception:
            logger.exception("Failed to evict agent for reservation")

        return False

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
        # Standard chart labels — required for the chart's database
        # NetworkPolicies to allow ingress from these dynamic pods. The
        # chart's Helm-rendered "agent" component selectors expect:
        #   app.kubernetes.io/name      = <chart name>      (e.g. srw-dev)
        #   app.kubernetes.io/instance  = <release name>    (e.g. ...-deployment)
        #   app.kubernetes.io/component = agent
        if self._chart_label_name:
            labels["app.kubernetes.io/name"] = self._chart_label_name
        if self._chart_label_instance:
            labels["app.kubernetes.io/instance"] = self._chart_label_instance
        if self._chart_label_name or self._chart_label_instance:
            labels["app.kubernetes.io/component"] = "agent"
        if thread_id:
            labels["srw/thread-id"] = thread_id[:12]
            # Full-value label consumed by the per-session Service selector
            # built by the session router (docs/features/direct_session_websockets.md).
            # The legacy `srw/thread-id` label above is kept for backwards-compat
            # with the lifecycle reconciler, which still selects on the truncated
            # form. K8s label values cap at 63 chars; a UUID fits.
            labels["srw.io/thread-id"] = thread_id
        # Build SHA label — lets the lifecycle reconciler enumerate stale
        # pods by selector without joining to the agents table. Set when
        # the image tag follows the `:sha-XXXXXXX` convention; absent for
        # `:latest` or semver-style tags (local dev — drift detection
        # is a no-op there anyway).
        if ":sha-" in self._agent_image:
            labels["srw/build-sha"] = self._agent_image.rsplit(":sha-", 1)[-1]

        containers: list[dict] = [
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
                    # Downward-API injection: the K8s-assigned pod UID. The
                    # agent reports this back at /api/agents/register so the
                    # session router can stamp ownerReferences on per-session
                    # Service/Ingress resources for K8s GC.
                    # (docs/features/direct_session_websockets.md)
                    {
                        "name": "POD_UID",
                        "valueFrom": {
                            "fieldRef": {"fieldPath": "metadata.uid"},
                        },
                    },
                    # Session handshake authentication. The orchestrator mints
                    # an HS256 JWT carrying `tid=<thread_id>`; the pod's
                    # validator (src/api/_session_auth.py) verifies the
                    # signature with SESSION_JWT_SECRET and checks the `tid`
                    # claim against SESSION_BOUND_THREAD_ID. Both must be set
                    # for the direct-WS flow; missing them closes every
                    # handshake with code 4500. Worker pods get empty values
                    # (the WS endpoints aren't reachable for them anyway).
                    {
                        "name": "SESSION_BOUND_THREAD_ID",
                        "value": thread_id or "",
                    },
                    {
                        "name": "SESSION_JWT_SECRET",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": os.environ.get(
                                    "SESSION_JWT_SECRET_NAME",
                                    "srw-session-jwt",
                                ),
                                "key": os.environ.get(
                                    "SESSION_JWT_SECRET_KEY",
                                    "jwt-secret",
                                ),
                                "optional": True,
                            },
                        },
                    },
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
        ]

        volumes: list[dict] = [
            # Scratch workspace (agent connects to real workspace
            # via SSH — this is just local temp storage)
            {"name": "workspace", "emptyDir": {"sizeLimit": "10Gi"}},
            {
                "name": "vm-ssh-key",
                "secret": {
                    "secretName": self._ssh_secret_name,
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
        ]

        if self._tailscale_enabled and self._headscale_url:
            tailscale_args = (
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
                f'--login-server="{self._headscale_url}" '
                '--hostname="${POD_NAME}" '
                "--accept-dns=false "
                "--timeout=60s 2>&1; then "
                'echo "Tailscale authenticated"; break; fi; '
                'echo "Auth retry in 15s..."; sleep 15; done; '
                # Supervision loop: re-up if the node loses its registration
                # (headscale #2006 lastSeen false-GC, control-plane restart,
                # network/DERP blip). `tailscale up` is idempotent when the
                # backend is already Running. Permanent loss is handled by the
                # liveness probe + the tunnel_dark reaper, not here. The grep
                # tolerates the space MarshalIndent puts after the JSON colon.
                'while kill -0 "$TSPID" 2>/dev/null; do '
                "if ! tailscale status --json 2>/dev/null | "
                "grep -qE '\"BackendState\":[[:space:]]*\"Running\"'; then "
                "tailscale up "
                '--auth-key="${TS_AUTHKEY}" '
                f'--login-server="{self._headscale_url}" '
                '--hostname="${POD_NAME}" '
                "--accept-dns=false --timeout=60s || true; "
                "fi; sleep 30; done"
            )
            # ceil(dark_timeout / 30s); >=1. The kubelet measures the sustained
            # dark window via failureThreshold, so the reaper needs no timing.
            dark_failures = max(1, (self._tailscale_dark_timeout + 29) // 30)
            containers.append(
                {
                    "name": "tailscale",
                    "image": "ghcr.io/tailscale/tailscale:v1.82.5",
                    "securityContext": {
                        "capabilities": {"add": ["NET_ADMIN", "NET_RAW"]},
                    },
                    "command": ["/bin/sh", "-c"],
                    "args": [tailscale_args],
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
                            "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                        },
                    ],
                    "volumeMounts": [
                        {
                            "name": "tailscale-state",
                            "mountPath": "/var/lib/tailscale",
                        }
                    ],
                    "livenessProbe": {
                        "exec": {
                            "command": [
                                "/bin/sh",
                                "-c",
                                "tailscale status --json 2>/dev/null | "
                                "grep -qE '\"BackendState\":[[:space:]]*\"Running\"'",
                            ]
                        },
                        "initialDelaySeconds": 120,
                        "periodSeconds": 30,
                        "failureThreshold": dark_failures,
                    },
                    "resources": {
                        "requests": {"memory": "64Mi", "cpu": "50m"},
                        "limits": {"memory": "128Mi", "cpu": "200m"},
                    },
                }
            )
            volumes.append(
                {"name": "tailscale-state", "emptyDir": {"sizeLimit": "16Mi"}}
            )

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
                "containers": containers,
                "volumes": volumes,
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
