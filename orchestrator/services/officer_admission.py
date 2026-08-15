"""Officer dispatch admission — the one gate every officer-created job passes.

Before this module the admission rules (hold fence, lineage-aware in-flight
count, slot roster check, server-side config stamp) lived inline in the
``POST /api/jobs`` handler. That was fine while the endpoint was the only way an
officer could start work. The auto-pull tick is a second way, and a second copy
of these rules would drift — quietly, in the direction of dispatching more
(docs/features/officer_backlog_pools.md §5.4).

Two entry points over one body:

* :func:`admit_in_transaction` — the caller already holds a transaction and
  wants the advisory lock to still be held when its own INSERT lands. This is
  what makes the tick's claim+create atomic.
* :func:`admit` — the endpoint's shape: opens its own short transaction, then
  hands the result back for a create that happens afterwards.

**The in-flight predicate is every non-terminal status, not the funnel's old
``('created','processing')``.** A paused or pending-review job still occupies
its slot, because it still owns the work: the alternative lets a second executor
start on a story the first one is halfway through and merely stalled on. The
direction of the change is deliberate — under-use converges back down when the
officer disposes of the stalled job, two executors on one surface do not
converge at all. It also makes the capacity predicate equal the claim predicate,
which is what keeps the tick and the endpoint from disagreeing about whether a
slot is free.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from services.officer_slots import SlotAdmissionError
from services.officer_slots import admit as admit_slot

logger = logging.getLogger(__name__)

# Occupying a slot means "this job still owns its work". Mirrors
# job_liveness.TERMINAL_STATUSES; kept as its own constant so a change here is
# a deliberate change to capacity, not a side effect of a liveness edit.
TERMINAL_JOB_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")

# Rendered once from the constant above, never hand-typed a second time — the
# same discipline project_backlog.py applies to its note-type list, and for the
# same reason: a second copy drifts and the drift is invisible.
_TERMINAL_STATUSES_SQL = "(" + ", ".join(f"'{s}'" for s in TERMINAL_JOB_STATUSES) + ")"

OFFICER_HELD_MESSAGE = (
    "conference in progress — this officer is held; scheduling resumes with "
    "the session brief after the conference ends."
)


def officer_is_held(officer_meta: dict[str, Any]) -> bool:
    """True while the Legate has this officer held (conference fence).

    Read straight off the officer's own metadata rather than inferred from a
    refused dispatch: the tick must skip a held officer silently, and bouncing
    off the endpoint's 409 to discover that would be both wasteful and wrong
    (it cannot tell "held" from "out of capacity").
    """
    return bool(officer_meta.get("hold"))


async def count_in_flight_by_slot(
    conn: Any, capacity_lineage: Sequence[Any]
) -> dict[str | None, int]:
    """In-flight job count per slot name across the post's whole lineage.

    Lineage, not thread: jobs created by a prior incarnation of this post keep
    occupying their slots across a decommission → recommission, or the century
    silently doubles its capacity the moment a new officer is commissioned
    (officer_post.md §4).
    """
    rows = await conn.fetch(
        f"""
        SELECT context->>'officer_slot' AS slot,
               COUNT(*) AS n
          FROM jobs
         WHERE created_by_thread_id = ANY($1::uuid[])
           AND status NOT IN {_TERMINAL_STATUSES_SQL}
         GROUP BY 1
        """,
        list(capacity_lineage),
    )
    return {r["slot"]: int(r["n"]) for r in rows}


async def admit_in_transaction(
    conn: Any,
    *,
    thread_id: str,
    officer_meta: dict[str, Any],
    capacity_lineage: Sequence[Any],
    requested_slot: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """Lock, count, admit — inside the caller's transaction.

    Returns ``(slot_name, config_patch)``. Raises :class:`SlotAdmissionError`
    when the roster has no room; the caller decides whether that is a 409 (the
    endpoint) or a skipped pool this tick (the tick).

    The advisory lock is keyed on the officer's thread id, so ordinary job
    creates never contend and parallel creates from ONE officer serialize. It
    is an xact lock: it releases when the caller's transaction closes, which is
    precisely why the tick keeps that transaction open through its INSERT.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", thread_id)
    in_flight = await count_in_flight_by_slot(conn, capacity_lineage)
    return admit_slot(officer_meta, requested_slot, in_flight)


async def admit(
    db: Any,
    *,
    thread_id: str,
    officer_meta: dict[str, Any],
    capacity_lineage: Sequence[Any],
    requested_slot: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """:func:`admit_in_transaction` with its own short transaction.

    The endpoint's shape. Note what it does NOT provide: the lock is gone by
    the time the caller inserts its job, so this path still has check-then-
    insert daylight. That is pre-existing and bounded (one officer, one HTTP
    request at a time in practice); the tick, which runs unattended on every
    replica, uses the transactional form plus the ticket-claim unique index
    instead of relying on it.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            return await admit_in_transaction(
                conn,
                thread_id=thread_id,
                officer_meta=officer_meta,
                capacity_lineage=capacity_lineage,
                requested_slot=requested_slot,
            )


__all__ = [
    "OFFICER_HELD_MESSAGE",
    "SlotAdmissionError",
    "TERMINAL_JOB_STATUSES",
    "admit",
    "admit_in_transaction",
    "count_in_flight_by_slot",
    "officer_is_held",
]
