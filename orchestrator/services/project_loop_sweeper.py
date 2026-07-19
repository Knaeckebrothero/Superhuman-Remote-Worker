"""Safety-net sweeper for project self-improvement loops.

A project loop advances via the ``_advance_project_loop`` completion hook when
every job in its in-flight turn reaches a terminal state; the turn's
membership — width 1 included — is tracked in ``current_stage_jobs``, and the
barrier (``claim_project_loop_stage_barrier``) is the only path that drains it
and rotates to the next turn. If the hook is ever missed — the agent process
dies after marking a job terminal but before the completion callback lands, or
the advance itself throws — the loop wedges: ``status='running'`` with a
terminal member still listed in ``current_stage_jobs``.

This sweeper is the backstop. Each tick it scans running loops and, for any
whose in-flight turn is already fully terminal, re-runs the advance for one
member. The barrier claim is atomic and idempotent, so a sweep that races a
late completion hook is a no-op for the loser — the turn rotates exactly once.

The sweeper also heals the *torn advance*: an advance that spawned the next
turn's jobs, or drained the barrier, but lost its write-back leaves the loop
wedged with BOTH pointer columns empty —
``current_job_id IS NULL AND current_stage_jobs='[]'`` — the single wedge
signature, regardless of the turn's width
(docs/issues/loop_advance_nonatomic_wedges_loop.md for the live incident).
Crucially, that same state is also the *normal transient window of every
healthy advance* — the barrier claim clears both columns seconds before the
write-back restores them — so seeing it once is NOT evidence of a tear.
Healing inside a live advance re-arms the claim and double-spawns the next
iteration (observed: duplicate iter-14 critics, 12 s apart, two replicas). The
discriminator is age: a healthy advance re-points within seconds, so the heal
only fires when the both-cleared state is older than
``PROJECT_LOOP_HEAL_GRACE_SECONDS`` (checked in Python against the row, and
authoritatively re-checked on the DB clock inside the guarded UPDATE).
Recovery restores the loop's newest spawned turn as barrier membership (plus
the width-1 display mirror when it's a single job) and reconciles the
counters the lost write-back would have set, deriving them from a member's
own ``context.loop_iteration`` stamp (spawn-time truth): ``total_jobs_run =
N``, ``seq_index = (N-1) % len(role_sequence)`` (the start endpoint spawns
iteration 1 at index 0 and the sequence is immutable after start), and
``remaining_iterations = max_iterations - (N-1)`` (seeded equal at create,
decremented once per completed advance). The restored turn then advances only
if every member is already terminal (the rotate itself was lost — re-run it
now); otherwise the members' own completion hooks (or the next tick) fire the
barrier.

Transitionally, a pre-0063 writer may still leave a width-1 turn tracked only
by the display pointer (``current_job_id`` set, ``current_stage_jobs`` empty);
the sweeper adopts that row into ``current_stage_jobs`` so the unified advance
drives it, deferring the actual sweep to the next tick.

Mirrors ``cron_dispatcher_loop``'s structure (tick + shutdown-aware wait).

Design: docs/features/project_self_improvement_loop.md (Phase 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Cadence is env-tunable mainly so tests can drive ticks faster than 60s. The
# completion hook handles the happy path instantly; this is only a backstop, so
# a coarse tick is fine.
TICK_SECONDS = int(os.getenv("PROJECT_LOOP_SWEEP_SECONDS", "60"))

# A running loop with current_job_id=NULL only counts as *torn* once the state
# is at least this old — younger means an advance is in flight (its claim just
# stamped updated_at) and healing would double-spawn. Orders of magnitude above
# any healthy advance (seconds) and irrelevant to the backstop's purpose (the
# real incident sat wedged for 12 h).
HEAL_GRACE_SECONDS = int(os.getenv("PROJECT_LOOP_HEAL_GRACE_SECONDS", "600"))

_TERMINAL = ("completed", "failed", "cancelled")

# advance_fn(job, result, actions) -> Awaitable — wired to _advance_project_loop.
AdvanceFn = Callable[[dict[str, Any], dict[str, Any], list[str]], Awaitable[None]]


async def project_loop_sweeper_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    advance_fn: AdvanceFn,
) -> None:
    """Recover wedged project loops until ``shutdown_event`` is set."""
    logger.info("Project loop sweeper started (tick=%ds)", TICK_SECONDS)
    while not shutdown_event.is_set():
        try:
            recovered = await _sweep_tick(db, advance_fn)
            if recovered:
                logger.info(
                    "Project loop sweeper recovered %d wedged loop(s)", recovered
                )
        except Exception:
            logger.exception("Project loop sweeper tick raised; will retry next tick")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Project loop sweeper stopped")


async def _sweep_tick(db: Any, advance_fn: AdvanceFn) -> int:
    """Recover any running loop whose in-flight turn stalled.

    Returns the number of loops recovered this tick.
    """
    recovered = 0
    for loop in await db.list_running_project_loops():
        # The in-flight turn is barrier-tracked in current_stage_jobs (width 1
        # included). The backstop only steps in once every member is terminal
        # (a missed barrier hook); the atomic claim makes the re-run idempotent.
        stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
        if stage_ids:
            recovered += await _sweep_stage(db, loop, stage_ids, advance_fn)
            continue

        cur = loop.get("current_job_id")
        if cur:
            # Transitional (a pre-0063 writer raced the deploy): a width-1
            # turn tracked only by the display pointer. Adopt it into the
            # barrier set so the unified advance can drive it; it is swept
            # as a normal stage next tick.
            logger.warning(
                "project loop %s: adopting legacy width-1 pointer %s into "
                "current_stage_jobs",
                str(loop.get("id"))[:8],
                str(cur)[:8],
            )
            await db.update_project_loop(
                str(loop["id"]), current_stage_jobs=[str(cur)]
            )
            continue

        # Both columns empty: a torn advance (write-back lost) or a crash
        # before the first spawn. The heal restores membership; it returns a
        # job only when the restored turn is already fully terminal, meaning
        # the rotate itself was lost — re-run it now.
        job = await _heal_wedged_loop(db, loop)
        if not job:
            continue
        logger.warning(
            "project loop %s: healed turn is fully terminal — recovering via "
            "barrier advance (lost rotate)",
            str(loop.get("id"))[:8],
        )
        try:
            # result={} → the advance derives failure from job.status, so
            # terminal-success and terminal-failure are both handled right.
            await advance_fn(job, {}, [])
            recovered += 1
        except Exception:
            logger.exception("project loop %s: sweeper advance failed", loop.get("id"))
    return recovered


async def _sweep_stage(
    db: Any, loop: dict[str, Any], stage_ids: list[str], advance_fn: AdvanceFn
) -> int:
    """Backstop for a loop with a turn in flight (any width).

    While any member is still running, the members' completion hooks fire the
    barrier — nothing to do. Once EVERY member is terminal, the last member's
    hook should have claimed the barrier and rotated; if the loop still lists
    them in-flight, that hook was missed. Re-run the advance for one member —
    the atomic barrier claim (``claim_project_loop_stage_barrier``) makes this
    idempotent: whichever of the real hook / this backstop commits first
    rotates, the other no-ops. No age gate needed (the barrier is the guard).
    Returns 1 if a recovery advance ran, else 0.
    """
    statuses = await db.get_loop_stage_member_statuses(stage_ids)
    states = [statuses.get(mid) for mid in stage_ids]
    if any(s is None or s not in _TERMINAL for s in states):
        return 0  # still in flight — the members' hooks will fire the barrier

    rep = await db.get_job(stage_ids[-1])
    if not rep:
        return 0
    logger.warning(
        "project loop %s: stage (%d jobs) all terminal but not rotated "
        "— recovering via barrier advance (missed completion hook)",
        str(loop.get("id"))[:8],
        len(stage_ids),
    )
    try:
        await advance_fn(rep, {}, [])
        return 1
    except Exception:
        logger.exception(
            "project loop %s: sweeper stage advance failed", loop.get("id")
        )
        return 0


def _derive_loop_counters(
    loop: dict[str, Any], job_ctx: dict[str, Any]
) -> tuple[int, int, int | None] | None:
    """Reconstruct (seq_index, total_jobs_run, remaining_iterations) for a
    wedged loop from its newest stage's spawn-time context stamps.

    Prefers the explicit stamps the spawn writes (``loop_seq_index`` +
    ``loop_remaining``, alongside ``loop_iteration`` = cumulative job count).
    These are spawn-time truth and — unlike the legacy modulo below — stay
    correct across variable-width parallel stages, where job-count no longer
    maps to stage index by ``(N-1) % len(roles)``. A present ``loop_seq_index``
    means ``loop_remaining`` is authoritative too (None ⇒ deadline-only budget).

    Legacy fallback (jobs spawned before the stamp existed — necessarily
    all-single-role loops): iteration N ↔ ``seq_index=(N-1) % len(roles)``,
    ``total_jobs_run=N``, ``remaining = max_iterations - (N-1)``. The role stamp
    cross-checks the index; on disagreement the stamp wins. Returns None when
    the context can't support a confident reconstruction.
    """
    try:
        iteration = int(job_ctx.get("loop_iteration") or 0)
    except (TypeError, ValueError):
        return None
    if iteration < 1:
        return None

    # Preferred path: explicit spawn-time stamps.
    if "loop_seq_index" in job_ctx:
        try:
            seq_index = int(job_ctx["loop_seq_index"])
        except (TypeError, ValueError):
            seq_index = None
        if seq_index is not None:
            rem = job_ctx.get("loop_remaining")
            remaining = int(rem) if rem is not None else None
            return seq_index, iteration, remaining

    # Legacy fallback: modulo derivation for pre-parallel single-role jobs.
    roles = loop.get("role_sequence") or []
    if not roles:
        return None
    seq_index = (iteration - 1) % len(roles)
    role = job_ctx.get("loop_role")
    if role and roles[seq_index] != role:
        if role not in roles:
            return None
        seq_index = roles.index(role)

    max_iter = loop.get("max_iterations")
    remaining = (
        max(0, int(max_iter) - (iteration - 1)) if max_iter is not None else None
    )
    return seq_index, iteration, remaining


def _wedge_age_seconds(loop: dict[str, Any]) -> float | None:
    """Seconds since the loop row was last written, or None if unknowable.

    ``updated_at`` was stamped by whatever nulled the pointer (the claim), so
    this is exactly "how long has the pointer been NULL". None (missing or
    non-datetime) defers the decision to the DB-side age guard.
    """
    updated_at = loop.get("updated_at")
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated_at).total_seconds()


async def _heal_wedged_loop(db: Any, loop: dict[str, Any]) -> dict[str, Any] | None:
    """Restore membership for a running loop with both pointer columns empty.

    That state is either the transient window of a live advance (young — see
    the age gate) or a torn advance whose write-back was lost. The newest
    STAGE (all jobs sharing the max ``loop_iteration``) is what the lost
    write-back would have pointed the loop at; restore it as the barrier
    membership (width 1 included, with the display mirror for a single
    member):

      * some member still running → the barrier fires when they finish;
        returns None (nothing to advance now).
      * all members terminal (the rotate itself was lost) → returns a
        representative so the caller re-runs the advance; the atomic barrier
        claim makes the re-run idempotent.

    Guarded by the age gate + the DB-side heal guards so a live advance's
    transient window is never mistaken for a tear (the double-spawn
    incident, docs/issues/loop_advance_nonatomic_wedges_loop.md).
    """
    loop_id = str(loop.get("id"))
    age = _wedge_age_seconds(loop)
    if age is not None and age < HEAL_GRACE_SECONDS:
        # Freshly-cleared pointers = the claim of a live advance, not a tear.
        # Healing now would re-arm the claim and double-spawn the turn.
        logger.debug(
            "project loop %s: pointers cleared but only %.0fs old — advance "
            "likely in flight, deferring heal",
            loop_id[:8],
            age,
        )
        return None

    members = await db.get_newest_loop_stage(loop_id)
    ctx = members[0].get("context") if members else None
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    derived = _derive_loop_counters(loop, ctx or {}) if members else None
    if not derived:
        # A running loop should always have an in-flight turn (start/advance
        # set one). No job at all, or an unreadable iteration stamp — nothing
        # to re-point at safely.
        logger.warning(
            "project loop %s is running with no in-flight turn — needs attention",
            loop_id[:8],
        )
        return None

    seq_index, total_jobs_run, remaining = derived
    member_ids = [str(m["id"]) for m in members]
    if not await db.heal_project_loop_stage(
        loop_id,
        member_ids,
        current_job_id=(member_ids[0] if len(member_ids) == 1 else None),
        seq_index=seq_index,
        total_jobs_run=total_jobs_run,
        remaining_iterations=remaining,
        min_wedge_age_seconds=HEAL_GRACE_SECONDS,
    ):
        # Another replica healed first, or the DB-side age guard saw a
        # fresher row than our read — not ours.
        return None

    non_terminal = [m for m in members if m.get("status") not in _TERMINAL]
    logger.warning(
        "project loop %s: healed torn advance — restored %d-member turn "
        "(%d still running; seq_index %s, remaining %s)",
        loop_id[:8],
        len(members),
        len(non_terminal),
        seq_index,
        remaining,
    )
    if non_terminal:
        return None  # members' completion hooks / next tick fire the barrier
    return members[0]  # all terminal: the rotate was lost — advance now
