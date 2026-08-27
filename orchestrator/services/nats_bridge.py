"""NATS Bridge — Optional VM Lifecycle Management.

Connects the orchestrator to the NATS messaging system for cross-cluster
VM lifecycle communication. Publishes create/delete commands and subscribes
to status updates from the VM Controller and Management Daemon.

NATS is fully optional. When NATS_URL is not configured or nats-py is not
installed, all operations gracefully return False/None and the system works
identically without it (the same optional-dependency pattern used elsewhere).

Subjects published:
  vm.lifecycle.create.{oid}         Request VM creation
  vm.lifecycle.delete.{oid}         Request VM teardown
  vm.lifecycle.get.{oid}            Request/reply for live VM status

Subjects subscribed:
  vm.lifecycle.status.{oid}         VM Controller status updates
  agent.vm.{oid}.*.register         Management Daemon registration
  agent.vm.{oid}.*.heartbeat        Management Daemon heartbeats
  sudo.request.{oid}.>              Sudo approval requests from sudo-gated daemons
  session.events.{oid}.>            Agent pod notification events (session.events.{oid}.{tid})

{oid} is the value of the ORCHESTRATOR_ID env var (Helm: .Values.orchestratorId,
defaults to chart fullname). Required to safely share a NATS hub with other
SRW orchestrators — empty value refuses to publish/subscribe.
"""

import asyncio
from collections.abc import Mapping
import json
import logging
import os
import time
from typing import Any, Callable, Optional
from uuid import UUID

try:
    import nats
    from nats.aio.client import Client as NatsClient

    NATS_AVAILABLE = True
except ImportError:
    nats = None
    NatsClient = None
    NATS_AVAILABLE = False

from .notification_feed import notification_feed
from .ssh_helpers import orchestrator_can_reach
from .vm_guest_events import record_heartbeat, record_register, resolve_vm_entity
from .workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY
from .vm_lifecycle_auth import (
    AUTH_FIELD,
    configured_secret,
    sign_payload,
    unsigned_payload,
    verify_payload,
)

logger = logging.getLogger(__name__)


def _entity_label(is_thread: Optional[bool]) -> str:
    """Log label for a VM's owning entity. ``unknown`` is a real outcome — an id
    that resolves to neither table — and must read differently from ``job``."""
    if is_thread is None:
        return "unknown-entity"
    return "thread" if is_thread else "job"


