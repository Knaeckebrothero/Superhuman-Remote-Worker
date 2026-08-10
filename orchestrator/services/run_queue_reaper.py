"""Leader-gated run_queue lease reaper (stateless-agents S1, doc §5.2).

Steals expired leases via the shared per-row CAS in ``src/shared/run_queue``
and, for session units, writes the post-steal journal signal so the user never
gets silent dead air: one transaction per stolen unit bumps
``threads.events_epoch`` (fencing any zombie journal writer out — the client
re-anchors via ``gone_beyond_horizon``), reconciles recoverable interrupt rows
that belong to the stolen token, and appends a ``turn.interrupted`` /
``turn.parked`` system frame. An unreceipted admitted interrupt is durable stop
intent: owner loss settles it as ``applied``/``hard`` without signalling a
successor's RAM, consumes its exact human input, and writes a linked ack. An
already committed receipt is authoritative and is terminalized without
duplication. Post-steal is a
sanctioned writer-exclusion context for ``append_system_frame``: the steal's
``lease_token`` bump already fenced the previous holder's persists, and the
epoch bump retires its in-memory seq counter (docs/features/stateless_agents.md
§5.3.2, system-writer class).

Leadership is a session-scoped advisory lock (``RUN_QUEUE_REAPER_ID``,
``orchestrator/database/lock_ids.py``) held on the same pooled connection the
sweep runs on, mirroring ``services/leader_election.py``'s shape: lock dies
with the session, a follower takes over within ~one interval. The lock only
avoids duplicate sweep WORK — correctness under the documented dual-leader
window comes from ``reap_expired``'s per-row CAS (each steal is returned to
exactly one caller, and the journal write is keyed off that returned record).

Error containment mirrors ``thread_events_prune_sweeper``: per-row failures
are logged and skipped, per-cycle failures drop leadership and re-contend,
and the loop itself never dies before shutdown.

Non-session unit kinds (``worker_batch``, ``bg_task``) are reaped only — no
journal writes; workers have no ``thread_events`` journal until S3 (doc §5.5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from database.lock_ids import RUN_QUEUE_REAPER_ID
from src.shared.event_journal import append_system_frame, bump_epoch
from src.shared.run_queue import (
    REAPER_GRACE_SECONDS,
    REAPER_INTERVAL_SECONDS,
    STATE_QUEUED,
    UNIT_KIND_SESSION_TURN,
    StolenUnit,
    reap_expired,
)
from src.shared.thread_interrupts import (
    consume_applied_interrupt_input_idle,
    interrupt_receipt_result,
)

logger = logging.getLogger(__name__)


STALE_INTERRUPT_RETRY_MAX_THREADS = 25


class JournalStealResult(str, Enum):
    """Typed outcome of the post-steal system-writer transaction."""

    WRITTEN = "written"
    SKIPPED_SUCCESSOR_CLAIMED = "skipped_successor_claimed"
    SKIPPED_QUEUE_CHANGED = "skipped_queue_changed"


@dataclass(frozen=True, slots=True)
class InterruptReconcileResult:
    """Durable rows settled by one exact writer-fenced transaction."""

    count: int
    target_turn_ids: tuple[int, ...]
    request_ids: tuple[str, ...]
    settled_queue_state: str | None = None
    requests_by_target: tuple[tuple[int, tuple[str, ...]], ...] = ()


_LOCK_THREAD_SQL = """
SELECT execution_lane, agent_id
FROM threads
WHERE id = $1::uuid
FOR UPDATE
"""

_LOCK_QUEUE_UNIT_SQL = """
SELECT unit_kind, state, lease_token, leased_by, last_leased_by,
       attempts_since_completion, input_seq, consumed_seq,
       control_input_seq, control_consumed_seq
FROM run_queue
WHERE unit_id = $1::uuid
FOR UPDATE
"""


_PENDING_STOLEN_INTERRUPTS_SQL = """
SELECT request.id, request.client_request_id, request.target_turn_id,
       request.accepted_lease_token, request.accepted_leased_by,
       request.outcome AS request_outcome, request.result AS request_result,
       receipt.epoch AS receipt_epoch, receipt.seq AS receipt_seq,
       receipt.kind AS receipt_kind, receipt.payload AS receipt_payload
FROM thread_interrupt_requests request
LEFT JOIN thread_events receipt
  ON receipt.thread_id = request.thread_id
 AND receipt.interrupt_request_id = request.id
