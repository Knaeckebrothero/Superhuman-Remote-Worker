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
claim strands the loop in ``status='running'`` with ``current_job_id=NULL`` —
a state the advance pre-check can never leave (see
docs/issues/loop_advance_nonatomic_wedges_loop.md for the live incident).
Recovery re-points the loop at its newest spawned job and reconciles the
counters the lost write-back would have set, deriving them from the job's own
``context.loop_iteration`` stamp (spawn-time truth): ``total_jobs_run = N``,
``seq_index = (N-1) % len(role_sequence)`` (the start endpoint spawns
iteration 1 at index 0 and the sequence is immutable after start), and
``remaining_iterations = max_iterations - (N-1)`` (seeded equal at create,
decremented once per completed advance). The healed pointer then flows into
the normal terminal-advance path above — same tick if the job already
finished, via the completion hook if it is still running.

Mirrors ``cron_dispatcher_loop``'s structure (tick + shutdown-aware wait).

Design: docs/features/project_self_improvement_loop.md (Phase 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Cadence is env-tunable mainly so tests can drive ticks faster than 60s. The
# completion hook handles the happy path instantly; this is only a backstop, so
# a coarse tick is fine.
TICK_SECONDS = int(os.getenv("PROJECT_LOOP_SWEEP_SECONDS", "60"))

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
        cur = loop.get("current_job_id")
        job = None
        if not cur:
            # Torn advance: the claim nulled the pointer but the write-back
            # never landed. Re-point at the newest spawned job so the normal
            # terminal-advance path below (or the completion hook, if the job
            # is still running) can take over.
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


def _derive_loop_counters(
    loop: dict[str, Any], job_ctx: dict[str, Any]
) -> tuple[int, int, int | None] | None:
    """Reconstruct (seq_index, total_jobs_run, remaining_iterations) for a
    wedged loop from its newest job's spawn-time context stamps.

    Derivations (invariants set by the start endpoint + advance, see module
    docstring): iteration N ↔ ``seq_index=(N-1) % len(roles)``,
    ``total_jobs_run=N``, ``remaining = max_iterations - (N-1)`` (None when
    the loop is deadline-bounded only). The role stamp cross-checks the index;
    on disagreement the stamp wins (it is spawn-time truth). Returns None when
    the context can't support a confident reconstruction.
    """
    roles = loop.get("role_sequence") or []
    try:
        iteration = int(job_ctx.get("loop_iteration") or 0)
    except (TypeError, ValueError):
        return None
    if iteration < 1 or not roles:
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


async def _heal_wedged_loop(
    db: Any, loop: dict[str, Any]
) -> dict[str, Any] | None:
    """Re-point a running loop with no current_job_id at its newest job.

    Returns the full job row on success so the caller can continue straight
    into the terminal-advance path. Returns None when the heal isn't possible
    (no spawned jobs / undecodable context) — logged loudly as before — or
    when a concurrent replica healed first (silent back-off; its sweep runs
    the advance).
    """
    loop_id = str(loop.get("id"))
    newest = await db.list_project_loop_jobs(loop_id, limit=1)
    job = await db.get_job(str(newest[0]["id"])) if newest else None
    ctx = job.get("context") if job else None
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    derived = _derive_loop_counters(loop, ctx or {}) if job else None
    if not derived:
        # A running loop should always have an in-flight job (start/advance
        # set one). No job (crash between loop create and first spawn) or an
        # unreadable iteration stamp — nothing to re-point at safely.
        logger.warning(
            "project loop %s is running with no current_job_id — needs attention",
            loop_id[:8],
        )
        return None

    seq_index, total_jobs_run, remaining = derived
    if not await db.heal_project_loop_pointer(
        loop_id,
        str(job["id"]),
        seq_index=seq_index,
        total_jobs_run=total_jobs_run,
        remaining_iterations=remaining,
    ):
        return None  # another replica healed first — its sweep advances

    logger.warning(
        "project loop %s: healed torn advance — re-pointed at %s job %s "
        "(iteration %s, seq_index %s, remaining %s)",
        loop_id[:8],
        (ctx or {}).get("loop_role") or job.get("config_name"),
        str(job["id"])[:8],
        total_jobs_run,
        seq_index,
        remaining,
    )
    return job
