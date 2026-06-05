"""NATS Bridge — Optional VM Lifecycle Management.

Connects the orchestrator to the NATS messaging system for cross-cluster
VM lifecycle communication. Publishes create/delete commands and subscribes
to status updates from the VM Controller and Management Daemon.

NATS is fully optional. When NATS_URL is not configured or nats-py is not
installed, all operations gracefully return False/None and the system works
identically without it (same pattern as MongoDB in database/mongodb.py).

Subjects published:
  vm.lifecycle.create.{oid}         Request VM creation
  vm.lifecycle.delete.{oid}         Request VM teardown
  vm.lifecycle.get.{oid}            Request/reply for live VM status
  agent.vm.{oid}.{job_id}.control   Freeze/resume/terminate agent process

Subjects subscribed:
  vm.lifecycle.status.{oid}         VM Controller status updates
  agent.vm.{oid}.*.register         Management Daemon registration
  agent.vm.{oid}.*.heartbeat        Management Daemon heartbeats
  agent.vm.{oid}.*.status           Management Daemon agent exit status
  sudo.request.{oid}.>              Sudo approval requests from sudo-gated daemons
  session.events.{oid}.>            Agent pod notification events (session.events.{oid}.{tid})

{oid} is the value of the ORCHESTRATOR_ID env var (Helm: .Values.orchestratorId,
defaults to chart fullname). Required to safely share a NATS hub with other
SRW orchestrators — empty value refuses to publish/subscribe.
"""

import asyncio
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

