"""VMInstanceManager — Phase 3 stateful manager for VM workspaces.

Wraps ``VMProvisioner`` (4-backend internal dispatch:
NATS / HTTP / direct KubeVirt / Docker pool), the snapshot service,
and the suspension service. Surfaces VMs to the unified lifecycle
reconciler so drift detection, crash recovery, and snapshot-before-
delete work the same way they do for agents and workspaces.

Unlike pods, VMs aren't enumerated via a K8s label selector — each
backend has its own listing convention, and the orchestrator already
keys VMs by job/thread id via ``jobs.context.vm`` and
``threads.metadata.vm`` JSONB. The manager iterates those rows
instead. The 4-backend dispatch stays inside ``VMProvisioner``; from
the lifecycle reconciler's perspective there is one VM kind.

Phase 3 scope: drift detection + crash recovery via the existing
``vm.status`` field. Deeper health probes (NATS daemon ping, HTTP
controller health endpoint) are a follow-up — they require new
provisioner methods, not new lifecycle scaffolding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from services.ssh_helpers import orchestrator_can_reach

from .types import Instance
from .workspace_manager import (
    infra_transient_retry_pending,
    orphan_grace_seconds,
    paused_within_grace,
)

logger = logging.getLogger(__name__)


# 'reviewing' is the verification-enabled twin of 'pending_review'. By *status*
# it is as quiescent as 'pending_review', but a critic subjob SSHes into the
# *parent's* live VM (inherited context.vm), so reaping on status alone kills the
# VM under the critic. The is_idle/is_reapable predicates therefore gate on
# ``has_live_shared_child`` (a non-terminal child bound to this same VM); the
# status set stays optimistic and the guard handles the dependency precisely.
# Kept in lockstep with WorkspaceInstanceManager._IDLE_JOB_STATUSES. (In the
# normal flow the guard, not the status set, is what keeps a reviewing VM alive:
# a live critic pins it, and by the time the critic ends the parent has moved to
# pending_review/completed — so the guard also avoids churning the VM's
# expensive ~3-min disk re-clone, see srw_vm_dispatcher_reconciler_churn.)
# See docs/issues/reviewing_parent_pod_reaped_under_critic.md.
_IDLE_JOB_STATUSES = frozenset(
    {"paused", "pending_review", "reviewing", "waiting_for_reply"}
)
_IDLE_THREAD_STATUSES = frozenset({"ended"})

# Terminal = bound work finished; reapable = pod/VM no longer needed at all
# (suspendable-idle ∪ terminal). Mirrors WorkspaceInstanceManager.
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_THREAD_STATUSES = frozenset({"ended"})
_REAPABLE_JOB_STATUSES = _IDLE_JOB_STATUSES | _TERMINAL_JOB_STATUSES
_REAPABLE_THREAD_STATUSES = _IDLE_THREAD_STATUSES | _TERMINAL_THREAD_STATUSES

# VM teardown is fire-and-forget to an external controller, and the DB
# context.vm row persists (status flips to 'deleting', later 'deleted', or
# 'suspended' on a clean suspend) until a fresh provision clears it. Unlike
# workspace pods — which vanish from the live pod list once deleted — a
# torn-down VM would otherwise stay enumerable and get re-reaped every tick.
# Skip these statuses so reap/drift/crash don't loop on a VM that's already
# gone or restorable-on-demand. ('deleted'/'none'/'suspended' are handled by
# ensure_workspace on the next dispatch: recreate or restore.)
_TORN_DOWN_VM_STATUSES = frozenset({"deleting", "deleted", "suspended"})

# 'failed' is exclusively a *provisioning* failure (the only three writers of
# vm_status='failed' are create_vm exception paths — there is no runtime-failed
# source). The dispatcher deliberately PARKS a 'failed' VM ("do NOT hot-retry
# every tick (shared VM cluster)"), so the reconciler must stay off it: reaping
# it (is_healthy=False → force-delete → status 'deleted') would flip it back to
# the dispatcher's re-provision branch and churn. Skip like torn-down.
_PARKED_VM_STATUSES = frozenset({"failed"})


def expected_vm_shas() -> set[str]:
    """SHAs from the configured default VM image tag.

    Reads ``DEFAULT_VM_IMAGE`` and extracts the suffix from a
    ``...:sha-<hash>`` tag. Per-job ``vm_image`` overrides aren't
    surfaced here — drift is measured against the orchestrator's
    current default. Returns an empty set for ``:latest``-style tags
    so drift checks short-circuit cleanly in local dev.
    """
    shas: set[str] = set()
    tag = os.environ.get("DEFAULT_VM_IMAGE", "")
    if ":sha-" in tag:
        shas.add(tag.rsplit(":sha-", 1)[-1])
    return shas


def _extract_sha(image: str | None) -> str | None:
    if not image or ":sha-" not in image:
        return None
    return image.rsplit(":sha-", 1)[-1]


def _vm_age_seconds(created_at: Any) -> float | None:
    """Age from a KubeVirt creationTimestamp (ISO string over the wire)."""
    if isinstance(created_at, str):
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(created_at, datetime):
        created = created_at
    else:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())


def _coerce_jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


class VMInstanceManager:
    """Lifecycle manager for the vm kind."""

    kind = "vm"

    def __init__(
        self,
        vm_provisioner: Any,
        suspension_service: Any,
        snapshot_service: Any,
        db: Any,
    ):
        self._provisioner = vm_provisioner
        self._suspension = suspension_service
        self._snapshot = snapshot_service
        self._db = db
        # Reachability probe cache: ssh_host -> (probed_at, ok).
        self._reach_cache: dict[str, tuple[float, bool]] = {}
        self._reach_ttl_s: float = 30.0
        self._clock = time.monotonic

    # -------------------------------------------------------------------------
    # Protocol implementation
    # -------------------------------------------------------------------------

    async def expected_versions(self) -> set[str]:
        return expected_vm_shas()

    async def list_instances(self) -> list[Instance]:
        if not self._provisioner_available():
            return []
        rows = await self._fetch_vm_rows()
        instances: list[Instance] = []
        for row in rows:
            inst = self._row_to_instance(row)
            if inst is None:
                continue
            # Live-child guard: only a reapable-status *job* VM can be torn down,
            # and only a job VM can have a critic subjob, so scope the query to
            # that case. Threads have no critic children.
            native_id = inst.metadata.get("vm_native_id")
            if (
                inst.metadata.get("scope") == "job"
                and inst.bound_to
                and native_id
                and inst.metadata.get("job_status") in _REAPABLE_JOB_STATUSES
            ):
                inst.metadata[
                    "has_live_shared_child"
                ] = await self._live_shared_child_exists(inst.bound_to, native_id)
            instances.append(inst)
        return instances

    async def is_healthy(self, inst: Instance) -> bool:
        # VMs report status in their own context. ``failed`` (a provisioning
        # failure) is now filtered out in _row_to_instance (_PARKED_VM_STATUSES)
        # so the dispatcher can hold it parked without the reconciler force-
        # deleting it — this branch is kept as defense-in-depth in case a failed
        # VM ever reaches here. ``suspended`` means already drained. Anything
        # else (provisioning/created/ready/restoring) is treated as healthy.
        status = inst.metadata.get("vm_status")
        if status == "failed":
            return False
        return True

    async def is_idle(self, inst: Instance) -> bool:
        if inst.metadata.get("has_live_shared_child"):
            return False
        ide_session_status = inst.metadata.get("ide_session_status")
        if ide_session_status in ("restoring", "active", "idle"):
            return False
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            # Warm grace for 'paused' (often a human-wait — sudo/VM-upgrade
            # approval with a 24h TTL); VM reaps are destructive (no volume
            # reattach), so acting on the next tick loses the workspace
            # minutes into that window. Mirrors WorkspaceInstanceManager.
            if paused_within_grace(inst.metadata):
                return False
            # A job paused for a transient-infra retry is NOT idle — it is
            # mid-recovery and will resume onto this exact VM. Reaping it would
            # hand the retry an empty workspace, which is the loss the retry
            # exists to avoid. Bounded by next_retry_at (≤1h). (Defect 1b)
            if infra_transient_retry_pending(inst.metadata):
                return False
            return job_status in _IDLE_JOB_STATUSES
        if thread_status:
            return thread_status in _IDLE_THREAD_STATUSES
        return False

    # -------------------------------------------------------------------------
    # Reap path (ReapableInstanceManager) — mirrors WorkspaceInstanceManager,
    # adjusted for VMs (ssh port default 22; vm_status='suspended' == clean;
    # give_up is force-delete — no volume reattach this round).
    # -------------------------------------------------------------------------

    async def is_reapable(self, inst: Instance) -> bool:
        """True when the bound work no longer needs the VM (idle ∪ terminal).

        A *dispatchable* job (created/paused, unassigned, freeze-free) is not
        idle — it is MID-RESUME, owned by the dispatcher, which is actively
        (re)provisioning or about to claim its VM. Reaping such a VM fought the
        dispatcher head-on: a version_upgrade drain leaves the VM 'ready', the
        dispatcher re-claims on resume, but the reconciler saw 'paused' and tore
        it down first — create→reap→create churn against the shared VM cluster,
        and a needless ~3-min disk re-clone on every resume. Hand off: the
        dispatcher owns bring-up; the reaper only reclaims genuinely idle VMs
        (pending_review / waiting_for_reply / a paused-but-still-frozen job) and
        terminal ones. Mirrors get_dispatchable_jobs' predicate exactly.

        Guard: a VM shared by a live child job (a critic SSHed into the parent's
        VM) is never reapable, regardless of the parent's own status. See
        ``_live_shared_child_exists`` and
        docs/issues/reviewing_parent_pod_reaped_under_critic.md.
        """
        if inst.metadata.get("has_live_shared_child"):
            return False
        ide_session_status = inst.metadata.get("ide_session_status")
        if ide_session_status in ("restoring", "active", "idle"):
            return False
        if inst.metadata.get("job_dispatchable"):
            return False
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            # Warm grace for 'paused' — see is_idle.
            if paused_within_grace(inst.metadata):
                return False
            # A job paused for a transient-infra retry is NOT idle — it is
            # mid-recovery and will resume onto this exact VM. Reaping it would
            # hand the retry an empty workspace, which is the loss the retry
            # exists to avoid. Bounded by next_retry_at (≤1h). (Defect 1b)
            if infra_transient_retry_pending(inst.metadata):
                return False
            return job_status in _REAPABLE_JOB_STATUSES
        if thread_status:
            return thread_status in _REAPABLE_THREAD_STATUSES
        return False

    def _is_terminal(self, inst: Instance) -> bool:
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            return job_status in _TERMINAL_JOB_STATUSES
        if thread_status:
            return thread_status in _TERMINAL_THREAD_STATUSES
        return False

    async def is_dirty(self, inst: Instance) -> bool:
        """True when the VM may hold un-snapshotted state worth saving.

        Same logic as the workspace manager (threads use ``total_turns`` vs
        ``last_snapshot_turns``; jobs are conservative), plus a VM wrinkle:
        ``vm_status == 'suspended'`` means the VM was already snapshotted to S3
        and torn down, so there is nothing left to lose — treat as clean.
        Never reads ``last_activity`` (contaminated by context merges).
        """
        if inst.metadata.get("vm_status") == "suspended":
            return False
        thread_status = inst.metadata.get("thread_status")
        if thread_status is not None:
            turns = inst.metadata.get("total_turns") or 0
            snap_turns = inst.metadata.get("last_snapshot_turns")
            if snap_turns is None:
                return turns > 0
            return turns > snap_turns
        if self._is_terminal(inst):
            return inst.metadata.get("snapshot_status") != "available"
        return True

    async def _tcp_probe(self, host: str, port: int) -> bool:
        """One-shot TCP connect with a short timeout. Overridable in tests."""

        def _connect() -> bool:
            try:
                with socket.create_connection((host, port), timeout=5):
                    return True
            except OSError:
                return False

        return await asyncio.to_thread(_connect)

    async def is_reachable(self, inst: Instance) -> bool:
        """Cached liveness probe to the VM's SSH endpoint.

        VMs run sshd on 22 by default (not 30022 like workspace pods). Used
        only in the reap path to choose snapshot-vs-retry; cached ~30s per host.
        """
        host = inst.metadata.get("ssh_host")
        if not host:
            return False
        port = int(inst.metadata.get("ssh_port") or 22)
        now = self._clock()
        cached = self._reach_cache.get(host)
        if cached is not None and (now - cached[0]) < self._reach_ttl_s:
            return cached[1]
        ok = await self._tcp_probe(host, port)
        self._reach_cache[host] = (now, ok)
        return ok

    def _max_attempts(self) -> int:
        try:
            return int(os.environ.get("WORKSPACE_SNAPSHOT_MAX_ATTEMPTS", "5"))
        except ValueError:
            return 5

    async def attempts_exhausted(self, inst: Instance) -> bool:
        # A tailnet-addressed VM the orchestrator has no route to can NEVER be
        # snapshotted from this vantage — every attempt is a guaranteed miss,
        # and the bounded retry just delays the force-delete by
        # max_attempts × tick (~5 min observed) while the dead VM holds cluster
        # capacity. Skip straight to exhausted. Honors the
        # ORCHESTRATOR_HAS_TAILNET_ROUTE escape hatch via orchestrator_can_reach
        # (see docs/done/golden_image_cold_import_fails_inflight_vm_jobs.md §C
        # and vm_ssh_readiness_probe_unroutable_from_orchestrator.md §F4).
        host = inst.metadata.get("ssh_host")
        if host and not orchestrator_can_reach(host):
            return True
        return (inst.metadata.get("snapshot_attempts") or 0) >= self._max_attempts()

    async def record_attempt(self, inst: Instance) -> None:
        """Persist an incremented snapshot-attempt counter to the bound VM ctx."""
        if self._db is None:
            return
        bound = inst.bound_to
        if not bound:
            return
        nxt = (inst.metadata.get("snapshot_attempts") or 0) + 1
        try:
            if inst.metadata.get("scope") == "thread":
                await self._db.merge_thread_vm_context(
                    bound, {"snapshot_attempts": nxt}
                )
            else:
                await self._db.merge_vm_context(bound, {"snapshot_attempts": nxt})
        except Exception:
            logger.exception("Failed to record snapshot attempt for VM %s", inst.id)

    async def give_up(self, inst: Instance, grace_s: int) -> None:
        """Escape hatch: dirty + unreachable + attempts exhausted.

        VMs have no volume-reattach (delete is destructive; disks live behind
        the external VM controller) — so the terminal action is force-delete.
        Volume-reattach for VMs is a separate, controller-side design.
        """
        await self.delete(inst, grace_s)

    async def snapshot(self, inst: Instance) -> str | None:
        if self._snapshot is None or not getattr(self._snapshot, "is_available", False):
            return None
        ssh_host = inst.metadata.get("ssh_host")
        ssh_port = inst.metadata.get("ssh_port") or 22
        if not ssh_host:
            return None
        bound = inst.bound_to
        if not bound:
            return None
        is_thread = inst.metadata.get("scope") == "thread"
        try:
            ok = await self._snapshot.capture_vm_snapshot(
                job_id=bound,
                ssh_host=ssh_host,
                ssh_port=int(ssh_port),
                source_type="vm",
                entity_type="threads" if is_thread else "jobs",
                work_marker=inst.metadata.get("total_turns"),
            )
            if ok:
                # Success clears the escape-hatch retry counter.
                try:
                    if is_thread:
                        await self._db.merge_thread_vm_context(
                            bound, {"snapshot_attempts": 0}
                        )
                    else:
                        await self._db.merge_vm_context(bound, {"snapshot_attempts": 0})
                except Exception:
                    logger.exception("Failed to reset attempts for VM %s", inst.id)
                return bound
            return None
        except Exception:
            logger.exception("Snapshot failed for VM %s (bound=%s)", inst.id, bound)
            return None

    async def restore(self, inst: Instance, snapshot_ref: str) -> None:
        # The suspension service dispatches to the right backend based
        # on the persisted ``provisioner`` field — same code path that
        # handles workspace restores. Phase 3 doesn't need its own
        # restore logic.
        if self._suspension is None or not getattr(
            self._suspension, "is_enabled", False
        ):
            return
        if inst.metadata.get("scope") == "thread":
            await self._suspension.restore_thread_workspace(snapshot_ref)
        else:
            await self._suspension.restore_workspace(snapshot_ref)

    async def signal_drain_pending(self, inst: Instance) -> None:
        """No-op: VMs have no in-pod drain hook (the management daemon
        listens on NATS but doesn't read intents from the orchestrator).
        Drift drain happens once the bound work becomes idle."""
        return None

    async def drain(self, inst: Instance, grace_s: int) -> None:
        await self.delete(inst, grace_s)

    async def delete(self, inst: Instance, grace_s: int) -> None:
        if not self._provisioner_available():
            return
        bound = inst.bound_to
        if not bound:
            logger.debug("delete skipped: no bound id for VM %s", inst.id)
            return
        try:
            if inst.metadata.get("scope") == "thread":
                await self._provisioner.delete_thread_vm(bound)
            else:
                await self._provisioner.delete_vm(bound)
        except Exception:
            logger.exception("Failed to delete VM %s", inst.id)

    async def reap_orphans(self) -> int:
        """Backstop GC: delete VMs whose owning row is gone from BOTH tables.

        The instance path enumerates VMs FROM the jobs/threads rows
        (``_fetch_vm_rows``), so a VM whose row was deleted never surfaces as
        an ``Instance`` — it is invisible, not merely un-reapable. Only the
        backend inventory (``vm_provisioner.list_vms``) can still see it.
        This once-per-tick sweep reaps a listed VM only when
          - its name encodes a valid UUID (``agent-vm-<uuid>``),
          - it is older than the shared orphan grace (never reap an in-flight
            provision whose row hasn't landed yet), and
          - the id has NO row in ``jobs`` NOR ``threads`` — a row of ANY
            status means the instance path / dispatcher owns the VM.
        A DB error skips the VM (unknown ≠ gone), and ``list_vms() is None``
        means the inventory is unavailable (old controller, docker pool) —
        return 0, do not treat as empty. Delete via the job-keyed path: the
        controller keys teardown purely by id (``agent-vm-<id>``), and with
        the row gone the job/thread distinction is unknowable and irrelevant.

        Delete-path coverage for VMs lives in ``delete_job`` (release while
        the row exists); this sweep closes the crash/failure window. See
        docs/done/deleted_job_orphans_workspace_pod.md.
        """
        if not self._provisioner_available() or self._db is None:
            return 0
        try:
            vms = await self._provisioner.list_vms()
        except Exception:
            logger.exception("VM orphan sweep: list failed")
            return 0
        if not vms:
            return 0
        reaped = 0
        for vm in vms:
            if not isinstance(vm, dict):
                continue
            entity_id = vm.get("entity_id") or ""
            try:
                uuid.UUID(entity_id)
            except (ValueError, AttributeError, TypeError):
                continue  # not one of ours — never touch it
            age = _vm_age_seconds(vm.get("created_at"))
            if age is None or age < orphan_grace_seconds():
                continue
            try:
                async with self._db.acquire() as conn:
                    row_exists = await conn.fetchval(
                        """
                        SELECT EXISTS (SELECT 1 FROM jobs WHERE id = $1::uuid)
                            OR EXISTS (SELECT 1 FROM threads WHERE id = $1::uuid)
                        """,
                        entity_id,
                    )
            except Exception:
                logger.exception(
                    "VM orphan sweep: row lookup failed for %s — skipping",
                    entity_id,
                )
                continue
            if row_exists:
                continue
            try:
                if await self._provisioner.delete_vm(entity_id):
                    reaped += 1
                    logger.warning(
                        "Orphan VM reaped: %s (no jobs/threads row, age %.0fs)",
                        vm.get("vm_name") or entity_id,
                        age,
                    )
            except Exception:
                logger.exception("Orphan VM delete failed: %s", entity_id)
        return reaped

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _provisioner_available(self) -> bool:
        return bool(getattr(self._provisioner, "is_available", False))

    async def _fetch_vm_rows(self) -> list[dict[str, Any]]:
        """Pull both job-bound and thread-bound VMs.

        VMs aren't a fleet-wide K8s object you can list_namespaced_pod;
        each backend (NATS / HTTP controller / KubeVirt / Docker)
        tracks them differently. The orchestrator's authoritative view
        is the ``vm`` JSONB on jobs and threads, so we read from there.
        """
        if self._db is None:
            return []
        rows: list[dict[str, Any]] = []
        try:
            async with self._db.acquire() as conn:
                job_rows = await conn.fetch(
                    """
                    SELECT id, status, context, updated_at, freeze_data,
                           (assigned_agent_id IS NULL) AS unassigned,
                           (freeze_data IS NULL) AS freeze_free
                    FROM jobs
                    WHERE context->'vm' IS NOT NULL
                      AND context->'vm' <> '{}'::jsonb
                    """
                )
                thread_rows = await conn.fetch(
                    """
                    SELECT id, status, total_turns, metadata
                    FROM threads
                    WHERE metadata->'vm' IS NOT NULL
                      AND metadata->'vm' <> '{}'::jsonb
                    """
                )
        except Exception:
            logger.exception("Failed to query VMs for lifecycle")
            return []

        for r in job_rows:
            ctx = _coerce_jsonb(r.get("context"))
            vm_ctx = _coerce_jsonb(ctx.get("vm"))
            if not vm_ctx:
                continue
            # Mirror get_dispatchable_jobs: created/paused + unassigned +
            # freeze-free == the dispatcher owns this VM's bring-up. Carried to
            # is_reapable so the reaper hands off a resuming VM (see there).
            dispatchable = (
                r.get("status") in ("created", "paused")
                and bool(r.get("unassigned"))
                and bool(r.get("freeze_free"))
            )
            rows.append(
                {
                    "scope": "job",
                    "bound_id": str(r["id"]),
                    "owner_status": r.get("status"),
                    "owner_updated_at": r.get("updated_at"),
                    "job_dispatchable": dispatchable,
                    # Carried for the infra_transient reap carve-out: a job
                    # paused for a DB-blip retry must keep its VM, or the retry
                    # resumes into an empty workspace. (Defect 1b)
                    "job_freeze_data": _coerce_jsonb(r.get("freeze_data")),
                    "vm_ctx": vm_ctx,
                    "ide_session_status": _coerce_jsonb(ctx.get("ide_session")).get(
                        "status"
                    ),
                    "snapshot_status": _coerce_jsonb(ctx.get("snapshot")).get("status"),
                }
            )
        for r in thread_rows:
            md = _coerce_jsonb(r.get("metadata"))
            vm_ctx = _coerce_jsonb(md.get("vm"))
            if not vm_ctx:
                continue
            rows.append(
                {
                    "scope": "thread",
                    "bound_id": str(r["id"]),
                    "owner_status": r.get("status"),
                    "vm_ctx": vm_ctx,
                    "ide_session_status": _coerce_jsonb(md.get("ide_session")).get(
                        "status"
                    ),
                    "total_turns": r.get("total_turns") or 0,
                    "snapshot_status": _coerce_jsonb(md.get("snapshot")).get("status"),
                }
            )
        return rows

    def _row_to_instance(self, row: dict[str, Any]) -> Instance | None:
        scope = row["scope"]
        bound_id = row["bound_id"]
        vm_ctx = row["vm_ctx"]
        # Already torn down / restorable-on-demand, or parked after a
        # provisioning failure → not a live instance the reconciler should act
        # on. Skip so reap/drift/crash don't loop on a VM whose teardown is
        # already in flight (_TORN_DOWN_VM_STATUSES) or that the dispatcher is
        # deliberately holding parked (_PARKED_VM_STATUSES).
        if vm_ctx.get("status") in _TORN_DOWN_VM_STATUSES | _PARKED_VM_STATUSES:
            return None
        # Identity: prefer a backend-native id when present, otherwise
        # synthesize one from scope+bound_id so the reconciler can
        # log/distinguish even before the backend assigns one.
        vm_native_id = (
            vm_ctx.get("vm_name") or vm_ctx.get("name") or vm_ctx.get("ssh_host")
        )
        inst_id = vm_native_id or f"vm-{scope}-{bound_id[:12]}"
        version = _extract_sha(vm_ctx.get("vm_image"))
        metadata: dict[str, Any] = {
            "scope": scope,
            # Backend-native id (None if only the synthesized fallback exists) —
            # the live-child guard matches critic subjobs on it. A critic
            # inherits the parent's context.vm, so its native id equals this
            # one; a delegation child on its own VM does not.
            "vm_native_id": vm_native_id,
            "vm_status": vm_ctx.get("status"),
            "ssh_host": vm_ctx.get("ssh_host") or vm_ctx.get("host"),
            "ssh_port": vm_ctx.get("ssh_port") or vm_ctx.get("port"),
            "provisioner": vm_ctx.get("provisioner"),
            "ide_session_status": row.get("ide_session_status"),
            # Reap-path inputs (mirror WorkspaceInstanceManager).
            "last_snapshot_turns": vm_ctx.get("last_snapshot_turns"),
            "snapshot_attempts": vm_ctx.get("snapshot_attempts") or 0,
            "snapshot_status": row.get("snapshot_status"),
        }
        if scope == "job":
            metadata["job_status"] = row.get("owner_status")
            metadata["job_updated_at"] = row.get("owner_updated_at")
            metadata["job_dispatchable"] = bool(row.get("job_dispatchable"))
            metadata["job_freeze"] = row.get("job_freeze_data") or {}
        else:
            metadata["thread_status"] = row.get("owner_status")
            metadata["total_turns"] = row.get("total_turns") or 0
        return Instance(
            kind=self.kind,
            id=inst_id,
            version=version,
            bound_to=bound_id,
            metadata=metadata,
        )

    async def _live_shared_child_exists(
        self, parent_job_id: str, vm_native_id: str | None
    ) -> bool:
        """True if a non-terminal child job shares this parent's VM.

        Mirrors ``WorkspaceInstanceManager._live_shared_child_exists``: a critic
        verification subjob inherits the parent's ``context.vm`` at spawn
        (``_trigger_verification_on_complete`` copies it) and SSHes into the
        *parent's* VM. Match on the VM's native id — the same
        ``vm_name / name / ssh_host`` COALESCE ``_row_to_instance`` uses for the
        instance id — which the critic's inherited copy reproduces exactly,
        whereas a delegation child (its own VM) does not, keeping the guard
        narrow. Fail-safe: on a DB error, assume shared (do not reap).
        """
        if self._db is None or not vm_native_id:
            return False
        try:
            async with self._db.acquire() as conn:
                return bool(
                    await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM jobs
                            WHERE parent_job_id = $1::uuid
                              AND status NOT IN ('completed', 'failed', 'cancelled')
                              AND COALESCE(
                                    context->'vm'->>'vm_name',
                                    context->'vm'->>'name',
                                    context->'vm'->>'ssh_host'
                                  ) = $2
                        )
                        """,
                        parent_job_id,
                        vm_native_id,
                    )
                )
        except Exception:
            logger.exception(
                "Failed to check live shared child for VM %s (parent %s); "
                "assuming shared — not reaping",
                vm_native_id,
                parent_job_id,
            )
            return True
