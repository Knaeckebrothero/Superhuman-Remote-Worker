"""Run-queue lease context shared by the stateless executor and fenced writers.

M3 of the stateless-agents S1 spine (knowledge-base/knowledge/features/stateless_agents.md §5.2):
while the stateless turn executor drives a claimed ``session_turn`` unit, every
persist to a *fenced* store (``thread_messages`` rows, the compaction
checkpoint row, the ``thread_events`` journal) must present the claim's
``(unit_id, lease_token)`` and abort when the fence rejects it. This tiny
module is the only shared surface between the executor
(``src/api/turn_executor.py``), the session app (``src/api/persistent_app.py``)
and the DB layer (``src/database/postgres_db.py``) — it deliberately imports
nothing from any of them, so it can sit below all three without cycles.

Why a mutable handle instead of a plain ``ContextVar[tuple]``: the persistent
loop task is created ONCE per attached session and then outlives claims
(soft affinity, §5.3.4 — the same pod re-claims the same thread and re-uses
the running loop). A ContextVar value is snapshotted into a task's context at
``create_task`` time, so an immutable tuple would pin the FIRST claim's token
into the loop forever and every later claim's persists would fence-fail. The
executor therefore installs one :class:`LeaseHandle` in its own context before
any task is spawned and mutates it per claim; fenced writers read the handle's
fields at write time.

The pinned lane never sets the ContextVar: :func:`get_current_lease` returns
``None`` and every fenced code path keeps today's behavior untouched.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Optional, Tuple


class LeaseLostError(RuntimeError):
    """A fenced persist found the run_queue lease gone (§5.2 fence contract).

    Raised by the DB layer when :func:`src.shared.run_queue.fence_lease`
    returns no row inside a persist transaction. The caller must treat it
    exactly like the heartbeat-lost path: abort the turn politely, persist
    nothing further, and call neither ``complete_unit`` nor ``release_unit``
    — the lease is already owned by someone else (or parked).
    """


class LeaseHandle:
    """Mutable ``(unit_id, lease_token)`` cell for the current executor claim.

    ``lost`` is an :class:`asyncio.Event` the executor waits on per claim; it
    is replaced (not just cleared) on every :meth:`update` so a stale set flag
    can never leak across claims. Any layer that discovers the lease is gone
    (heartbeat renewal returning nothing, a persist fence rejecting) calls
    :meth:`mark_lost`; the executor reacts by aborting the in-flight turn.
    """

    __slots__ = ("unit_id", "lease_token", "executor_id", "pod_uid", "lost")

    def __init__(self) -> None:
        self.unit_id: Optional[str] = None
        self.lease_token: int = 0
        self.executor_id: Optional[str] = None
        self.pod_uid: Optional[str] = None
        self.lost: asyncio.Event = asyncio.Event()

    def update(
        self,
        unit_id,
        lease_token: int,
        *,
        executor_id: str | None = None,
        pod_uid: str | None = None,
    ) -> None:
        """Point the handle at a freshly claimed lease (one call per claim)."""
        self.unit_id = str(unit_id)
        self.lease_token = int(lease_token)
        self.executor_id = str(executor_id).strip() if executor_id else None
        self.pod_uid = str(pod_uid).strip() if pod_uid else None
        # Fresh event per claim: waiters bind to the claim they serve.
        self.lost = asyncio.Event()

    def mark_lost(self) -> None:
        self.lost.set()

    @property
    def active(self) -> bool:
        return self.unit_id is not None


# The executor's root context sets this once; tasks it spawns (the persistent
# loop, the event writer, per-turn helpers) inherit the SAME handle object.
# Default None = pinned lane = no fencing anywhere.
current_lease: ContextVar[Optional[LeaseHandle]] = ContextVar(
    "current_lease", default=None
)


def get_current_lease() -> Optional[Tuple[str, int]]:
    """``(unit_id, lease_token)`` of the active claim, or ``None`` (pinned lane)."""
    handle = current_lease.get()
    if handle is None or handle.unit_id is None:
        return None
    return (handle.unit_id, handle.lease_token)


def mark_current_lease_lost() -> None:
    """Signal the executor (if any) that the active lease is gone."""
    handle = current_lease.get()
    if handle is not None:
        handle.mark_lost()
