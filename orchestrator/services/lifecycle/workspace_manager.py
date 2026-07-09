"""WorkspaceInstanceManager — Phase 2 stateful manager.

Wraps ``ContainerProvisioner``, ``WorkspaceSuspensionService``, and
``SnapshotService`` to surface workspace pods to the unified lifecycle
reconciler. Implements ``StatefulInstanceManager`` so the reconciler
calls ``snapshot()`` before any drift-driven ``drain()`` — the snapshot
ends up in S3, the pod gets deleted, and on next dispatch
``WorkspaceSuspensionService.restore_*`` rehydrates a fresh-version pod
from the same S3 reference.

Phase 2a scope: drift detection + snapshot/drain integration.
Phase 2b adds crash recovery for ``Unknown`` / ``Failed`` workspace
pods (the gap in ``docs/issues/stuck_thread_workspace_pods.md``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

from .types import Instance
from services.workspace_lifecycle import WorkspaceOwner

logger = logging.getLogger(__name__)


_LABEL_SELECTOR = "srw.io/component=agent-workspace"

# Sentinel distinguishing "the bound-row lookup failed" (DB error / no DB) from
# "the lookup succeeded and found no row". The distinction matters: a missing
# row means the job/thread was deleted and the pod is an orphan we may reap
# (age-gated), while a failed lookup means we know nothing and must not act.
# See docs/done/deleted_job_orphans_workspace_pod.md.
_FETCH_FAILED = object()

# 'reviewing' is the verification-enabled twin of 'pending_review'
# (determine_job_status sets it on a critic-enabled completion freeze). The
# parent has frozen, so by *status* it is as quiescent as 'pending_review' and
# belongs in the idle set. The catch: a critic subjob SSHes into the *parent's*
# live workspace pod (shared by design, to read the parent's output/), so
# reaping on status alone pulls the pod out from under a live critic → headless
# Service with zero endpoints → the critic's next SSH fails NXDOMAIN and the
# whole review dies. The is_idle/is_reapable predicates below therefore gate on
# ``has_live_shared_child`` (a non-terminal child bound to this same pod); the
# status set stays optimistic and the guard handles the dependency precisely.
# See docs/issues/reviewing_parent_pod_reaped_under_critic.md.
_IDLE_JOB_STATUSES = frozenset(
    {"paused", "pending_review", "reviewing", "waiting_for_reply"}
)
_IDLE_THREAD_STATUSES = frozenset({"ended"})

# Terminal = bound work is finished; nothing to preserve beyond an existing
# snapshot. Reapable = the pod is no longer needed at all — the union of
# suspendable-idle (snapshot + free) and terminal (clean up).
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_THREAD_STATUSES = frozenset({"ended"})
_REAPABLE_JOB_STATUSES = _IDLE_JOB_STATUSES | _TERMINAL_JOB_STATUSES
_REAPABLE_THREAD_STATUSES = _IDLE_THREAD_STATUSES | _TERMINAL_THREAD_STATUSES


def expected_workspace_shas() -> set[str]:
    """SHAs from the configured workspace image tag.

    Reads ``WORKSPACE_IMAGE`` and extracts the suffix from a
    ``...:sha-<hash>`` tag. Returns empty set for ``:latest`` or
    semver-style tags — the reconciler then skips drift checks.
    """
    shas: set[str] = set()
    tag = os.environ.get("WORKSPACE_IMAGE", "")
    if ":sha-" in tag:
        shas.add(tag.rsplit(":sha-", 1)[-1])
    return shas


def orphan_grace_seconds() -> float:
    """Minimum instance age before a missing-row pod/VM is treated as an orphan.

    Must comfortably exceed the create-instance → persist-context window during
    provisioning (seconds); anything shorter risks reaping an in-flight
    instance whose row simply hasn't landed yet. Shared by the workspace
    missing-row reap and the VM orphan sweep."""
    try:
        return float(os.environ.get("WORKSPACE_ORPHAN_GRACE_SECONDS", "900"))
    except ValueError:
        return 900.0


def _pod_age_seconds(pod: Any) -> float | None:
    """Pod age from creationTimestamp, or None when it can't be determined."""
    try:
        created = pod.metadata.creation_timestamp
        if not isinstance(created, datetime):
            return None
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
    except Exception:
        return None