WHERE request.thread_id = $1::uuid
  AND request.accepted_lease_token = $2::bigint
  AND (request.outcome IS NULL
       OR (request.outcome = 'applied'
           AND NOT (COALESCE(request.result, '{}'::jsonb)
                    ? 'consumed_input_seq')))
ORDER BY request.requested_at, request.id
FOR UPDATE OF request
"""

_STALE_INTERRUPT_RETRY_CANDIDATES_SQL = """
SELECT request.thread_id, min(request.requested_at) AS oldest_request
FROM thread_interrupt_requests request
JOIN threads thread ON thread.id = request.thread_id
JOIN run_queue queue ON queue.unit_id = request.thread_id
WHERE (request.outcome IS NULL
       OR (request.outcome = 'applied'
           AND NOT (COALESCE(request.result, '{}'::jsonb)
                    ? 'consumed_input_seq')))
  AND thread.execution_lane = 'stateless'
  AND thread.agent_id IS NULL
  AND queue.unit_kind = 'session_turn'
  AND queue.lease_token > request.accepted_lease_token
  AND queue.leased_by IS NULL
  AND queue.state = 'parked'
GROUP BY request.thread_id
ORDER BY min(request.requested_at), request.thread_id
LIMIT $1::integer
"""

_PENDING_STALE_INTERRUPT_RETRY_SQL = """
SELECT request.id, request.client_request_id, request.target_turn_id,
       request.accepted_lease_token, request.accepted_leased_by,
       request.outcome AS request_outcome, request.result AS request_result,
       receipt.epoch AS receipt_epoch, receipt.seq AS receipt_seq,
       receipt.kind AS receipt_kind, receipt.payload AS receipt_payload
FROM thread_interrupt_requests request
JOIN threads thread ON thread.id = request.thread_id
LEFT JOIN thread_events receipt
  ON receipt.thread_id = request.thread_id
 AND receipt.interrupt_request_id = request.id
WHERE request.thread_id = $1::uuid
  AND (request.outcome IS NULL
       OR (request.outcome = 'applied'
           AND NOT (COALESCE(request.result, '{}'::jsonb)
                    ? 'consumed_input_seq')))
  AND request.accepted_lease_token < $2::bigint
  AND thread.execution_lane = 'stateless'
  AND thread.agent_id IS NULL
ORDER BY request.requested_at, request.id
FOR UPDATE OF request
"""

_TERMINALIZE_STOLEN_INTERRUPT_SQL = """
UPDATE thread_interrupt_requests
SET outcome = $4::text,
    result = $5::jsonb,
    applied_mode = $6::text,
    applied_at = now(),
    applied_lease_token = $3::bigint,
    journal_epoch = $7::integer,
    journal_seq = $8::bigint,
    acknowledged_at = now(),
    error_code = $9::text
WHERE id = $1::uuid
  AND thread_id = $2::uuid
  AND accepted_lease_token = $3::bigint
  AND outcome IS NULL
RETURNING id
"""


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep up to ``seconds``, waking immediately on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


def _receipt_payload(value: Any) -> dict[str, Any]:
    """Return one receipt payload as an object or fail the reconciliation."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("interrupt receipt payload is not valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("interrupt receipt payload is not an object")


async def _reconcile_stolen_interrupts(
    conn: Any,
    *,
    thread_id: str,
    unit: StolenUnit,
) -> InterruptReconcileResult:
    """Settle recoverable requests owned by the lease that was just stolen.

    The run_queue CAS has already bumped the token before this function is
    reachable. A missing old token means an older/mock ``StolenUnit`` that
    cannot prove which inbox generation it retired, so it is left untouched.
    A non-consecutive token pair is a corrupt boundary and fails the enclosing
    transaction rather than rejecting somebody else's requests.

    A valid pre-existing receipt wins: the former owner may have committed its
    journal frame and crashed before finalization. Otherwise exact-turn
    admission itself is authoritative stop intent: the post-steal writer emits
    a correlated ``applied``/``hard`` ack, consumes that target input, and never
    signals a successor's RAM. Every terminal update uses the immutable old
    token and exact event cursor.
    """
    previous_token = unit.previous_lease_token
    if previous_token is None:
        return InterruptReconcileResult(0, (), ())
    previous_token = int(previous_token)
    if previous_token <= 0 or int(unit.lease_token) != previous_token + 1:
        raise RuntimeError(
            "stolen session unit has a non-consecutive lease-token boundary: "
            f"{previous_token}->{unit.lease_token}"
        )

    pending = await conn.fetch(
        _PENDING_STOLEN_INTERRUPTS_SQL,
        thread_id,
        previous_token,
    )
    return await _reconcile_interrupt_rows(
        conn,
        thread_id=thread_id,
        current_lease_token=int(unit.lease_token),
        pending=pending,
    )


