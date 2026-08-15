"""Officer auto-pull tick — machine liveness for the century's backlog.

The officer decides WHAT gets worked; this decides only WHEN a ready ticket
becomes a job. Every judgment call stays his: classification, arming a ticket,
reviewing an outcome, re-arming it. The tick's whole job is to notice that a
pool has a free slot and an authorized ticket, and to close that gap within a
minute instead of whenever he next wakes (docs/features/officer_backlog_pools.md
§5).

**Claims are one-shot.** A ticket is claimed the moment a job carries its id —
in any status, terminal included. Dispatch consumes readiness; only the officer
re-arming the ticket (a fresh ``ready_at``) makes it eligible again. The
original design released claims when a job went terminal, which meant every
completed ticket was re-dispatched a minute later and every failing one was
re-burned at breaker cadence, forever. It also gives the century a dead-letter
queue for free: a failed ticket parks until someone looks at it.

**Correctness does not rest on leadership.** The tick is mounted leader-gated
because N replicas scanning is waste, but dual-leader windows are real and
acknowledged in-tree, so the claim itself is what has to be safe: the ticket
check, the capacity count and the job INSERT happen in ONE advisory-locked
transaction, and a unique index on ``(project_id, ticket)`` over non-terminal
jobs refuses a racing second claim outright.

**Failures and outages are different things.** The pool breaker counts job
failures only. An honest ``goal_achieved=false`` never trips it — punishing a
truthful negative report is how you train a fleet to stop filing them — and
neither does a KB outage, a usage-ledger hiccup, or an exception in this module.
Infra problems log and retry; they never open a breaker.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence

from services.officer_admission import (
    SlotAdmissionError,
    admit_in_transaction,
    officer_is_held,
)
from services.officer_slots import roster_from_meta
from services.project_backlog import fetch_backlog, fetch_ticket_state
from services.work_categories import (
    EXECUTOR,
    category_block,
    classify_ticket,
    resolve_expert,
)
from src.shared.backlog_tags import READY_TAG, category_tag

logger = logging.getLogger(__name__)

TICK_SECONDS = int(os.getenv("OFFICER_BACKLOG_TICK_SECONDS", "60"))

# A claim whose job has not moved in this long renders as "claimed-but-stalled".
# Never auto-released: a second job for a claimed ticket must not exist until
# the officer releases the first, which is the failure mode silent-redelivery
# queues are famous for.
STALE_CLAIM_HOURS = float(os.getenv("OFFICER_STALE_CLAIM_HOURS", "4"))

# pending_review claims page rather than merely rendering — that lane has a
# known dead zone, and a silently stranded review is exactly the invisibility
# this feature exists to end.
PENDING_REVIEW_PAGE_HOURS = float(os.getenv("OFFICER_PENDING_REVIEW_PAGE_HOURS", "24"))

# Two consecutive job failures on DISTINCT tickets open a pool for 30 minutes.
# Distinct because two failures on one ticket are one problem, not a pattern.
BREAKER_FAILURES = 2
BREAKER_OPEN_MINUTES = float(os.getenv("OFFICER_POOL_BREAKER_MINUTES", "30"))

# A floor breach wakes the officer at most this often per pool. Event-driven
# replenishment beats a passive number on a card, but a queue that is short
# because there is genuinely nothing to do must not page him every minute.
FLOOR_WAKE_DEBOUNCE_HOURS = float(os.getenv("OFFICER_FLOOR_WAKE_HOURS", "6"))

# Candidate tickets pulled per pool per tick. The tick dispatches at most one
# job per pool anyway; the rest of the window exists so a claimed or ambiguous
# head-of-queue does not block the ticket behind it.
_CANDIDATE_LIMIT = 10

ProvisionFn = Callable[..., Awaitable[None]]
GrantsFn = Callable[..., Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_or_none(value: Any) -> Any:
    """asyncpg wants a real UUID for a uuid column; tests hand in strings."""
    import uuid as _uuid

    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _officer_meta(thread: dict[str, Any]) -> dict[str, Any]:
    """The officer block off a thread row, tolerant of JSON-as-text metadata."""
    import json

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    block = (metadata.get("config_override") or {}).get("officer") or {}
    return block if isinstance(block, dict) else {}


def _officer_state(thread: dict[str, Any]) -> dict[str, Any]:
    import json

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    state = metadata.get("officer_state") or {}
    return state if isinstance(state, dict) else {}


def auto_pull_enabled(officer_meta: dict[str, Any]) -> bool:
    """Ships OFF, flipped per century (§13.1).

    A century whose officer has not triaged a backlog yet must not start
    pulling whatever happens to be tagged.
    """
    return officer_meta.get("auto_pull") in (True, "true", "True", 1)


def pools_from_meta(officer_meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Roster slots that carry a category — the ones the tick may fill.

    A slot with no category is not a pool and is never touched here: it stays
    exactly what it was before this feature, officer-directed capacity.
    """
    roster = roster_from_meta(officer_meta) or {}
    return {
        name: spec
        for name, spec in roster.items()
        if isinstance(spec, dict) and spec.get("category")
    }


