"""Result types for the run_queue contract (stateless_agents.md §5.1/§5.2).

Plain frozen dataclasses mirroring exactly what the SQL in ``queries.py``
returns. No behavior lives here — the invariants are enforced by the SQL and
documented on the functions that produce these values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ClaimedUnit:
    """One successful claim: the lease the executor now holds.

    ``lease_token`` is the fencing token every subsequent persist/heartbeat/
    complete/release call must present. ``input_seq``/``consumed_seq`` are the
    watermarks *as read atomically at claim time* — the caller compares them
    BEFORE invoking the LLM (skip-if-answered, §5.1) and passes the input
    watermark it actually answered back to ``complete_unit``.
    """

    unit_id: UUID
    unit_kind: str
    fair_key: str | None
    lease_token: int
    input_seq: int | None
    consumed_seq: int | None
    attempts_since_completion: int
    leased_until: datetime
    control_input_seq: int = 0
    control_consumed_seq: int = 0


@dataclass(frozen=True, slots=True)
class StolenUnit:
    """One lease stolen by the reaper.

    ``leased_by``, ``previous_lease_token``, and
    ``interrupt_admission_turn_id`` describe the *previous* holder, read just
    before the steal; ``lease_token`` and ``state`` are the post-steal values.
    The old token lets callers terminalize exact-lease inbox rows without
    deriving it from the new token, while the captured admission turn lets a
    crash frame identify its target even when no request row committed.
    The caller — not this module — bumps ``threads.events_epoch`` and writes
    the ``turn.interrupted`` / ``turn.parked`` system frames from this record
    (layering: run_queue never touches threads/thread_events).
    """

    unit_id: UUID
    unit_kind: str
    state: str
    attempts_since_completion: int
    leased_by: str | None
    lease_token: int
    previous_lease_token: int | None = None
    interrupt_admission_turn_id: int | None = None
    lifecycle_journaled: bool = False


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Outcome of an admission call.

    ``status`` is one of the ``ENQUEUE_*`` constants in ``queries.py``;
    ``state`` is the row's resulting state. ``status == ENQUEUE_PARKED``
    means the unit stayed parked (input recorded, nothing runnable) — parked
    units re-enter the queue only via an explicit ``unpark_unit``.
    """

    status: str
    state: str


@dataclass(frozen=True, slots=True)
class QueueWatermarks:
    """The two watermarks + an honest pending flag for one unit.

    ``has_pending_input`` is ``input_seq > consumed_seq`` (NULL-aware). It is
    NOT a message count: seqs are not dense per unit, so callers needing an
    exact queue depth must count ``thread_messages`` rows above the consumed
    watermark themselves. A ``state == 'queued'`` row is runnable regardless
    of this flag (e.g. a worker batch carries no input watermark at all).
    """

    state: str
    input_seq: int | None
    consumed_seq: int | None
    has_pending_input: bool
    control_input_seq: int = 0
    control_consumed_seq: int = 0
    has_pending_control: bool = False