def paused_grace_seconds() -> float:
    """Warm-grace window before a ``paused`` job's workspace becomes reapable.

    A pause is frequently a human-wait (sudo/VM-upgrade approval: 24 h TTL;
    review pauses) — reaping on the very next tick destroys the workspace
    minutes into that window (see
    docs/issues/vm_upgrade_pause_workspace_reaped_before_approval.md). Keep it
    warm for the grace so a fast decision resumes losslessly; a slow one pays
    a snapshot-restore. Defaults to the suspension sweep's
    ``WORKSPACE_IDLE_TIMEOUT`` (minutes, default 30) so the graceful
    snapshot-then-free path gets first claim on the workspace.
    """
    try:
        return float(os.environ["WORKSPACE_PAUSED_REAP_GRACE_S"])
    except (KeyError, ValueError):
        pass
    try:
        return float(os.environ.get("WORKSPACE_IDLE_TIMEOUT", "30")) * 60.0
    except ValueError:
        return 1800.0


def _paused_age_seconds(metadata: dict[str, Any]) -> float | None:
    """Seconds since the bound job's last row update (pause-time proxy).

    ``jobs.updated_at`` is bumped by the status flip that paused the job;
    later bookkeeping merges bump it too, which only ever EXTENDS the grace
    (activity-bumped, Coder-style). None when the timestamp is unavailable.
    """
    ts = metadata.get("job_updated_at")
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def paused_within_grace(metadata: dict[str, Any]) -> bool:
    """True while a ``paused`` job's workspace is still inside the warm grace.

    An unknown pause age counts as inside the grace (conservative: never
    destroy on missing data — mirrors the ``_FETCH_FAILED`` stance).
    """
    if metadata.get("job_status") != "paused":
        return False
    age = _paused_age_seconds(metadata)
    return age is None or age < paused_grace_seconds()


def _pod_volume_is_ephemeral(pod: Any) -> bool:
    """True if the pod's workspace-data volume is emptyDir (vs a PVC).

    Defaults to True (ephemeral) when the volume can't be read — matches the
    current fleet default and keeps the reaper conservative.
    """
    try:
        for vol in pod.spec.volumes or []:
            if getattr(vol, "name", None) == "workspace-data":
                if getattr(vol, "persistent_volume_claim", None) is not None:
                    return False
                return getattr(vol, "empty_dir", None) is not None
    except Exception:
        pass
    return True