async def _reconcile_interrupt_rows(
    conn: Any,
    *,
    thread_id: str,
    current_lease_token: int,
    pending: Any,
) -> InterruptReconcileResult:
    """Reconcile already-locked request rows from their durable receipts."""
    reconciled = 0
    target_turn_ids: list[int] = []
    request_ids: list[str] = []
    requests_by_target: dict[int, list[str]] = {}
    applied_groups: list[tuple[int, int, str]] = []
    settled_queue_state: str | None = None
    for request in pending:
        request_id = request["id"]
        client_request_id = request["client_request_id"]
        target_turn_id = int(request["target_turn_id"])
        accepted_lease_token = int(request["accepted_lease_token"])
        request_outcome = request["request_outcome"]
        if request["receipt_epoch"] is not None:
            receipt_result = interrupt_receipt_result(
                request_id=request_id,
                client_request_id=client_request_id,
                target_turn_id=target_turn_id,
                event_kind=str(request["receipt_kind"]),
                event_payload=request["receipt_payload"],
            )
            if receipt_result is None:
                raise RuntimeError(
                    f"stolen interrupt has an invalid durable receipt: {request_id}"
                )
            outcome, mode, error_code = receipt_result
            payload = _receipt_payload(request["receipt_payload"])
            epoch = int(request["receipt_epoch"])
            seq = int(request["receipt_seq"])
            if request_outcome is not None and request_outcome != outcome:
                raise RuntimeError(
                    "terminal interrupt outcome disagrees with durable receipt: "
                    f"{request_id}"
                )
        else:
            if request_outcome is not None:
                raise RuntimeError(
                    f"terminal interrupt is missing its durable receipt: {request_id}"
                )
            outcome = "applied"
            mode = "hard"
            error_code = None
            payload = {
                "request_id": str(request_id),
                "client_request_id": str(client_request_id),
                "target_turn_id": target_turn_id,
                "applied": True,
                "mode": mode,
                "reason": "owner_lost",
                "owner_loss_reason": "lease_expired",
            }
            receipt = await append_system_frame(
                conn,
                thread_id=thread_id,
                kind="interrupt.ack",
                payload=payload,
                interrupt_request_id=str(request_id),
            )
            if receipt is None:
                raise RuntimeError(
                    f"thread disappeared while settling interrupt {request_id}"
                )
            epoch, seq = receipt

        if request_outcome is None:
            updated = await conn.fetchval(
                _TERMINALIZE_STOLEN_INTERRUPT_SQL,
                request_id,
                thread_id,
                accepted_lease_token,
                outcome,
                json.dumps(payload),
                mode,
                epoch,
                seq,
                error_code,
            )
            if updated is None:
                raise RuntimeError(
                    f"stolen interrupt terminalization lost exact row {request_id}"
                )
        if outcome == "applied":
            group = (accepted_lease_token, target_turn_id, str(request_id))
            if not any(group[:2] == existing[:2] for existing in applied_groups):
                applied_groups.append(group)
        reconciled += 1
        if target_turn_id not in target_turn_ids:
            target_turn_ids.append(target_turn_id)
        request_ids.append(str(request_id))
        requests_by_target.setdefault(target_turn_id, []).append(str(request_id))

    # Terminalize the whole locked batch first. The group helper then sees and
    # stamps every applied sibling before advancing exactly one human input.
    # Distinct old lease/turn groups are settled in durable request order; a
    # queued result exposes the next human input to the following group.
    for accepted_lease_token, target_turn_id, request_id in applied_groups:
        consumed = await consume_applied_interrupt_input_idle(
            conn,
            thread_id=thread_id,
            current_lease_token=int(current_lease_token),
            accepted_lease_token=accepted_lease_token,
            target_turn_id=target_turn_id,
            request_id=request_id,
        )
        if consumed is None:
            raise RuntimeError(
                "applied stolen interrupt lost its exact idle queue owner: "
                f"{request_id}"
            )
        settled_queue_state = consumed.state
    return InterruptReconcileResult(
        reconciled,
        tuple(target_turn_ids),
        tuple(request_ids),
        settled_queue_state,
        tuple(
            (target_turn_id, tuple(ids))
            for target_turn_id, ids in requests_by_target.items()
        ),
    )


