"""Owner-keyed workspace lifecycle: one provisioning path for jobs and sessions.

See docs/features/unified_workspace_provisioning.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

OwnerKind = Literal["job", "session"]


@dataclass(frozen=True)
class WorkspaceOwner:
    """Identifies whose workspace this is. Collapses the job/thread split."""

    kind: OwnerKind
    id: str

    @classmethod
    def job(cls, job_id: str) -> "WorkspaceOwner":
        return cls("job", job_id)

    @classmethod
    def session(cls, thread_id: str) -> "WorkspaceOwner":
        return cls("session", thread_id)

    @property
    def pod_name(self) -> str:
        prefix = "workspace" if self.kind == "job" else "ws-thread"
        return f"{prefix}-{self.id[:12]}"

    @property
    def label_key(self) -> str:
        return "srw/job-id" if self.kind == "job" else "srw/thread-id"

    @property
    def component_label(self) -> str:
        return "workspace" if self.kind == "job" else "thread-workspace"

    @property
    def network_tier_kind(self) -> str:
        # Arg expected by ContainerProvisioner._resolve_network_tier / DB.
        return "job" if self.kind == "job" else "thread"


class EnsureOutcome(Enum):
    READY = "ready"  # workspace usable now → caller may dispatch
    PENDING = "pending"  # in progress (creating/restoring/created) → caller retries next cycle
    FAILED = "failed"  # creation failed / terminal → caller decides policy


@dataclass
class EnsureResult:
    outcome: EnsureOutcome
    status: Optional[str] = None


async def _create(owner: "WorkspaceOwner", provisioner, ws_config) -> "EnsureResult":
    ok = await provisioner.create_workspace(owner, **(ws_config or {}))
    return EnsureResult(
        EnsureOutcome.PENDING if ok else EnsureOutcome.FAILED,
        status="creating" if ok else "failed",
    )


async def ensure_workspace(
    owner: "WorkspaceOwner",
    *,
    provisioner,
    suspension,
    current_status: Optional[str],
    ws_config: Optional[dict] = None,
) -> "EnsureResult":
    """Idempotently drive owner's workspace toward 'ready'. Owner-agnostic
    extraction of the job dispatcher's container branch (main.py).

    Behavior notes (intentional):
    * 'failed' is owner-aware: SESSIONS self-heal (recreate); JOBS surface FAILED
      so the dispatcher fails the job — preserving the original dispatcher behavior
      (a job with a failed workspace fails; it does not silently retry forever).
    * 'suspended' kicks off restore as a FIRE-AND-FORGET task (restore is slow:
      pod create + SSH snapshot extract) and returns PENDING — matching the
      original `asyncio.create_task(restore_workspace(...))`; awaiting would block
      the dispatcher loop.
    * No drift check (status 'ready' → READY unconditionally) — matches the original
      dispatcher, which did no live-pod probe on 'ready'. (Drift recovery is a
      deferred enhancement.)
    """
    s = current_status
    if s in (None, "", "deleted", "none"):
        # No live workspace → (re)create one (both kinds).
        return await _create(owner, provisioner, ws_config)
    if s == "failed":
        if owner.kind == "session":
            return await _create(owner, provisioner, ws_config)
        return EnsureResult(EnsureOutcome.FAILED, status="failed")
    if s == "suspended":
        asyncio.create_task(suspension.restore(owner))
        return EnsureResult(EnsureOutcome.PENDING, status="restoring")
    # "Already progressing" set — keep in sync with the NOT IN clause in
    # PostgresDB.list_threads_needing_workspace (database/postgres.py).
    if s in ("created", "creating", "restoring", "suspending", "pending"):
        return EnsureResult(EnsureOutcome.PENDING, status=s)
    if s == "ready":
        # Drift check (design: "ready, pod missing → treat as failed →
        # recreate"). The DB says ready, but the pod may be gone (out-of-band
        # delete / node restart) or a dead tombstone. Probe liveness and
        # recreate ONLY on a confirmed-dead pod; unknown / non-k8s / transient
        # (probe returns None) keeps trusting 'ready' so a blip never
        # false-recreates a healthy workspace.
        probe = getattr(provisioner, "workspace_pod_live", None)
        if probe is not None and await probe(owner) is False:
            return await _create(owner, provisioner, ws_config)
        return EnsureResult(EnsureOutcome.READY, status="ready")
    # Unknown / unexpected status — wait (the dispatcher skips and retries).
    return EnsureResult(EnsureOutcome.PENDING, status=s)