def breaker_is_open(state: dict[str, Any], pool: str, now: datetime) -> bool:
    """True while ``pool``'s breaker is still open. Per-pool, never global —
    a burning research pool must not stop the executor from shipping."""
    entry = (state.get("backlog_breakers") or {}).get(pool) or {}
    until = _aware(entry.get("until"))
    return bool(until and until > now)


def evaluate_breaker(
    recent: Sequence[dict[str, Any]], state: dict[str, Any], pool: str
) -> Optional[dict[str, Any]]:
    """Should ``pool``'s breaker trip on this history? Returns the new entry.

    ``recent`` is the pool's terminal ticket-claiming jobs, newest first. The
    breaker opens when the two most recent outcomes on DISTINCT tickets are both
    ``failed`` — a chain, not an incident. Returns None when it should not trip,
    including when it already tripped on this same newest failure: re-deriving
    from history alone would re-open the breaker forever once those two rows
    stop changing.
    """
    outcomes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in recent:
        ticket = job.get("ticket_note_id")
        if not ticket or ticket in seen:
            continue
        seen.add(ticket)
        outcomes.append(job)
        if len(outcomes) == BREAKER_FAILURES:
            break
    if len(outcomes) < BREAKER_FAILURES:
        return None
    if any(job.get("status") != "failed" for job in outcomes):
        return None
    newest = str(outcomes[0].get("id"))
    prior = (state.get("backlog_breakers") or {}).get(pool) or {}
    if str(prior.get("tripped_on_job") or "") == newest:
        return None
    return {
        "tripped_on_job": newest,
        "tickets": [str(job.get("ticket_note_id")) for job in outcomes],
        "cause": f"{BREAKER_FAILURES} consecutive job failures on distinct tickets",
    }