def _turn_frame_payload(
    *,
    attempts: int,
    stolen_from: str | None,
    interrupts: InterruptReconcileResult,
    fallback_target_turn_id: int | None = None,
) -> dict[str, Any]:
    """Correlate a steal frame with the exact interrupted turn when known."""
    payload: dict[str, Any] = {
        "reason": "lease_expired",
        "attempts": int(attempts),
        "stolen_from": stolen_from,
    }
    if len(interrupts.target_turn_ids) == 1:
        target_turn_id = interrupts.target_turn_ids[0]
        payload["target_turn_id"] = target_turn_id
        payload["turn_id"] = target_turn_id
    elif interrupts.target_turn_ids:
        payload["target_turn_ids"] = list(interrupts.target_turn_ids)
    elif fallback_target_turn_id is not None:
        target_turn_id = int(fallback_target_turn_id)
        payload["target_turn_id"] = target_turn_id
        payload["turn_id"] = target_turn_id
    if len(interrupts.request_ids) == 1:
        payload["interrupt_request_id"] = interrupts.request_ids[0]
    if interrupts.request_ids:
        payload["interrupt_request_ids"] = list(interrupts.request_ids)
    return payload


def _turn_frame_slices(
    interrupts: InterruptReconcileResult,
) -> tuple[InterruptReconcileResult, ...]:
    """One exact-target lifecycle slice per interrupted turn."""
    if not interrupts.requests_by_target:
        return (interrupts,)
    return tuple(
        InterruptReconcileResult(
            count=len(request_ids),
            target_turn_ids=(target_turn_id,),
            request_ids=request_ids,
            settled_queue_state=interrupts.settled_queue_state,
            requests_by_target=((target_turn_id, request_ids),),
        )
        for target_turn_id, request_ids in interrupts.requests_by_target
    )


async def _append_turn_frames(
    conn: Any,
    *,
    thread_id: str,
    kind: str,
    attempts: int,
    stolen_from: str | None,
    interrupts: InterruptReconcileResult,
    fallback_target_turn_id: int | None = None,
) -> None:
    """Append exact-target terminal frames, failing the enclosing transaction."""
    for frame_interrupts in _turn_frame_slices(interrupts):
        frame = await append_system_frame(
            conn,
            thread_id=thread_id,
            kind=kind,
            payload=_turn_frame_payload(
                attempts=attempts,
                stolen_from=stolen_from,
                interrupts=frame_interrupts,
                fallback_target_turn_id=fallback_target_turn_id,
            ),
        )
        if frame is None:
            raise RuntimeError(
                f"thread disappeared while journaling lifecycle {thread_id}"
            )


def _post_steal_queue_result(
    thread: Any,
    queue: Any,
    unit: StolenUnit,
) -> JournalStealResult:
    """Validate the locked queue row before entering system-writer context."""
    if (
        queue is not None
        and str(queue["state"]) == "leased"
        and int(queue["lease_token"]) > int(unit.lease_token)
    ):
        return JournalStealResult.SKIPPED_SUCCESSOR_CLAIMED
    if (
        thread is None
        or str(thread["execution_lane"] or "") != "stateless"
        or thread["agent_id"] is not None
        or queue is None
        or str(queue["unit_kind"]) != UNIT_KIND_SESSION_TURN
        or str(queue["state"]) not in {"queued", "parked"}
        or str(queue["state"]) != str(unit.state)
        or int(queue["lease_token"]) != int(unit.lease_token)
        or queue["leased_by"] is not None
    ):
        return JournalStealResult.SKIPPED_QUEUE_CHANGED
    return JournalStealResult.WRITTEN


