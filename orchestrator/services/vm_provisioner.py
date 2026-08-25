"""VM lifecycle management for explicit same-cluster and external modes."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Optional
from uuid import UUID, uuid4

import httpx

from services.workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY

from .container_provisioner import DEFAULT_NETWORK_TIER
from .nats_bridge import nats_bridge
from .vm_lifecycle_auth import (
    AUTH_FIELD,
    configured_secret,
    sign_payload,
    unsigned_payload,
    verify_payload,
)

logger = logging.getLogger(__name__)

_VALID_VM_MODES = frozenset({"off", "same-cluster", "external"})
_warned_unset_vm_mode = False
_warned_invalid_vm_mode = False


@dataclass(frozen=True, slots=True)
class VMTeardownIdentity:
    """Immutable VM incarnation captured before a destructive lifecycle call."""

    provision_generation: str
    vm_uid: str | None
    rootdisk_pvc_uid: str | None


@dataclass(frozen=True, slots=True)
class VMTeardownResult:
    """Bounded result distinguishing completion from identity supersession."""

    disposition: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class _VMTeardownProbe:
    """Authenticated observation used to reconcile an ambiguous delete."""

    disposition: str
    identity: VMTeardownIdentity | None = None
    rootdisk_identity_known: bool = False


def _provision_generation(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if str(parsed) == value else None


def _safe_vm_uid(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(character.isspace() for character in value)
    ):
        return None
    return value


def _safe_ssh_host_key_fingerprint(value: object) -> str | None:
    """Accept only a canonical OpenSSH SHA256 fingerprint."""

    if not isinstance(value, str) or not value.startswith("SHA256:"):
        return None
    encoded = value.removeprefix("SHA256:")
    if len(encoded) != 43:
        return None
    try:
        digest = base64.b64decode((encoded + "=").encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None
    return value if len(digest) == 32 else None


def _extract_vm_context(job: dict) -> dict:
    """Extract the vm sub-dict from a job's context."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    return ctx.get("vm", {})


def _http_lifecycle_query(
    payload: Mapping[str, Any], *, operation: str, secret: bytes | None
) -> dict[str, str]:
    """Encode an authenticated envelope into stable HTTP query fields."""

    signed = sign_payload(
        payload,
        direction="request",
        operation=operation,
        secret=secret,
    )
    auth = signed.get(AUTH_FIELD)
    if not isinstance(auth, Mapping):
        return {}
    return {
        "lifecycle_auth": str(auth["signature"]),
        "lifecycle_auth_issued_at": str(auth["issued_at"]),
        "lifecycle_auth_request_id": str(auth["request_id"]),
    }


