"""Wake an interactive session when a worker job it created finishes.

A session can already launch worker jobs. The missing half was the *return
path*: the orchestrator flipped a status column and went quiet, so a session
that delegated work either sat in a polling loop or simply never learned the
result. This module is that return path.

Framing it as "a notification feature" undersells it. Every session-created job
dispatches immediately (``create_job`` ends in ``_trigger_dispatch()``) and
there is no "start after", so a session that lays out a multi-stage plan — three
designers, then a critic, then a human gate, then an implementer — cannot
execute it. Its only options are to fire everything at once or to launch each
stage when the previous one lands. **The wake is what makes the second possible;
the conversation becomes the scheduler.**

Design: docs/features/session_wake_on_job_completion.md.

Shape
-----
``maybe_wake_session`` is a *decision function*, not a hook on one choke point,
because there is no terminal-state choke point in this codebase — a job also
reaches a terminal state via cascade-cancel, critic approval of its target
(which never calls ``/complete`` at all), diff accept/reject, a dozen
dispatch-time failures, and several sweepers. Missing one would mean a session
that waits forever, which is *indistinguishable from the bug this feature
fixes*, so the design does not rely on enumerating them: the covered paths call
the decision function for **latency**, and the claim query independently finds
terminal jobs that owe a wake **by status**, so an unhooked path costs one
sweeper tick rather than a lost completion. Adding a hook to a newly-discovered
terminal path is therefore an optimization, never a bug fix.

The durable claim on the jobs row is the MECHANISM; the post-commit send is a
latency optimization layered on top of it. Nothing in the completion path is
transactional (``postgres_db.acquire()`` yields a raw pooled connection; every
statement autocommits), so a crash between the status write and a direct POST
loses the wake permanently and silently. Because the fast path goes through the
same atomic claim, losing it costs latency, not correctness.

Two rules this module exists to enforce
---------------------------------------
1. **Claim, commit, then send.** Never hold a transaction open across the HTTP
   call. The send is not idempotent — the agent's ``/api/input`` mints a fresh
   message id per call and unconditionally enqueues, so a duplicate is a visible
   message in the user's transcript *plus* a second paid LLM turn.
2. **Do NOT leader-gate any of this.** ``_trigger_dispatch``'s leader gate sits
   right next to where the completion hook goes and invites a copy-paste, but it
   exists because *dispatch is a singleton*, not for dedup. The agent's
   ``/complete`` POST is already load-balanced to exactly one replica, so
   leader-gating the wake would fire it only when the agent happened to hit the
   leader and would silently drop ~half of all wakes. The claim is what provides
   single-firing, and it works from every replica.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx

from services.session_lifecycle import probe_ready

logger = logging.getLogger(__name__)

# Statuses that owe the creating session a wake. 'pending_review' is included on
# purpose: the job stopped and wants a decision, which is exactly the moment the
# session should hear about it. It is also why the dedup key carries the status
# — a later approve flips pending_review → completed, and that IS a second,
# legitimate wake.
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "pending_review")

# Backstop cadence. The opportunistic post-commit drain handles the happy path
# in milliseconds; this only catches wakes whose sender died or whose terminal
# path has no hook. Env-tunable mainly so tests can drive ticks faster.
TICK_SECONDS = int(os.getenv("SESSION_WAKE_SWEEP_SECONDS", "20"))

# How long a claim is exclusively ours before another replica may re-claim it.
# Must comfortably exceed one delivery attempt (probe 2s + POST ≤ 10s) so a slow
# send is not double-delivered; short enough that a SIGKILL mid-rollout costs
# seconds, not minutes, of latency.
VISIBILITY_TIMEOUT_SECONDS = int(os.getenv("SESSION_WAKE_VISIBILITY_SECONDS", "120"))

# Attempt budget before a wake is buried as 'dead'. Burying matters: without it
# one permanently-unreachable session re-claims forever and starves live wakes
# behind it in the claim's ORDER BY.
MAX_ATTEMPTS = int(os.getenv("SESSION_WAKE_MAX_ATTEMPTS", "8"))

# Claim batch size. Bounded because every row in a batch holds its claim until
# the batch finishes; see the visibility-window arithmetic in drain_pending_wakes
# before raising it.
CLAIM_BATCH = int(os.getenv("SESSION_WAKE_CLAIM_BATCH", "20"))

# In-flight deliveries per drain. Keeps the worst-case batch inside the
# visibility window and makes a fan-out land together instead of in sequence.
# Small enough that a burst cannot open 20 sockets to 20 dead pod IPs at once.
_DELIVER_CONCURRENCY = int(os.getenv("SESSION_WAKE_DELIVER_CONCURRENCY", "5"))

# Summary text is a pointer to the result, not the result. Anything longer
# belongs behind get_worker_job.
_SUMMARY_CHARS = 500

_TERMINAL_FOR_COUNTS = ("completed", "failed", "cancelled")


# --------------------------------------------------------------------------
# Firing
# --------------------------------------------------------------------------


async def maybe_wake_session(db: Any, job_id: str, terminal_status: str) -> bool:
    """Record that ``job_id`` reaching ``terminal_status`` owes a session a wake.

    Returns True iff a wake was newly enqueued. Safe to call from any completion
    path, for any job, more than once: every filter that matters (was this job
    created by a session, did its owner opt out, has this exact terminal status
    already been delivered, is a wake already in flight) lives in the SQL guard,
    so the caller needs to know nothing about the job.

    This is a **latency optimization, not the mechanism**. The claim query finds
    terminal jobs owing a wake by status on its own, so a terminal path that
    forgets to call this still delivers — one sweeper tick later instead of
    immediately. Calling it moves that to milliseconds and makes "a wake is
    owed" an explicit, greppable row state instead of an inference.

    Deliberately takes an id rather than the job dict. Terminal paths hand
    around job dicts assembled by half a dozen different queries with different
    projections; a dict-based pre-filter would silently no-op wherever the
    projection lacks the column — the exact silent-drop failure mode this
    feature exists to remove. One indexed UPDATE by primary key is cheaper than
    that risk.

    Never raises: a completion path must not fail because a notification could
    not be enqueued.
    """
    if terminal_status not in TERMINAL_STATUSES:
        return False
    try:
        enqueued = await db.mark_job_wake_pending(str(job_id), terminal_status)
    except Exception:
        logger.exception(
            "session wake: failed to enqueue for job %s (%s)",
            str(job_id)[:8],
            terminal_status,
        )
        return False
    if enqueued:
        logger.info(
            "session wake: enqueued for job %s (%s)", str(job_id)[:8], terminal_status
        )
    return enqueued


def kick_drain(db: Any) -> None:
    """Fire-and-forget the claim-and-send right after a completion commits.

    Pure latency optimization. Losing this task — cancelled at shutdown, raised
    and swallowed, never scheduled because the process died — is harmless by
    construction: the row is already durably 'pending' and the sweeper re-claims
    it. That is the whole reason the claim comes first.
    """

    async def _run() -> None:
        try:
            await drain_pending_wakes(db)
        except Exception:
            logger.exception("session wake: opportunistic drain raised (non-fatal)")

    try:
        asyncio.create_task(_run(), name="session-wake-drain")
    except RuntimeError:
        # No running loop (sync context / shutdown). The sweeper covers it.
        pass


# --------------------------------------------------------------------------
# Claim + deliver
# --------------------------------------------------------------------------


async def drain_pending_wakes(db: Any, *, limit: int = CLAIM_BATCH) -> int:
    """Claim owed wakes and deliver them. Returns the number delivered.

    Claim first, COMMIT, then send — the claim call returns with its transaction
    closed, so no lock is held across the HTTP call. Holding one there is a
    documented way to melt a database (lock-acquisition degradation plus dead
    tuples from the long-lived snapshot), and it buys nothing: the visibility
    timeout already covers the crash case.

    **The batch must finish inside the visibility window.** A claim only belongs
    to this drain for ``VISIBILITY_TIMEOUT_SECONDS``; overrun it and another
    replica re-claims a row this one is still sending, which is the duplicate
    the whole claim exists to prevent. Delivering serially would blow that
    budget on the realistic bad case — a fan-out into dead pods costs ~12s each
    (2s probe + up to 10s POST), so 20 of them is ~240s against a 120s window.
    Hence bounded concurrency: at ``_DELIVER_CONCURRENCY`` in flight, worst-case
    batch time is ``ceil(limit / concurrency) * 12s``, comfortably inside the
    window at the shipped values. Anything that raises ``CLAIM_BATCH`` or lowers
    ``VISIBILITY_TIMEOUT_SECONDS`` has to re-check that arithmetic.

    Concurrency also happens to be right for the workload: these are independent
    HTTP calls to different pods, and the case the feature exists for — six jobs
    of a fan-out landing together — is precisely the one serial delivery would
    make slowest.
    """
    try:
        claimed = await db.claim_pending_job_wakes(
            limit=limit, visibility_timeout_seconds=VISIBILITY_TIMEOUT_SECONDS
        )
    except Exception:
        logger.exception("session wake: claim failed")
        return 0
    if not claimed:
        return 0

    gate = asyncio.Semaphore(_DELIVER_CONCURRENCY)

    async def _one(row: dict[str, Any]) -> bool:
        async with gate:
            return await _deliver_and_settle(db, row)

    results = await asyncio.gather(
        *(_one(row) for row in claimed), return_exceptions=True
    )
    return sum(1 for r in results if r is True)


async def _deliver_and_settle(db: Any, row: dict[str, Any]) -> bool:
    """Deliver one claimed wake and record the outcome. True iff delivered.

    Settling is in its own try: a delivery that succeeded but whose settle write
    failed must NOT be reported as delivered, and must leave the row in
    'sending' so the visibility timeout re-claims it. Re-delivering a notice is
    the lesser evil against marking one sent that never arrived.
    """
    job_id = str(row["id"])
    status = str(row.get("status") or "")
    try:
        ok = await _deliver(db, row)
    except Exception:
        logger.exception("session wake: delivery raised for job %s", job_id[:8])
        ok = False

    try:
        if ok:
            await db.finish_job_wake(job_id, status)
            return True
        state = await db.release_job_wake(job_id, max_attempts=MAX_ATTEMPTS)
        if state == "dead":
            logger.error(
                "session wake: job %s exhausted %d attempts — thread %s will "
                "never learn this job finished",
                job_id[:8],
                MAX_ATTEMPTS,
                str(row.get("created_by_thread_id"))[:8],
            )
    except Exception:
        # The row stays 'sending'; the visibility timeout re-claims it.
        logger.exception("session wake: failed to settle claim for job %s", job_id[:8])
    return False


async def _deliver(db: Any, row: dict[str, Any]) -> bool:
    """Deliver one claimed wake. True = delivered (or nothing left to deliver)."""
    thread_id = row.get("created_by_thread_id")
    if not thread_id:
        # The creating thread was hard-deleted; the FK's ON DELETE SET NULL
        # already nulled the backref. Nobody to wake — consume the claim.
        return True
    thread_id = str(thread_id)

    # Re-read the thread INSIDE the attempt rather than trusting anything read
    # at claim time. 'awaiting_user' in particular races a 60s sweeper
    # (mark_orphaned_threads_suspended) that rewrites exactly that state to
    # 'suspended' with agent_id = NULL, so the binding can vanish between the
    # claim and the send.
    try:
        thread = await db.get_thread(thread_id)
    except Exception:
        logger.exception("session wake: thread lookup failed for %s", thread_id[:8])
        return False
    if thread is None:
        return True

    text = await _format_wake_message(db, row, thread_id)

    agent = await _resolve_live_agent(db, thread)
    if agent is not None and await _inject_live(agent, text):
        logger.info(
            "session wake: injected into live thread %s (job %s)",
            thread_id[:8],
            str(row["id"])[:8],
        )
        return True

    # Suspended / detached / ended, or the live inject bounced. Write the notice
    # durably so it lands on the next resume, and tell the user out-of-band.
    #
    # 'ended' is deliberately NOT a do-nothing case: an active thread whose pod
    # dies is marked 'ended', not 'suspended', and ended threads are
    # user-resumable — treating 'ended' as terminal would silently drop
    # completions for a supported case.
    return await _deliver_durable(db, thread, thread_id, row, text)


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------


async def _resolve_live_agent(db: Any, thread: dict[str, Any]) -> Optional[dict]:
    """Return the agent pod serving this thread, or None if it isn't live.

    Copied from ``GET /api/sessions/{thread_id}/connection`` — the only path
    that combines DB state with a live probe. Two things make the copy
    deliberate rather than lazy:

    * **``probe_ready`` is not optional.** ``agent.status`` is heartbeat-driven
      and lags reality by up to ~4 minutes (3-minute staleness timeout on a 60s
      tick), and heartbeat freshness is deliberately not used against zombies
      because zombies heartbeat normally. Without the probe we would POST into a
      black hole and burn the attempt budget.
    * **Do NOT reuse ``_resolve_thread_for_forwarding``.** It looks exactly
      right and is wrong here: it RESTORES suspended workspaces as a side effect
      — the Phase-2 resume path Phase 1 explicitly promises not to touch — omits
      the ``status != 'offline'`` guard, and needs a user dict for owner auth.

    Unlike ``/connection`` this does not self-heal a stale binding. Clearing
    ``thread.agent_id`` is a user-driven repair on a foreground request; a
    background delivery has no business mutating session bindings on the way
    past.
    """
    agent_id = thread.get("agent_id")
    if not agent_id:
        return None
    try:
        agent = await db.get_agent(str(agent_id))
    except Exception:
        return None
    if agent is None or agent.get("status") == "offline":
        return None
    if not agent.get("pod_ip"):
        return None
    if agent.get("status") not in ("ready", "working", "session"):
        return None
    # Defensive pod_port: a NULL column would make int(None) raise.
    if not await probe_ready(str(agent["pod_ip"]), int(agent.get("pod_port") or 8001)):
        return None
    return agent


async def _inject_live(agent: dict[str, Any], text: str) -> bool:
    """POST the notice onto the agent's input queue. True iff it was accepted.

    ``/api/input`` persists the message before acknowledging and only then
    enqueues it (session_silent_failure_audit.md #1) — exactly the durability an
    injected completion wants. ``role='event'`` keeps the persisted row out of
    the human-bubble family so the transcript does not claim the user said this.

    A 503 means the loop is not running (the pod is up but the session is not
    attached); that is a fall-back-to-durable case, not an error.

    Split timeout on purpose: a flat 30s against a black-holed pod IP would burn
    30s inside the drain for every dead session.
    """
    pod_ip = str(agent["pod_ip"])
    pod_port = int(agent.get("pod_port") or 8001)
    url = f"http://{pod_ip}:{pod_port}/api/input"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0)
        ) as client:
            resp = await client.post(url, json={"content": text, "role": "event"})
    except Exception as exc:
        logger.info("session wake: live inject to %s failed: %s", pod_ip, exc)
        return False
    if resp.status_code == 200:
        return True
    logger.info(
        "session wake: live inject returned %s (%s)",
        resp.status_code,
        resp.text[:200],
    )
    return False


# --------------------------------------------------------------------------
# Durable branch
# --------------------------------------------------------------------------


async def _deliver_durable(
    db: Any,
    thread: dict[str, Any],
    thread_id: str,
    row: dict[str, Any],
    text: str,
) -> bool:
    """Persist the notice and notify the user out-of-band.

    The durable branch is ``thread_messages``, NOT ``thread_events``. The event
    log looks like the natural home and is unusable: its ``seq`` is allocated by
    the *agent*, in process memory, and appending into a live epoch from the
    orchestrator would silently destroy a batch of the agent's frames — while
    appending into a suspended epoch is unreachable anyway, because replay is
    hard-scoped to ``threads.events_epoch``, which is bumped unconditionally on
    every attach. ``thread_messages`` has a database-allocated BIGSERIAL, no
    retention prune, and the orchestrator already writes it.

    No pod restore happens here. Waking a suspended session is the restore path,
    which reads a whole snapshot tar into RAM and has OOM-killed the
    orchestrator before; N jobs finishing at once would be N concurrent
    restores. That is Phase 2, and it needs rate-limiting first.
    """
    try:
        await db.save_thread_message(
            thread_id=thread_id,
            role="event",
            content=text,
        )
    except Exception:
        logger.exception(
            "session wake: durable write failed for thread %s", thread_id[:8]
        )
        return False

    logger.info(
        "session wake: wrote durable notice to thread %s (status=%s, job %s)",
        thread_id[:8],
        thread.get("status"),
        str(row["id"])[:8],
    )

    # Best-effort user-facing ping. Only on the durable branch — a live session
    # already showed the user the wake, and emailing them about it would be
    # noise. Failure here must not un-deliver the notice above.
    try:
        await _notify_owner(db, thread, thread_id, row)
    except Exception:
        logger.warning(
            "session wake: owner notification failed for thread %s", thread_id[:8]
        )
    return True


async def _notify_owner(
    db: Any, thread: dict[str, Any], thread_id: str, row: dict[str, Any]
) -> None:
    """Tell the owner out of band that a job their session launched finished.

    The recipient MUST be resolved and passed explicitly.
    ``notification_service.dispatch`` takes ``user_id`` for preference lookup
    only — its email leg sends to ``recipient_email or ""``, so omitting it does
    not fall back to the user's address; it hands the empty string to the email
    service, which refuses to send and logs a warning. Nothing raises, so the
    notification is silently dropped while the wake still reports success.
    (Caught by the 2026-07-27 dev live gate: "Refusing to send email to
    undeliverable recipient(s): ['']". Every other caller —
    ``_notify_operator_freeze`` — resolves the user row first for this reason.)

    This is the half of the durable branch that actually reaches a user who has
    closed the tab, so a silent drop here defeats the branch's purpose.
    """
    from services.notification_service import notification_service

    user_id = thread.get("user_id")
    if not user_id:
        return
    try:
        user = await db.get_user(str(user_id))
    except Exception:
        user = None
    if not user or not user.get("email"):
        logger.info(
            "session wake: owner %s has no email — skipping out-of-band notice",
            str(user_id)[:8],
        )
        return

    job_id = str(row["id"])
    short = job_id[:8]
    description = (row.get("description") or "")[:100]
    status = str(row.get("status") or "finished")
    await notification_service.dispatch(
        user_id=str(user_id),
        job_id=job_id,
        subject=f"Job {short} {status} — your session is waiting",
        message_md=(
            f"**Job `{short}`** launched from your session "
            f"**{thread.get('title') or 'Untitled'}** is now `{status}`.\n\n"
            f"**Task:** {description}\n\n"
            "Reopen the session to pick the result up."
        ),
        job_description=description,
        config_name=str(row.get("config_name") or "worker_base"),
        thread_id=thread_id,
        recipient_email=user.get("email"),
        recipient_name=user.get("display_name") or "User",
    )


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


async def _format_wake_message(db: Any, row: dict[str, Any], thread_id: str) -> str:
    """Render the injected notice.

    Pointers, not payloads. ``Outputs`` names the tools that read the result; it
    does not inline it. Inlining would make every wake expensive and defeat the
    entire point of delegating the work. ``Task`` is the one field the
    delegation formatter (``_format_delegation_results``) gets away without,
    because its children arrive as one batch — a session may have fanned out
    three jobs twenty minutes and one compaction ago and cannot otherwise tell
    them apart.

    ``[JOB_FINISHED]`` joins the shipped bracket-tag family and gives the
    cockpit a cheap literal to match.
    """
    job_id = str(row["id"])
    status = str(row.get("status") or "unknown")
    description = (row.get("description") or "").strip()

    lines = [
        "[JOB_FINISHED] A worker job you created has reached a terminal state.",
        "",
        f"- Job: {job_id[:8]} ({await _agent_label(db, row)})",
        f"- Status: {status}",
    ]
    if description:
        lines.append(f"- Task: {_truncate(description, 300)}")

    freeze = _as_dict(row.get("freeze_data"))
    summary = str(freeze.get("summary") or "").strip()
    if summary:
        lines.append(f"- Summary: {_truncate(summary, _SUMMARY_CHARS)}")
    confidence = freeze.get("confidence")
    if confidence is not None:
        lines.append(f"- Confidence: {confidence}")
    deliverables = freeze.get("deliverables")
    if isinstance(deliverables, list) and deliverables:
        lines.append(
            f"- Outputs: {len(deliverables)} deliverables — read them with "
            "get_worker_job / get_job_workspace_file"
        )
    else:
        lines.append(
            "- Outputs: read them with get_worker_job / get_job_workspace_file"
        )

    error = (row.get("error_message") or "").strip()
    if error and status in ("failed", "cancelled"):
        lines.append(f"- Error: {_truncate(error, 300)}")

    siblings = await _sibling_line(db, thread_id)
    if siblings:
        lines += ["", siblings]

    # Exactly one line of closing instruction. It is a reminder; the policy
    # lives in the <scheduled_work> system-prompt block.
    lines += ["", "Decide now: inspect this result, or note it and continue."]
    return "\n".join(lines)


async def _agent_label(db: Any, row: dict[str, Any]) -> str:
    """'expert: designer' when a DB expert drove the job, else the config name."""
    expert_id = row.get("expert_id")
    if expert_id:
        try:
            expert = await db.get_expert_by_id(str(expert_id))
        except Exception:
            expert = None
        if expert and expert.get("name"):
            return f"expert: {expert['name']}"
    return f"config: {row.get('config_name') or 'worker_base'}"


async def _sibling_line(db: Any, thread_id: str) -> str:
    """ "1 of 3 finished — 1 still running, 1 failed".

    Free once created_by_thread_id exists, and it saves the agent a
    list_worker_jobs round-trip on EVERY wake just to decide whether this is the
    moment to act.
    """
    try:
        counts = await db.get_thread_job_counts(thread_id)
    except Exception:
        return ""
    total = counts.get("total") or 0
    if total <= 1:
        return ""
    parts = []
    if counts.get("running"):
        parts.append(f"{counts['running']} still running")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failed")
    if counts.get("cancelled"):
        parts.append(f"{counts['cancelled']} cancelled")
    tail = f" — {', '.join(parts)}" if parts else ""
    return (
        f"Your outstanding jobs: {counts.get('finished', 0)} of {total} finished{tail}."
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """JSONB reads come back as raw JSON strings on this driver; parse defensively."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Backstop
# --------------------------------------------------------------------------


async def session_wake_sweeper_loop(db: Any, shutdown_event: asyncio.Event) -> None:
    """Deliver every wake the fast path missed, until shutdown.

    Two classes of miss, both handled by the same claim query: a send whose
    replica died holding the claim, and a terminal path that never called
    :func:`maybe_wake_session` at all (dispatch-time failures, the LLM-outage
    fail path, VM-upgrade approval expiry — all direct DB writes with no hook).
    The second is why this loop is a correctness component and not just a
    retrier.

    **Not leader-gated, on purpose** — see the module docstring. Single-firing
    comes from the claim, which works from every replica; leader-gating would
    add nothing and would make the loop a SPOF during a leader handover.

    No age-grace is needed here, unlike ``project_loop_sweeper``. That sweeper
    needed one because the state it heals ("both pointer columns cleared") is
    also the normal transient window of a healthy advance, so acting early
    double-spawned a turn. Here the equivalent window — a claim in flight — is
    represented by a distinct state ('sending') that the claim query only
    reconsiders after the visibility timeout. The grace is the timeout.
    """
    logger.info(
        "Session wake sweeper started (tick=%ds, visibility=%ds, max_attempts=%d)",
        TICK_SECONDS,
        VISIBILITY_TIMEOUT_SECONDS,
        MAX_ATTEMPTS,
    )
    while not shutdown_event.is_set():
        try:
            sent = await drain_pending_wakes(db)
            if sent:
                logger.info("Session wake sweeper delivered %d wake(s)", sent)
        except Exception:
            logger.exception("Session wake sweeper tick raised; will retry next tick")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Session wake sweeper stopped")
