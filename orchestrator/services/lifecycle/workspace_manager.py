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
from typing import Any

from .types import Instance
from services.workspace_lifecycle import WorkspaceOwner

logger = logging.getLogger(__name__)


_LABEL_SELECTOR = "srw.io/component=agent-workspace"

_IDLE_JOB_STATUSES = frozenset({"paused", "pending_review", "waiting_for_reply"})
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
            }
            if thread_id:
                row = await self._fetch_thread(thread_id)
                if row is not None:
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
                if row is not None:
                    metadata["job_status"] = row.get("status")
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
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
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
        A pod with no bound row is never reapable (context may be in flight).
        """
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            return job_status in _REAPABLE_JOB_STATUSES
        if thread_status:
            return thread_status in _REAPABLE_THREAD_STATUSES
        return False

    def _is_terminal(self, inst: Instance) -> bool:
        """Bound work is finished (vs merely paused) — nothing to preserve
        beyond an existing snapshot."""
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

        Jobs: no monotonic turn counter exists in Postgres (audit count is in
        Mongo — deliberately not consulted here). Conservative: a terminal job
        with an existing snapshot is clean (it got a completion capture);
        otherwise dirty (attempt a snapshot; the escape hatch bounds the
        unreachable case).

        NOTE: never reads ``last_activity`` — it is bumped by the orchestrator's
        own context merges and cannot distinguish real work from bookkeeping.
        """
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
            )
            return bound if ok else None
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
        try:
            if "srw/thread-id" in labels:
                await self._provisioner.delete_workspace(WorkspaceOwner.session(bound))
            else:
                await self._provisioner.delete_workspace(WorkspaceOwner.job(bound))
        except Exception:
            logger.exception("Failed to delete workspace pod %s", inst.id)

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

    async def _fetch_job(self, job_id: str) -> dict[str, Any] | None:
        if self._db is None:
            return None
        try:
            row = await self._db.get_job(job_id)
            return row
        except Exception:
            logger.exception("Failed to fetch job %s for workspace lifecycle", job_id)
            return None

    async def _fetch_thread(self, thread_id: str) -> dict[str, Any] | None:
        if self._db is None:
            return None
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
            return None
