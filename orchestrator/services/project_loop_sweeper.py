"""Safety-net sweeper for project self-improvement loops.

A project loop advances via the ``_advance_project_loop`` completion hook when
its current job reaches a terminal state. If that hook is ever missed — the
agent process dies after marking the job terminal but before the completion
callback lands, or the advance itself throws inside the fan-out — the loop would
wedge: ``status='running'`` with a terminal ``current_job`` that never advances.

This sweeper is the backstop. Each tick it scans running loops and, for any
whose current job is already terminal, re-runs the advance. The advance is
atomic and idempotent (``claim_project_loop_advance`` nulls ``current_job_id``
under a conditional UPDATE), so a sweep that races a late completion hook is a
no-op for the loser — the next job is spawned exactly once.

The sweeper also heals the *torn advance*: the advance runs as three separate
transactions (claim → create next job → re-point), so an interrupt after the
claim strands the loop in ``status='running'`` with ``current_job_id=NULL``
(see docs/issues/loop_advance_nonatomic_wedges_loop.md for the live incident).
Crucially, that same state is also the *normal transient window of every
healthy advance* — the claim nulls the pointer seconds before the write-back
restores it — so a NULL pointer alone is NOT evidence of a tear. Healing
inside a live advance re-arms the claim and double-spawns the next iteration
(observed: duplicate iter-14 critics, 12 s apart, two replicas). The
discriminator is age: the claim stamps ``updated_at=now()`` and a healthy
advance re-points within seconds, so the heal only fires when the NULL state
is older than ``PROJECT_LOOP_HEAL_GRACE_SECONDS`` (checked in Python against
the row, and authoritatively re-checked on the DB clock inside the guarded
UPDATE). Recovery then re-points the loop at its newest spawned job and
reconciles the counters the lost write-back would have set, deriving them
from the job's own ``context.loop_iteration`` stamp (spawn-time truth):
``total_jobs_run = N``, ``seq_index = (N-1) % len(role_sequence)`` (the start
endpoint spawns iteration 1 at index 0 and the sequence is immutable after
start), and ``remaining_iterations = max_iterations - (N-1)`` (seeded equal
at create, decremented once per completed advance). The healed pointer then
flows into the normal terminal-advance path above — same tick if the job
already finished, via the completion hook if it is still running.

Parallel (fan-out) stages add one shape: while ``current_stage_jobs`` is
non-empty the loop is barriered on those members, so the sweep only steps in
once every member is terminal (a missed barrier hook) and re-runs the advance
for one — the atomic barrier claim makes that idempotent. A *torn* parallel
advance drains the set in one shot, so it lands on the same NULL-pointer /
empty-set signature as a single-role tear; the heal tells them apart by the
newest stage's width and whether its members are still running (restore the
barrier) or all terminal (re-point + rotate). docs/features/loop_parallel_stages.md.

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
    """Re-run the advance for any running loop whose current job is terminal.

    Returns the number of loops recovered this tick.
    """
    recovered = 0
    for loop in await db.list_running_project_loops():
        # A parallel (fan-out) stage is in flight when current_stage_jobs is
        # non-empty. It advances through the barrier, not current_job_id — the
        # backstop here only fires when every member is already terminal (a
        # missed barrier hook). The barrier claim makes the re-run idempotent.
        stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
        if stage_ids:
            recovered += await _sweep_parallel_stage(db, loop, stage_ids, advance_fn)
            continue

        cur = loop.get("current_job_id")
        job = None
        if not cur:
            # Torn advance: the claim nulled the pointer but the write-back
            # never landed. Re-point at the newest spawned job/stage so the
            # normal terminal-advance path below (or the completion hook, if the
            # job is still running) can take over.
            job = await _heal_wedged_loop(db, loop)
            if job is None:
                continue
            cur = str(job["id"])

        if job is None:
            job = await db.get_job(str(cur))
        if not job:
            continue
        if job.get("status") not in _TERMINAL:
            continue  # in-flight; the completion hook will advance it

        logger.warning(
            "project loop %s: current job %s is terminal (%s) but loop still "
            "running — recovering via advance (missed completion hook)",
            str(loop.get("id"))[:8],
            str(cur)[:8],
            job.get("status"),
        )
        try:
            # result={} → _advance_project_loop derives failure from job.status,
            # so terminal-success and terminal-failure are both handled right.
            await advance_fn(job, {}, [])
            recovered += 1
        except Exception:
            logger.exception("project loop %s: sweeper advance failed", loop.get("id"))
    return recovered


async def _sweep_parallel_stage(
    db: Any, loop: dict[str, Any], stage_ids: list[str], advance_fn: AdvanceFn
) -> int:
    """Backstop for a loop with a parallel (fan-out) stage in flight.

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
        "project loop %s: parallel stage (%d jobs) all terminal but not rotated "
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
    """Re-point a running loop whose pointer is NULL and stage set is empty.

    Both signatures collapse here: a torn single-role advance and a torn
    parallel advance leave the same row state (current_job_id NULL,
    current_stage_jobs '[]') — membership is drained in one shot, so there is no
    partial-shrink to distinguish. The newest STAGE (all jobs sharing the max
    loop_iteration) is the discriminator:

      * width 1 (single-role stage) → re-point ``current_job_id`` at it (as
        before); the caller advances if it is terminal, else the completion
        hook does.
      * width N, some member still running (torn during the next stage's spawn)
        → restore ``current_stage_jobs`` so the barrier can fire when they
        finish. Returns None (the members / next tick drive the rotate).
      * width N, all terminal (torn after the last-out barrier drained the set
        but before the rotate) → re-point ``current_job_id`` at a representative
        so the normal single-job advance rotates.

    Returns the job row to advance on, or None (deferred / restored / not ours).
    Guarded by the age gate + the DB-side heal guards so a live advance's
    transient NULL window is never mistaken for a tear.
    """
    loop_id = str(loop.get("id"))
    age = _wedge_age_seconds(loop)
    if age is not None and age < HEAL_GRACE_SECONDS:
        # Freshly-nulled pointer = the claim of a live advance, not a tear.
        # Healing now would re-arm the claim and double-spawn the iteration.
        logger.debug(
            "project loop %s: current_job_id is NULL but only %.0fs old — "
            "advance likely in flight, deferring heal",
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
        # A running loop should always have an in-flight job (start/advance set
        # one). No job (crash between loop create and first spawn) or an
        # unreadable iteration stamp — nothing to re-point at safely.
        logger.warning(
            "project loop %s is running with no current_job_id — needs attention",
            loop_id[:8],
        )
        return None

    seq_index, total_jobs_run, remaining = derived
    role_label = (ctx or {}).get("loop_role") or members[0].get("config_name")

    # Parallel stage torn with members still running: restore the barrier set.
    if len(members) > 1:
        non_terminal = [m for m in members if m.get("status") not in _TERMINAL]
        if non_terminal:
            if not await db.heal_project_loop_stage(
                loop_id,
                [str(m["id"]) for m in members],
                seq_index=seq_index,
                total_jobs_run=total_jobs_run,
                remaining_iterations=remaining,
                min_wedge_age_seconds=HEAL_GRACE_SECONDS,
            ):
                return None
            logger.warning(
                "project loop %s: healed torn advance — restored parallel stage "
                "(%d members, %d still running; seq_index %s, remaining %s)",
                loop_id[:8],
                len(members),
                len(non_terminal),
                seq_index,
                remaining,
            )
            return None  # members' hooks / next tick fire the barrier

    # Single-role stage, or a parallel stage whose members are all terminal (the
    # rotate was lost): re-point current_job_id at a representative so the normal
    # single-job advance rotates.
    rep = members[0]
    if not await db.heal_project_loop_pointer(
        loop_id,
        str(rep["id"]),
        seq_index=seq_index,
        total_jobs_run=total_jobs_run,
        remaining_iterations=remaining,
        min_wedge_age_seconds=HEAL_GRACE_SECONDS,
    ):
        # Another replica healed first, or the DB-side age guard saw a fresher
        # row than our read (an advance re-claimed meanwhile) — not ours.
        return None

    logger.warning(
        "project loop %s: healed torn advance — re-pointed at %s job %s "
        "(iteration %s, seq_index %s, remaining %s%s)",
        loop_id[:8],
        role_label,
        str(rep["id"])[:8],
        total_jobs_run,
        seq_index,
        remaining,
        f"; {len(members)}-job stage all terminal" if len(members) > 1 else "",
    )
    return rep