def vm_persistent_rootdisk_enabled() -> bool:
    """Whether the VM controller keeps rootdisks across VM deletion.

    Mirrors the controller's own ``VM_PERSISTENT_ROOTDISK``; the orchestrator
    cannot observe the controller's config, so the two are set from the same
    Helm value and this is the orchestrator's copy. Read at call time rather
    than import so tests (and a config reload) see changes.

    **Enable the controller first.** Turning this on against a controller that
    still cascade-deletes disks would let VM session suspend tear a workspace
    down believing the files survive — they would not. The reverse order is
    harmless: the controller keeps disks nobody asks it to keep, and the
    delete-status handler records what actually happened either way.

    knowledge-base/knowledge/features/vm_persistent_rootdisk.md
    """
    return os.environ.get("VM_PERSISTENT_ROOTDISK", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class VMProvisioner:
    """Unified VM provisioner selected by the explicit ``VM_MODE`` contract."""

    def __init__(self):
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._vm_namespace: str = os.environ.get("VM_NAMESPACE", "agent-vms")
        self._default_vm_image: str = os.environ.get(
            "DEFAULT_VM_IMAGE",
            "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:latest",
        )
        # HTTP controller transport (same-cluster, no NATS).
        self._controller_url: str = os.environ.get("VM_CONTROLLER_URL", "").rstrip("/")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._http_timeout: float = float(os.environ.get("VM_CONTROLLER_TIMEOUT", "30"))
        self._lifecycle_hmac_secret = configured_secret()

    @property
    def is_available(self) -> bool:
        """Whether the backend required by the configured VM mode is available."""
        if self.mode == "same-cluster":
            return self._http_available
        if self.mode == "external":
            return self._nats_available
        return False

    @property
    def _http_available(self) -> bool:
        """True if a co-located VM controller HTTP endpoint is configured."""
        return self.mode == "same-cluster" and bool(self._controller_url)

    @property
    def _nats_available(self) -> bool:
        """True if the external-mode NATS bridge is connected."""
        return self.mode == "external" and nats_bridge.is_available

    @property
    def _docker_available(self) -> bool:
        """True if QEMU-in-Docker VMs are configured."""
        from .docker_provisioner import docker_provisioner

        return len(docker_provisioner.vm_hosts) > 0

    @property
    def mode(self) -> str:
        """Return ``off``, ``same-cluster``, or ``external`` from ``VM_MODE``."""
        global _warned_invalid_vm_mode, _warned_unset_vm_mode

        raw_mode = os.environ.get("VM_MODE")
        if raw_mode is None:
            if not _warned_unset_vm_mode:
                logger.warning("VM_MODE is unset; VM provisioning is disabled")
                _warned_unset_vm_mode = True
            return "off"
        mode = raw_mode.strip().lower()
        if mode not in _VALID_VM_MODES:
            if not _warned_invalid_vm_mode:
                logger.warning(
                    "Invalid VM_MODE=%r; VM provisioning is disabled", raw_mode
                )
                _warned_invalid_vm_mode = True
            return "off"
        return mode

    async def _current_provision_generation(
        self, entity_type: str, entity_id: str
    ) -> str | None:
        """Read the durable generation used to fence lifecycle commands."""

        if not self._db:
            return None
        try:
            if entity_type == "thread":
                row = await self._db.get_thread(entity_id)
                metadata = row.get("metadata") if isinstance(row, Mapping) else None
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                context = metadata.get("vm") if isinstance(metadata, Mapping) else None
            else:
                row = await self._db.get_job(entity_id)
                context = _extract_vm_context(row) if isinstance(row, dict) else None
            if not isinstance(context, Mapping):
                return None
            return _provision_generation(context.get("provision_generation"))
        except Exception:
            logger.exception(
                "Could not read current VM provision generation for %s %s",
                entity_type,
                entity_id,
            )
            return None

    async def _set_context_if_generation(
        self,
        entity_type: str,
        entity_id: str,
        generation: str,
        updates: dict,
        *,
        require_status_not_ready: bool = False,
    ) -> bool:
        if entity_type == "thread":
            return await self._set_thread_vm_context_if_generation(
                entity_id,
                generation,
                updates,
                require_status_not_ready=require_status_not_ready,
            )
        return await self._set_vm_context_if_generation(
            entity_id,
            generation,
            updates,
            require_status_not_ready=require_status_not_ready,
        )

    async def _persist_status_identity(
        self, entity_type: str, entity_id: str, data: Mapping[str, Any]
    ) -> bool:
        """Persist query-discovered identities only from authenticated evidence."""

        if data.get("_identity_authenticated") is not True:
            return False
        generation = _provision_generation(data.get("provision_generation"))
        if generation is None:
            return False
        updates: dict[str, Any] = {}
        for key in ("vm_name", "namespace"):
            if isinstance(data.get(key), str):
                updates[key] = data[key]
        vm_uid = _safe_vm_uid(data.get("vm_uid"))
        if vm_uid is not None:
            updates.update(
                {
                    "vm_uid": vm_uid,
                    "identity_authenticated": True,
                    "identity_provision_generation": generation,
                }
            )
        if (root_uid := _safe_vm_uid(data.get("rootdisk_pvc_uid"))) is not None:
            updates["rootdisk_pvc_uid"] = root_uid
        return await self._set_context_if_generation(
            entity_type,
            entity_id,
            generation,
            updates,
        )

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

        if self._http_available:
            self._http_client = httpx.AsyncClient(
                base_url=self._controller_url,
                timeout=self._http_timeout,
            )

        mode = self.mode
        logger.info("VM provisioner configured with VM_MODE=%s", mode)
        if self._nats_available:
            logger.info("VM provisioner ready: external NATS mode")
        elif self._http_available:
            logger.info(
                "VM provisioner ready: same-cluster HTTP mode (controller=%s)",
                self._controller_url,
            )
        else:
            logger.info("VM provisioner unavailable for VM_MODE=%s", mode)

    async def disconnect(self) -> None:
        """Close the HTTP client (if any)."""
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                logger.debug("Error closing VM controller HTTP client", exc_info=True)
            self._http_client = None

    async def _set_vm_context_if_generation(
        self,
        job_id: str,
        generation: str,
        updates: dict,
        *,
        require_status_not_ready: bool = False,
    ) -> bool:
        if not self._db:
            return False
        try:
            if not require_status_not_ready:
                return bool(
                    await self._db.merge_vm_context_if_provision_generation(
                        job_id, generation, updates
                    )
                )
            return bool(
                await self._db.merge_vm_context_if_provision_generation(
                    job_id,
                    generation,
                    updates,
                    require_status_not_ready=require_status_not_ready,
                )
            )
        except Exception:
            logger.exception(
                "Failed to update generation-guarded VM context for job %s", job_id
            )
            return False

    # =========================================================================
    # Public API
    # =========================================================================

    async def create_vm(
        self,
        job_id: str,
        agent_config: str = "worker_base",
        vm_image: Optional[str] = None,
        cpu_cores: int = 8,
        memory: str = "16Gi",
        description: str = "",
        fresh: bool = True,
    ) -> bool | dict[str, Any]:
        """Create a VM for a job.

        Uses the transport selected by ``VM_MODE``. The compose-only Docker
        fallback remains available for its existing local development path.

        Args:
            fresh: True (default) for a genuine (re)provision. False for a
                deferred-create poll re-issue — the controller answered
                ``waiting_golden`` (a shared golden image is importing) or
                ``waiting_capacity`` (the cluster VM cap is full) or
                ``waiting_headscale`` (the mesh VPN is down, so a VM would be
                unreachable), no VM exists yet, and the dispatcher re-sends
                create as the poll. A poll must NOT reset the provision
                context: ``golden_wait_started_at`` /
                ``headscale_wait_started_at`` anchor those budgets across
                polls, and the context status must stay ``waiting_*`` (not
                flip to 'provisioning') so the decision logic keeps polling
                instead of burning boot-budget waits.
                ``provisioned_at`` alone is rolled forward so that when the
                golden completes and the create finally builds the VM, the boot
                budget starts from ≈ that moment, not from the first poll.

        Returns:
            The controller response when HTTP accepted the request, otherwise
            the transport's boolean acknowledgement.
        """
        # A (re)provisioned VM must start with a CLEAN reap counter and no stale
        # SSH endpoint. context.vm is *merged* (not replaced) across provisions,
        # so a prior incarnation's snapshot_attempts — which reaches the reaper's
        # max and makes attempts_exhausted instantly true, force-deleting the new
        # VM on its first tick — and its dead ssh_host would otherwise leak into
        # this fresh VM. provisioned_at anchors the dispatcher's provisioning
        # timeout. Runs before backend dispatch so every transport inherits it.
        if fresh:
            fresh_context = self._fresh_provision_ctx()
            generation = fresh_context["provision_generation"]
            await self._set_vm_context(job_id, fresh_context)
        else:
            await self._set_vm_context(job_id, {"provisioned_at": time.time()})
            generation = await self._current_provision_generation("job", job_id)
        if self._nats_available:
            return await nats_bridge.request_vm_create(
                job_id=job_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="job",
                set_provisioning=fresh,
                provision_generation=generation,
            )

        if self._http_available:
            return await self._create_http(
                job_id=job_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="job",
                set_provisioning=fresh,
                provision_generation=generation,
            )

        # Docker Compose mode: assign from QEMU-in-Docker pool
        if self._docker_available:
            from .docker_provisioner import docker_provisioner

            result = await docker_provisioner.assign_vm(job_id)
            return result is not None

        return False

    async def capture_vm_teardown_identity(self, job_id: str) -> VMTeardownIdentity:
        """Capture one authenticated VM generation/UID tuple for replay.

        The provision generation is mandatory on every backend.  Immutable VM
        and rootdisk UIDs are included only when the lifecycle controller has
        authenticated them for that exact generation; an unauthenticated guest
        report can never become teardown authority.
        """

        if not self._db:
            raise RuntimeError("VM teardown identity database is unavailable")
        row = await self._db.get_job(job_id)
        if not isinstance(row, dict):
            raise RuntimeError("VM teardown job no longer exists")
        context = _extract_vm_context(row)
        generation = _provision_generation(context.get("provision_generation"))
        if generation is None:
            raise RuntimeError("VM teardown provision generation is unavailable")

        authenticated = (
            context.get("identity_authenticated") is True
            and _provision_generation(context.get("identity_provision_generation"))
            == generation
        )
        vm_uid = _safe_vm_uid(context.get("vm_uid")) if authenticated else None
        rootdisk_uid = (
            _safe_vm_uid(context.get("rootdisk_pvc_uid")) if authenticated else None
        )
        if vm_uid is None or rootdisk_uid is None:
            probe = await self._probe_vm_teardown_identity(job_id, generation)
            if probe.disposition == "unknown":
                raise RuntimeError("VM teardown identity probe is unavailable")
            if probe.disposition == "superseded":
                raise RuntimeError("VM teardown provision generation changed")
            if probe.identity is not None:
                probed_vm_uid = probe.identity.vm_uid
                probed_rootdisk_uid = probe.identity.rootdisk_pvc_uid
                if (
                    vm_uid is not None
                    and probed_vm_uid is not None
                    and vm_uid != probed_vm_uid
                ) or (
                    rootdisk_uid is not None
                    and probe.rootdisk_identity_known
                    and rootdisk_uid != probed_rootdisk_uid
                ):
                    raise RuntimeError("VM teardown immutable identity changed")
                if vm_uid is None:
                    vm_uid = probed_vm_uid
                if rootdisk_uid is None and probe.rootdisk_identity_known:
                    rootdisk_uid = probed_rootdisk_uid
            if probe.disposition == "present" and (
                not probe.rootdisk_identity_known or rootdisk_uid is None
            ):
                raise RuntimeError("VM teardown rootdisk identity is unavailable")
        return VMTeardownIdentity(
            provision_generation=generation,
            vm_uid=vm_uid,
            rootdisk_pvc_uid=rootdisk_uid,
        )

    async def _probe_vm_teardown_identity(
        self,
        job_id: str,
        generation: str,
    ) -> _VMTeardownProbe:
        """Probe the exact backend without collapsing absence into transport loss."""

        result: Mapping[str, Any] | None
        if self._nats_available:
            result = await nats_bridge.query_vm_status(
                job_id,
                provision_generation=generation,
                exact_absence=True,
            )
        elif self._http_available:
            result = await self._query_http(
                job_id,
                provision_generation=generation,
                exact_absence=True,
            )
        else:
            return _VMTeardownProbe("unknown")

        if not isinstance(result, Mapping):
            return _VMTeardownProbe("unknown")
        if result.get("_identity_authenticated") is not True:
            return _VMTeardownProbe("unknown")
        observed_generation = _provision_generation(result.get("provision_generation"))
        if observed_generation != generation:
            return _VMTeardownProbe("superseded")
        status = str(result.get("status") or "")
        rootdisk_known = result.get("rootdisk_identity_known") is True
        identity = VMTeardownIdentity(
            provision_generation=generation,
            vm_uid=_safe_vm_uid(result.get("vm_uid")),
            rootdisk_pvc_uid=_safe_vm_uid(result.get("rootdisk_pvc_uid")),
        )
        if status == "not_found":
            return _VMTeardownProbe(
                "absent",
                identity,
                rootdisk_identity_known=rootdisk_known,
            )
        if status in {"query_failed", "delete_failed"} or identity.vm_uid is None:
            return _VMTeardownProbe("unknown")
        return _VMTeardownProbe(
            "present",
            identity,
            rootdisk_identity_known=rootdisk_known,
        )

    async def revalidate_vm_teardown_identity(
        self,
        job_id: str,
        identity: VMTeardownIdentity,
    ) -> str:
        """Re-prove an exact VM incarnation immediately before snapshot I/O."""

        generation = _provision_generation(identity.provision_generation)
        if generation is None:
            return "unknown"
        if await self._current_provision_generation("job", job_id) != generation:
            return "superseded"
        probe = await self._probe_vm_teardown_identity(job_id, generation)
        classification = self._classify_captured_probe(probe, identity, purge_disk=True)
        if classification == "matched":
            return "matched"
        if classification in {"superseded", "completed"}:
            return "superseded"
        return "unknown"

    @staticmethod
    def _classify_captured_probe(
        probe: _VMTeardownProbe,
        identity: VMTeardownIdentity,
        *,
        purge_disk: bool = True,
    ) -> str:
        """Map one authenticated observation to completed/matched/superseded."""

        if probe.disposition in {"unknown", "superseded"}:
            return probe.disposition
        current = probe.identity
        if current is None:
            return "unknown"
        if probe.disposition == "present":
            if identity.vm_uid is None or current.vm_uid != identity.vm_uid:
                return "superseded"
            if purge_disk:
                if not probe.rootdisk_identity_known:
                    return "unknown"
                if identity.rootdisk_pvc_uid is None:
                    return "unknown"
                if current.rootdisk_pvc_uid != identity.rootdisk_pvc_uid:
                    return "superseded"
            return "matched"
        if not probe.rootdisk_identity_known:
            return "unknown"
        if current.rootdisk_pvc_uid is None:
            # VM and rootdisk both proven absent: a lost delete response is
            # exact idempotent success even when DB context is still stale.
            return "completed"
        if identity.rootdisk_pvc_uid is None:
            return "unknown"
        if current.rootdisk_pvc_uid != identity.rootdisk_pvc_uid:
            return "superseded"
        return "matched" if purge_disk else "completed"

    async def delete_vm_captured(
        self,
        job_id: str,
        identity: VMTeardownIdentity,
        *,
        purge_disk: bool = True,
    ) -> VMTeardownResult:
        """Delete only the VM/rootdisk incarnation captured in an intent."""

        generation = _provision_generation(identity.provision_generation)
        if generation is None:
            return VMTeardownResult("identity_invalid", False)
        current_generation = await self._current_provision_generation("job", job_id)
        if current_generation != generation:
            return VMTeardownResult("identity_superseded", False)
        probe = await self._probe_vm_teardown_identity(job_id, generation)
        classification = self._classify_captured_probe(
            probe, identity, purge_disk=purge_disk
        )
        if classification == "superseded":
            return VMTeardownResult("identity_superseded", False)
        if classification == "completed":
            return VMTeardownResult("completed", True)
        if classification != "matched":
            return VMTeardownResult("identity_unknown", False)
        await self._delete_vm_with_identity(
            job_id,
            purge_disk=purge_disk,
            provision_generation=generation,
            expected_vm_uid=_safe_vm_uid(identity.vm_uid),
            expected_rootdisk_pvc_uid=_safe_vm_uid(identity.rootdisk_pvc_uid),
        )
        reprobe = await self._probe_vm_teardown_identity(job_id, generation)
        reclassification = self._classify_captured_probe(
            reprobe, identity, purge_disk=purge_disk
        )
        if reclassification == "completed":
            return VMTeardownResult("completed", True)
        if reclassification == "superseded":
            return VMTeardownResult("identity_superseded", False)
        return VMTeardownResult("retry_pending", False)

    async def _terminal_snapshot_already_captured(self, job_id: str) -> bool:
        """True when this VM incarnation's terminal snapshot is already in S3.

        A captured teardown is retried whenever the controller has not yet
        confirmed the exact incarnation gone; re-capturing on every retry
        SSHes into a VM that is already shutting down. The snapshot from the
        first attempt is keyed to this incarnation when it was created after
        the VM was provisioned, so reuse it instead.
        """
        if not self._db:
            return False
        try:
            row = await self._db.get_job(job_id)
            if not isinstance(row, dict):
                return False
            ctx = row.get("context") or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            snapshot = ctx.get("snapshot") or {}
            vm_ctx = _extract_vm_context(row)
            if (
                snapshot.get("status") != "available"
                or snapshot.get("source_type") != "vm"
                or snapshot.get("phase_number") is not None
            ):
                return False
            created_at = datetime.fromisoformat(str(snapshot.get("created_at")))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            provisioned_at = float(vm_ctx.get("provisioned_at") or 0)
            reusable = created_at.timestamp() >= provisioned_at
            if reusable:
                logger.info(
                    "Reusing the terminal snapshot already captured for job %s "
                    "(created %s); not re-capturing on teardown retry",
                    job_id,
                    snapshot.get("created_at"),
                )
            return reusable
        except Exception:
            logger.debug(
                "Could not evaluate the existing snapshot for job %s",
                job_id,
                exc_info=True,
            )
            return False

    async def release_vm_captured(
        self,
        job_id: str,
        identity: VMTeardownIdentity,
        *,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
    ) -> VMTeardownResult:
        """Best-effort archive, then release only the captured VM incarnation."""

        generation = _provision_generation(identity.provision_generation)
        if generation is None:
            return VMTeardownResult("identity_invalid", False)
        if await self._current_provision_generation("job", job_id) != generation:
            return VMTeardownResult("identity_superseded", False)
        probe = await self._probe_vm_teardown_identity(job_id, generation)
        classification = self._classify_captured_probe(probe, identity, purge_disk=True)
        if classification == "superseded":
            return VMTeardownResult("identity_superseded", False)
        if classification == "completed":
            return VMTeardownResult("completed", True)
        if classification != "matched":
            return VMTeardownResult("identity_unknown", False)

        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and ssh_host
            and ssh_port
            and not await self._terminal_snapshot_already_captured(job_id)
        ):
            try:
                captured = await self._snapshot_service.capture_vm_snapshot(
                    job_id=job_id,
                    ssh_host=ssh_host,
                    ssh_port=int(ssh_port),
                    source_type="vm",
                )
                if not captured:
                    logger.warning(
                        "Captured VM snapshot skipped for job %s; deleting exact "
                        "incarnation under terminal teardown policy",
                        job_id,
                    )
            except Exception:
                logger.exception(
                    "Captured VM snapshot failed for job %s; deleting exact "
                    "incarnation under terminal teardown policy",
                    job_id,
                )
        return await self.delete_vm_captured(job_id, identity, purge_disk=True)

    async def delete_orphan_vm_captured(
        self,
        job_id: str,
        identity: VMTeardownIdentity,
        *,
        purge_disk: bool = True,
    ) -> VMTeardownResult:
        """Delete an inventory-proven VM after both owning rows are absent."""

        generation = _provision_generation(identity.provision_generation)
        vm_uid = _safe_vm_uid(identity.vm_uid)
        rootdisk_uid = _safe_vm_uid(identity.rootdisk_pvc_uid)
        if (
            generation is None
            or vm_uid is None
            or (purge_disk and rootdisk_uid is None)
        ):
            return VMTeardownResult("identity_unknown", False)
        probe = await self._probe_vm_teardown_identity(job_id, generation)
        classification = self._classify_captured_probe(
            probe, identity, purge_disk=purge_disk
        )
        if classification == "completed":
            return VMTeardownResult("completed", True)
        if classification == "superseded":
            return VMTeardownResult("identity_superseded", False)
        if classification != "matched":
            return VMTeardownResult("identity_unknown", False)
        await self._delete_vm_with_identity(
            job_id,
            purge_disk=purge_disk,
            provision_generation=generation,
            expected_vm_uid=vm_uid,
            expected_rootdisk_pvc_uid=rootdisk_uid,
        )
        reprobe = await self._probe_vm_teardown_identity(job_id, generation)
        reclassification = self._classify_captured_probe(
            reprobe, identity, purge_disk=purge_disk
        )
        if reclassification == "completed":
            return VMTeardownResult("completed", True)
        if reclassification == "superseded":
            return VMTeardownResult("identity_superseded", False)
        return VMTeardownResult("retry_pending", False)

    async def delete_vm(self, job_id: str, purge_disk: bool = True) -> bool:
        """Delete a VM for a job.

        Args:
            job_id: Job UUID.
            purge_disk: False when a recreate is expected (crash recovery, the
                reconciler giving up on a dirty VM) — the controller keeps the
                persistent rootdisk and the Headscale node so the next create
                reattaches the same files. Default True: terminal, everything
                goes. Honoured by the lifecycle controller over NATS or HTTP.

        Returns:
            True if the request was accepted, False otherwise.
        """
        generation = await self._current_provision_generation("job", job_id)
        return await self._delete_vm_with_identity(
            job_id,
            purge_disk=purge_disk,
            provision_generation=generation,
        )

    async def _delete_vm_with_identity(
        self,
        job_id: str,
        *,
        purge_disk: bool,
        provision_generation: str | None,
        expected_vm_uid: str | None = None,
        expected_rootdisk_pvc_uid: str | None = None,
    ) -> bool:
        generation = _provision_generation(provision_generation)
        if self._nats_available:
            kwargs: dict[str, Any] = {
                "purge_disk": purge_disk,
                "provision_generation": generation,
                "entity_type": "job",
            }
            if expected_vm_uid is not None:
                kwargs["expected_vm_uid"] = expected_vm_uid
            if expected_rootdisk_pvc_uid is not None:
                kwargs["expected_rootdisk_pvc_uid"] = expected_rootdisk_pvc_uid
            return await nats_bridge.request_vm_delete(job_id, **kwargs)

        if self._http_available:
            kwargs = {
                "entity_type": "job",
                "purge_disk": purge_disk,
                "provision_generation": generation,
            }
            if expected_vm_uid is not None:
                kwargs["expected_vm_uid"] = expected_vm_uid
            if expected_rootdisk_pvc_uid is not None:
                kwargs["expected_rootdisk_pvc_uid"] = expected_rootdisk_pvc_uid
            return await self._delete_http(job_id, **kwargs)

        return False

    async def release_vm(
        self,
        job_id: str,
        ssh_host: Optional[str] = None,
        ssh_port: Optional[int] = None,
    ) -> bool:
        """Snapshot a job VM to S3, then delete it.

        Args:
            job_id: Job UUID.
            ssh_host: SSH host for snapshot (read from DB context if omitted).
            ssh_port: SSH port for snapshot (read from DB context if omitted).

        Returns:
            True if deletion succeeded (snapshot failure is non-fatal).
        """
        # Resolve SSH coordinates from DB if not provided
        if not ssh_host and self._db:
            try:
                job = await self._db.get_job(job_id)
                if job:
                    vm_ctx = _extract_vm_context(job)
                    ssh_host = ssh_host or vm_ctx.get("ssh_host")
                    ssh_port = ssh_port or vm_ctx.get("ssh_port")
            except Exception:
                logger.debug("Could not read VM context for job %s", job_id)

        # Snapshot before delete (best-effort)
        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and ssh_host
            and ssh_port
        ):
            try:
                # capture_vm_snapshot RETURNS False (it does not raise) when it
                # declines — notably for a VM workspace, whose only address is on
                # the tailnet the orchestrator cannot reach. Ignoring the return
                # made this log a capture that never happened, on every VM
                # release. knowledge-base/knowledge/issues/
                # vm_workspace_snapshot_unreachable_from_orchestrator.md
                captured = await self._snapshot_service.capture_vm_snapshot(
                    job_id=job_id,
                    ssh_host=ssh_host,
                    ssh_port=int(ssh_port),
                    source_type="vm",
                )
                if captured:
                    logger.info(
                        "VM snapshot captured for job %s before release", job_id
                    )
                else:
                    logger.warning(
                        "VM snapshot SKIPPED for job %s (%s:%s) — deleting anyway; "
                        "workspace state not yet pushed to git will be lost. See "
                        "context.snapshot.status for the reason.",
                        job_id,
                        ssh_host,
                        ssh_port,
                    )
            except Exception:
                logger.exception(
                    "VM snapshot failed for job %s — deleting anyway", job_id
                )

        return await self.delete_vm(job_id)

    async def release_thread_vm(
        self,
        thread_id: str,
        ssh_host: Optional[str] = None,
        ssh_port: Optional[int] = None,
    ) -> bool:
        """Snapshot a thread VM to S3, then delete it.

        Args:
            thread_id: Thread UUID.
            ssh_host: SSH host for snapshot (read from DB if omitted).
            ssh_port: SSH port for snapshot (read from DB if omitted).

        Returns:
            True if deletion succeeded (snapshot failure is non-fatal).
        """
        # Resolve SSH coordinates from DB if not provided
        if not ssh_host and self._db:
            try:
                thread = await self._db.get_thread(thread_id)
                if thread:
                    metadata = thread.get("metadata") or {}
                    if isinstance(metadata, str):
                        import json

                        metadata = json.loads(metadata)
                    vm_ctx = metadata.get("vm") or {}
                    ssh_host = ssh_host or vm_ctx.get("ssh_host")
                    ssh_port = ssh_port or vm_ctx.get("ssh_port")
            except Exception:
                logger.debug("Could not read VM context for thread %s", thread_id)

        # Snapshot before delete (best-effort)
        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and ssh_host
            and ssh_port
        ):
            try:
                # See release_vm: a False return means "declined", not "raised".
                captured = await self._snapshot_service.capture_vm_snapshot(
                    job_id=thread_id,
                    ssh_host=ssh_host,
                    ssh_port=int(ssh_port),
                    source_type="vm",
                    entity_type="threads",
                )
                if captured:
                    logger.info(
                        "VM snapshot captured for thread %s before release", thread_id
                    )
                else:
                    logger.warning(
                        "VM snapshot SKIPPED for thread %s (%s:%s) — deleting "
                        "anyway; workspace state not yet pushed to git will be "
                        "lost. See metadata.snapshot.status for the reason.",
                        thread_id,
                        ssh_host,
                        ssh_port,
                    )
            except Exception:
                logger.exception(
                    "VM snapshot failed for thread %s — deleting anyway", thread_id
                )

        return await self.delete_thread_vm(thread_id)

    async def query_status(
        self,
        job_id: str,
        timeout: float = 5.0,
        entity_type: str = "job",
    ) -> Optional[dict]:
        """Query live VM status.

        External mode uses NATS request/reply. Same-cluster mode uses the HTTP
        controller's ``GET /vms/{job_id}`` route.

        Returns:
            Status dict or None if unavailable.
        """
        generation = await self._current_provision_generation(entity_type, job_id)
        result: Optional[dict]
        if self._nats_available:
            result = await nats_bridge.query_vm_status(
                job_id,
                timeout,
                provision_generation=generation,
            )

        elif self._http_available:
            result = await self._query_http(
                job_id,
                timeout,
                provision_generation=generation,
            )

        else:
            return None
        if result is not None:
            if result.get("status") == "not_found":
                return None
            authenticated = result.get("_identity_authenticated") is True
            current = await self._persist_status_identity(entity_type, job_id, result)
            if authenticated and not current:
                return None
            result.pop("_identity_authenticated", None)
        return result

    async def list_vms(
        self, *, include_teardown_identity: bool = False
    ) -> Optional[list]:
        """Enumerate the VMs the backend is actually running.

        Inventory source for the lifecycle VM orphan sweep — the DB-derived
        instance view can't see a VM whose owning row was deleted. Returns a
        list of ``{vm_name, entity_id, created_at, phase}`` dicts, or None
        when no transport can answer (docker pool has no dynamic VMs; an old
        controller without the list op times out / 404s). None means
        "unknown", never "no VMs" — callers must not reap on it.
        """
        if self._nats_available:
            if include_teardown_identity:
                return await nats_bridge.request_vm_list(include_teardown_identity=True)
            return await nats_bridge.request_vm_list()

        if self._http_available:
            return await self._list_http(
                include_teardown_identity=include_teardown_identity
            )

        return None

    # HTTP controller backend (same-cluster, controller-mediated)
    #
    # The controller still owns KubeVirt RBAC and the VM template; this
    # transport just swaps NATS for HTTP. Status updates that NATS pushed
    # asynchronously now arrive synchronously in the response body, so
    # context updates happen here in the orchestrator instead of via the
    # vm.lifecycle.status subscription.
    #
    # Caveat: in-VM daemon events (register/heartbeat/freeze/resume) are
    # NOT carried by this transport. Same-cluster deployments that need
    # those still need NATS (or a future HTTP webhook from the daemon).
    # =========================================================================

    async def _create_http(
        self,
        job_id: str,
        agent_config: str,
        vm_image: Optional[str],
        cpu_cores: int,
        memory: str,
        description: str,
        entity_type: str = "job",
        set_provisioning: bool = True,
        provision_generation: str | None = None,
    ) -> bool | dict[str, Any]:
        """Create a VM by POSTing to the co-located VM controller.

        ``set_provisioning=False`` marks a deferred-create poll re-issue: skip the
        interim 'provisioning' context write so the status stays
        ``waiting_*`` between polls (the response merge below still
        records whatever the controller answered).
        """
        if self._http_client is None:
            return False

        network_tier = DEFAULT_NETWORK_TIER
        if self._db is not None:
            try:
                network_tier = (
                    await self._db.get_workspace_network_tier(job_id, entity_type)
                    or DEFAULT_NETWORK_TIER
                )
            except Exception:
                logger.exception(
                    "Failed to resolve network_tier for %s=%s; using default",
                    entity_type,
                    job_id,
                )

        payload: dict[str, Any] = {
            "job_id": job_id,
            "entity_type": entity_type,
            "agent_config": agent_config,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "description": description,
            "nats_url": "",  # No NATS in same-cluster mode
            "network_tier": network_tier,
        }
        if orchestrator_url := os.getenv("ORCHESTRATOR_URL"):
            payload["orchestrator_url"] = orchestrator_url
        generation = _provision_generation(provision_generation)
        if self._lifecycle_hmac_secret is not None and generation is None:
            logger.error(
                "Refusing authenticated HTTP VM create for %s %s without a "
                "current provision generation",
                entity_type,
                job_id,
            )
            return False
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
        request_auth = payload.get(AUTH_FIELD)
        request_id = (
            request_auth.get("request_id")
            if isinstance(request_auth, Mapping)
            else None
        )

        try:
            if set_provisioning:
                if generation is not None:
                    await self._set_context_if_generation(
                        entity_type,
                        job_id,
                        generation,
                        {"status": "provisioning"},
                    )
                else:
                    await self._set_context(
                        entity_type, job_id, {"status": "provisioning"}
                    )
            resp = await self._http_client.post("/vms", json=payload)
            data = resp.json()
            if not isinstance(data, Mapping) or not verify_payload(
                data,
                direction="response",
                operation="create",
                secret=self._lifecycle_hmac_secret,
                expected_correlation_id=request_id,
            ):
                raise RuntimeError(
                    "VM controller create response authentication failed"
                )
            resp.raise_for_status()
            data = unsigned_payload(data)

            updates = {
                "status": data.get("status", "created"),
                "vm_name": data.get("vm_name"),
                "namespace": data.get("namespace"),
                "provisioned_by": "http",
            }
            # New controllers return the immutable admitted VM UID. During a
            # rolling upgrade an older controller may omit it; the fresh
            # context reset leaves vm_uid=None so metering classifies that VM
            # as legacy-unknown instead of trusting its reusable name.
            # Deferred-create responses carry telemetry instead of a VM name
            # (golden import progress, or why the mesh VPN is unreachable);
            # surface it for the dispatcher's poll logging and park message.
            for key in (
                "golden",
                "golden_phase",
                "golden_progress",
                "headscale_error",
                "running_vms",
                "max_concurrent_vms",
            ):
                if data.get(key) is not None:
                    updates[key] = data[key]
            response_generation = _provision_generation(
                data.get("provision_generation")
            )
            identity_updates: dict[str, Any] = {}
            if (
                self._lifecycle_hmac_secret is not None
                and response_generation is not None
                and response_generation == generation
            ):
                if (vm_uid := _safe_vm_uid(data.get("vm_uid"))) is not None:
                    identity_updates["vm_uid"] = vm_uid
                if (
                    rootdisk_pvc_uid := _safe_vm_uid(data.get("rootdisk_pvc_uid"))
                ) is not None:
                    identity_updates["rootdisk_pvc_uid"] = rootdisk_pvc_uid
                if (
                    host_key_fingerprint := _safe_ssh_host_key_fingerprint(
                        data.get("ssh_host_key_fingerprint")
                    )
                ) is not None:
                    # The controller created this pin before the VM and Secret
                    # admission result crossed the authenticated transport. It
                    # belongs in this generation-CAS merge with vm_uid so a
                    # stale response can never arm readiness for a new guest.
                    identity_updates["ssh_host_key_fingerprint"] = host_key_fingerprint
                if identity_updates:
                    identity_updates.update(
                        {
                            "identity_authenticated": True,
                            "identity_provision_generation": response_generation,
                        }
                    )
            if response_generation and response_generation == generation:
                merged = await self._set_context_if_generation(
                    entity_type,
                    job_id,
                    response_generation,
                    {**updates, **identity_updates},
                )
                if not merged:
                    if self._lifecycle_hmac_secret is not None:
                        logger.warning(
                            "Ignoring stale authenticated HTTP create response for "
                            "%s %s",
                            entity_type,
                            job_id,
                        )
                    else:
                        await self._set_context(entity_type, job_id, updates)
            else:
                if self._lifecycle_hmac_secret is not None:
                    logger.warning(
                        "Ignoring authenticated HTTP create response with a stale or "
                        "missing provision generation for %s %s",
                        entity_type,
                        job_id,
                    )
                else:
                    await self._set_context(entity_type, job_id, updates)
            if data.get("status") == "waiting_golden":
                logger.info(
                    "VM create deferred (http): golden %s importing (%s %s)",
                    data.get("golden"),
                    entity_type,
                    job_id,
                )
            elif data.get("status") == "waiting_capacity":
                logger.info(
                    "VM create deferred (http): capacity %s/%s (%s %s)",
                    data.get("running_vms"),
                    data.get("max_concurrent_vms"),
                    entity_type,
                    job_id,
                )
            elif data.get("status") == "waiting_headscale":
                logger.info(
                    "VM create deferred (http): Headscale unavailable (%s) (%s %s)",
                    data.get("headscale_error"),
                    entity_type,
                    job_id,
                )
            else:
                logger.info(
                    "VM created (http): %s (%s %s)",
                    data.get("vm_name"),
                    entity_type,
                    job_id,
                )
            return dict(data)
        except httpx.HTTPStatusError as e:
            error = _extract_http_error(e.response)
            logger.error(
                "VM controller rejected create for %s %s: %s",
                entity_type,
                job_id,
                error,
            )
            failure = {
                "status": "failed",
                "error": error,
                "provisioned_by": "http",
            }
            if generation is not None:
                await self._set_context_if_generation(
                    entity_type, job_id, generation, failure
                )
            else:
                await self._set_context(entity_type, job_id, failure)
            return False
        except Exception as e:
            logger.error("HTTP create failed for %s %s: %s", entity_type, job_id, e)
            failure = {
                "status": "failed",
                "error": str(e),
                "provisioned_by": "http",
            }
            if generation is not None:
                await self._set_context_if_generation(
                    entity_type, job_id, generation, failure
                )
            else:
                await self._set_context(entity_type, job_id, failure)
            return False

    async def _delete_http(
        self,
        job_id: str,
        entity_type: str = "job",
        purge_disk: bool = True,
        provision_generation: str | None = None,
        expected_vm_uid: str | None = None,
        expected_rootdisk_pvc_uid: str | None = None,
    ) -> bool:
        """Delete a VM by sending DELETE to the co-located VM controller."""
        if self._http_client is None:
            return False

        generation = _provision_generation(provision_generation)
        if self._lifecycle_hmac_secret is not None and generation is None:
            logger.error(
                "Refusing authenticated HTTP VM delete for %s without a current "
                "provision generation",
                job_id,
            )
            return False
        signed_payload = {
            "job_id": job_id,
            "purge_disk": purge_disk,
            "provision_generation": generation,
        }
        if expected_vm_uid is not None:
            signed_payload["expected_vm_uid"] = expected_vm_uid
        if expected_rootdisk_pvc_uid is not None:
            signed_payload["expected_rootdisk_pvc_uid"] = expected_rootdisk_pvc_uid
        params: dict[str, str] = {}
        if not purge_disk:
            params["purge_disk"] = "false"
        if generation is not None:
            params["provision_generation"] = generation
        if expected_vm_uid is not None:
            params["expected_vm_uid"] = expected_vm_uid
        if expected_rootdisk_pvc_uid is not None:
            params["expected_rootdisk_pvc_uid"] = expected_rootdisk_pvc_uid
        params.update(
            _http_lifecycle_query(
                signed_payload,
                operation="delete",
                secret=self._lifecycle_hmac_secret,
            )
        )
        request_id = params.get("lifecycle_auth_request_id")
        try:
            resp = await self._http_client.delete(f"/vms/{job_id}", params=params)
            data: Mapping[str, Any] | None = None
            if self._lifecycle_hmac_secret is not None or resp.status_code != 404:
                candidate = resp.json()
                if not isinstance(candidate, Mapping) or not verify_payload(
                    candidate,
                    direction="response",
                    operation="delete",
                    secret=self._lifecycle_hmac_secret,
                    expected_correlation_id=request_id,
                ):
                    raise RuntimeError(
                        "VM controller delete response authentication failed"
                    )
                data = unsigned_payload(candidate)
            if resp.status_code != 404:
                resp.raise_for_status()
            if self._lifecycle_hmac_secret is not None:
                response_generation = _provision_generation(
                    data.get("provision_generation") if data else None
                )
                if response_generation != generation:
                    raise RuntimeError(
                        "VM controller delete response generation mismatch"
                    )
            if generation is not None:
                await self._set_context_if_generation(
                    entity_type, job_id, generation, {"status": "deleted"}
                )
            else:
                await self._set_context(entity_type, job_id, {"status": "deleted"})
            logger.info("VM deleted (http): %s %s", entity_type, job_id)
            return True
        except httpx.HTTPStatusError as e:
            error = _extract_http_error(e.response)
            logger.error(
                "VM controller rejected delete for %s %s: %s",
                entity_type,
                job_id,
                error,
            )
            return False
        except Exception as e:
            logger.error("HTTP delete failed for %s %s: %s", entity_type, job_id, e)
            return False

    async def _query_http(
        self,
        job_id: str,
        timeout: float = 5.0,
        provision_generation: str | None = None,
        *,
        exact_absence: bool = False,
    ) -> Optional[dict]:
        """Query VM status via the co-located VM controller."""
        if self._http_client is None:
            return None

        generation = _provision_generation(provision_generation)
        if self._lifecycle_hmac_secret is not None and generation is None:
            return None
        signed_payload = {
            "job_id": job_id,
            "provision_generation": generation,
        }
        params: dict[str, str] = {}
        if generation is not None:
            params["provision_generation"] = generation
        if exact_absence:
            signed_payload["exact_absence"] = True
            params["exact_absence"] = "true"
        params.update(
            _http_lifecycle_query(
                signed_payload,
                operation="status",
                secret=self._lifecycle_hmac_secret,
            )
        )
        request_id = params.get("lifecycle_auth_request_id")
        try:
            resp = await self._http_client.get(
                f"/vms/{job_id}", params=params, timeout=timeout
            )
            data = resp.json()
            if not isinstance(data, Mapping) or not verify_payload(
                data,
                direction="response",
                operation="status",
                secret=self._lifecycle_hmac_secret,
                expected_correlation_id=request_id,
            ):
                return None
            result = unsigned_payload(data)
            if (
                self._lifecycle_hmac_secret is not None
                and _provision_generation(result.get("provision_generation"))
                != generation
            ):
                return None
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            if self._lifecycle_hmac_secret is not None:
                result["_identity_authenticated"] = True
            return result
        except Exception as e:
            logger.debug("HTTP status query failed for job %s: %s", job_id, e)
            return None

    async def _list_http(
        self, *, include_teardown_identity: bool = False
    ) -> Optional[list]:
        """List managed VMs via the co-located VM controller.

        A 404 means the controller predates the list op — unknown, not empty.
        """
        if self._http_client is None:
            return None

        try:
            signed_payload = {
                **(
                    {"include_teardown_identity": True}
                    if include_teardown_identity
                    else {}
                )
            }
            params = _http_lifecycle_query(
                signed_payload, operation="list", secret=self._lifecycle_hmac_secret
            )
            if include_teardown_identity:
                params["include_teardown_identity"] = "true"
            resp = await self._http_client.get(
                "/vms",
                params=params,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, Mapping) or not verify_payload(
                data,
                direction="response",
                operation="list",
                secret=self._lifecycle_hmac_secret,
                expected_correlation_id=params.get("lifecycle_auth_request_id"),
            ):
                return None
            vms = unsigned_payload(data).get("vms")
            return vms if isinstance(vms, list) else None
        except Exception as e:
            logger.debug("HTTP VM list failed: %s", e)
            return None

    async def _set_context(
        self, entity_type: str, entity_id: str, updates: dict
    ) -> None:
        """Route context updates to the right table based on entity type."""
        if entity_type == "thread":
            await self._set_thread_vm_context(entity_id, updates)
        else:
            await self._set_vm_context(entity_id, updates)

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _fresh_provision_ctx() -> dict:
        """Reap-counter/endpoint reset + provision timestamp for a new VM.

        Merged into context.vm at the start of every (re)provision so the new
        incarnation does not inherit the previous one's snapshot_attempts (which
        would make the lifecycle reaper's attempts_exhausted instantly true), a
        dead ssh_host, or stale SSH-readiness probe identity. ``provisioned_at``
        (epoch seconds) anchors the dispatcher's provisioning-timeout escalation.
        """
        return {
            "snapshot_attempts": 0,
            "ssh_host": None,
            "ssh_port": None,
            "ssh_registration_id": None,
            "registered_at": None,
            "ssh_verified_at": None,
            "ssh_probe_attempts": 0,
            "ssh_probe_error": None,
            "ssh_probe_failed_at": None,
            # A fresh provision gets a fresh controller-owned keypair. Leave
            # readiness fail-closed until the admitted controller response
            # installs the matching public fingerprint for this generation.
            "ssh_host_key_fingerprint": None,
            # Never let a previous VM incarnation's UID authenticate the next
            # one between create dispatch and the admitted controller result.
            "vm_uid": None,
            # A PVC name is reusable.  Only the newly admitted immutable UID
            # may authenticate this incarnation's rootdisk for metering.
            "rootdisk_pvc_uid": None,
            "identity_authenticated": False,
            "identity_provision_generation": None,
            # Opaque incarnation nonce. Controller identities are merged only
            # through a DB-side compare-and-merge against this exact value.
            "provision_generation": str(uuid4()),
            # The provisioner now owns and attests the guest host key. Canvas
            # remains closed until its separate workspace-generation binding
            # is implemented; key ownership alone does not enable that gate.
            CANVAS_WORKSPACE_GENERATION_KEY: None,
            # Golden-wait anchor from a previous incarnation must not cap this
            # provision's patience for a cold golden import (dispatcher stamps
            # it again on the first waiting_golden it sees). Same for the
            # Headscale-wait anchor.
            "golden_wait_started_at": None,
            "capacity_wait_started_at": None,
            "headscale_wait_started_at": None,
            # Same for the teardown anchor: a stale one would make this
            # incarnation read as instantly-stuck the moment it enters
            # 'deleting', and the dispatcher would recycle it on sight.
            "deleting_started_at": None,
            "headscale_error": None,
            "provisioned_at": time.time(),
        }

    async def _set_vm_context(self, job_id: str, updates: dict) -> None:
        """Atomically merge updates into the job's context.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_vm_context(job_id, updates)
        except Exception:
            logger.exception("Failed to update VM context for job %s", job_id)

    # =========================================================================
    # Thread VM support (persistent agent sessions)
    # =========================================================================

    async def create_thread_vm(
        self,
        thread_id: str,
        agent_config: str = "worker_base",
        vm_image: Optional[str] = None,
        cpu_cores: int = 8,
        memory: str = "16Gi",
        description: str = "",
    ) -> bool | dict[str, Any]:
        """Create a VM for a persistent thread.

        Mirrors create_vm() but routes context to threads.metadata.vm
        and uses entity_type="thread" for NATS bridge routing.

        Returns:
            True if the request was accepted, False otherwise.
        """
        # See create_vm: reset the reap counter / stale endpoint and stamp
        # provisioned_at before backend dispatch (thread-scoped context).
        fresh_context = self._fresh_provision_ctx()
        generation = fresh_context["provision_generation"]
        await self._set_thread_vm_context(thread_id, fresh_context)
        if self._nats_available:
            return await nats_bridge.request_vm_create(
                job_id=thread_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="thread",
                provision_generation=generation,
            )

        if self._http_available:
            return await self._create_http(
                job_id=thread_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="thread",
                provision_generation=generation,
            )

        return False

    async def delete_thread_vm(self, thread_id: str, purge_disk: bool = True) -> bool:
        """Delete a VM for a persistent thread.

        Args:
            thread_id: Thread UUID.
            purge_disk: False when the session expects to come back (suspend) —
                see ``delete_vm``.

        Returns:
            True if the request was accepted, False otherwise.
        """
        generation = await self._current_provision_generation("thread", thread_id)
        if self._nats_available:
            return await nats_bridge.request_vm_delete(
                thread_id,
                purge_disk=purge_disk,
                provision_generation=generation,
                entity_type="thread",
            )

        if self._http_available:
            return await self._delete_http(
                thread_id,
                entity_type="thread",
                purge_disk=purge_disk,
                provision_generation=generation,
            )

        return False

    async def _set_thread_vm_context(self, thread_id: str, updates: dict) -> None:
        """Atomically merge updates into thread's metadata.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_thread_vm_context(thread_id, updates)
        except Exception:
            logger.exception("Failed to update thread VM context for %s", thread_id)

    async def _set_thread_vm_context_if_generation(
        self,
        thread_id: str,
        generation: str,
        updates: dict,
        *,
        require_status_not_ready: bool = False,
    ) -> bool:
        if not self._db:
            return False
        try:
            if not require_status_not_ready:
                return bool(
                    await self._db.merge_thread_vm_context_if_provision_generation(
                        thread_id, generation, updates
                    )
                )
            return bool(
                await self._db.merge_thread_vm_context_if_provision_generation(
                    thread_id,
                    generation,
                    updates,
                    require_status_not_ready=require_status_not_ready,
                )
            )
        except Exception:
            logger.exception(
                "Failed to update generation-guarded thread VM context for %s",
                thread_id,
            )
            return False


def _extract_http_error(response: httpx.Response) -> str:
    """Pull a useful error message out of a controller HTTP error response."""
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:
        pass
    text = (response.text or "").strip()
    return text or f"HTTP {response.status_code}"


# Module-level singleton
vm_provisioner = VMProvisioner()