async def _journal_steal(conn: Any, unit: StolenUnit) -> JournalStealResult:
    """Post-steal journal write for ONE session unit — one transaction.

    ``bump_epoch`` first (fences the zombie's journal writer and resets the
    seq high-water mark), then stale interrupt reconciliation, then the turn
    frame. All share one transaction so a crash cannot expose a partial set of
    receipts or a bumped epoch with no explanatory frame. ``unit.state`` is
    the post-steal state:
    ``'queued'`` → the turn will be retried (``turn.interrupted``);
    ``'parked'`` → attempts exhausted, operator unpark required
    (``turn.parked``).
    """
    thread_id = str(unit.unit_id)
    async with conn.transaction():
        # Global per-thread lock order is threads -> run_queue. REST interrupt
        # admission uses the same order, so neither side can form a lock cycle.
        thread = await conn.fetchrow(_LOCK_THREAD_SQL, thread_id)
        queue = await conn.fetchrow(_LOCK_QUEUE_UNIT_SQL, thread_id)
        queue_result = _post_steal_queue_result(thread, queue, unit)
        if queue_result is not JournalStealResult.WRITTEN:
            return queue_result
        await bump_epoch(conn, thread_id=thread_id)
        interrupts = await _reconcile_stolen_interrupts(
            conn,
            thread_id=thread_id,
            unit=unit,
        )
        # Applied settlement consumes the stopped input and can override a
        # post-steal parked row to done/queued. Its lifecycle edge is an
        # interruption, not a poison-unit park; choose only after settlement.
        kind = (
            "turn.interrupted"
            if unit.state == STATE_QUEUED
            or interrupts.settled_queue_state in {"done", "queued"}
            else "turn.parked"
        )
        await _append_turn_frames(
            conn,
            thread_id=thread_id,
            kind=kind,
            attempts=unit.attempts_since_completion,
            stolen_from=unit.leased_by,
            interrupts=interrupts,
            fallback_target_turn_id=unit.interrupt_admission_turn_id,
        )
    return JournalStealResult.WRITTEN


def _queue_allows_stale_retry(queue: Any) -> bool:
    """True only for a provably writer-free post-steal parked row.

    A voluntarily released queued row can keep an attached warm journal
    allocator even after ``last_leased_by`` is cleared. Queued repair is thus
    delegated to its next live claimant; parked post-steal rows are the only
    safe periodic system-writer context.
    """
    return bool(
        queue is not None
        and str(queue["unit_kind"]) == UNIT_KIND_SESSION_TURN
        and str(queue["state"]) == "parked"
        and queue["leased_by"] is None
    )


async def _retry_stale_interrupt_thread(
    conn: Any,
    *,
    thread_id: str,
) -> int:
    """Reconstruct one failed parked post-steal journal transaction."""
    async with conn.transaction():
        # Match interrupt admission's threads -> run_queue lock order.
        thread = await conn.fetchrow(_LOCK_THREAD_SQL, thread_id)
        if (
            thread is None
            or str(thread["execution_lane"] or "") != "stateless"
            or thread["agent_id"] is not None
        ):
            return 0
        queue = await conn.fetchrow(_LOCK_QUEUE_UNIT_SQL, thread_id)
        if not _queue_allows_stale_retry(queue):
            return 0
        queue_token = int(queue["lease_token"])
        pending = await conn.fetch(
            _PENDING_STALE_INTERRUPT_RETRY_SQL,
            thread_id,
            queue_token,
        )
        if not pending:
            return 0
        # The original journal-steal transaction failed as one unit, so retry
        # reconstructs the whole boundary even when every ack receipt predates
        # it: epoch bump, row reconciliation, and turn.parked are one commit.
        await bump_epoch(conn, thread_id=thread_id)
        interrupts = await _reconcile_interrupt_rows(
            conn,
            thread_id=thread_id,
            current_lease_token=queue_token,
            pending=pending,
        )
        owners = {
            str(request["accepted_leased_by"])
            for request in pending
            if request["accepted_leased_by"] is not None
        }
        await _append_turn_frames(
            conn,
            thread_id=thread_id,
            kind=(
                "turn.interrupted"
                if interrupts.settled_queue_state in {"done", "queued"}
                else "turn.parked"
            ),
            attempts=int(queue["attempts_since_completion"]),
            stolen_from=next(iter(owners)) if len(owners) == 1 else None,
            interrupts=interrupts,
        )
        return interrupts.count


