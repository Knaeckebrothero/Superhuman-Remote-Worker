"""Hand-rolled monthly partition maintenance for the Postgres audit store.

Owns *all* partition DDL after ``migrations/audit/0001_initial.sql``: it creates
months ahead of the write head (N+2 lookahead), keeps the partitioned parents'
planner stats fresh (autovacuum never ANALYZEs partitioned parents), and exposes
a health snapshot for the orchestrator health payload.

Why hand-rolled and not ``pg_partman``: the stock ``postgres`` images the chart
deploys don't ship the extension, and the migration runner + lifespan already
provide the scheduling and locking primitives. The honest budget is ~200 LoC for
three tables at monthly cadence — lighter than an extension plus its background
worker / cron config. Mechanics verified against the live cluster (PG 15.18;
identical on 16): ``CREATE TABLE (LIKE parent INCLUDING ALL)`` copies
indexes/compression/checks, ATTACH matches them, per-leaf reloptions land, and a
missing-partition insert fails loudly with SQLSTATE ``23514``.

Design: ``docs/features/postgres_audit_store_implementation.md`` §4.

**Lean-cut scope (PR 1):** creation + ANALYZE + status/alarms are live.
Auto-retention (``retire_partitions``: DETACH CONCURRENTLY → grace → DROP) is
deliberately deferred — at the thesis/dev data scale we will not approach the
90/365-day windows for many months, and the detach/drop protocol is the
fiddliest, highest-blast-radius part of the module (it must run *outside* a
transaction on a dedicated connection, and warrants its own retention drill).
``retire_partitions`` is a documented stub below; re-enabling retention is a
single-function implementation plus wiring it into ``maintenance_pass``.

**Hard boundaries** (true regardless of the lean cut):
- No DEFAULT partition, ever — it silently parks misrouted rows, then blocks
  ATTACH validation and forbids DETACH CONCURRENTLY.
- Never trust ``IF NOT EXISTS`` / name existence as idempotency: truth comes
  from ``pg_inherits`` (attached-or-not), never from a name match.
- No DDL inside the migration runner's transaction (this module is a runtime
  task for exactly that reason).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

# table -> retention days (retention itself is deferred in PR 1; the windows are
# kept here so status/alarms and the future retire path share one source).
PARENTS: dict[str, int] = {
    "llm_requests": 90,
    "agent_audit": 90,
    "chat_history": 365,
}

LOOKAHEAD_MONTHS = 2  # current month + 2 pre-created (N+2)
GRACE_DAYS = 3  # detach -> drop buffer (used once retention is re-enabled)

# Advisory-lock key for partition DDL. Distinct from migrate.py's
# 0x5352575F4D4947 ("SRW_MIG") so a slow maintenance pass can never block
# startup migrations on the audit instance. "SRW_AUDT" packed into int64.
MAINT_LOCK_ID = 0x5352575F41554454

# Partition naming: <parent>_pYYYY_MM (UTC months).
PART_SUFFIX_RE = re.compile(r"_p(\d{4})_(\d{2})$")

# MUST stay in sync with the per-leaf reloptions in 0001_initial.sql.
STORAGE_PARAMS = (
    "fillfactor = 100, "
    "autovacuum_vacuum_insert_scale_factor = 0.05, "
    "autovacuum_vacuum_insert_threshold = 10000, "
    "autovacuum_analyze_scale_factor = 0.02, "
    "autovacuum_freeze_min_age = 0"
)

# Lookahead alarm thresholds (days until the newest partition's upper bound).
_LOOKAHEAD_WARN_DAYS = 45
_LOOKAHEAD_CRIT_DAYS = 31


def _quote_ident(ident: str) -> str:
    """Double-quote a SQL identifier (defensive — all callers pass controlled
    names derived from the PARENTS dict + computed YYYY_MM suffixes)."""
    return '"' + ident.replace('"', '""') + '"'


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    """Return (year, month) n months after the given (year, month). 1-based month."""
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def _month_bounds(year: int, month: int) -> tuple[str, str, str]:
    """Return (partition_suffix, from_date, to_date) for a month.

    Bounds are ``YYYY-MM-01`` date strings; with ``timezone='UTC'`` set on the
    creating transaction they pin midnight-UTC month boundaries, matching the
    bootstrap DO block in 0001_initial.sql.
    """
    ny, nm = _add_months(year, month, 1)
    return (
        f"{year:04d}_{month:02d}",
        f"{year:04d}-{month:02d}-01",
        f"{ny:04d}-{nm:02d}-01",
    )


async def _attached_children(
    conn: asyncpg.Connection, parent: str
) -> list[asyncpg.Record]:
    """Attached + detach-pending children of ``parent`` from the catalog."""
    return await conn.fetch(
        """
        SELECT c.relname AS relname, i.inhdetachpending AS detach_pending
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = $1
        """,
        parent,
    )


async def ensure_partitions(
    pool: asyncpg.Pool, *, lookahead_months: int = LOOKAHEAD_MONTHS
) -> list[str]:
    """Create the current month + ``lookahead_months`` ahead for every parent.

    Idempotent and concurrency-safe: one transaction per parent, serialized by a
    transaction-scoped advisory lock, with truth read from ``pg_inherits`` (never
    name existence). Uses the low-lock CREATE+ATTACH recipe (SHARE UPDATE
    EXCLUSIVE on the parent — writers never stall) rather than ``CREATE ...
    PARTITION OF`` (ACCESS EXCLUSIVE). Returns the names of partitions created or
    attached this pass.
    """
    created: list[str] = []

    async with pool.acquire() as conn:
        for parent in PARENTS:
            try:
                async with conn.transaction():
                    # First statement: serialize concurrent creators (future HA
                    # replicas / overlap with a slow prior pass). xact-scoped, so
                    # a crashed pass can't leak it.
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1)", MAINT_LOCK_ID
                    )
                    # Deterministic month boundaries + fail-fast on ATTACH queueing
                    # behind a long query (the N+2 buffer absorbs a missed pass).
                    await conn.execute("SET LOCAL timezone = 'UTC'")
                    await conn.execute("SET LOCAL lock_timeout = '5s'")

                    rows = await _attached_children(conn, parent)
                    attached = {r["relname"] for r in rows if not r["detach_pending"]}

                    now = datetime.now(timezone.utc)
                    for i in range(lookahead_months + 1):
                        y, m = _add_months(now.year, now.month, i)
                        suffix, lo, hi = _month_bounds(y, m)
                        part = f"{parent}_p{suffix}"
                        if part in attached:
                            continue
                        if await _create_and_attach(conn, parent, part, lo, hi):
                            created.append(part)
            except asyncpg.exceptions.LockNotAvailableError:
                # ATTACH queued behind a long-running transaction past the 5s
                # timeout; the transaction rolled back cleanly. Retry next pass.
                logger.warning(
                    "audit_partitions: lock_timeout creating partitions for %s; "
                    "retrying next pass (N+2 buffer covers it)",
                    parent,
                )
            except Exception:
                logger.exception(
                    "audit_partitions: failed ensuring partitions for %s (non-fatal)",
                    parent,
                )

    return created


async def _create_and_attach(
    conn: asyncpg.Connection, parent: str, part: str, lo: str, hi: str
) -> bool:
    """Create (or resume) and ATTACH one leaf partition. Returns True on attach.

    Crash recovery: a name that already exists *standalone* (a prior pass died
    between CREATE and ATTACH) is resumed if empty, or ERROR-logged and skipped
    if it somehow holds rows (operator decision — never silently absorb data).
    """
    qparent, qpart = _quote_ident(parent), _quote_ident(part)
    oid = await conn.fetchval("SELECT to_regclass($1)", part)

    if oid is not None:
        # Exists but not attached (attached names were filtered out by the
        # caller). Only resume an empty standalone table.
        rowcount = await conn.fetchval(f"SELECT count(*) FROM {qpart}")
        if rowcount:
            logger.error(
                "audit_partitions: %s exists standalone with %s row(s); not "
                "auto-attaching — operator must inspect/clear it",
                part,
                rowcount,
            )
            return False
    else:
        await conn.execute(f"CREATE TABLE {qpart} (LIKE {qparent} INCLUDING ALL)")
        await conn.execute(f"ALTER TABLE {qpart} SET ({STORAGE_PARAMS})")

    # A matching CHECK lets ATTACH skip the validation scan (free on an empty
    # leaf, load-bearing if a non-empty standalone is ever resumed). Idempotent.
    bound = f"{part}_bound"
    qbound = _quote_ident(bound)
    await conn.execute(f"ALTER TABLE {qpart} DROP CONSTRAINT IF EXISTS {qbound}")
    await conn.execute(
        f"ALTER TABLE {qpart} ADD CONSTRAINT {qbound} "
        f"CHECK (\"timestamp\" >= '{lo}' AND \"timestamp\" < '{hi}')"
    )
    await conn.execute(
        f"ALTER TABLE {qparent} ATTACH PARTITION {qpart} "
        f"FOR VALUES FROM ('{lo}') TO ('{hi}')"
    )
    await conn.execute(f"ALTER TABLE {qpart} DROP CONSTRAINT IF EXISTS {qbound}")
    logger.info("audit_partitions: created partition %s [%s, %s)", part, lo, hi)
    return True


async def retire_partitions(
    pool: asyncpg.Pool,
    *,
    retention: dict[str, int] = PARENTS,
    grace_days: int = GRACE_DAYS,
) -> dict:
    """DEFERRED in PR 1 (lean cut) — no-op.

    The full protocol (spec: implementation package §4) detaches partitions whose
    upper bound is older than the retention window via ``DETACH PARTITION ...
    CONCURRENTLY``, waits ``grace_days``, then DROPs the standalone table. It must
    run on a dedicated connection *outside* any transaction (DETACH CONCURRENTLY
    commits internally), guarded by a session-scoped ``pg_try_advisory_lock``,
    strictly serial per parent (at most one pending-detach per table), and
    FINALIZE any partition stuck mid-detach before new work.

    Re-enable by implementing that here and calling it from ``maintenance_pass``;
    add the retention drill (backdated partition → detach → finalize → drop) to
    the test suite at the same time.
    """
    logger.debug(
        "audit_partitions: retention deferred (PR 1 lean cut); partitions accumulate"
    )
    return {"detached": [], "dropped": [], "deferred": True}


async def analyze_parents(
    pool: asyncpg.Pool, *, max_age_days: int = 7, force: bool = False
) -> list[str]:
    """ANALYZE the partitioned parents when their stats are stale or ``force``.

    Autovacuum never ANALYZEs partitioned parents (only their leaves), and stale
    parent stats are a documented production failure (row estimates off by 10^6).
    On PG 15/16 a parent ANALYZE recurses into every leaf — fine at our
    retention-bounded sizes given the 6h cadence + 7-day staleness floor.
    """
    analyzed: list[str] = []
    async with pool.acquire() as conn:
        for parent in PARENTS:
            try:
                if not force:
                    last = await conn.fetchval(
                        """
                        SELECT GREATEST(last_analyze, last_autoanalyze)
                        FROM pg_stat_user_tables WHERE relname = $1
                        """,
                        parent,
                    )
                    if last is not None:
                        age_days = (
                            datetime.now(timezone.utc) - last
                        ).total_seconds() / 86400
                        if age_days < max_age_days:
                            continue
                await conn.execute(f"ANALYZE {_quote_ident(parent)}")
                analyzed.append(parent)
            except Exception:
                logger.exception(
                    "audit_partitions: ANALYZE %s failed (non-fatal)", parent
                )
    return analyzed


async def partition_status(pool: asyncpg.Pool) -> dict:
    """Per-parent health snapshot. Pure reads, no locks — safe for the health
    payload. ``days_until_unpartitioned`` is the control metric for the
    lookahead alarms.
    """
    now = datetime.now(timezone.utc)
    status: dict[str, dict] = {}

    async with pool.acquire() as conn:
        for parent in PARENTS:
            rows = await _attached_children(conn, parent)
            attached = [r for r in rows if not r["detach_pending"]]
            detach_pending = sum(1 for r in rows if r["detach_pending"])

            newest_upper: Optional[datetime] = None
            for r in attached:
                mo = PART_SUFFIX_RE.search(r["relname"])
                if not mo:
                    continue
                # Upper bound = first day of the month AFTER the partition's month
                # (our naming is authoritative for partitions we created/attached).
                y, m = int(mo.group(1)), int(mo.group(2))
                uy, um = _add_months(y, m, 1)
                upper = datetime(uy, um, 1, tzinfo=timezone.utc)
                if newest_upper is None or upper > newest_upper:
                    newest_upper = upper

            last_analyze = await conn.fetchval(
                """
                SELECT GREATEST(last_analyze, last_autoanalyze)
                FROM pg_stat_user_tables WHERE relname = $1
                """,
                parent,
            )
            awaiting_drop = await conn.fetchval(
                """
                SELECT count(*) FROM pg_class c
                WHERE c.relkind = 'r'
                  AND c.relname ~ ('^' || $1 || '_p[0-9]{4}_[0-9]{2}$')
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid
                  )
                """,
                parent,
            )

            status[parent] = {
                "attached": len(attached),
                "newest_upper_bound": newest_upper,
                "days_until_unpartitioned": (
                    (newest_upper - now).total_seconds() / 86400
                    if newest_upper is not None
                    else None
                ),
                "detach_pending": detach_pending,
                "awaiting_drop": int(awaiting_drop or 0),
                "last_parent_analyze": last_analyze,
            }

    return status


def _emit_alarms(status: dict) -> None:
    """Log-based alarms matching the stack's alerting (no metrics backend)."""
    for parent, s in status.items():
        days = s.get("days_until_unpartitioned")
        if days is None:
            logger.error(
                "audit_partitions.lookahead_critical: %s has no attached "
                "partitions — writes will 23514",
                parent,
            )
        elif days < _LOOKAHEAD_CRIT_DAYS:
            logger.error(
                "audit_partitions.lookahead_critical: %s runs out of partitions "
                "in %.1f days (creation has been failing ~%d+ days)",
                parent,
                days,
                _LOOKAHEAD_WARN_DAYS,
            )
        elif days < _LOOKAHEAD_WARN_DAYS:
            logger.warning(
                "audit_partitions.lookahead_low: %s has %.1f days of partitions left",
                parent,
                days,
            )
        if s.get("detach_pending"):
            logger.error(
                "audit_partitions: %s has %d partition(s) pending detach — "
                "needs FINALIZE",
                parent,
                s["detach_pending"],
            )


