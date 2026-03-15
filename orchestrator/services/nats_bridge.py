"""NATS Bridge — Optional VM Lifecycle Management.

Connects the orchestrator to the NATS messaging system for cross-cluster
VM lifecycle communication. Publishes create/delete commands and subscribes
to status updates from the VM Controller and Management Daemon.

NATS is fully optional. When NATS_URL is not configured or nats-py is not
installed, all operations gracefully return False/None and the system works
identically without it (same pattern as MongoDB in database/mongodb.py).

Subjects published:
  vm.lifecycle.create          Request VM creation
  vm.lifecycle.delete          Request VM teardown
  vm.lifecycle.get             Request/reply for live VM status
  agent.vm.{job_id}.control    Freeze/resume/terminate agent process

Subjects subscribed:
  vm.lifecycle.status          VM Controller status updates
  agent.vm.*.register          Management Daemon registration
  agent.vm.*.heartbeat         Management Daemon heartbeats
  agent.vm.*.status            Management Daemon agent exit status
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

try:
    import nats
    from nats.aio.client import Client as NatsClient
    NATS_AVAILABLE = True
except ImportError:
    nats = None
    NatsClient = None
    NATS_AVAILABLE = False

logger = logging.getLogger(__name__)


class NatsBridge:
    """Optional NATS bridge for VM lifecycle management.

    Follows the MongoDB graceful degradation pattern: if NATS_URL is not
    configured or nats-py is not installed, all operations are no-ops.
    """

    def __init__(self, url: Optional[str] = None):
        """Initialize NATS bridge.

        Args:
            url: NATS server URL. Falls back to NATS_URL env var.
        """
        if not NATS_AVAILABLE:
            logger.warning(
                "nats-py not installed. NATS features disabled. "
                "Install with: pip install nats-py"
            )

        self._url = url or os.getenv("NATS_URL")
        self._nc: Optional[Any] = None
        self._db: Optional[Any] = None
        self._on_vm_ready: Optional[Callable] = None
        self._available: bool = False

        if not self._url:
            logger.info("NATS_URL not configured. VM lifecycle features disabled.")

    @property
    def is_available(self) -> bool:
        """Check if NATS is connected and available."""
        return self._available

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def connect(
        self,
        db: Any,
        on_vm_ready: Optional[Callable] = None,
    ) -> None:
        """Connect to NATS and subscribe to VM lifecycle subjects.

        Args:
            db: PostgresDB instance for job context updates.
            on_vm_ready: Optional callback invoked when a daemon registers
                         (e.g. to trigger dispatch).

        On failure, logs a warning and sets _available = False (no exception).
        """
        if self._nc is not None:
            return

        if not NATS_AVAILABLE:
            self._available = False
            return

        if not self._url:
            self._available = False
            return

        self._db = db
        self._on_vm_ready = on_vm_ready

        try:
            self._nc = await nats.connect(
                self._url,
                error_cb=self._on_error,
                disconnected_cb=self._on_disconnect,
                reconnected_cb=self._on_reconnect,
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
            )

            # Subscribe to VM lifecycle subjects (4 specific subscriptions)
            await self._nc.subscribe("vm.lifecycle.status", cb=self._on_vm_lifecycle_status)
            await self._nc.subscribe("agent.vm.*.register", cb=self._on_daemon_register)
            await self._nc.subscribe("agent.vm.*.heartbeat", cb=self._on_daemon_heartbeat)
            await self._nc.subscribe("agent.vm.*.status", cb=self._on_daemon_status)

            self._available = True
            logger.info("NATS bridge connected: %s", self._url)
        except Exception as e:
            logger.warning("NATS bridge connection failed: %s", e)
            self._available = False
            self._nc = None

    async def disconnect(self) -> None:
        """Drain NATS connection and clean up."""
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception as e:
                logger.warning("Error draining NATS connection: %s", e)
            self._nc = None
            self._available = False
            logger.info("NATS bridge disconnected")

    # =========================================================================
    # Publishers
    # =========================================================================

    async def request_vm_create(
        self,
        job_id: str,
        agent_config: str = "defaults",
        vm_image: Optional[str] = None,
        cpu_cores: int = 2,
        memory: str = "4Gi",
        description: str = "",
    ) -> bool:
        """Publish a VM creation request.

        Args:
            job_id: Job UUID to create a VM for.
            agent_config: Agent config name to run in the VM.
            vm_image: Container disk image (None = controller default).
            cpu_cores: Number of CPU cores.
            memory: Memory allocation (e.g. "4Gi").
            description: Job description passed to the agent.

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False

        payload = {
            "job_id": job_id,
            "agent_config": agent_config,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "nats_url": self._url,
            "description": description,
        }
        if vm_image:
            payload["vm_image"] = vm_image

        try:
            await self._nc.publish(
                "vm.lifecycle.create",
                json.dumps(payload).encode(),
            )
            logger.info("Published vm.lifecycle.create for job %s", job_id)

            # Update job context to reflect provisioning state
            await self._set_vm_context(job_id, {"status": "provisioning"})
            return True
        except Exception as e:
            logger.error("Failed to publish vm.lifecycle.create for job %s: %s", job_id, e)
            return False

    async def request_vm_delete(self, job_id: str) -> bool:
        """Publish a VM deletion request.

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False

        payload = {"job_id": job_id}
        try:
            await self._nc.publish(
                "vm.lifecycle.delete",
                json.dumps(payload).encode(),
            )
            logger.info("Published vm.lifecycle.delete for job %s", job_id)
            await self._set_vm_context(job_id, {"status": "deleting"})
            return True
        except Exception as e:
            logger.error("Failed to publish vm.lifecycle.delete for job %s: %s", job_id, e)
            return False

    async def query_vm_status(self, job_id: str, timeout: float = 5.0) -> Optional[dict]:
        """Query live VM status via NATS request/reply.

        Args:
            job_id: Job UUID whose VM to query.
            timeout: Seconds to wait for a reply.

        Returns:
            Status dict from the VM controller, or None if unavailable/timeout.
        """
        if not self._available:
            return None

        payload = {"job_id": job_id}
        try:
            response = await self._nc.request(
                "vm.lifecycle.get",
                json.dumps(payload).encode(),
                timeout=timeout,
            )
            return json.loads(response.data.decode())
        except Exception as e:
            logger.debug("VM status query failed for job %s: %s", job_id, e)
            return None

    async def send_control(self, job_id: str, action: str) -> bool:
        """Send a control command to the management daemon in a VM.

        Args:
            job_id: Job UUID.
            action: One of "freeze", "resume", "terminate".

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False

        payload = {"action": action}
        subject = f"agent.vm.{job_id}.control"
        try:
            await self._nc.publish(subject, json.dumps(payload).encode())
            logger.info("Sent control '%s' to %s", action, subject)
            return True
        except Exception as e:
            logger.error("Failed to send control '%s' to %s: %s", action, subject, e)
            return False

    # =========================================================================
    # Subscription handlers
    # =========================================================================

    async def _on_vm_lifecycle_status(self, msg) -> None:
        """Handle vm.lifecycle.status — VM controller status updates.

        Payload: {job_id, status, vm_name, namespace, error?}
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            updates = {
                "status": data.get("status"),
                "vm_name": data.get("vm_name"),
                "namespace": data.get("namespace"),
            }
            if data.get("error"):
                updates["error"] = data["error"]
            # VM controller reports the pod IP once the VMI is running —
            # this is the address agents use for SSH (not the VM's internal
            # 10.0.2.x masquerade address).
            if data.get("pod_ip"):
                updates["pod_ip"] = data["pod_ip"]

            logger.info(
                "VM lifecycle status for job %s: %s (pod_ip=%s)",
                job_id, data.get("status"), data.get("pod_ip"),
            )
            await self._set_vm_context(job_id, updates)
        except Exception:
            logger.exception("Error handling vm.lifecycle.status")

    async def _on_daemon_register(self, msg) -> None:
        """Handle agent.vm.*.register — daemon announces VM is ready.

        Payload: {job_id, hostname, ip, pid}

        For SSH access, we prefer the pod_ip (reported earlier by the VM
        controller) over the daemon's self-reported IP.  The daemon runs
        inside the VM and sees its masquerade address (10.0.2.x), which
        is not reachable from cluster pods.  The pod IP (from the VMI
        status) is the address that agents can actually SSH to.
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            # Prefer the pod IP (set by VM controller) over the daemon's
            # self-reported IP (which is the VM-internal masquerade address).
            existing_vm_ctx = {}
            if self._db:
                try:
                    job = await self._db.get_job(job_id)
                    if job:
                        ctx = job.get("context") or {}
                        if isinstance(ctx, str):
                            ctx = json.loads(ctx)
                        existing_vm_ctx = ctx.get("vm", {})
                except Exception:
                    pass

            pod_ip = existing_vm_ctx.get("pod_ip")
            daemon_ip = data.get("ip") or data.get("hostname")
            ssh_host = pod_ip or daemon_ip

            logger.info(
                "Daemon registered for job %s (ssh_host=%s, pod_ip=%s, daemon_ip=%s)",
                job_id, ssh_host, pod_ip, daemon_ip,
            )
            await self._set_vm_context(job_id, {
                "status": "ready",
                "ssh_host": ssh_host,
                "hostname": data.get("hostname"),
                "daemon_pid": data.get("pid"),
            })

            # Trigger callback (e.g. dispatch)
            if self._on_vm_ready:
                try:
                    self._on_vm_ready()
                except Exception:
                    logger.exception("on_vm_ready callback failed")
        except Exception:
            logger.exception("Error handling daemon register")

    async def _on_daemon_heartbeat(self, msg) -> None:
        """Handle agent.vm.*.heartbeat — periodic health updates.

        Payload: {job_id, agent_pid, agent_running, cpu_percent, memory_percent, disk_percent}
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            logger.debug("Heartbeat for job %s: agent_running=%s", job_id, data.get("agent_running"))
            await self._set_vm_context(job_id, {
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            logger.exception("Error handling daemon heartbeat")

    async def _on_daemon_status(self, msg) -> None:
        """Handle agent.vm.*.status — agent process exited.

        Payload: {job_id, status: "completed"|"failed", exit_code}
        Informational only — the agent's HTTP /job/complete is authoritative.
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            logger.info(
                "Agent in VM exited for job %s: status=%s, exit_code=%s",
                job_id,
                data.get("status"),
                data.get("exit_code"),
            )
        except Exception:
            logger.exception("Error handling daemon status")

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _set_vm_context(self, job_id: str, updates: dict) -> None:
        """Atomically merge updates into the job's context.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_vm_context(job_id, updates)
        except Exception:
            logger.exception("Failed to update VM context for job %s", job_id)

    # =========================================================================
    # NATS connection callbacks
    # =========================================================================

    async def _on_error(self, e) -> None:
        logger.error("NATS error: %s", e)

    async def _on_disconnect(self) -> None:
        logger.warning("NATS disconnected")

    async def _on_reconnect(self) -> None:
        logger.info("NATS reconnected")


# Module-level singleton
nats_bridge = NatsBridge()