async def retry_stale_interrupt_requests(
    conn: Any,
    *,
    max_threads: int = STALE_INTERRUPT_RETRY_MAX_THREADS,
) -> int:
    """Bounded periodic repair for failed post-steal interrupt transactions.

    Candidate discovery is advisory. Each thread is rechecked while holding
    its exact queue row ``FOR UPDATE``. A claim that commits first makes the
    row leased and is skipped; a retry that locks first excludes claims until
    its receipt/finalization transaction commits. Per-thread errors are
    contained so a corrupt receipt cannot starve unrelated repairs.
    """
    if max_threads <= 0:
        return 0
    candidates = await conn.fetch(
        _STALE_INTERRUPT_RETRY_CANDIDATES_SQL,
        int(max_threads),
    )
    reconciled = 0
    for candidate in candidates:
        thread_id = str(candidate["thread_id"])
        try:
            reconciled += await _retry_stale_interrupt_thread(
                conn,
                thread_id=thread_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "run_queue reaper: stale interrupt retry failed for thread %s "
                "(contained)",
                thread_id,
            )
    return reconciled


async def reap_cycle(
    conn: Any,
    *,
    grace_seconds: float = REAPER_GRACE_SECONDS,
) -> int:
    """One sweep: steal expired leases, journal the session ones. Returns the
    steal count.

    Runs OUTSIDE any enclosing transaction (``reap_expired``'s contract: each
    per-row CAS steal commits independently); only the per-unit journal write
    opens its own transaction. Per-row errors are contained so one bad unit
    never blocks the rest of the pass.
    """
    stolen = await reap_expired(conn, grace_seconds=grace_seconds)
    for unit in stolen:
        # Greppable ops line — one per steal (M6 fault-injection anchors on it).
        logger.info(
            "run_queue steal: unit=%s kind=%s pod=%s attempts=%d new_state=%s",
            unit.unit_id,
            unit.unit_kind,
            unit.leased_by,
            unit.attempts_since_completion,
            unit.state,
        )
        if unit.unit_kind != UNIT_KIND_SESSION_TURN:
            # Reap only — no journal for non-session kinds (S3).
            continue
        try:
            result = await _journal_steal(conn, unit)
            if result is not JournalStealResult.WRITTEN:
                logger.info(
                    "run_queue reaper: post-steal journal skipped for unit %s (%s)",
                    unit.unit_id,
                    result.value,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Contained: the steal itself already committed (lease fenced);
            # only the user-visible journal signal was lost for this unit.
            logger.exception(
                "run_queue reaper: journal write failed for unit %s (contained)",
                unit.unit_id,
            )
    await retry_stale_interrupt_requests(conn)
    return len(stolen)


async def run_queue_reaper_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float = float(REAPER_INTERVAL_SECONDS),
    grace_seconds: float = float(REAPER_GRACE_SECONDS),
) -> None:
    """Contend for the reaper advisory lock and sweep while holding it.

    ``db`` is the orchestrator's ``PostgresDB`` (uses ``db._pool`` directly:
    the advisory lock is session-scoped, so lock and sweep must share one
    dedicated connection whose lifetime IS the leadership tenure). Followers
    re-contend every ``interval_seconds``. Wired in ``main.py``'s lifespan
    beside ``thread_events_prune_sweeper`` and stopped via the same
    ``shutdown_event``.
    """
    logger.info(
        "run_queue reaper started (interval=%.0fs grace=%.0fs)",
        interval_seconds,
        grace_seconds,
    )
    while not shutdown_event.is_set():
        pool = getattr(db, "_pool", None)
        if pool is None:
            # Orchestrator still booting; retry next interval.
            await _sleep_or_shutdown(interval_seconds, shutdown_event)
            continue
        conn = None
        try:
            conn = await pool.acquire()
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", RUN_QUEUE_REAPER_ID
            )
            if not got:
                await _sleep_or_shutdown(interval_seconds, shutdown_event)
                continue
            logger.info("run_queue reaper: leadership acquired")
            try:
                while not shutdown_event.is_set():
                    await reap_cycle(conn, grace_seconds=grace_seconds)
                    await _sleep_or_shutdown(interval_seconds, shutdown_event)
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1)", RUN_QUEUE_REAPER_ID
                    )
                except Exception:
                    # Best-effort: the lock auto-releases with the session.
                    pass
                logger.info("run_queue reaper: leadership released")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Connection lost / DB blip / cycle-level failure: drop leadership
            # (lock releases with the session) and re-contend after a beat.
            logger.warning("run_queue reaper error (non-fatal, retrying): %s", e)
            await _sleep_or_shutdown(interval_seconds, shutdown_event)
        finally:
            if conn is not None:
                try:
                    await pool.release(conn)
                except Exception:
                    pass
    logger.info("run_queue reaper stopped")