def stale_claims(
    claims: Sequence[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Claims whose job has not moved in STALE_CLAIM_HOURS, oldest first.

    Surfaced, never auto-released (§5.3). ``updated_at`` is acceptable here and
    only here: this is a control-plane staleness question ("has anything touched
    this row"), not the liveness verdict — a job that looks stalled to the tick
    still gets its real reading from ``compute_jobs_liveness`` on the sitrep.
    """
    cutoff = now - timedelta(hours=STALE_CLAIM_HOURS)
    out = []
    for job in claims:
        moved = _aware(job.get("updated_at")) or _aware(job.get("created_at"))
        if moved and moved < cutoff:
            out.append(
                {
                    "job_id": str(job.get("id")),
                    "ticket_note_id": job.get("ticket_note_id"),
                    "slot": job.get("officer_slot"),
                    "status": job.get("status"),
                    "since": moved.isoformat(),
                    "age_hours": round((now - moved).total_seconds() / 3600.0, 1),
                }
            )
    out.sort(key=lambda item: item["since"])
    return out


def eligible_tickets(
    rows: Sequence[dict[str, Any]],
    claims: dict[str, datetime],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split ready tickets into (dispatchable, skip-reasons).

    Three ways a ready-tagged ticket is still not dispatchable, and each is
    reported rather than silently dropped:

    * **Ambiguous** — two ``category:`` tags, or an expert pin that names no
      real config. Guessing would dispatch into the wrong pool under the
      officer's name, and an unvalidated pin fails at agent boot where it looks
      like a job failure and chain-trips the breaker.
    * **Unauthorized** — the ``ready`` tag is present but ``ready_at`` is NULL.
      That happens after a vault rebuild lost the timestamp, and the fail-closed
      direction is the only defensible one for a dispatch authorization.
    * **Claimed** — a job already carries this ticket and the officer has not
      re-armed it since.
    """
    ready: list[dict[str, Any]] = []
    notes: list[str] = []
    for row in rows:
        note_id = str(row.get("note_id") or "")
        classification = classify_ticket(row.get("tags"))
        if classification.problems:
            notes.append(f"{note_id}: {'; '.join(classification.problems)}")
            continue
        authorized_at = _aware(row.get("ready_at"))
        if authorized_at is None:
            notes.append(f"{note_id}: ready tag with no ready_at (re-arm it)")
            continue
        claimed_at = _aware(claims.get(note_id))
        if claimed_at is not None and claimed_at >= authorized_at:
            notes.append(f"{note_id}: claimed at {claimed_at.isoformat()}")
            continue
        ready.append({**row, "classification": classification})
    return ready, notes


async def _spend_exhausted(
    usage_ledger: Any,
    *,
    project_id: str,
    ceiling: float,
    ref_ids: Optional[Sequence[str]] = None,
) -> bool:
    """True when today's spend has reached an OPTIONAL ceiling.

    Fail-open, like the officer's own ceiling: a usage-ledger outage must not
    stop the century from working. Ceilings are unset by default (§13.3), so
    this is skipped entirely for most posts — with no ceiling and no per-ticket
    caps, the century has no mechanical spend brake at all, which is the
    deliberate design: the officer is the brake.
    """
    if (
        not ceiling
        or usage_ledger is None
        or not getattr(usage_ledger, "is_available", False)
    ):
        return False
    try:
        now = _now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if ref_ids is not None:
            if not ref_ids:
                return False
            usage = await usage_ledger.query_usage(
                from_ts=day_start, to_ts=now, ref_ids=list(ref_ids)
            )
        else:
            usage = await usage_ledger.query_usage(
                from_ts=day_start, to_ts=now, scope_project_id=project_id
            )
        return float(usage.get("total_cost_usd") or 0.0) >= float(ceiling)
    except Exception:
        logger.warning(
            "officer backlog: spend check failed for project %s (fail-open)",
            str(project_id)[:8],
            exc_info=True,
        )
        return False


async def _executor_blocked(
    db: Any,
    vector_db: Any,
    *,
    capacity_lineage: Sequence[str],
    project_id: str,
    ticket_is_parallel_safe: bool,
) -> Optional[str]:
    """Reason the executor lane is closed right now, or None.

    Two independent gates, both fail-safe toward NOT dispatching:

    1. **Singleton.** At most one executor claim in flight across all executor
       pools, unless the officer tagged this ticket ``parallel-safe``. Executors
       write to shared project state; two of them make conflicting implicit
       decisions about the same surface.
    2. **Disposition.** The previous executor's ticket must be *dispositioned*
       — closed, or explicitly re-readied — not merely terminal. The deliverable
       gate checks that files exist, never what is in them, so without this an
       executor chain builds on unreviewed work and review debt compounds
       straight into the deliverable. If the officer stops reviewing, executors
       stop. That is the intended direction.
    """
    in_flight = await db.list_officer_slot_claims(list(capacity_lineage), limit=50)
    live_executors = [
        job for job in in_flight if (job.get("work_category") or "") == EXECUTOR
    ]
    if live_executors and not ticket_is_parallel_safe:
        return f"executor singleton held by job {str(live_executors[0]['id'])[:8]}"

    recent = await db.list_officer_slot_claims(
        list(capacity_lineage), include_terminal=True, limit=25
    )
    previous = next(
        (
            job
            for job in recent
            if (job.get("work_category") or "") == EXECUTOR
            and job.get("status") in ("completed", "failed", "cancelled")
        ),
        None,
    )
    if previous is None:
        return None
    ticket_id = previous.get("ticket_note_id")
    if not ticket_id:
        return None
    try:
        ticket = await fetch_ticket_state(vector_db, project_id, str(ticket_id))
    except Exception:
        # A KB outage is not a disposition signal. Skip cleanly rather than
        # assuming either answer; the next tick re-reads.
        logger.warning(
            "officer backlog: disposition read failed for ticket %s", ticket_id
        )
        return "disposition unreadable (KB unavailable)"
    if ticket is None or ticket.get("status") != "active":
        return None  # closed — dispositioned
    re_armed = _aware(ticket.get("ready_at"))
    created = _aware(previous.get("created_at"))
    if re_armed and created and re_armed > created:
        return None  # explicitly re-readied — dispositioned
    return (
        f"previous executor ticket {ticket_id} is undispositioned "
        f"(job {str(previous['id'])[:8]}) — close it or re-ready it"
    )


async def _dispatch_one(
    db: Any,
    *,
    officer_thread_id: str,
    officer_meta: dict[str, Any],
    capacity_lineage: Sequence[str],
    project_id: str,
    owner_user_id: Optional[str],
    pool: str,
    category: str,
    ticket: dict[str, Any],
    provision_repo: Optional[ProvisionFn],
    trigger_dispatch: Optional[Callable[[], None]],
    enforce_grants: Optional[GrantsFn],
) -> Optional[dict[str, Any]]:
    """Claim + create in one transaction, then provision and nudge.

    Returns the created job row, or None when admission refused (a full pool, a
    racing claim). Raises only on genuinely unexpected faults, which the caller
    logs without touching the breaker — infra failures are not job failures.
    """
    classification = ticket["classification"]
    expert = resolve_expert(classification)
    note_id = str(ticket["note_id"])
    title = str(ticket.get("title") or note_id)

    context = {
        "ticket_note_id": note_id,
        "work_category": category,
        "officer_slot": pool,
        # The category contract rides the kickoff, never `instructions` —
        # instructions REPLACES the rendered instructions.md template wholesale.
        "kickoff_message": (
            f"{category_block(category)}\n\n"
            f"TICKET {note_id} — {title}\n\n"
            "Read the full ticket from the project knowledge base "
            f"(kb_read '{note_id}') before you start; the officer's triage "
            "notes live there."
        ),
    }
    # Tick jobs are officer-reviewed by construction. Without this a completion
    # on a non-full-autonomy project parks in pending_review — a lane with a
    # known dead zone — holding its claim forever and double-reviewing what the
    # officer already reviews. It lives in config_override (not context) because
    # that is what both grant PEPs read, and it only survives them because the
    # job is dispatched as `lifecycle`, whose grant class raises the autonomy
    # ceiling to full while keeping the owner's capability grants. Loop jobs
    # carry exactly this pair for exactly this reason.
    config_override: dict[str, Any] = {"autonomy": "full"}

    authorized_at = _aware(ticket.get("ready_at"))

    async with db.acquire() as conn:
        async with conn.transaction():
            try:
                slot_name, slot_patch = await admit_in_transaction(
                    conn,
                    thread_id=officer_thread_id,
                    officer_meta=officer_meta,
                    capacity_lineage=capacity_lineage,
                    requested_slot=pool,
                )
            except SlotAdmissionError as exc:
                logger.debug(
                    "officer backlog: pool %s full (%s)", pool, exc, exc_info=False
                )
                return None
            if slot_name:
                context["officer_slot"] = slot_name
            if slot_patch:
                config_override.update(slot_patch)

            # Re-read the claim under the lock. The eligibility pass ran on a
            # different connection, so a racing replica could have claimed this
            # ticket since. uq_jobs_active_ticket_claim would refuse the INSERT
            # anyway, but losing that race is normal contention, not an
            # incident: catching it here makes it a quiet skip instead of a
            # stack trace every time two replicas tick together. (Only the
            # app-side half can be closed this way — ready_at lives in the
            # vector DB and cannot join this transaction, which is the residual
            # window §5 accepts and the guidance lane steers around.)
            claimed = await conn.fetchval(
                """
                SELECT MAX(created_at)
                  FROM jobs
                 WHERE project_id = $1 AND context->>'ticket_note_id' = $2
                """,
                _uuid_or_none(project_id),
                note_id,
            )
            claimed_at = _aware(claimed)
            if (
                claimed_at is not None
                and authorized_at is not None
                and claimed_at >= authorized_at
            ):
                logger.info(
                    "officer=%s pool=%s skip=%s claimed since eligibility read",
                    officer_thread_id[:8],
                    pool,
                    note_id,
                )
                return None

            # Explicit grant check against the post owner — the internal spawn
            # path bypasses the endpoint's PEP, and a slot that pins a VM
            # backend or a restricted model must still be something this owner
            # may actually be granted. Resolved under the SAME runner class the
            # job will dispatch as, or the autonomy exemption stamped above
            # would be refused here on every review-ceiling project.
            if enforce_grants is not None:
                await enforce_grants(
                    config_override,
                    user_id=owner_user_id,
                    project_ids=[project_id] if project_id else [],
                )

            job = await db.create_job(
                description=f"[{category}] {title}",
                config_name=expert,
                config_override=config_override or None,
                context=context,
                user_id=owner_user_id,
                project_id=project_id,
                created_by_thread_id=officer_thread_id,
                wake_on_complete=True,
                # Not a bespoke class: `lifecycle` is exactly "system subjob
                # with the owner's capability grants and a full autonomy
                # ceiling", which is what a tick job is. jobs_runner_kind_check
                # accepts user | lifecycle | service and nothing else.
                runner_kind="lifecycle",
                conn=conn,
            )

    if provision_repo is not None:
        try:
            await provision_repo(job, category=category)
        except Exception:
            logger.exception(
                "officer backlog: repo provisioning failed for job %s — sealing",
                str(job.get("id"))[:8],
            )
            try:
                await db.update_job_status(
                    str(job["id"]),
                    status="failed",
                    error_message="officer backlog: repo provisioning failed",
                )
            except Exception:
                logger.exception(
                    "officer backlog: could not seal unprovisioned job %s",
                    str(job.get("id"))[:8],
                )
            raise

    if trigger_dispatch is not None:
        try:
            trigger_dispatch()
        except Exception:
            logger.exception("officer backlog: dispatch nudge raised (non-fatal)")

    return job


async def tick_officer(
    db: Any,
    vector_db: Any,
    officer_row: dict[str, Any],
    *,
    provision_repo: Optional[ProvisionFn] = None,
    trigger_dispatch: Optional[Callable[[], None]] = None,
    enforce_grants: Optional[GrantsFn] = None,
    usage_ledger: Any = None,
    notify: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """One officer's pass. Never raises — a bad post must not stop the fleet."""
    now = now or _now()
    counts = {"dispatched": 0, "skipped": 0, "breakers_opened": 0, "wakes": 0}

    thread_id = str(officer_row.get("id") or "")
    project_id = str(officer_row.get("project_id") or "")
    if not thread_id or not project_id or vector_db is None:
        return counts

    meta = _officer_meta(officer_row)
    if not auto_pull_enabled(meta):
        return counts
    if officer_is_held(meta):
        logger.debug("officer backlog: %s held — skipping", thread_id[:8])
        return counts

    pools = pools_from_meta(meta)
    if not pools:
        return counts

    if await _spend_exhausted(
        usage_ledger,
        project_id=project_id,
        ceiling=float(meta.get("worker_spend_ceiling_daily") or 0.0),
    ):
        logger.info("officer=%s skip=century-spend-ceiling", thread_id[:8])
        counts["skipped"] += 1
        return counts

    state = _officer_state(officer_row)
    lineage = await db.get_officer_capacity_lineage(thread_id)
    owner_user_id = officer_row.get("user_id") or officer_row.get("owner_user_id")

    state_patch: dict[str, Any] = {}
    breakers = dict(state.get("backlog_breakers") or {})
    floor_wakes = dict(state.get("backlog_floor_wakes") or {})

    # Stale claims: computed once for the whole post, recorded for the sitrep.
    open_claims = await db.list_officer_slot_claims(lineage, limit=50)
    stalled = stale_claims(open_claims, now)
    state_patch["backlog_stale_claims"] = stalled
    for claim in stalled:
        if (
            claim["status"] == "pending_review"
            and claim["age_hours"] >= PENDING_REVIEW_PAGE_HOURS
            and notify is not None
        ):
            try:
                await notify(
                    db,
                    project_id,
                    source="backlog_stale_claim",
                    dedup_key=f"stale-claim:{claim['job_id']}",
                    payload=claim,
                )
                counts["wakes"] += 1
            except Exception:
                logger.warning(
                    "officer backlog: stale-claim page failed", exc_info=True
                )

    for pool, spec in sorted(pools.items()):
        category = str(spec["category"])
        if breaker_is_open(state, pool, now):
            logger.info("officer=%s pool=%s skip=breaker-open", thread_id[:8], pool)
            counts["skipped"] += 1
            continue

        # Breaker evaluation runs BEFORE the pull, on the pool's own history.
        pool_history = await db.list_officer_slot_claims(
            lineage, slot=pool, include_terminal=True, limit=10
        )
        terminal_history = [
            job
            for job in pool_history
            if job.get("status") in ("completed", "failed", "cancelled")
        ]
        tripped = evaluate_breaker(terminal_history, state, pool)
        if tripped:
            tripped["until"] = (
                now + timedelta(minutes=BREAKER_OPEN_MINUTES)
            ).isoformat()
            breakers[pool] = tripped
            state_patch["backlog_breakers"] = breakers
            counts["breakers_opened"] += 1
            logger.warning(
                "officer=%s pool=%s breaker=opened cause=%s",
                thread_id[:8],
                pool,
                tripped["cause"],
            )
            continue

        slot_ceiling = float(spec.get("spend_ceiling_daily") or 0.0)
        if slot_ceiling:
            pool_job_ids = [
                str(job["id"])
                for job in await db.list_officer_slot_claims(
                    lineage, slot=pool, include_terminal=True, limit=100
                )
            ]
            if await _spend_exhausted(
                usage_ledger,
                project_id=project_id,
                ceiling=slot_ceiling,
                ref_ids=pool_job_ids,
            ):
                logger.info(
                    "officer=%s pool=%s skip=slot-spend-ceiling", thread_id[:8], pool
                )
                counts["skipped"] += 1
                continue

        try:
            rows, _counts = await fetch_backlog(
                vector_db,
                project_id,
                require_tags=[READY_TAG, category_tag(category)],
                limit=_CANDIDATE_LIMIT,
            )
        except Exception:
            # A KB/pgvector outage skips cleanly this tick. Infra failures
            # never feed breakers.
            logger.warning(
                "officer=%s pool=%s skip=kb-unavailable", thread_id[:8], pool
            )
            counts["skipped"] += 1
            continue

        claims = await db.newest_ticket_claims(
            project_id, [str(r.get("note_id")) for r in rows]
        )
        ready, notes = eligible_tickets(rows, claims, now)
        for note in notes:
            logger.info("officer=%s pool=%s skip=%s", thread_id[:8], pool, note)

        # Floor = the pool's own slot count (§13.2): if every agent in the pool
        # lands at once, each must find a ticket waiting. The floor therefore
        # scales with the kit rather than needing its own knob.
        floor = int(spec.get("count") or 0)
        if len(ready) < floor:
            last = _aware(floor_wakes.get(pool))
            if last is None or (now - last) >= timedelta(
                hours=FLOOR_WAKE_DEBOUNCE_HOURS
            ):
                if notify is not None:
                    try:
                        await notify(
                            db,
                            project_id,
                            source="backlog_floor_breach",
                            dedup_key=f"floor:{pool}",
                            payload={
                                "pool": pool,
                                "category": category,
                                "ready": len(ready),
                                "floor": floor,
                            },
                        )
                        counts["wakes"] += 1
                    except Exception:
                        logger.warning(
                            "officer backlog: floor wake failed", exc_info=True
                        )
                floor_wakes[pool] = now.isoformat()
                state_patch["backlog_floor_wakes"] = floor_wakes

        if not ready:
            continue

        ticket = ready[0]
        if category == EXECUTOR:
            blocked = await _executor_blocked(
                db,
                vector_db,
                capacity_lineage=lineage,
                project_id=project_id,
                ticket_is_parallel_safe=ticket["classification"].parallel_safe,
            )
            if blocked:
                logger.info("officer=%s pool=%s skip=%s", thread_id[:8], pool, blocked)
                counts["skipped"] += 1
                continue

        try:
            job = await _dispatch_one(
                db,
                officer_thread_id=thread_id,
                officer_meta=meta,
                capacity_lineage=lineage,
                project_id=project_id,
                owner_user_id=str(owner_user_id) if owner_user_id else None,
                pool=pool,
                category=category,
                ticket=ticket,
                provision_repo=provision_repo,
                trigger_dispatch=trigger_dispatch,
                enforce_grants=enforce_grants,
            )
        except Exception:
            # Includes the unique-index refusal of a racing double-claim. Not a
            # job failure: nothing ran, so the breaker stays shut.
            logger.warning(
                "officer=%s pool=%s skip=dispatch-failed ticket=%s",
                thread_id[:8],
                pool,
                ticket.get("note_id"),
                exc_info=True,
            )
            counts["skipped"] += 1
            continue

        if job is None:
            counts["skipped"] += 1
            continue
        counts["dispatched"] += 1
        logger.info(
            "officer=%s pool=%s dispatched=%s/%s",
            thread_id[:8],
            pool,
            ticket.get("note_id"),
            str(job.get("id"))[:8],
        )

    if state_patch:
        try:
            await db.merge_thread_officer_state(thread_id, state_patch)
        except Exception:
            logger.warning(
                "officer backlog: officer_state merge failed for %s",
                thread_id[:8],
                exc_info=True,
            )

    return counts


async def officer_backlog_tick_once(
    db: Any,
    vector_db: Any,
    *,
    provision_repo: Optional[ProvisionFn] = None,
    trigger_dispatch: Optional[Callable[[], None]] = None,
    enforce_grants: Optional[GrantsFn] = None,
    usage_ledger: Any = None,
    notify: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """One tick across every commissioned officer. Errors isolate per officer."""
    totals = {"dispatched": 0, "skipped": 0, "breakers_opened": 0, "wakes": 0}
    try:
        officers = await db.list_officer_threads()
    except Exception:
        logger.exception("officer backlog: officer listing failed")
        return totals

    for officer_row in officers:
        try:
            counts = await tick_officer(
                db,
                vector_db,
                officer_row,
                provision_repo=provision_repo,
                trigger_dispatch=trigger_dispatch,
                enforce_grants=enforce_grants,
                usage_ledger=usage_ledger,
                notify=notify,
                now=now,
            )
        except Exception:
            logger.exception(
                "officer backlog: tick failed for officer %s (continuing)",
                str(officer_row.get("id"))[:8],
            )
            continue
        for key, value in counts.items():
            totals[key] += value
    return totals


async def officer_backlog_tick_loop(
    db: Any,
    vector_db: Any,
    shutdown_event: asyncio.Event,
    *,
    provision_repo: Optional[ProvisionFn] = None,
    trigger_dispatch: Optional[Callable[[], None]] = None,
    enforce_grants: Optional[GrantsFn] = None,
    usage_ledger: Any = None,
    notify: Any = None,
) -> None:
    """The ~60s tick, mounted leader-gated from main.py's lifespan."""
    logger.info(
        "Officer backlog tick started (tick=%ds, stale_claim=%.1fh, breaker=%.0fm)",
        TICK_SECONDS,
        STALE_CLAIM_HOURS,
        BREAKER_OPEN_MINUTES,
    )
    while not shutdown_event.is_set():
        try:
            counts = await officer_backlog_tick_once(
                db,
                vector_db,
                provision_repo=provision_repo,
                trigger_dispatch=trigger_dispatch,
                enforce_grants=enforce_grants,
                usage_ledger=usage_ledger,
                notify=notify,
            )
            if any(counts.values()):
                logger.info("Officer backlog tick: %s", counts)
        except Exception:
            logger.exception("Officer backlog tick raised; will retry next tick")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Officer backlog tick stopped")


__all__ = [
    "BREAKER_FAILURES",
    "BREAKER_OPEN_MINUTES",
    "FLOOR_WAKE_DEBOUNCE_HOURS",
    "PENDING_REVIEW_PAGE_HOURS",
    "STALE_CLAIM_HOURS",
    "TICK_SECONDS",
    "auto_pull_enabled",
    "breaker_is_open",
    "eligible_tickets",
    "evaluate_breaker",
    "officer_backlog_tick_loop",
    "officer_backlog_tick_once",
    "pools_from_meta",
    "stale_claims",
    "tick_officer",
]