def _provision_generation(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if str(parsed) == value else None


class NatsBridge:
    """Optional NATS bridge for VM lifecycle management.

    Follows the same optional-dependency graceful degradation pattern: if NATS_URL is not
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
        # cross-talk on a shared NATS hub (see knowledge-base/knowledge/issues/nats_subject_acl_hardening.md).
        self._orchestrator_id = (os.getenv("ORCHESTRATOR_ID") or "").strip()
        self._nc: Optional[Any] = None
        self._db: Optional[Any] = None
        self._on_vm_ready: Optional[Callable] = None
        self._available: bool = False
        self._lifecycle_hmac_secret = configured_secret()

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

    @property
    def lifecycle_identity_authenticated(self) -> bool:
        """Whether controller lifecycle evidence can be authenticated."""

        return self._lifecycle_hmac_secret is not None

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
                # knowledge-base/knowledge/issues/nats_subject_acl_hardening.md.
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
        agent_config: str = "worker_base",
        vm_image: Optional[str] = None,
        cpu_cores: int = 8,
        memory: str = "16Gi",
        description: str = "",
        entity_type: str = "job",
        set_provisioning: bool = True,
        provision_generation: str | None = None,
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
            set_provisioning: False for a golden-poll re-issue — the context
                status must stay ``waiting_golden`` between polls so the
                dispatcher keeps polling instead of treating the job as a
                booting VM (the controller's status publish overwrites it
                with the truth on every poll response).

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False
        generation = _provision_generation(provision_generation)
        if self._lifecycle_hmac_secret is not None and generation is None:
            logger.error(
                "Refusing authenticated VM create for %s %s without a current "
                "provision generation",
                entity_type,
                job_id,
            )
            return False

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
            "entity_type": entity_type,
            "agent_config": agent_config,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "nats_url": self._url,
            "description": description,
            "orchestrator_id": self._orchestrator_id,
        }
        if generation is not None:
            payload["provision_generation"] = generation
        if vm_image:
            payload["vm_image"] = vm_image
        payload = sign_payload(
            payload,
            direction="request",
            operation="create",
            secret=self._lifecycle_hmac_secret,
        )
        try:
            await self._nc.publish(
                subject,
                json.dumps(payload).encode(),
            )
            logger.info("Published %s for %s %s", subject, entity_type, job_id)

            # Update context to reflect provisioning state
            if set_provisioning:
                if entity_type == "thread":
                    if generation is not None:
                        await self._set_thread_vm_context_if_generation(
                            job_id, generation, {"status": "provisioning"}
                        )
                    else:
                        await self._set_thread_vm_context(
                            job_id, {"status": "provisioning"}
                        )
                else:
                    if generation is not None:
                        await self._set_vm_context_if_generation(
                            job_id, generation, {"status": "provisioning"}
                        )
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

    async def request_vm_delete(
        self,
        job_id: str,
        purge_disk: bool = True,
        provision_generation: str | None = None,
        entity_type: str = "job",
        expected_vm_uid: str | None = None,
        expected_rootdisk_pvc_uid: str | None = None,
    ) -> bool:
        """Publish a VM deletion request.

        Args:
            job_id: Job or thread UUID (VM names are ``agent-vm-<id>`` for both).
            purge_disk: False keeps the persistent rootdisk and the Headscale
                node for a recreate. The controller defaults the field to True,
                so an un-upgraded one is unaffected by its presence.

        Returns:
            True if published, False if NATS unavailable.
        """
        if not self._available:
            return False
        generation = _provision_generation(provision_generation)
        if self._lifecycle_hmac_secret is not None and generation is None:
            logger.error(
                "Refusing authenticated VM delete for %s without a current "
                "provision generation",
                job_id,
            )
            return False

        subject = self._subj("vm.lifecycle.delete")
        if subject is None:
            logger.error(
                "Refusing vm.lifecycle.delete publish for job %s — ORCHESTRATOR_ID unset",
                job_id,
            )
            return False

        payload = {
            "job_id": job_id,
            "orchestrator_id": self._orchestrator_id,
            "purge_disk": purge_disk,
        }
        if generation is not None:
            payload["provision_generation"] = generation
        if expected_vm_uid is not None:
            payload["expected_vm_uid"] = expected_vm_uid
        if expected_rootdisk_pvc_uid is not None:
            payload["expected_rootdisk_pvc_uid"] = expected_rootdisk_pvc_uid
        payload = sign_payload(
            payload,
            direction="request",
            operation="delete",
            secret=self._lifecycle_hmac_secret,
        )
        try:
            await self._nc.publish(
                subject,
                json.dumps(payload).encode(),
            )
            logger.info("Published %s for job %s", subject, job_id)
            # Stamp when the teardown started. Delete requests and their answers
            # are fire-and-forget core NATS, so a dropped message would otherwise
            # strand the job in 'deleting' forever; the dispatcher uses this to
            # tell an in-flight teardown from a stuck one
            # (dispatch_guards.vm_provisioning_decision).
            deleting_ctx = {"status": "deleting", "deleting_started_at": time.time()}
            if generation is not None:
                if entity_type == "thread":
                    await self._set_thread_vm_context_if_generation(
                        job_id, generation, deleting_ctx
                    )
                else:
                    await self._set_vm_context_if_generation(
                        job_id, generation, deleting_ctx
                    )
            elif entity_type == "thread":
                await self._set_thread_vm_context(job_id, deleting_ctx)
            else:
                await self._set_vm_context(job_id, deleting_ctx)
            return True
        except Exception as e:
            logger.error("Failed to publish %s for job %s: %s", subject, job_id, e)
            return False

    async def query_vm_status(
        self,
        job_id: str,
        timeout: float = 5.0,
        provision_generation: str | None = None,
        *,
        exact_absence: bool = False,
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
        generation = _provision_generation(provision_generation)
        if self._lifecycle_hmac_secret is not None and generation is None:
            logger.error(
                "Refusing authenticated VM status query for %s without a current "
                "provision generation",
                job_id,
            )
            return None

        payload = {"job_id": job_id, "orchestrator_id": self._orchestrator_id}
        if generation is not None:
            payload["provision_generation"] = generation
        if exact_absence:
            payload["exact_absence"] = True
        payload = sign_payload(
            payload,
            direction="request",
            operation="status",
            secret=self._lifecycle_hmac_secret,
        )
        request_auth = payload.get(AUTH_FIELD)
        request_id = (
            request_auth.get("request_id")
            if isinstance(request_auth, Mapping)
            else None
        )
        try:
            response = await self._nc.request(
                subject,
                json.dumps(payload).encode(),
                timeout=timeout,
            )
            data = json.loads(response.data.decode())
            # Defensive guard: if a JetStream stream's subject filter ever
            # covers this request/reply subject, the request inbox receives the
            # stream's publish-ack ({"stream": ..., "seq": ...}) — which races
            # ahead of (and beats) the VM controller's real reply. Never surface
            # that ack as VM status; treat it as no response. See
            # knowledge-base/knowledge/issues/vm_live_status_query_shadowed_by_jetstream_stream.md
            if (
                isinstance(data, dict)
                and "stream" in data
                and "seq" in data
                and "job_id" not in data
            ):
                logger.warning(
                    "vm.lifecycle.get for job %s received a JetStream ack (%s) "
                    "instead of a controller reply — a stream is shadowing the "
                    "request subject; treating as no response",
                    job_id,
                    data,
                )
                return None
            if not isinstance(data, dict) or not verify_payload(
                data,
                direction="response",
                operation="status",
                secret=self._lifecycle_hmac_secret,
                expected_correlation_id=request_id,
            ):
                logger.warning(
                    "Dropping unauthenticated VM status response for %s", job_id
                )
                return None
            result = unsigned_payload(data)
            if self._lifecycle_hmac_secret is not None:
                if (
                    result.get("job_id") != job_id
                    or _provision_generation(result.get("provision_generation"))
                    != generation
                ):
                    logger.warning(
                        "Dropping VM status response for %s with mismatched "
                        "identity generation",
                        job_id,
                    )
                    return None
                result["_identity_authenticated"] = True
            return result
        except Exception as e:
            logger.debug("VM status query failed for job %s: %s", job_id, e)
            return None

    async def request_vm_list(
        self,
        timeout: float = 5.0,
        *,
        include_teardown_identity: bool = False,
    ) -> Optional[list]:
        """List the controller's managed VMs via NATS request/reply.

        Inventory source for the lifecycle VM orphan sweep. Returns the list
        of ``{vm_name, entity_id, created_at, phase}`` dicts, or None when
        unavailable — which includes an old controller that doesn't subscribe
        to ``vm.lifecycle.list`` yet (the request simply times out). Callers
        must treat None as "unknown", never as "no VMs".
        """
        if not self._available:
            return None

        subject = self._subj("vm.lifecycle.list")
        if subject is None:
            logger.error("Refusing vm.lifecycle.list — ORCHESTRATOR_ID unset")
            return None

        payload = sign_payload(
            {
                "orchestrator_id": self._orchestrator_id,
                **(
                    {"include_teardown_identity": True}
                    if include_teardown_identity
                    else {}
                ),
            },
            direction="request",
            operation="list",
            secret=self._lifecycle_hmac_secret,
        )
        request_auth = payload.get(AUTH_FIELD)
        request_id = (
            request_auth.get("request_id")
            if isinstance(request_auth, Mapping)
            else None
        )
        try:
            response = await self._nc.request(
                subject,
                json.dumps(payload).encode(),
                timeout=timeout,
            )
            data = json.loads(response.data.decode())
            # Same JetStream-ack guard as query_vm_status: a stream shadowing
            # the request subject answers with its publish-ack before the
            # controller's real reply. See
            # knowledge-base/knowledge/issues/vm_live_status_query_shadowed_by_jetstream_stream.md
            if isinstance(data, dict) and "stream" in data and "seq" in data:
                logger.warning(
                    "vm.lifecycle.list received a JetStream ack (%s) instead of "
                    "a controller reply — treating as no response",
                    data,
                )
                return None
            if not isinstance(data, dict) or not verify_payload(
                data,
                direction="response",
                operation="list",
                secret=self._lifecycle_hmac_secret,
                expected_correlation_id=request_id,
            ):
                logger.warning("Dropping unauthenticated VM list response")
                return None
            data = unsigned_payload(data)
            vms = data.get("vms") if isinstance(data, dict) else None
            return vms if isinstance(vms, list) else None
        except Exception as e:
            logger.debug("VM list request failed: %s", e)
            return None

    async def _on_vm_lifecycle_status(self, msg) -> None:
        """Handle vm.lifecycle.status — VM controller status updates.

        Payload: {job_id, status, vm_name, vm_uid, rootdisk_pvc_uid,
                  namespace, error?}
        """
        if os.getenv("VM_MODE", "off").strip().lower() != "external":
            return
        try:
            data = json.loads(msg.data.decode())
            if not isinstance(data, Mapping):
                return
            auth = data.get(AUTH_FIELD)
            operation = auth.get("operation") if isinstance(auth, Mapping) else "status"
            if operation not in {"create", "delete", "status"} or not verify_payload(
                data,
                direction="response",
                operation=operation,
                secret=self._lifecycle_hmac_secret,
            ):
                logger.warning("Dropping unauthenticated VM lifecycle status")
                return
            identity_authenticated = self._lifecycle_hmac_secret is not None
            data = unsigned_payload(data)
            job_id = data.get("job_id")
            if not job_id:
                return

            updates = {
                "status": data.get("status"),
                "vm_name": data.get("vm_name"),
                "namespace": data.get("namespace"),
            }
            # Slice 3 controllers include the immutable UID from the admitted
            # VirtualMachine object. Older controllers omit the key during a
            # rolling upgrade; leave the fresh context's vm_uid=None in that
            # case so metering records legacy-unknown rather than name-only
            # ownership. Never clear a known UID on delete/status payloads that
            # legitimately omit it.
            identity_updates: dict[str, Any] = {}
            response_generation = _provision_generation(
                data.get("provision_generation")
            )
            if identity_authenticated and response_generation and "vm_uid" in data:
                vm_uid = data.get("vm_uid")
                if (
                    isinstance(vm_uid, str)
                    and vm_uid
                    and vm_uid == vm_uid.strip()
                    and len(vm_uid) <= 256
                    and not any(character.isspace() for character in vm_uid)
                ):
                    identity_updates["vm_uid"] = vm_uid
                else:
                    logger.warning(
                        "Ignoring invalid VM UID in lifecycle status for %s", job_id
                    )
            # The controller reads the admitted root PVC and returns its
            # immutable UID.  Older controllers omit it; the provisioner's
            # fresh-context reset leaves the value null, so the metering join
            # fails closed instead of authenticating a reusable PVC name.
            if (
                identity_authenticated
                and response_generation
                and "rootdisk_pvc_uid" in data
            ):
                rootdisk_pvc_uid = data.get("rootdisk_pvc_uid")
                if (
                    isinstance(rootdisk_pvc_uid, str)
                    and rootdisk_pvc_uid
                    and rootdisk_pvc_uid == rootdisk_pvc_uid.strip()
                    and len(rootdisk_pvc_uid) <= 256
                    and not any(character.isspace() for character in rootdisk_pvc_uid)
                ):
                    identity_updates["rootdisk_pvc_uid"] = rootdisk_pvc_uid
                else:
                    logger.warning(
                        "Ignoring invalid rootdisk PVC UID in lifecycle status for %s",
                        job_id,
                    )
            if data.get("error"):
                updates["error"] = data["error"]
            if data.get("pod_ip"):
                updates["pod_ip"] = data["pod_ip"]
            if data.get("ssh_nodeport"):
                updates["ssh_nodeport"] = data["ssh_nodeport"]
            # Deferred-create telemetry — golden DV name + import progress, or
            # why Headscale is unreachable. Used by the dispatcher's poll
            # logging and park error message.
            for key in (
                "golden",
                "golden_phase",
                "golden_progress",
                "headscale_error",
            ):
                if data.get(key) is not None:
                    updates[key] = data[key]

            # What the controller ACTUALLY did with the rootdisk, which is not
            # necessarily what we asked for: a controller without
            # VM_PERSISTENT_ROOTDISK cascade-deletes the disk no matter what
            # purge_disk said. Recording its answer (rather than our intent)
            # keeps context.vm.rootdisk honest for the kept-disk GC sweep, and
            # the warning names the drift instead of letting it be silent.
            if data.get("status") == "deleted":
                if data.get("rootdisk") is not None:
                    updates["rootdisk"] = data["rootdisk"]
                else:
                    logger.warning(
                        "VM controller reported no rootdisk disposition for %s — it "
                        "predates persistent rootdisks; any keep intent was NOT "
                        "honoured and the disk is gone",
                        job_id,
                    )

            is_thread = await self._vm_entity_is_thread(job_id)
            logger.info(
                "VM lifecycle status for %s %s: %s",
                _entity_label(is_thread),
                job_id,
                data.get("status"),
            )
            if is_thread is None:
                logger.warning(
                    "Dropping vm.lifecycle.status for %s — not a known thread or "
                    "job; refusing to guess a table.",
                    job_id,
                )
                return
            if identity_updates:
                identity_updates.update(
                    {
                        "identity_authenticated": True,
                        "identity_provision_generation": response_generation,
                    }
                )
            if identity_authenticated:
                if response_generation is None:
                    logger.warning(
                        "Dropping authenticated lifecycle status for %s without a "
                        "canonical provision generation",
                        job_id,
                    )
                    return
                guarded_updates = {**updates, **identity_updates}
                if is_thread:
                    guarded_updates[CANVAS_WORKSPACE_GENERATION_KEY] = None
                    merged = await self._set_thread_vm_context_if_generation(
                        job_id,
                        response_generation,
                        guarded_updates,
                    )
                else:
                    merged = await self._set_vm_context_if_generation(
                        job_id,
                        response_generation,
                        guarded_updates,
                    )
                if merged:
                    return
                logger.warning(
                    "Ignoring lifecycle identity for %s because provision generation "
                    "is no longer current",
                    job_id,
                )
                return
            if is_thread:
                updates[CANVAS_WORKSPACE_GENERATION_KEY] = None
                await self._set_thread_vm_context(job_id, updates)
            else:
                await self._set_vm_context(job_id, updates)
        except Exception:
            logger.exception("Error handling vm.lifecycle.status")

    async def _on_daemon_register(self, msg) -> None:
        """Decode and delegate external-mode daemon registration evidence."""
        if os.getenv("VM_MODE", "off").strip().lower() != "external":
            return
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return

            if not self._is_leader():
                logger.debug("Ignoring daemon register for %s on follower", job_id)
                return
            identity = await resolve_vm_entity(self._db, job_id)
            if identity is None:
                logger.warning(
                    "Dropping daemon register for %s — not a known thread or job; "
                    "refusing to guess a table.",
                    job_id,
                )
                return
            result = await record_register(
                self._db,
                identity,
                data,
                authoritative=True,
                on_ready=lambda resolved, host, port: self._notify_vm_ready(
                    resolved.entity_id,
                    resolved.entity_type == "thread",
                    host,
                    port,
                ),
            )
            if not result.merged:
                return
            if result.status == "ready":
                logger.info(
                    "VM SSH ready for %s %s (%s:%d, evidence: %s)",
                    identity.entity_type,
                    job_id,
                    result.ssh_host,
                    result.ssh_port,
                    result.ready_source,
                )
            else:
                logger.info(
                    "Daemon registered for %s %s (ssh=%s:%d), status=%s",
                    identity.entity_type,
                    job_id,
                    result.ssh_host,
                    result.ssh_port,
                    result.status,
                )
        except Exception:
            logger.exception("Error handling daemon register")

    @staticmethod
    def _is_leader() -> bool:
        from services.leader_election import is_leader

        return is_leader.is_set()

    def _notify_vm_ready(
        self, entity_id: str, is_thread: bool, ssh_host: str, ssh_port: int
    ) -> None:
        """Post-promotion side effects: IDE config seed + dispatch poke."""
        asyncio.create_task(
            self._seed_vm_ide_config(entity_id, is_thread, ssh_host, ssh_port)
        )

        if self._on_vm_ready:
            try:
                self._on_vm_ready()
            except Exception:
                logger.exception("on_vm_ready callback failed")

    async def _seed_vm_ide_config(
        self, entity_id: str, is_thread: bool, ssh_host: str, ssh_port: int
    ) -> None:
        """Seed the owner-user's code-server config into a freshly-ready VM.

        Best-effort: resolves the job/thread owner, then writes their stored
        settings/keybindings/snippets over SSH. Never raises.
        """
        if not orchestrator_can_reach(ssh_host):
            # Tailnet target — SSH from the orchestrator would black-hole.
            # Skip visibly instead of timing out quietly; IDE config seeding
            # is not supported on the VM backend (see knowledge-base/knowledge/issues/
            # vm_ssh_readiness_probe_unroutable_from_orchestrator.md).
            logger.info(
                "Skipping IDE config seed for %s %s (%s:%d): orchestrator "
                "has no route to tailnet targets",
                "thread" if is_thread else "job",
                entity_id,
                ssh_host,
                ssh_port,
            )
            return
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
        """Decode and delegate external-mode daemon heartbeat evidence."""
        if os.getenv("VM_MODE", "off").strip().lower() != "external":
            return
        try:
            data = json.loads(msg.data.decode())
            job_id = data.get("job_id")
            if not job_id:
                return
            if not self._is_leader():
                logger.debug("Ignoring daemon heartbeat for %s on follower", job_id)
                return
            identity = await resolve_vm_entity(self._db, job_id)
            if identity is None:
                return
            await record_heartbeat(self._db, identity, data)

        except Exception:
            logger.exception("Error handling daemon heartbeat")

    async def _on_session_event(self, msg: Any) -> None:
        """Forward a session.events.{oid}.{tid} event to the SSE notification feed.

        Defense-in-depth: the payload's claimed thread_id must match the
        subject's thread_id AND must resolve to an existing thread in DB.
        Tracked for transport-layer hardening in
        knowledge-base/knowledge/issues/nats_subject_acl_hardening.md.
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
            "workspace_upgrade.needed": "session.workspace_upgrade",
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

    async def _set_vm_context_if_generation(
        self, job_id: str, generation: str, updates: dict
    ) -> bool:
        if not self._db:
            return False
        try:
            return bool(
                await self._db.merge_vm_context_if_provision_generation(
                    job_id, generation, updates
                )
            )
        except Exception:
            logger.exception(
                "Failed to update generation-guarded VM context for job %s", job_id
            )
            return False

    async def _set_thread_vm_context_if_generation(
        self, thread_id: str, generation: str, updates: dict
    ) -> bool:
        if not self._db:
            return False
        try:
            return bool(
                await self._db.merge_thread_vm_context_if_provision_generation(
                    thread_id, generation, updates
                )
            )
        except Exception:
            logger.exception(
                "Failed to update generation-guarded thread VM context for %s",
                thread_id,
            )
            return False

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _vm_entity_is_thread(self, entity_id: str) -> Optional[bool]:
        """Resolve whether a VM's entity id names a thread or a job.

        True = thread, False = job, None = neither/unresolvable.

        This MUST come from the database. It was previously a process-local set
        populated only on the replica that published ``vm.lifecycle.create``,
        which is broken under HA: with 2+ replicas the daemon's register is
        handled by the *leader*, which may not be the publisher. The leader then
        had no entry for the id, treated a thread UUID as a job, and wrote
        ``ssh_host`` into a ``jobs`` row that does not exist — stranding the
        thread at ``status='created'`` forever (a coin flip per VM session, and
        lost entirely on any orchestrator restart).
        knowledge-base/knowledge/issues/session_vm_backend_never_attaches.md (Defect 4)

        Callers must treat None as "do not write": guessing a table is exactly
        the failure mode this replaces.
        """
        if not self._db:
            return None
        try:
            if await self._db.get_thread(entity_id):
                return True
            if await self._db.get_job(entity_id):
                return False
        except Exception:
            logger.exception(
                "Could not resolve VM entity %s to a thread or job", entity_id
            )
            return None
        return None

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