from .notification_feed import notification_feed

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
        # Per-orchestrator scoping for vm.lifecycle.* subjects. When this is
        # blank we refuse to publish/subscribe to scoped subjects rather than
        # silently falling back to flat ones — flat subjects re-introduce
        # cross-talk on a shared NATS hub (see docs/issues/nats_subject_acl_hardening.md).
        self._orchestrator_id = (os.getenv("ORCHESTRATOR_ID") or "").strip()
        self._nc: Optional[Any] = None
        self._db: Optional[Any] = None
        self._on_vm_ready: Optional[Callable] = None
        self._available: bool = False
        # Track which VM IDs correspond to threads (vs jobs) for context routing
        self._thread_vm_ids: set[str] = set()

        if not self._url:
            logger.info("NATS_URL not configured. VM lifecycle features disabled.")
        elif not self._orchestrator_id:
            logger.warning(
                "NATS_URL set but ORCHESTRATOR_ID is empty — vm.lifecycle.* "
                "publishes and subscribes will be refused. Set ORCHESTRATOR_ID "
                "(via Helm orchestratorId value) to enable VM features."
            )

    @property
    def is_available(self) -> bool:
        """Check if NATS is connected and available."""
        return self._available

    def _subj(self, leaf: str) -> Optional[str]:
        """Append our orchestrator id to a vm.lifecycle subject.

        Returns None when ORCHESTRATOR_ID is unset; callers MUST skip the
        publish/subscribe rather than fall back to the flat subject — flat
        subjects would re-introduce cross-talk on a shared NATS hub.
        """
        if not self._orchestrator_id:
            return None
        return f"{leaf}.{self._orchestrator_id}"

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

            # Subscribe to VM lifecycle subjects. All broadcast subjects are
            # per-orchestrator scoped so multiple SRW installs can share a
            # NATS hub without cross-talk. Refuse to fall back to flat
            # subjects when ORCHESTRATOR_ID is unset — that would re-introduce
            # cross-talk on a shared hub.
            if not self._orchestrator_id:
                logger.error(
                    "Skipping all NATS subscribes — ORCHESTRATOR_ID unset. "
                    "Set ORCHESTRATOR_ID (Helm: orchestratorId value) to enable."
                )
            else:
                oid = self._orchestrator_id
                await self._nc.subscribe(
                    f"vm.lifecycle.status.{oid}", cb=self._on_vm_lifecycle_status
                )
                await self._nc.subscribe(
                    f"agent.vm.{oid}.*.register", cb=self._on_daemon_register
                )
                await self._nc.subscribe(
                    f"agent.vm.{oid}.*.heartbeat", cb=self._on_daemon_heartbeat
                )
                await self._nc.subscribe(
                    f"agent.vm.{oid}.*.status", cb=self._on_daemon_status
                )

                # Subscribe to sudo approval requests (from sudo-gated daemons)
                from .sudo_gate import sudo_gate

                sudo_gate.connect(db=db, nc=self._nc)
                await self._nc.subscribe(
                    f"sudo.request.{oid}.>", cb=sudo_gate.on_sudo_request
                )

                # Session event re-broadcast: agent pods publish notification
                # events to session.events.{oid}.{thread_id}; we forward to
                # the SSE feed after filtering. The defense-in-depth
                # payload-level thread_id check guards against a misbehaving
                # pod publishing for another thread. See
                # docs/issues/nats_subject_acl_hardening.md.
                await self._nc.subscribe(
                    f"session.events.{oid}.>", cb=self._on_session_event
                )

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
        entity_type: str = "job",
    ) -> bool:
        """Publish a VM creation request.

        Args:
            job_id: Job or thread UUID to create a VM for.
            agent_config: Agent config name to run in the VM.
            vm_image: Container disk image (None = controller default).
            cpu_cores: Number of CPU cores.
            memory: Memory allocation (e.g. "4Gi").
            description: Job description passed to the agent.
            entity_type: "job" (default) or "thread" — controls which DB
                table receives context updates when the daemon registers.

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False

        # Track thread VMs so NATS callbacks route context to threads table
        if entity_type == "thread":
            self._thread_vm_ids.add(job_id)

        subject = self._subj("vm.lifecycle.create")
        if subject is None:
            logger.error(
                "Refusing vm.lifecycle.create publish for %s %s — ORCHESTRATOR_ID unset",
                entity_type,
                job_id,
            )
            return False

        payload = {
            "job_id": job_id,
            "agent_config": agent_config,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "nats_url": self._url,
            "description": description,
            "orchestrator_id": self._orchestrator_id,
        }
        if vm_image:
            payload["vm_image"] = vm_image

        try:
            await self._nc.publish(
                subject,
                json.dumps(payload).encode(),
            )
            logger.info("Published %s for %s %s", subject, entity_type, job_id)

            # Update context to reflect provisioning state
            if entity_type == "thread":
                await self._set_thread_vm_context(job_id, {"status": "provisioning"})
            else:
                await self._set_vm_context(job_id, {"status": "provisioning"})
            return True
        except Exception as e:
            logger.error(
                "Failed to publish %s for %s %s: %s",
                subject,
                entity_type,
                job_id,
                e,
            )
            return False

    async def request_vm_delete(self, job_id: str) -> bool:
        """Publish a VM deletion request.

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False

        subject = self._subj("vm.lifecycle.delete")
        if subject is None:
            logger.error(
                "Refusing vm.lifecycle.delete publish for job %s — ORCHESTRATOR_ID unset",
                job_id,
            )
            return False

        payload = {"job_id": job_id, "orchestrator_id": self._orchestrator_id}
        try:
            await self._nc.publish(
                subject,
                json.dumps(payload).encode(),
            )
            logger.info("Published %s for job %s", subject, job_id)
            await self._set_vm_context(job_id, {"status": "deleting"})
            return True
        except Exception as e:
            logger.error("Failed to publish %s for job %s: %s", subject, job_id, e)
            return False

    async def query_vm_status(
        self, job_id: str, timeout: float = 5.0
    ) -> Optional[dict]:
        """Query live VM status via NATS request/reply.

        Args:
            job_id: Job UUID whose VM to query.
            timeout: Seconds to wait for a reply.

        Returns:
            Status dict from the VM controller, or None if unavailable/timeout.
        """
        if not self._available:
            return None

        subject = self._subj("vm.lifecycle.get")
        if subject is None:
            logger.error(
                "Refusing vm.lifecycle.get for job %s — ORCHESTRATOR_ID unset",
                job_id,
            )
            return None

        payload = {"job_id": job_id, "orchestrator_id": self._orchestrator_id}
        try:
            response = await self._nc.request(
                subject,
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

        if not self._orchestrator_id:
            logger.error(
                "Refusing agent.vm.*.control publish for job %s — ORCHESTRATOR_ID unset",
                job_id,
            )
            return False

        payload = {"action": action, "orchestrator_id": self._orchestrator_id}
        subject = f"agent.vm.{self._orchestrator_id}.{job_id}.control"
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
            if data.get("pod_ip"):
                updates["pod_ip"] = data["pod_ip"]
            if data.get("ssh_nodeport"):
                updates["ssh_nodeport"] = data["ssh_nodeport"]

            logger.info(
                "VM lifecycle status for %s %s: %s",
                "thread" if job_id in self._thread_vm_ids else "job",
                job_id,
                data.get("status"),
            )
            if job_id in self._thread_vm_ids:
                await self._set_thread_vm_context(job_id, updates)
            else:
                await self._set_vm_context(job_id, updates)
        except Exception:
            logger.exception("Error handling vm.lifecycle.status")

    async def _on_daemon_register(self, msg) -> None:
        """Handle agent.vm.*.register — daemon announces VM is ready.

        Payload: {job_id, hostname, ip, pid}

        The daemon reports its Tailscale IP (100.64.x.y), which is directly
        reachable from agent pods via the Headscale mesh VPN. No NodePort
        lookup or pod-ip query needed.
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            # Daemon reports its Tailscale IP — directly reachable from agent pods
            ssh_host = data.get("ip") or data.get("hostname")
            ssh_port = 22

            is_thread = job_id in self._thread_vm_ids
            logger.info(
                "Daemon registered for %s %s (ssh=%s:%d)",
                "thread" if is_thread else "job",
                job_id,
                ssh_host,
                ssh_port,
            )
            vm_updates = {
                "status": "ready",
                "ssh_host": ssh_host,
                "ssh_port": ssh_port,
                "hostname": data.get("hostname"),
                "daemon_pid": data.get("pid"),
                "recovering": False,  # Clear recovery guard
            }
            if is_thread:
                await self._set_thread_vm_context(job_id, vm_updates)
            else:
                await self._set_vm_context(job_id, vm_updates)

            # Seed the owner-user's code-server config into the freshly-ready VM
            # (theme/keybindings/snippets). Fire-and-forget so the register
            # handler isn't blocked on SSH; the helper is best-effort.
            if ssh_host:
                asyncio.create_task(
                    self._seed_vm_ide_config(job_id, is_thread, ssh_host, ssh_port)
                )

            # Trigger callback (e.g. dispatch)
            if self._on_vm_ready:
                try:
                    self._on_vm_ready()
                except Exception:
                    logger.exception("on_vm_ready callback failed")
        except Exception:
            logger.exception("Error handling daemon register")

    async def _seed_vm_ide_config(
        self, entity_id: str, is_thread: bool, ssh_host: str, ssh_port: int
    ) -> None:
        """Seed the owner-user's code-server config into a freshly-ready VM.

        Best-effort: resolves the job/thread owner, then writes their stored
        settings/keybindings/snippets over SSH. Never raises.
        """
        if not self._db:
            return
        try:
            from services.ide_settings import seed_ide_config_for_user

            row = (
                await self._db.get_thread(entity_id)
                if is_thread
                else await self._db.get_job(entity_id)
            )
            user_id = row.get("user_id") if isinstance(row, dict) else None
            await seed_ide_config_for_user(self._db, user_id, ssh_host, ssh_port)

            # Restore license/globalStorage + non-Open-VSX bytes into the VM
            # (Phase B). Best-effort; no-ops when S3 is unavailable.
            from services.snapshot_service import snapshot_service

            if user_id and snapshot_service.is_available:
                from services.ide_profile_store import IdeProfileStore
                from services.ide_settings import IdeSettingsStore, seed_ide_profile

                items = await IdeSettingsStore(self._db).get_extensions(str(user_id))
                profile = IdeProfileStore(
                    snapshot_service._s3, snapshot_service._bucket
                )
                await seed_ide_profile(
                    user_id=str(user_id),
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    profile_store=profile,
                    ext_items=items,
                )
        except Exception:
            logger.debug("ide seed (vm) failed for %s", entity_id, exc_info=True)

    async def _on_daemon_heartbeat(self, msg) -> None:
        """Handle agent.vm.*.heartbeat — periodic health updates.

        Payload: {job_id, agent_pid, agent_running, cpu_percent, memory_percent,
                  disk_percent, code_server_connections?}
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            logger.debug(
                "Heartbeat for %s %s: agent_running=%s",
                "thread" if job_id in self._thread_vm_ids else "job",
                job_id,
                data.get("agent_running"),
            )
            now = datetime.now(timezone.utc).isoformat()
            if job_id in self._thread_vm_ids:
                await self._set_thread_vm_context(job_id, {"last_heartbeat": now})
            else:
                await self._set_vm_context(job_id, {"last_heartbeat": now})

            # Track code-server activity for IDE session idle detection.
            # When the daemon reports active code-server connections, update
            # last_activity in the IDE session context so the TTL sweeper
            # knows the session is still in use.
            cs_connections = data.get("code_server_connections")
            if cs_connections is not None and self._db:
                try:
                    updates = {"code_server_connections": cs_connections}
                    if cs_connections > 0:
                        updates["last_activity"] = now
                        updates["status"] = "active"
                    else:
                        # No connections — mark as idle (if currently active)
                        updates["status"] = "idle"
                    await self._db.merge_ide_session_context(job_id, updates)
                except Exception:
                    pass  # Non-critical, don't break heartbeat processing

        except Exception:
            logger.exception("Error handling daemon heartbeat")

    async def _on_session_event(self, msg: Any) -> None:
        """Forward a session.events.{oid}.{tid} event to the SSE notification feed.

        Defense-in-depth: the payload's claimed thread_id must match the
        subject's thread_id AND must resolve to an existing thread in DB.
        Tracked for transport-layer hardening in
        docs/issues/nats_subject_acl_hardening.md.
        """
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning("session.events: invalid JSON: %s", e)
            return

        # Extract thread_id from subject ("session.events.{oid}.{tid}"). The
        # subscriber wildcard is `session.events.{oid}.>` so only this
        # orchestrator's events arrive here; the oid token in subject_parts[2]
        # is informational (already filtered by NATS).
        subject_parts = msg.subject.split(".", 3)
        if (
            len(subject_parts) != 4
            or subject_parts[0:2] != ["session", "events"]
            or not subject_parts[3]
        ):
            logger.warning("session.events: malformed subject: %s", msg.subject)
            return
        subject_tid = subject_parts[3]
        payload_tid = payload.get("thread_id")

        if payload_tid != subject_tid:
            logger.warning(
                "session.events: payload tid %r != subject tid %r — dropped",
                payload_tid,
                subject_tid,
            )
            return

        if self._db is None:
            return
        thread = await self._db.get_thread(subject_tid)
        if not thread:
            logger.debug("session.events: unknown thread %s — dropped", subject_tid)
            return

        user_id = str(thread.get("user_id") or "")
        if not user_id:
            return

        # Map the pod's event method to a notification feed event type.
        method = payload.get("method", "")
        event_type_map = {
            "permission.request": "session.permission_request",
            "vm_upgrade.needed": "session.vm_upgrade",
            "approve": "session.resolved",
            "deny": "session.resolved",
            "ready": "session.waiting",
        }
        event_type = event_type_map.get(method)
        if not event_type:
            return

        notification_feed.broadcast(
            user_id,
            event_type,
            {
                "thread_id": subject_tid,
                "method": method,
                "params": payload.get("params", {}),
            },
        )

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

    async def _set_thread_vm_context(self, thread_id: str, updates: dict) -> None:
        """Atomically merge updates into a thread's metadata.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_thread_vm_context(thread_id, updates)
        except Exception:
            logger.exception("Failed to update thread VM context for %s", thread_id)

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