async def maintenance_pass(pool: asyncpg.Pool) -> dict:
    """One full pass: ensure -> (retire: deferred) -> analyze -> status + alarm.

    Emits a structured ``audit_partitions.pass_ok`` heartbeat; its absence is
    itself the signal that maintenance has stopped running.
    """
    created = await ensure_partitions(pool)
    retired = await retire_partitions(pool)  # no-op in the lean cut
    analyzed = await analyze_parents(pool, force=bool(created))
    status = await partition_status(pool)
    _emit_alarms(status)

    logger.info(
        "audit_partitions.pass_ok created=%s analyzed=%s retire=%s",
        created,
        analyzed,
        "deferred" if retired.get("deferred") else retired,
    )
    return {
        "created": created,
        "analyzed": analyzed,
        "retired": retired,
        "status": status,
    }


async def maintenance_loop(
    pool: Optional[asyncpg.Pool],
    shutdown_event: asyncio.Event,
    *,
    interval_s: int = 6 * 3600,
    jitter_s: int = 1800,
) -> None:
    """Lifespan task body: run a pass at startup (a pod that slept across a month
    boundary self-heals on boot), then every ~6h with 0–30min jitter.

    Cooperative shutdown via ``shutdown_event`` (the codebase convention for all
    lifespan loops). Every pass is wrapped so a failure ERROR-logs and the loop
    continues — partition maintenance must never crash the orchestrator.
    """
    if pool is None:
        logger.info("audit_partitions: no audit pool — maintenance loop not started")
        return

    logger.info(
        "audit_partitions: maintenance loop started (interval=%ds, jitter<=%ds)",
        interval_s,
        jitter_s,
    )
    while not shutdown_event.is_set():
        try:
            await maintenance_pass(pool)
        except Exception:
            logger.exception("audit_partitions: maintenance pass failed (non-fatal)")

        delay = interval_s + random.uniform(0, jitter_s)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass  # interval elapsed → next pass
    logger.info("audit_partitions: maintenance loop stopped")
