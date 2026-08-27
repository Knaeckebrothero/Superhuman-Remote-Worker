"""Durable attached-client presence for stateless sessions.

The browser never announces an execution lane and never receives one.  The
owner-gated orchestrator SSE stream attests that a client is attached by
renewing one database-clock TTL row per thread.  Executors consume that signal
only for permission-card lifetime and ``awaiting_user`` UX.

This module deliberately does *not* make presence an ownership primitive.  A
live run-queue lease still owns stateless writes; presence is cooperative and
may retain a card or delay attention sleep, but it can never authorize a write
or keep a lease/finalizer alive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

    Connection = asyncpg.Connection
    Pool = asyncpg.Pool
else:
    Connection = Any
    Pool = Any


DEFAULT_PRESENCE_TTL_SECONDS = 30.0
DEFAULT_PRESENCE_RENEW_SECONDS = 10.0

_THREAD_ADVISORY_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))"
)


@dataclass(frozen=True)
class PresenceRefresh:
    """Result of one SSE establishment/renewal."""

    served: bool
    became_live: bool


@dataclass(frozen=True)
class PermissionExpiryResult:
    """Result of one stateless permission timeout boundary."""

    status: str | None
    owner_live: bool
    live_for_seconds: float | None


async def refresh_thread_presence(
    pool: Pool,
    *,
    thread_id: UUID | str,
    ttl_seconds: float = DEFAULT_PRESENCE_TTL_SECONDS,
    establish: bool = False,
) -> PresenceRefresh:
    """Establish or renew one stateless thread's attached-client TTL.

    Establishment mirrors the pinned lane's 0→1 subscriber transition: if the
    prior TTL was absent/expired, an ``awaiting_user`` thread becomes active.
    Periodic renewals never change thread status, which preserves polite-mode's
    explicit natural pause while a viewer remains attached.
    """

    ttl = float(ttl_seconds)
    if ttl <= 0:
        raise ValueError("presence TTL must be positive")

    thread_key = str(thread_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serializes first-tab detection with renewals and the stateless
            # natural-pause oracle, including the no-row case that a row lock
            # cannot cover. Hash collisions only serialize unrelated threads.
            await conn.execute(_THREAD_ADVISORY_LOCK_SQL, thread_key)
            lane = await conn.fetchval(
                "SELECT execution_lane FROM threads WHERE id = $1::uuid FOR SHARE",
                thread_key,
            )
            if lane != "stateless":
                return PresenceRefresh(served=False, became_live=False)

            was_live = bool(
                await conn.fetchval(
                    "SELECT expires_at > clock_timestamp() "
                    "FROM thread_client_presence WHERE thread_id = $1::uuid",
                    thread_key,
                )
            )
            await conn.execute(
                "INSERT INTO thread_client_presence "
                "       (thread_id, refreshed_at, expires_at) "
                "VALUES ($1::uuid, clock_timestamp(), "
                "        clock_timestamp() + make_interval(secs => $2::double precision)) "
                "ON CONFLICT (thread_id) DO UPDATE "
                "SET refreshed_at = clock_timestamp(), "
                "    expires_at = clock_timestamp() "
                "        + make_interval(secs => $2::double precision)",
                thread_key,
                ttl,
            )
            became_live = not was_live
            if establish and became_live:
                # Do not refresh last_activity on periodic heartbeats. The
                # status transition itself mirrors the old first-subscriber
                # reattach behavior and disarms attention sleep.
                await conn.execute(
                    "UPDATE threads "
                    "SET status = 'active', awaiting_user_since = NULL, "
                    "    extend_count = 0, last_activity = clock_timestamp() "
                    "WHERE id = $1::uuid "
                    "  AND execution_lane = 'stateless' "
                    "  AND status = 'awaiting_user'",
                    thread_key,
                )
            return PresenceRefresh(served=True, became_live=became_live)


async def mark_stateless_natural_pause(
    pool: Pool,
    *,
    thread_id: UUID | str,
    lease_token: int,
    require_untethered: bool,
) -> bool:
    """Best-effort stateless ``active → awaiting_user`` lifecycle marker.

    This is an advisory lifecycle write, not turn output. The exact lease
    predicate rejects an already-stale caller, while the durable turn/journal
    stores retain their stronger ``FOR SHARE`` persist fences. Taking that
    queue lock here and then the threads row would invert ``/input``'s existing
    threads→queue order and introduce a deadlock cycle.
    """

    thread_key = str(thread_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_THREAD_ADVISORY_LOCK_SQL, thread_key)
            updated = await conn.fetchval(
                "UPDATE threads AS thread "
                "SET status = 'awaiting_user', "
                "    awaiting_user_since = CASE "
                "        WHEN thread.status = 'awaiting_user' "
                "        THEN thread.awaiting_user_since ELSE clock_timestamp() END, "
                "    extend_count = CASE "
                "        WHEN thread.status = 'awaiting_user' "
                "        THEN thread.extend_count ELSE 0 END, "
                "    last_activity = clock_timestamp() "
                "WHERE thread.id = $1::uuid "
                "  AND thread.execution_lane = 'stateless' "
                "  AND thread.status IN ('active', 'awaiting_user') "
                "  AND EXISTS ("
                "      SELECT 1 FROM run_queue AS queue "
                "      WHERE queue.unit_id = thread.id "
                "        AND queue.unit_kind = 'session_turn' "
                "        AND queue.state = 'leased' "
                "        AND queue.lease_token = $2::bigint"
                "  ) "
                "  AND (NOT $3::boolean OR NOT EXISTS ("
                "      SELECT 1 FROM thread_client_presence AS presence "
                "      WHERE presence.thread_id = thread.id "
                "        AND presence.expires_at > clock_timestamp()"
                "  )) "
                "RETURNING thread.id",
                thread_key,
                int(lease_token),
                bool(require_untethered),
            )
            return updated is not None


async def expire_permission_if_untethered(
    conn: Connection,
    *,
    thread_id: UUID | str,
    request_id: UUID | str,
    lease_token: int,
) -> PermissionExpiryResult:
    """CAS-expire a pending stateless gate only when no SSE TTL is live.

    The data-modifying CTE holds the run-queue row ``FOR SHARE`` for the whole
    statement, so a concurrent steal cannot land between the owner check and
    the irreversible permission update. A live presence returns its remaining
    TTL so the caller can recheck at expiry rather than sleep another full
    permission polling interval.
    """

    async with conn.transaction():
        # This must be its own statement before the expiry statement. Under
        # READ COMMITTED, a statement that starts *then* waits on the advisory
        # lock retains its pre-renewal snapshot and can still expire the row.
        # Acquiring first gives the following statement a fresh post-renewal
        # snapshot while retaining the lock through commit.
        await conn.execute(_THREAD_ADVISORY_LOCK_SQL, str(thread_id))
        row = await conn.fetchrow(
            "WITH owner AS MATERIALIZED ("
            "    SELECT queue.unit_id "
            "    FROM run_queue AS queue "
            "    WHERE queue.unit_id = $1::uuid "
            "      AND queue.unit_kind = 'session_turn' "
            "      AND queue.state = 'leased' "
            "      AND queue.lease_token = $3::bigint "
            "    FOR SHARE OF queue"
            "), live_presence AS MATERIALIZED ("
            "    SELECT GREATEST(0::double precision, "
            "             EXTRACT(EPOCH FROM "
            "                 (presence.expires_at - clock_timestamp()))) "
            "           AS remaining_seconds "
            "    FROM thread_client_presence AS presence "
            "    WHERE presence.thread_id = $1::uuid "
            "      AND presence.expires_at > clock_timestamp()"
            "), expired AS ("
            "    UPDATE thread_permission_requests AS request "
            "    SET status = 'expired', decided_at = clock_timestamp(), "
            "        decided_by = 'system' "
            "    WHERE request.id = $2::uuid "
            "      AND request.thread_id = $1::uuid "
            "      AND request.status = 'pending' "
            "      AND EXISTS (SELECT 1 FROM owner) "
            "      AND NOT EXISTS (SELECT 1 FROM live_presence) "
            "    RETURNING request.status"
            ") "
            "SELECT COALESCE((SELECT status FROM expired), request.status) "
            "           AS status, "
            "       EXISTS (SELECT 1 FROM owner) AS owner_live, "
            "       (SELECT remaining_seconds FROM live_presence) "
            "           AS live_for_seconds "
            "FROM thread_permission_requests AS request "
            "WHERE request.id = $2::uuid AND request.thread_id = $1::uuid",
            str(thread_id),
            str(request_id),
            int(lease_token),
        )
    if row is None:
        return PermissionExpiryResult(
            status=None, owner_live=False, live_for_seconds=None
        )
    remaining = row["live_for_seconds"]
    return PermissionExpiryResult(
        status=str(row["status"]) if row["status"] is not None else None,
        owner_live=bool(row["owner_live"]),
        live_for_seconds=float(remaining) if remaining is not None else None,
    )


async def promote_expired_stateless_pauses(
    pool: Pool,
    *,
    limit: int = 50,
) -> list[str]:
    """Promote idle stateless threads after the final client's grace expires.

    A disconnect deliberately leaves its TTL row in place so reload and
    multi-tab handoff do not flicker. If a turn reaches its natural pause
    during that grace, the executor correctly skips ``awaiting_user``; this
    leader-run convergence pass arms it once the durable queue says the turn
    is fully done and the TTL really expired.
    """

    bounded_limit = max(1, min(int(limit), 500))
    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            "SELECT thread.id "
            "FROM threads AS thread "
            "JOIN run_queue AS queue ON queue.unit_id = thread.id "
            "WHERE thread.execution_lane = 'stateless' "
            "  AND thread.status = 'active' "
            "  AND thread.total_turns > 0 "
            "  AND queue.unit_kind = 'session_turn' "
            "  AND queue.state = 'done' "
            "  AND COALESCE(thread.metadata->'config_override'->'officer'"
            "                         ->>'enabled', 'false') <> 'true' "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM thread_client_presence AS presence "
            "      WHERE presence.thread_id = thread.id "
            "        AND presence.expires_at > clock_timestamp()"
            "  ) "
            "ORDER BY thread.last_activity ASC NULLS FIRST "
            "LIMIT $1",
            bounded_limit,
        )

    promoted: list[str] = []
    for candidate in candidates:
        thread_key = str(candidate["id"])
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_THREAD_ADVISORY_LOCK_SQL, thread_key)
                updated = await conn.fetchval(
                    "UPDATE threads AS thread "
                    "SET status = 'awaiting_user', "
                    "    awaiting_user_since = clock_timestamp(), "
                    "    extend_count = 0, "
                    "    last_activity = clock_timestamp() "
                    "WHERE thread.id = $1::uuid "
                    "  AND thread.execution_lane = 'stateless' "
                    "  AND thread.status = 'active' "
                    "  AND thread.total_turns > 0 "
                    "  AND COALESCE(thread.metadata->'config_override'->'officer'"
                    "                         ->>'enabled', 'false') <> 'true' "
                    "  AND EXISTS ("
                    "      SELECT 1 FROM run_queue AS queue "
                    "      WHERE queue.unit_id = thread.id "
                    "        AND queue.unit_kind = 'session_turn' "
                    "        AND queue.state = 'done'"
                    "  ) "
                    "  AND NOT EXISTS ("
                    "      SELECT 1 FROM thread_client_presence AS presence "
                    "      WHERE presence.thread_id = thread.id "
                    "        AND presence.expires_at > clock_timestamp()"
                    "  ) "
                    "RETURNING thread.id",
                    thread_key,
                )
                if updated is not None:
                    promoted.append(str(updated))
    return promoted
