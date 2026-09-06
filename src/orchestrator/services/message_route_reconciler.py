"""Leader-gated reconciler for worker-message routes (M4).

Implements officer_message_routing.md §5.2 — the two deadlines the config has
always advertised and never enforced, plus delivery repair and the
terminal-job auto-close backstop:

0. **Terminal-job auto-close** — still-open routes whose job already reached
   completed/failed/cancelled are CAS-closed with an audit stamp ("closed
   automatically: job cancelled"). This is the backstop for the
   ``maybe_wake_session`` hook: terminal paths that write status directly
   (cascade cancels, dispatch-time failures, sweepers) close here one tick
   later instead of haunting the officer sitrep as "open" forever.
1. **Officer response SLA** — ``pending_officer`` blocking routes past
   ``officer_deadline`` are CAS-moved to ``escalated_to_user`` and the same
   thread is dispatched to the user, with the reason recorded on the route.
2. **Total blocking timeout** — blocking routes past ``total_deadline``
   (``communication.blocking_timeout_hours``, 24h default, frozen into the
   deadline at send time from the job's resolved config) are CAS-moved to
   ``timed_out`` and the job is resumed with an explicit system reply: no
   answer arrived; proceed under the existing autonomy policy with a
   documented conservative assumption, or complete honestly as unable to
   proceed. A cancelled/terminal job receives only the route audit
   transition.
3. **Delivery repair** — escalated/direct routes whose user dispatch never
   stamped ``user_delivery_at`` are redelivered; ``pending_officer`` routes
   whose durable wake intent died are marked ``delivery_failed`` and fall
   back to the user (§5.1).

Exactly-once: every deadline claim is a single-statement
``FOR UPDATE SKIP LOCKED`` + state CAS (see
``PostgresDB.claim_officer_sla_escalations`` / ``claim_total_timeout_routes``),
and the resume itself is CAS'd on ``status='waiting_for_reply'`` plus the
route/freeze generation (``freeze_data.route_id``) — an officer or user answer
racing a deadline unblocks the worker exactly once whichever side wins. A
crash between the timed_out CAS and the resume is healed by the
``list_timed_out_routes_still_frozen`` repair leg, so the CAS-first order can
never strand a frozen job.

Leader-gated (mounted with ``run_when_leader`` in main.py's lifespan): the
per-route CAS already gives correctness; the gate keeps N replicas from
redundantly scanning and double-dispatching user notifications in the
send-then-stamp window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from orchestrator.services import message_routing as routing

logger = logging.getLogger(__name__)

TICK_SECONDS = int(os.getenv("MESSAGE_ROUTE_SWEEP_SECONDS", "30"))

# How long a pre-delivery state may sit without proof of delivery before the
# repair legs act. Comfortably above one in-request delivery attempt so the
# reconciler never races a healthy send.
DELIVERY_GRACE_SECONDS = int(os.getenv("MESSAGE_ROUTE_DELIVERY_GRACE_SECONDS", "180"))

_CLAIM_BATCH = int(os.getenv("MESSAGE_ROUTE_CLAIM_BATCH", "20"))

# The §5.2 system reply a timed-out worker resumes with. Explicit about what
# happened and what is allowed — the worker must not hallucinate an answer.
TIMEOUT_SYSTEM_REPLY = (
    "[SYSTEM — no answer arrived] Your blocking message (thread {thread_id}, "
    "subject: {subject!r}) reached its total timeout of {hours:g} hours "
    "without a reply from any human or officer. No one has answered; do not "
    "invent one. Proceed under your existing autonomy policy using the most "
    "conservative reasonable assumption — and document that assumption "
    "prominently in your deliverables — or, if no safe assumption exists, "
    "complete honestly as unable to proceed, stating exactly what input was "
    "missing."
)

ResumeFn = Callable[..., Awaitable[bool]]


def _parse_freeze(job: Dict[str, Any]) -> Dict[str, Any]:
    freeze = job.get("freeze_data")
    if isinstance(freeze, str):
        try:
            freeze = json.loads(freeze)
        except (TypeError, ValueError):
            freeze = None
    return freeze if isinstance(freeze, dict) else {}


def _freeze_matches_route(job: Dict[str, Any], route: Dict[str, Any]) -> bool:
    """True when the job's CURRENT freeze belongs to this exact route.

    The generation fence: a job unblocked and re-frozen by a LATER blocking
    message must never be resumed against an old route's deadline. Routes
    always stamp ``route_id`` into their freeze; the thread_id fallback only
    covers a hand-repaired freeze blob.
    """
    if str(job.get("status") or "") != "waiting_for_reply":
        return False
    freeze = _parse_freeze(job)
    frozen_route = freeze.get("route_id")
    if frozen_route is not None:
        return str(frozen_route) == str(route.get("route_id"))
    return str(freeze.get("thread_id") or "") == str(route.get("thread_id") or "")


async def _resume_timed_out_route(
    db: Any, route: Dict[str, Any], resume_job: Optional[ResumeFn]
) -> str:
    """Resume the worker behind a timed-out route. Returns the outcome label.

    ``skipped_terminal``: terminal job — the route audit transition (already
    written by the claim CAS) is all it gets. ``skipped_generation``: the job
    is not frozen on this route (an answer won the job CAS) — nothing to do.
    ``resumed`` / ``resume_lost``: the resume CAS outcome.
    """
    job = await db.get_job(str(route["job_id"]))
    if not job:
        return "skipped_missing_job"
    if routing.job_is_terminal(job):
        return "skipped_terminal"
    if not _freeze_matches_route(job, route):
        return "skipped_generation"
    if resume_job is None:
        logger.error(
            "message routes: no resume hook configured — route %s timed out "
            "but job %s stays frozen until the repair leg finds a hook",
            str(route["route_id"])[:8],
            str(route["job_id"])[:8],
        )
        return "resume_unavailable"
    snapshot = route.get("policy_snapshot") or {}
    freeze = _parse_freeze(job)
    feedback = TIMEOUT_SYSTEM_REPLY.format(
        thread_id=route.get("thread_id"),
        subject=str(freeze.get("subject") or "(unknown)"),
        hours=float(
            (snapshot.get("total_timeout_hours") or routing.blocking_timeout_hours(job))
        ),
    )
    resumed = await resume_job(
        str(route["job_id"]),
        feedback=feedback,
        reason=(
            "The blocking message this job was waiting on reached its total "
            "timeout with no reply; the job was resumed with an explicit "
            "no-answer notice."
        ),
        expected_status="waiting_for_reply",
        # OC-04: the Python check above is a cheap pre-filter, not the
        # guarantee. Between it and the write the job can resume and refreeze
        # onto a different route; only this token makes the CAS refuse that.
        expected_route_id=str(route["route_id"]),
    )
    return "resumed" if resumed else "resume_lost"


async def reconcile_message_routes_once(
    db: Any,
    *,
    now: Optional[datetime] = None,
    resume_job: Optional[ResumeFn] = None,
    notifier: Any = None,
    delivery_grace_seconds: int = DELIVERY_GRACE_SECONDS,
    limit: int = _CLAIM_BATCH,
) -> Dict[str, int]:
    """One reconciler tick. Every leg is independent and isolates its errors.

    ``now`` is injectable for fake-clock tests; production passes None and
    the DB compares deadlines against the wall clock it is handed.
    """
    now = now or datetime.now(timezone.utc)
    counts = {
        "closed_terminal": 0,
        "sla_escalated": 0,
        "sla_delivered": 0,
        "redelivered": 0,
        "timed_out": 0,
        "resumed": 0,
        "repair_resumed": 0,
        "delivery_failed": 0,
    }

    # ---- Leg 0: auto-close open routes of already-terminal jobs. ----
    # The backstop for the maybe_wake_session hook: terminal transitions that
    # write jobs.status directly (cascade cancels, dispatch-time failures,
    # sweepers) never call the hook, and without this leg their routes stay
    # "open" in every sitrep/inbox forever with no safe closure verb. The
    # claim CAS stamps the audit transition and retires the routes' pending
    # officer wake intents; closing escalates/resumes/delivers nothing.
    try:
        closed = await db.close_message_routes_for_terminal_jobs(limit=limit)
    except Exception:
        logger.exception("message routes: terminal-job close sweep failed")
        closed = []
    for route in closed:
        counts["closed_terminal"] += 1
        logger.info(
            "message routes: closed route %s (job %s) — job already terminal",
            str(route["route_id"])[:8],
            str(route["job_id"])[:8],
        )

    # ---- Leg 1: officer SLA expiry → escalate to the user (§5.2 #1). ----
    try:
        claimed = await db.claim_officer_sla_escalations(now=now, limit=limit)
    except Exception:
        logger.exception("message routes: SLA claim failed")
        claimed = []
    for route in claimed:
        counts["sla_escalated"] += 1
        delivered = await routing.deliver_route_to_user(
            db, route, reason="officer_sla_expired", notifier=notifier
        )
        if delivered:
            counts["sla_delivered"] += 1
        logger.info(
            "message routes: SLA expired for route %s (job %s) — escalated "
            "to the user (delivered=%s)",
            str(route["route_id"])[:8],
            str(route["job_id"])[:8],
            delivered,
        )

    # ---- Leg 2: total blocking timeout → timed_out + resume (§5.2 #2). ----
    try:
        timed_out = await db.claim_total_timeout_routes(now=now, limit=limit)
    except Exception:
        logger.exception("message routes: total-timeout claim failed")
        timed_out = []
    for route in timed_out:
        counts["timed_out"] += 1
        try:
            outcome = await _resume_timed_out_route(db, route, resume_job)
        except Exception:
            logger.exception(
                "message routes: timeout resume raised for route %s (repair "
                "leg will retry)",
                str(route["route_id"])[:8],
            )
            outcome = "resume_error"
        if outcome == "resumed":
            counts["resumed"] += 1
        logger.info(
            "message routes: total timeout for route %s (job %s) — %s",
            str(route["route_id"])[:8],
            str(route["job_id"])[:8],
            outcome,
        )

    # ---- Leg 2b: crash repair — timed_out but the job is still frozen on
    # this exact generation (the reconciler died between CAS and resume). ----
    try:
        stranded = await db.list_timed_out_routes_still_frozen(limit=limit)
    except Exception:
        logger.exception("message routes: stranded-frozen scan failed")
        stranded = []
    for route in stranded:
        try:
            outcome = await _resume_timed_out_route(db, route, resume_job)
        except Exception:
            logger.exception(
                "message routes: repair resume raised for route %s",
                str(route["route_id"])[:8],
            )
            continue
        if outcome == "resumed":
            counts["repair_resumed"] += 1
            logger.warning(
                "message routes: repaired stranded frozen job %s (route %s "
                "was timed_out without a landed resume)",
                str(route["job_id"])[:8],
                str(route["route_id"])[:8],
            )

    # ---- Leg 3a: user redelivery — owed deliveries that never stamped. ----
    try:
        owed = await db.list_routes_needing_user_redelivery(
            older_than_seconds=delivery_grace_seconds, limit=limit
        )
    except Exception:
        logger.exception("message routes: redelivery scan failed")
        owed = []
    for route in owed:
        reason = (
            "delivery_repair"
            if str(route.get("state")) in ("pending_both", "user_direct")
            else "officer_sla_expired"
        )
        delivered = await routing.deliver_route_to_user(
            db, route, reason=reason, notifier=notifier
        )
        if delivered:
            counts["redelivered"] += 1

    # ---- Leg 3b: dead officer wake intent → delivery_failed → user (§5.1). ----
    try:
        stale = await db.list_stale_pending_officer_routes(
            older_than_seconds=delivery_grace_seconds, limit=limit
        )
    except Exception:
        logger.exception("message routes: stale-officer scan failed")
        stale = []
    for route in stale:
        try:
            failed = await db.transition_message_route(
                str(route["route_id"]),
                to_state="delivery_failed",
                expected_states=["pending_officer"],
                actor_kind="system",
                actor_id="reconciler",
                note="officer wake intent lost",
            )
        except Exception:
            logger.exception(
                "message routes: delivery_failed CAS raised for route %s",
                str(route["route_id"])[:8],
            )
            continue
        if not failed:
            continue
        counts["delivery_failed"] += 1
        outcome = await routing.escalate_route(
            db,
            failed,
            reason="officer_delivery_failed",
            actor_kind="system",
            actor_id="reconciler",
            expected_states=("delivery_failed",),
            notifier=notifier,
        )
        logger.warning(
            "message routes: officer wake intent lost for route %s (job %s) "
            "— fell back to the user (escalated=%s, delivered=%s)",
            str(route["route_id"])[:8],
            str(route["job_id"])[:8],
            outcome["escalated"],
            outcome["delivered"],
        )

    return counts


async def message_route_reconciler_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    resume_job: Optional[ResumeFn] = None,
) -> None:
    """The ~30s tick, mounted leader-gated from main.py's lifespan.

    ``resume_job`` is main's ``_internal_resume_job`` (injected at mount so
    this module never imports main at import time); the notifier resolves to
    the notification_service singleton inside the delivery helpers.
    """
    logger.info(
        "Message-route reconciler started (tick=%ds, grace=%ds)",
        TICK_SECONDS,
        DELIVERY_GRACE_SECONDS,
    )
    while not shutdown_event.is_set():
        try:
            counts = await reconcile_message_routes_once(db, resume_job=resume_job)
            if any(counts.values()):
                logger.info("Message-route reconciler tick: %s", counts)
        except Exception:
            logger.exception(
                "Message-route reconciler tick raised; will retry next tick"
            )
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Message-route reconciler stopped")
