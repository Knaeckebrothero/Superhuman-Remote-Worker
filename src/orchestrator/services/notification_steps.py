"""The escalate-on-timeout sweeper for the unified notification feed.

``record()`` writes a row's deferred channel steps to ``notification_steps``
(knowledge-base/knowledge/features/unified_notification_system.md D5/D6/D8).
This module is the small, dumb engine that runs them: claim what is due,
evaluate each step's conditions *now* (was the row seen? did anyone settle
the source?), apply the recipient's preference matrix and quiet hours, then
send one message per (recipient, channel, batch key) group and settle the
rows. All policy lives in the catalog's data; nothing here knows what a job
or an officer is — it only knows whether a source probe says "resolved".

Outcomes per step (``notification_steps.state`` / ``detail``):

* ``skipped``   — a condition no longer holds (``condition:not_seen``), the
                  channel is off for this category (``preference``) or has no
                  transport (``channel_unconfigured``), the row was archived,
                  or the channel already went out (``already_delivered``).
* ``cancelled`` — the source was resolved (normally done atomically by the
                  resolve hooks; this is the belt-and-braces path).
* deferred      — quiet hours: ``due_at`` moves to the window's end, the
                  claim is released, the attempt is not counted.
* ``done``      — a delivery was attempted and the provider accepted it.
* retried       — the provider failed: ``due_at`` moves by the backoff
                  ladder; after ``MAX_ATTEMPTS`` the step is ``failed``.

Runs leader-gated every ``SWEEP_INTERVAL_SECONDS``. Concurrent sweepers
(the transient dual-leader window) split the due set with
``FOR UPDATE SKIP LOCKED``; a sweeper that dies mid-pass loses its lease
after ``lease_minutes`` and the next pass picks the steps up again — the
delivery claim ledger, not the step lease, is what prevents a double send.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.services.notification_catalog import (
    bypasses_quiet_hours,
    category_spec,
    channel_enabled,
    first_failing_condition,
    quiet_hours_window,
)

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30
CLAIM_LIMIT = 200
LEASE_MINUTES = 10
# Provider failures: retry after 5, then 15, then 45 minutes; then give up.
BACKOFF_MINUTES: tuple[int, ...] = (5, 15, 45)
MAX_ATTEMPTS = len(BACKOFF_MINUTES)
# A quiet-hours window we cannot compute an end for (should not happen —
# the same parser said we are inside one) still must not spin: park an hour.
QUIET_HOURS_FALLBACK_MINUTES = 60


def group_key(step: dict[str, Any]) -> tuple[str, str, str, str]:
    """Steps that become one message: same recipient, same channel, and a
    shared batch key (an unbatched step is its own group)."""
    batch = step.get("batch_key") or f"step:{step['id']}"
    return (
        str(step.get("recipient_kind")),
        str(step.get("recipient_id")),
        str(step.get("step_kind")),
        str(batch),
    )


def retry_due_at(attempt: int, now: datetime) -> datetime | None:
    """Where the backoff ladder puts the next try after ``attempt`` failures,
    or ``None`` once the ladder is exhausted."""
    if attempt < 1 or attempt > MAX_ATTEMPTS:
        return None
    return now + timedelta(minutes=BACKOFF_MINUTES[attempt - 1])


def _bypasses_quiet_hours(step: dict[str, Any]) -> bool:
    try:
        spec = category_spec(str(step.get("category")))
    except ValueError:
        return False
    return bypasses_quiet_hours(spec, str(step.get("severity")))


async def process_due_steps(
    *,
    db: Any,
    service: Any,
    worker_id: str,
    now: datetime | None = None,
    limit: int = CLAIM_LIMIT,
    lease_minutes: int = LEASE_MINUTES,
) -> dict[str, int]:
    """One sweeper pass. Returns counters for the log line and the tests."""
    now = now or datetime.now(timezone.utc)
    stats: Counter[str] = Counter()
    claimed = await db.claim_due_notification_steps(
        worker_id=worker_id, limit=limit, lease_minutes=lease_minutes
    )
    stats["claimed"] = len(claimed)
    if not claimed:
        return dict(stats)

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for step in claimed:
        groups.setdefault(group_key(step), []).append(step)

    settings_cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    probe_cache: dict[tuple[str, str], bool] = {}

    async def _settings(recipient_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if recipient_id not in settings_cache:
            settings_cache[recipient_id] = (
                await service._get_user_settings(recipient_id) or {},
                await service._get_user_channels(recipient_id) or {},
            )
        return settings_cache[recipient_id]

    async def _resolved(step: dict[str, Any]) -> bool:
        kind, sid = step.get("source_kind"), step.get("source_id")
        if not kind or sid is None:
            return False
        key = (str(kind), str(sid))
        if key not in probe_cache:
            probe_cache[key] = await service._source_resolved(kind, sid)
        return probe_cache[key]

    async def _settle(ids: list[Any], state: str, detail: str) -> None:
        if ids:
            await db.settle_notification_steps(ids, state=state, detail=detail)
            stats[state] += len(ids)

    async def _suppress(members: list[dict[str, Any]], channel: str, reason: str):
        for member in members:
            await service._record_suppressed(
                str(member["notification_id"]), channel, reason
            )

    for (recipient_kind, recipient_id, channel, _batch), members in groups.items():
        settings, channels = await _settings(recipient_id)
        categories = (settings.get("communication") or {}).get("categories")

        live: list[dict[str, Any]] = []
        skipped: dict[str, list[Any]] = {}
        cancelled: list[Any] = []
        for step in members:
            if step.get("archived_at"):
                skipped.setdefault("archived", []).append(step["id"])
                continue
            if step.get("resolved_at"):
                cancelled.append(step["id"])
                continue
            failing = first_failing_condition(
                step.get("conditions"), step, source_resolved=await _resolved(step)
            )
            if failing:
                skipped.setdefault(f"condition:{failing}", []).append(step["id"])
                continue
            if recipient_kind != "user":
                skipped.setdefault("recipient_kind", []).append(step["id"])
                continue
            if not service._channel_deliverable(channel):
                skipped.setdefault("channel_unconfigured", []).append(step["id"])
                await _suppress([step], channel, "channel_unconfigured")
                continue
            if not channel_enabled(
                channels, categories, str(step["category"]), channel
            ):
                skipped.setdefault("preference", []).append(step["id"])
                await _suppress([step], channel, "preference")
                continue
            live.append(step)

        for reason, ids in skipped.items():
            await _settle(ids, "skipped", reason)
        await _settle(cancelled, "cancelled", "resolved")
        if not live:
            continue

        inside, window_end = quiet_hours_window(settings, now)
        if inside:
            deferrable = [s for s in live if not _bypasses_quiet_hours(s)]
            if deferrable:
                resume_at = window_end or (
                    now + timedelta(minutes=QUIET_HOURS_FALLBACK_MINUTES)
                )
                await db.defer_notification_steps(
                    [s["id"] for s in deferrable],
                    due_at=resume_at,
                    detail="quiet_hours",
                )
                stats["deferred"] += len(deferrable)
                live = [s for s in live if s not in deferrable]
            if not live:
                continue

        outcome = await service.send_step_group(live, channel=channel)
        await _settle(outcome.get("already") or [], "skipped", "already_delivered")
        if outcome.get("unaddressed"):
            unaddressed = [s for s in live if s["id"] in set(outcome["unaddressed"])]
            await _suppress(unaddressed, channel, "no_email")
            await _settle([s["id"] for s in unaddressed], "skipped", "no_email")
        attempted_ids = set(outcome.get("attempted") or [])
        attempted = [s for s in live if s["id"] in attempted_ids]
        if not attempted:
            continue
        stats["batches"] += 1
        if outcome.get("ok"):
            await _settle(
                [s["id"] for s in attempted], "done", f"batch:{outcome['batch_id']}"
            )
            stats["sent"] += len(attempted)
            continue
        error = str(outcome.get("error") or "send failed")[:500]
        exhausted = [s for s in attempted if int(s.get("attempt") or 0) >= MAX_ATTEMPTS]
        retry = [s for s in attempted if s not in exhausted]
        await _settle([s["id"] for s in exhausted], "failed", error)
        # Members of one batch share an attempt count in practice (they were
        # claimed together); the earliest ladder rung keeps them together.
        if retry:
            rung = min(int(s.get("attempt") or 1) for s in retry)
            due = retry_due_at(rung, now) or (
                now + timedelta(minutes=BACKOFF_MINUTES[-1])
            )
            await db.retry_notification_steps(
                [s["id"] for s in retry], due_at=due, detail=f"retry:{error}"
            )
            stats["retried"] += len(retry)

    return dict(stats)


async def notification_steps_loop(
    shutdown_event: asyncio.Event,
    db: Any,
    service: Any,
    *,
    interval_seconds: int = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Leader-gated background loop (registered in ``main.py`` next to the
    other ``run_when_leader`` sweeps)."""
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    logger.info("notification steps sweeper started (%s)", worker_id)
    while not shutdown_event.is_set():
        try:
            stats = await process_due_steps(db=db, service=service, worker_id=worker_id)
            if stats.get("claimed"):
                logger.info(
                    "notification steps: %s",
                    ", ".join(f"{k}={v}" for k, v in sorted(stats.items()) if v),
                )
        except Exception as e:
            logger.error("notification steps sweep failed: %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            pass
    logger.info("notification steps sweeper stopped")


__all__ = [
    "BACKOFF_MINUTES",
    "CLAIM_LIMIT",
    "LEASE_MINUTES",
    "MAX_ATTEMPTS",
    "SWEEP_INTERVAL_SECONDS",
    "group_key",
    "notification_steps_loop",
    "process_due_steps",
    "retry_due_at",
]