class WorkspaceInstanceManager:
    """Lifecycle manager for the workspace kind."""

    kind = "workspace"

    def __init__(
        self,
        container_provisioner: Any,
        suspension_service: Any,
        snapshot_service: Any,
        db: Any,
        label_selector: str = _LABEL_SELECTOR,
    ):
        # Loose typing matches the agent manager — production passes the
        # singleton modules; tests pass mocks. Only the methods used here
        # need to exist on the underlying objects.
        self._provisioner = container_provisioner
        self._suspension = suspension_service
        self._snapshot = snapshot_service
        self._db = db
        self._label_selector = label_selector
        # Reachability probe cache: pod_ip -> (probed_at, ok). Single
        # orchestrator process, so a plain dict is sufficient.
        self._reach_cache: dict[str, tuple[float, bool]] = {}
        self._reach_ttl_s: float = 30.0
        self._clock = time.monotonic

    # -------------------------------------------------------------------------
    # Protocol implementation
    # -------------------------------------------------------------------------

    async def expected_versions(self) -> set[str]:
        return expected_workspace_shas()

    async def list_instances(self) -> list[Instance]:
        if not self._provisioner_ready():
            return []

        pods = await self._list_pods()
        instances: list[Instance] = []
        for pod in pods:
            labels = pod.metadata.labels or {}
            # Job workspaces carry srw/job-id; thread (session) workspaces
            # carry srw/thread-id. Check thread-id first so we route to the
            # right table.
            thread_id = labels.get("srw/thread-id")
            job_id = labels.get("srw/job-id") if not thread_id else None
            metadata: dict[str, Any] = {
                "pod_phase": pod.status.phase,
                "labels": dict(labels),
                "kind_label": labels.get("srw/component"),
                "volume_ephemeral": _pod_volume_is_ephemeral(pod),
                "pod_age_s": _pod_age_seconds(pod),
            }
            if thread_id:
                row = await self._fetch_thread(thread_id)
                if row is _FETCH_FAILED:
                    pass  # unknown state — leave metadata bare, never reap
                elif row is None:
                    metadata["bound_row_missing"] = True
                else:
                    metadata["thread_status"] = row.get("status")
                    metadata["total_turns"] = row.get("total_turns") or 0
                    md = row.get("metadata") or {}
                    if isinstance(md, str):
                        try:
                            md = json.loads(md)
                        except (json.JSONDecodeError, ValueError):
                            md = {}
                    ws = md.get("workspace_container") or {}
                    metadata["workspace_status"] = ws.get("status")
                    metadata["pod_ip"] = ws.get("pod_ip")
                    metadata["last_snapshot_turns"] = ws.get("last_snapshot_turns")
                    metadata["snapshot_attempts"] = ws.get("snapshot_attempts") or 0
                    snap = md.get("snapshot") or {}
                    metadata["snapshot_status"] = snap.get("status")
            elif job_id:
                row = await self._fetch_job(job_id)
                if row is _FETCH_FAILED:
                    pass  # unknown state — leave metadata bare, never reap
                elif row is None:
                    metadata["bound_row_missing"] = True
                else:
                    metadata["job_status"] = row.get("status")
                    metadata["job_updated_at"] = row.get("updated_at")
                    ctx = row.get("context") or {}
                    if isinstance(ctx, str):
                        try:
                            ctx = json.loads(ctx)
                        except (json.JSONDecodeError, ValueError):
                            ctx = {}
                    ws_ctx = ctx.get("workspace_container") or {}
                    metadata["workspace_status"] = ws_ctx.get("status")
                    metadata["pod_ip"] = ws_ctx.get("pod_ip")
                    metadata["snapshot_attempts"] = ws_ctx.get("snapshot_attempts") or 0
                    snap = ctx.get("snapshot") or {}
                    metadata["snapshot_status"] = snap.get("status")
                    # Only a reapable-status parent can be torn down, so only
                    # then does the live-child guard matter — skip the query
                    # otherwise. Keys the guard on the real dependency (a critic
                    # SSHed into this pod), not on job_status alone.
                    if metadata["job_status"] in _REAPABLE_JOB_STATUSES:
                        metadata[
                            "has_live_shared_child"
                        ] = await self._live_shared_child_exists(
                            job_id, pod.metadata.name
                        )

            instances.append(
                Instance(
                    kind=self.kind,
                    id=pod.metadata.name,
                    version=labels.get("srw/build-sha"),
                    bound_to=thread_id or job_id,
                    metadata=metadata,
                )
            )
        return instances

    async def is_healthy(self, inst: Instance) -> bool:
        # Phase 2a: phase Running is the cheap signal. Phase 2b adds a
        # crash detector that catches Unknown/Failed pods explicitly.
        return inst.metadata.get("pod_phase") in (None, "Running", "Pending")

    async def is_idle(self, inst: Instance) -> bool:
        """A workspace is drainable when its bound work is paused/ended.

        We don't gate on ``last_activity > IDLE_TIMEOUT`` here — that's
        the existing ``workspace_idle_sweeper``'s domain (suspension on
        no-traffic). For drift detection we want to react as soon as the
        bound work is in a quiescent state, regardless of how long.
        """
        if inst.metadata.get("has_live_shared_child"):
            return False
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            # 'paused' gets a warm grace: it is often a human-wait (sudo/VM
            # approval, 24h TTL) and acting on the next tick destroys the
            # workspace minutes into that window.
            if paused_within_grace(inst.metadata):
                return False
            return job_status in _IDLE_JOB_STATUSES
        if thread_status:
            return thread_status in _IDLE_THREAD_STATUSES
        # No bound row → not safe to claim idle (pod may have been
        # created but DB context not yet persisted).
        return False

    async def is_reapable(self, inst: Instance) -> bool:
        """True when the bound work no longer needs the pod.

        Superset of ``is_idle``: adds terminal job/thread states. Terminal
        instances get cleaned up; suspendable-idle ones get snapshot+freed.

        A pod whose bound row is confirmed gone (``bound_row_missing`` — the
        job/thread was deleted) is an orphan and reapable once the pod is
        older than the orphan grace period. The age gate protects the
        pod-created-but-row-not-yet-persisted provisioning window, which is
        seconds long — the grace default is minutes. A pod whose row state is
        merely *unknown* (lookup failed) is never reapable.
        See docs/done/deleted_job_orphans_workspace_pod.md.

        Guard: a workspace shared by a live child job (a critic SSHed into the
        parent's pod) is never reapable, regardless of the parent's own status —
        reaping would strand the child. See ``_live_shared_child_exists`` and
        docs/issues/reviewing_parent_pod_reaped_under_critic.md.
        """
        if inst.metadata.get("has_live_shared_child"):
            return False
        if inst.metadata.get("bound_row_missing"):
            age = inst.metadata.get("pod_age_s")
            return age is not None and age >= self._orphan_grace_s()
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            # Warm grace for 'paused' — see is_idle.
            if paused_within_grace(inst.metadata):
                return False
            return job_status in _REAPABLE_JOB_STATUSES
        if thread_status:
            return thread_status in _REAPABLE_THREAD_STATUSES
        return False

    def _is_terminal(self, inst: Instance) -> bool:
        """Bound work is finished (vs merely paused) — nothing to preserve
        beyond an existing snapshot. A deleted row is definitively finished,
        so orphans count as terminal (delete() then reclaims PVC + Service,
        and give_up() won't recreate the pod)."""
        if inst.metadata.get("bound_row_missing"):
            return True
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            return job_status in _TERMINAL_JOB_STATUSES
        if thread_status:
            return thread_status in _TERMINAL_THREAD_STATUSES
        return False

    async def is_dirty(self, inst: Instance) -> bool:
        """True when the workspace may hold un-snapshotted state worth saving.

        Threads: precise — current ``total_turns`` vs the turn count recorded
        at last snapshot (``last_snapshot_turns``). Zero turns, or turns equal
        to the snapshot, means clean.

        Jobs: no monotonic turn counter exists in the app DB (audit count is in
        the audit store — deliberately not consulted here). Conservative: a terminal job
        with an existing snapshot is clean (it got a completion capture);
        otherwise dirty (attempt a snapshot; the escape hatch bounds the
        unreachable case).

        NOTE: never reads ``last_activity`` — it is bumped by the orchestrator's
        own context merges and cannot distinguish real work from bookkeeping.
        """
        # Orphan (bound row deleted): nothing is worth saving — no entity can
        # ever restore the snapshot, and record_attempt would merge into a
        # deleted row (silent no-op), retrying forever. Clean → direct delete.
        if inst.metadata.get("bound_row_missing"):
            return False
        thread_status = inst.metadata.get("thread_status")
        if thread_status is not None:
            turns = inst.metadata.get("total_turns") or 0
            snap_turns = inst.metadata.get("last_snapshot_turns")
            if snap_turns is None:
                return turns > 0
            return turns > snap_turns
        # Job path: no turn counter.
        if self._is_terminal(inst):
            return inst.metadata.get("snapshot_status") != "available"
        return True

    async def is_state_ephemeral(self, inst: Instance) -> bool:
        """True when pod-local storage dies with the pod (emptyDir).

        Ephemeral → a crashed/unreachable pod's state is unrecoverable, so the
        terminal action is delete-the-tombstone. PVC-backed → state survives on
        the volume; the terminal action is recreate-pod-keep-PVC. Defaults to
        ephemeral (today's fleet default) when the volume mode is unknown.
        """
        return bool(inst.metadata.get("volume_ephemeral", True))

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
        """Cached liveness probe to the pod's SSH port (30022).

        Used ONLY in the reap path to choose snapshot-vs-retry — never in
        ``is_healthy`` (an unreachable busy pod must not be force-deleted over
        a transient blip). Cached ~30s per pod IP.
        """
        host = inst.metadata.get("pod_ip")
        if not host:
            return False
        now = self._clock()
        cached = self._reach_cache.get(host)
        if cached is not None and (now - cached[0]) < self._reach_ttl_s:
            return cached[1]
        ok = await self._tcp_probe(host, 30022)
        self._reach_cache[host] = (now, ok)
        return ok

    def _max_attempts(self) -> int:
        try:
            return int(os.environ.get("WORKSPACE_SNAPSHOT_MAX_ATTEMPTS", "5"))
        except ValueError:
            return 5

    def _orphan_grace_s(self) -> float:
        return orphan_grace_seconds()

    async def attempts_exhausted(self, inst: Instance) -> bool:
        return (inst.metadata.get("snapshot_attempts") or 0) >= self._max_attempts()

    async def record_attempt(self, inst: Instance) -> None:
        """Persist an incremented snapshot-attempt counter to the bound row."""
        if self._db is None:
            return
        bound = inst.bound_to
        if not bound:
            return
        nxt = (inst.metadata.get("snapshot_attempts") or 0) + 1
        labels = inst.metadata.get("labels") or {}
        try:
            if "srw/thread-id" in labels:
                await self._db.merge_thread_workspace_context(
                    bound, {"snapshot_attempts": nxt}
                )
            else:
                await self._db.merge_workspace_container_context(
                    bound, {"snapshot_attempts": nxt}
                )
        except Exception:
            logger.exception("Failed to record snapshot attempt for %s", inst.id)

    async def give_up(self, inst: Instance, grace_s: int) -> None:
        """Escape hatch: dirty + unreachable + attempts exhausted.

        Ephemeral storage → delete the pod (state already unrecoverable).
        PVC-backed + non-terminal → recreate the pod against the same PVC so the
        volume reattaches; the PVC is NOT deleted. PVC-backed + terminal → the
        ``delete`` call below already reclaimed the PVC, so do NOT recreate
        (recreating a pod for finished work would be pointless and would race
        that delete). Branch (a): see workspace_pvc_branch_a_implementation.md.
        """
        bound = inst.bound_to
        if not bound:
            return
        labels = inst.metadata.get("labels") or {}
        owner = (
            WorkspaceOwner.session(bound)
            if "srw/thread-id" in labels
            else WorkspaceOwner.job(bound)
        )
        await self.delete(inst, grace_s)
        if not self._is_terminal(inst) and not inst.metadata.get(
            "volume_ephemeral", True
        ):
            try:
                await self._provisioner.create_workspace(owner)
            except Exception:
                logger.exception("PVC give_up recreate failed for %s", inst.id)

    async def snapshot(self, inst: Instance) -> str | None:
        """Capture the workspace contents to S3.

        Returns a snapshot reference token (currently the bound id —
        SnapshotService keys by job/thread id), or ``None`` if the
        snapshot path isn't usable for this instance.
        """
        if self._snapshot is None or not getattr(self._snapshot, "is_available", False):
            return None
        ssh_host = inst.metadata.get("pod_ip")
        if not ssh_host:
            return None
        bound = inst.bound_to
        if not bound:
            return None
        try:
            ok = await self._snapshot.capture_vm_snapshot(
                job_id=bound,
                ssh_host=ssh_host,
                ssh_port=30022,
                source_type="pod",
                entity_type=(
                    "threads"
                    if "srw/thread-id" in (inst.metadata.get("labels") or {})
                    else "jobs"
                ),
                work_marker=inst.metadata.get("total_turns"),
            )
            if ok:
                # Mark the in-memory instance too — the reconciler calls
                # delete() right after a successful snapshot(), and delete()
                # reads this to decide the reap-and-restore handoff below.
                inst.metadata["snapshot_status"] = "available"
                # Success clears the escape-hatch retry counter.
                labels = inst.metadata.get("labels") or {}
                try:
                    if "srw/thread-id" in labels:
                        await self._db.merge_thread_workspace_context(
                            bound, {"snapshot_attempts": 0}
                        )
                    else:
                        await self._db.merge_workspace_container_context(
                            bound, {"snapshot_attempts": 0}
                        )
                except Exception:
                    logger.exception("Failed to reset attempts for %s", inst.id)
                return bound
            return None
        except Exception:
            logger.exception(
                "Snapshot failed for workspace %s (bound=%s)", inst.id, bound
            )
            return None

    async def restore(self, inst: Instance, snapshot_ref: str) -> None:
        """Restore the workspace from a snapshot reference.

        Phase 2a delegates to the existing suspension service so the
        provisioner-dispatch logic (K8s/Docker/VM) is shared. The
        ``snapshot_ref`` is the bound job/thread id — that's how the
        suspension service keys the restore.
        """
        if self._suspension is None or not getattr(
            self._suspension, "is_enabled", False
        ):
            return
        if "srw/thread-id" in inst.metadata.get("labels", {}):
            await self._suspension.restore_thread_workspace(snapshot_ref)
        else:
            await self._suspension.restore_workspace(snapshot_ref)

    async def signal_drain_pending(self, inst: Instance) -> None:
        """No-op: workspaces have no in-pod drain hook to react to a
        soft signal. The reconciler picks them up on a future tick
        when the bound job/thread becomes idle (paused/ended), and
        ``drain`` actuates immediately at that point."""
        return None

    async def drain(self, inst: Instance, grace_s: int) -> None:
        """Delete the pod (snapshot has already run when reached via tick).

        The reconciler tick calls ``snapshot()`` before ``drain()`` for
        stateful kinds, so this method is the pure delete step. Direct
        delete calls (via the orchestrator's ad-hoc paths) still go
        through the existing services and don't pass through here.
        """
        await self.delete(inst, grace_s)

    async def delete(self, inst: Instance, grace_s: int) -> None:
        if not self._provisioner_ready():
            return
        bound = inst.bound_to
        if not bound:
            logger.debug("delete skipped: no bound job/thread for %s", inst.id)
            return
        labels = inst.metadata.get("labels") or {}
        owner = (
            WorkspaceOwner.session(bound)
            if "srw/thread-id" in labels
            else WorkspaceOwner.job(bound)
        )
        try:
            await self._provisioner.delete_workspace(owner)
        except Exception:
            logger.exception("Failed to delete workspace pod %s", inst.id)
            return
        # Reap-and-restore handoff: a NON-terminal emptyDir workspace whose
        # state was captured to S3 is 'suspended', not 'deleted' — the next
        # dispatch then routes through the suspension restore
        # (ensure_workspace: 'suspended' → restore) instead of re-creating a
        # blank pod, so a paused job (e.g. waiting on a sudo/VM-upgrade
        # decision) resumes with its files. PVC-backed pods skip this: their
        # state survives on the volume and an S3 extract could roll newer
        # files back. The provisioner wrote 'deleted' above; overwrite it.
        # See docs/issues/vm_upgrade_pause_workspace_reaped_before_approval.md.
        if (
            not self._is_terminal(inst)
            and inst.metadata.get("volume_ephemeral", True)
            and inst.metadata.get("snapshot_status") == "available"
        ):
            try:
                if "srw/thread-id" in labels:
                    await self._db.merge_thread_workspace_context(
                        bound, {"status": "suspended"}
                    )
                else:
                    await self._db.merge_workspace_container_context(
                        bound, {"status": "suspended"}
                    )
                logger.info(
                    "Reaped workspace %s marked 'suspended' (snapshot in S3) — "
                    "next dispatch restores instead of recreating blank",
                    inst.id,
                )
            except Exception:
                logger.exception("Failed to mark %s suspended after reap", inst.id)
        # PVC GC (Branch a leak guard): a PVC-backed workspace keeps its volume
        # across pod recreates (suspend/restore, drift recovery, give_up
        # reattach), so we reclaim it ONLY when the bound work is terminal —
        # completed/failed/cancelled job, ended thread. That is the
        # "PVC dies when the job dies" guard the emptyDir-era simplification
        # asked for. emptyDir instances have no PVC (skip); a missing PVC is an
        # idempotent 404. The backstop reap_orphans() sweep covers the cases
        # this inline path can miss (pod already gone, delete failed, restart).
        if self._is_terminal(inst) and not inst.metadata.get("volume_ephemeral", True):
            try:
                await self._provisioner.delete_workspace_pvc(owner)
                logger.info(
                    "Terminal workspace PVC reclaimed for %s %s",
                    owner.kind,
                    owner.id,
                )
            except Exception:
                logger.exception("Failed to delete terminal PVC for %s", inst.id)
            # The stable-DNS Service shares the PVC's lifecycle — reclaim it too.
            try:
                await self._provisioner._delete_service(owner)
            except Exception:
                logger.exception("Failed to delete terminal Service for %s", inst.id)

    async def reap_orphans(self) -> int:
        """Backstop GC: delete job workspace PVCs whose job is terminal or gone.

        The inline terminal delete (``delete()``) handles the common path, but a
        PVC can outlive its pod — the pod was already gone when teardown ran, the
        inline delete failed, or the orchestrator restarted mid-teardown. Such a
        PVC never surfaces as a live ``Instance`` (it has no pod), so the
        reconciler's per-instance reap can't see it. This once-per-tick sweep
        lists job workspace PVCs directly and deletes any whose owning job is
        terminal (completed/failed/cancelled) or no longer exists, AND that has
        no live pod. emptyDir fleets have no such PVCs → no-op. Runs regardless
        of ``WORKSPACE_PVC_ENABLED`` so a rollback (flag flipped off) still
        drains leftover PVCs as their jobs finish.

        Returns the number of PVCs deleted.
        """
        if not self._provisioner_ready() or self._db is None:
            return 0
        core = self._provisioner._core_api
        ns = self._provisioner._namespace
        try:
            pvcs = await asyncio.to_thread(
                core.list_namespaced_persistent_volume_claim,
                namespace=ns,
                label_selector=self._label_selector,
            )
        except Exception:
            logger.exception("Orphan PVC sweep: list failed")
            return 0
        items = list(getattr(pvcs, "items", []) or [])
        if not items:
            return 0
        # One pod list → the set of job ids that still have a live workspace
        # pod. Never reap a PVC out from under a running pod; the instance path
        # tears down pod+PVC together for those.
        live_job_ids = {
            (p.metadata.labels or {}).get("srw/job-id") for p in await self._list_pods()
        }
        live_job_ids.discard(None)
        reaped = 0
        for pvc in items:
            name = pvc.metadata.name
            job_id = (pvc.metadata.labels or {}).get("srw/job-id")
            # v1 scope: job workspace PVCs only (pvc-workspace-*). Skip the
            # shared agent scratch PVC, session PVCs, and anything unlabeled.
            if not job_id or not name.startswith("pvc-workspace-"):
                continue
            if job_id in live_job_ids:
                continue
            # Resolve existence/status with a DIRECT query that separates the
            # three cases — a transient DB error must NOT look like "job gone"
            # and trigger a wrong delete. row present → use status; no row →
            # genuinely gone (reap); query raised → unknown (skip).
            try:
                async with self._db.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT status FROM jobs WHERE id = $1::uuid", job_id
                    )
            except Exception:
                logger.exception(
                    "Orphan PVC sweep: job lookup failed for %s — skipping", job_id
                )
                continue
            status = row["status"] if row else None
            if row is not None and status not in _TERMINAL_JOB_STATUSES:
                continue  # job still active → keep its volume
            try:
                if await self._provisioner._delete_pvc(name):
                    reaped += 1
                    logger.info(
                        "Orphan workspace PVC reaped: %s (job=%s status=%s)",
                        name,
                        job_id,
                        status or "gone",
                    )
            except Exception:
                logger.exception("Orphan PVC delete failed: %s", name)
            # Reclaim the stable-DNS Service for the same orphan (shares the PVC
            # lifecycle). The Service name == the workspace pod name.
            try:
                await self._provisioner._delete_service(WorkspaceOwner.job(job_id))
            except Exception:
                logger.exception("Orphan Service delete failed for job %s", job_id)
        if reaped:
            logger.warning(
                "Orphan PVC sweep reclaimed %d workspace PVC(s) — inline "
                "terminal-delete missed them (pod gone / delete failed / restart)",
                reaped,
            )
        return reaped

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _provisioner_ready(self) -> bool:
        return bool(getattr(self._provisioner, "_k8s_available", False))

    async def _list_pods(self) -> list[Any]:
        try:
            result = await asyncio.to_thread(
                self._provisioner._core_api.list_namespaced_pod,
                namespace=self._provisioner._namespace,
                label_selector=self._label_selector,
            )
        except Exception:
            logger.exception("Failed to list workspace pods for lifecycle")
            return []
        return list(result.items)

    async def _fetch_job(self, job_id: str) -> dict[str, Any] | None | Any:
        """Bound-row lookup: the row, None when confirmed absent, or
        ``_FETCH_FAILED`` when the lookup itself failed (DB error / no DB)."""
        if self._db is None:
            return _FETCH_FAILED
        try:
            return await self._db.get_job(job_id)
        except Exception:
            logger.exception("Failed to fetch job %s for workspace lifecycle", job_id)
            return _FETCH_FAILED

    async def _fetch_thread(self, thread_id: str) -> dict[str, Any] | None | Any:
        """Same three-way contract as ``_fetch_job``."""
        if self._db is None:
            return _FETCH_FAILED
        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, status, ended_at, agent_id, total_turns, metadata "
                    "FROM threads WHERE id = $1",
                    thread_id,
                )
            return dict(row) if row else None
        except Exception:
            logger.exception(
                "Failed to fetch thread %s for workspace lifecycle", thread_id
            )
            return _FETCH_FAILED

    async def _live_shared_child_exists(
        self, parent_job_id: str, pod_name: str | None
    ) -> bool:
        """True if a non-terminal child job shares this parent's workspace pod.

        A critic verification subjob inherits the parent's ``workspace_container``
        at spawn (``_trigger_verification_on_complete`` copies it) and SSHes into
        the *parent's* pod instead of getting its own. While such a child is
        alive, reaping the parent pod strands it (headless Service with zero
        endpoints → NXDOMAIN), which is the P0 bug. Match on the inherited
        ``pod_name`` — stable (unlike ``pod_ip``, which churns on restore) and
        exact: the critic's copy equals this pod's name, whereas a delegation
        child (which gets its own pod) does not, so the guard stays narrow.

        Fail-safe: on a DB error, assume a child is present (do not reap) —
        mirrors ``reap_orphans``' "DB error → don't delete" stance.
        """
        if self._db is None or not pod_name:
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
                              AND context->'workspace_container'->>'pod_name' = $2
                        )
                        """,
                        parent_job_id,
                        pod_name,
                    )
                )
        except Exception:
            logger.exception(
                "Failed to check live shared child for pod %s (parent %s); "
                "assuming shared — not reaping",
                pod_name,
                parent_job_id,
            )
            return True
