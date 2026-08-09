"""Real-Postgres tests for the run_queue substrate (stateless-agents S1, M1).

Exercises src/shared/run_queue/ against the actual migration
(orchestrator/database/migrations/app/0115_run_queue.sql) on a live scratch
database. SKIP LOCKED claim races, the FOR SHARE persist fence blocking a
reaper steal, and the per-row steal CAS are lock-manager semantics that mocks
cannot represent — these tests are the contract's proof.

Gate: the RUN_QUEUE_TEST_DSN env var must point at a SCRATCH database (the
name must contain "test"; the fixture refuses anything else so the live app
DB can never be truncated). Local k3d flow:

    kubectl --context=k3d-srw -n srw port-forward svc/srw-postgres 55440:5432 &
    # CREATE DATABASE run_queue_test OWNER srw;  (once)
    RUN_QUEUE_TEST_DSN=postgresql://srw:dev_pg_password@localhost:55440/run_queue_test \
        pytest tests/test_run_queue.py -x -q

Without the env var the whole module skips (CI stays green).

Time assertions compare against the DATABASE clock (``now()``) wherever the
contract does, so a skewed local clock cannot flake them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.shared.run_queue import (
    ENQUEUE_DEDUPED,
    ENQUEUE_INPUT_RECORDED,
    ENQUEUE_INSERTED,
    ENQUEUE_PARKED,
    ENQUEUE_REQUEUED,
    ENQUEUE_UPDATED,
    STATE_DONE,
    STATE_LEASED,
    STATE_PARKED,
    STATE_QUEUED,
    UNIT_KIND_BG_TASK,
    UNIT_KIND_SESSION_TURN,
    UNIT_KIND_WORKER_BATCH,
    claim_unit,
    complete_unit,
    enqueue_unit,
    fence_lease,
    heartbeat_unit,
    list_active,
    queue_depth_for,
    reap_expired,
    record_control_seq,
    record_input_seq,
    release_unit,
    unpark_unit,
)
from src.shared.thread_controls import (
    adopt_next_pinned_control_request,
    fetch_next_control_request,
    finalize_control_request,
)

DSN = os.environ.get("RUN_QUEUE_TEST_DSN", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DSN, reason="RUN_QUEUE_TEST_DSN not set (scratch Postgres required)"
    ),
]

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
# Every migration that shapes run_queue, applied in order — the tests run
# against the SAME DDL production does, so a column the SQL contract depends
# on (e.g. 0117's last_leased_by) cannot pass here and fail on a real cluster.
MIGRATION_FILES = [
    _MIGRATIONS_DIR / "0115_run_queue.sql",
    _MIGRATIONS_DIR / "0117_run_queue_affinity.sql",
    _MIGRATIONS_DIR / "0119_thread_control_inbox.sql",
    _MIGRATIONS_DIR / "0120_thread_control_receipt_idx.notx.sql",
    _MIGRATIONS_DIR / "0121_thread_control_validate_constraints.sql",
]

SESSION = UNIT_KIND_SESSION_TURN
WORKER = UNIT_KIND_WORKER_BATCH
BG = UNIT_KIND_BG_TASK


# =============================================================================
# Fixtures
# =============================================================================


def _assert_scratch_dsn() -> None:
    """Refuse to run against anything that is not clearly a scratch DB."""
    dbname = DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in dbname:
        pytest.exit(
            f"RUN_QUEUE_TEST_DSN points at database '{dbname}' — refusing: "
            "the scratch database name must contain 'test' (these tests "
            "DROP and TRUNCATE tables)."
        )


async def _apply_schema() -> None:
    conn = await asyncpg.connect(DSN, timeout=10)
    try:
        await conn.execute("DROP TABLE IF EXISTS thread_events CASCADE")
        await conn.execute("DROP TABLE IF EXISTS thread_control_requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS run_queue CASCADE")
        await conn.execute("DROP TABLE IF EXISTS threads CASCADE")
        await conn.execute("DROP TABLE IF EXISTS agents CASCADE")
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        # Minimal prerequisite stubs: 0115 ALTERs threads and 0119 ALTERs
        # thread_events. The queue API does not otherwise touch either table;
        # they exist here only so the queue-shaping migrations apply verbatim.
        await conn.execute("CREATE TABLE agents (id UUID PRIMARY KEY, thread_id UUID)")
        await conn.execute(
            "CREATE TABLE threads ("
            "id UUID PRIMARY KEY, user_id UUID, agent_id UUID, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "permission_mode TEXT NOT NULL DEFAULT 'supervised', "
            "metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
            ")"
        )
        await conn.execute(
            "CREATE TABLE thread_events ("
            "id BIGSERIAL PRIMARY KEY, thread_id UUID, epoch INTEGER, "
            "seq BIGINT, kind TEXT, payload JSONB"
            ")"
        )
        for migration in MIGRATION_FILES:
            await conn.execute(migration.read_text())
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def schema():
    """Apply the run_queue migrations to the scratch DB once per session."""
    _assert_scratch_dsn()
    asyncio.run(_apply_schema())
    yield


@pytest_asyncio.fixture
async def conn(schema):
    """A fresh connection with an empty run_queue (enqueue_ord restarted)."""
    c = await asyncpg.connect(DSN, timeout=10)
    await c.execute(
        "TRUNCATE thread_events, thread_control_requests, run_queue, "
        "agents, threads RESTART IDENTITY CASCADE"
    )
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture
async def extra_conn():
    """Factory for additional connections (concurrency tests)."""
    opened: list[asyncpg.Connection] = []

    async def _open() -> asyncpg.Connection:
        c = await asyncpg.connect(DSN, timeout=10)
        opened.append(c)
        return c

    try:
        yield _open
    finally:
        for c in opened:
            with contextlib.suppress(Exception):
                await c.close()


# =============================================================================
# Helpers
# =============================================================================


async def _row(conn, unit_id: UUID):
    return await conn.fetchrow("SELECT * FROM run_queue WHERE unit_id = $1", unit_id)


async def _expire(conn, unit_id: UUID, seconds: float = 3600.0) -> None:
    """Force a lease into the (well past grace) expired zone."""
    await conn.execute(
        "UPDATE run_queue SET leased_until = now() - make_interval(secs => $2) "
        "WHERE unit_id = $1",
        unit_id,
        seconds,
    )


async def _clear_backoff(conn, unit_id: UUID) -> None:
    await conn.execute(
        "UPDATE run_queue SET run_after = now() WHERE unit_id = $1", unit_id
    )


async def _db_true(conn, expr: str, *args) -> bool:
    return bool(await conn.fetchval(f"SELECT {expr}", *args))


def _future(hours: float = 1.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# =============================================================================
# Schema contract
# =============================================================================


class TestSchema:
    async def test_indexes_exist(self, conn):
        names = {
            r["indexname"]
            for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'run_queue'"
            )
        }
        assert {
            "run_queue_pkey",
            "idx_run_queue_claim",
            "idx_run_queue_expiry",
            "idx_run_queue_dedup",
        } <= names

    async def test_threads_execution_lane_default_pinned(self, conn):
        tid = uuid4()
        await conn.execute("INSERT INTO threads (id) VALUES ($1)", tid)
        lane = await conn.fetchval(
            "SELECT execution_lane FROM threads WHERE id = $1", tid
        )
        assert lane == "pinned"


# =============================================================================
# Enqueue (admission)
# =============================================================================


class TestEnqueue:
    async def test_insert_creates_queued_row_with_defaults(self, conn):
        u = uuid4()
        res = await enqueue_unit(
            conn, unit_id=u, unit_kind=SESSION, fair_key="user-1", input_seq=5
        )
        assert res.status == ENQUEUE_INSERTED
        assert res.state == STATE_QUEUED
        row = await _row(conn, u)
        assert row["state"] == STATE_QUEUED
        assert row["lease_token"] == 0
        assert row["attempts_since_completion"] == 0
        assert row["max_attempts"] == 5
        assert row["input_seq"] == 5
        # Creation initializes consumed_seq to input_seq - 1 (the lane-flip
        # boundary): pre-queue history is never treated as pending.
        assert row["consumed_seq"] == 4
        assert row["fair_key"] == "user-1"

    async def test_enqueue_accepts_str_unit_id(self, conn):
        u = uuid4()
        res = await enqueue_unit(conn, unit_id=str(u), unit_kind=SESSION)
        assert res.status == ENQUEUE_INSERTED
        assert await _row(conn, u) is not None

    async def test_enqueue_on_done_requeues_and_preserves_token(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert (
            await complete_unit(
                conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=None
            )
            == STATE_DONE
        )
        res = await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        assert res.status == ENQUEUE_REQUEUED
        assert res.state == STATE_QUEUED
        row = await _row(conn, u)
        assert row["lease_token"] == claimed.lease_token  # NEVER reset

    @pytest.mark.parametrize(
        ("first", "second", "expected"), [(5, 3, 5), (3, 7, 7), (0, 0, 0)]
    )
    async def test_enqueue_on_queued_merges_priority_greatest(
        self, conn, first, second, expected
    ):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, priority=first)
        res = await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, priority=second)
        assert res.status == ENQUEUE_UPDATED
        assert (await _row(conn, u))["priority"] == expected

    async def test_enqueue_on_queued_advances_run_after_to_earliest(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, run_after=_future())
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)  # default: now
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed is not None and claimed.unit_id == u

    async def test_enqueue_on_queued_never_delays_run_after(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, run_after=_future())
        assert await _db_true(
            conn, "run_after <= now() FROM run_queue WHERE unit_id = $1", u
        )

    async def test_enqueue_on_queued_preserves_fifo_position(self, conn):
        ua, ub = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=ua, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=ub, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=ua, unit_kind=SESSION)  # merge, no reorder
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed.unit_id == ua

    async def test_enqueue_on_leased_bumps_input_seq_only(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, priority=1, input_seq=2)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        res = await enqueue_unit(
            conn, unit_id=u, unit_kind=SESSION, priority=9, input_seq=7, fair_key="x"
        )
        assert res.status == ENQUEUE_INPUT_RECORDED
        assert res.state == STATE_LEASED
        row = await _row(conn, u)
        assert row["state"] == STATE_LEASED  # lease untouched
        assert row["input_seq"] == 7
        assert row["priority"] == 1  # NOT merged while leased
        assert row["fair_key"] is None  # NOT touched while leased

    async def test_enqueue_input_seq_monotonic(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=9)
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=5)
        assert (await _row(conn, u))["input_seq"] == 9


# =============================================================================
# Claim ordering (matrix 1)
# =============================================================================


class TestClaimOrder:
    async def test_priority_desc_wins(self, conn):
        low, high = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=low, unit_kind=SESSION, priority=0)
        await enqueue_unit(conn, unit_id=high, unit_kind=SESSION, priority=5)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed.unit_id == high

    async def test_fifo_within_priority(self, conn):
        first, second = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=first, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=second, unit_kind=SESSION)
        assert (
            await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ).unit_id == first
        assert (
            await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ).unit_id == second

    async def test_enqueue_ord_tiebreak_on_equal_queued_at(self, conn):
        units = [uuid4() for _ in range(3)]
        for u in units:
            await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        # Force identical queued_at (one statement = one now()) — the claim
        # must still return deterministic insertion order via enqueue_ord.
        await conn.execute(
            "UPDATE run_queue SET queued_at = now() WHERE unit_id = ANY($1)", units
        )
        got = [
            (await claim_unit(conn, unit_kind=SESSION, pod_name="p1")).unit_id
            for _ in units
        ]
        assert got == units

    async def test_run_after_future_not_claimable(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, run_after=_future())
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None
        await _clear_backoff(conn, u)
        assert (await claim_unit(conn, unit_kind=SESSION, pod_name="p1")).unit_id == u

    async def test_unit_kind_filter(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=WORKER)
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None
        assert (await claim_unit(conn, unit_kind=WORKER, pod_name="p1")).unit_id == u

    async def test_empty_queue_returns_none(self, conn):
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None

    async def test_claim_sets_lease_fields(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="pod-a", lease_ttl_seconds=60
        )
        assert claimed.lease_token == 1
        assert claimed.attempts_since_completion == 1
        row = await _row(conn, u)
        assert row["state"] == STATE_LEASED
        assert row["leased_by"] == "pod-a"
        assert await _db_true(
            conn,
            "leased_until BETWEEN now() + interval '50 seconds' "
            "AND now() + interval '70 seconds' FROM run_queue WHERE unit_id = $1",
            u,
        )

    async def test_claim_returns_watermarks(self, conn):
        """Matrix 4: skip-if-answered material flows claim → complete → claim."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=5)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        # consumed_seq starts at input_seq - 1 (lane-flip boundary), so the
        # first claim's pending window is exactly the enqueued message.
        assert (claimed.input_seq, claimed.consumed_seq) == (5, 4)
        assert (
            await complete_unit(
                conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=5
            )
            == STATE_DONE
        )
        assert await record_input_seq(
            conn, unit_id=u, unit_kind=SESSION, input_seq=9
        ) == (STATE_QUEUED)
        # A DIFFERENT pod picking the unit up is the scenario under test
        # (watermark handoff), so opt out of the affinity window that would
        # otherwise reserve the unit for p1 — that contract has its own
        # class, TestAffinityGrace.
        again = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p2", affinity_grace_seconds=0.0
        )
        assert (again.input_seq, again.consumed_seq) == (9, 5)
        assert again.consumed_seq < again.input_seq  # caller's skip-if-answered check


# =============================================================================
# Fairness (matrix 2)
# =============================================================================


class TestFairness:
    async def test_complete_requeue_moves_unit_behind_older_unit(self, conn):
        busy, other = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=busy, unit_kind=SESSION, input_seq=1)
        await enqueue_unit(conn, unit_id=other, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed.unit_id == busy  # enqueued first
        await record_input_seq(conn, unit_id=busy, unit_kind=SESSION, input_seq=2)
        assert (
            await complete_unit(
                conn, unit_id=busy, lease_token=claimed.lease_token, consumed_seq=1
            )
            == STATE_QUEUED  # re-queued: input 2 > consumed 1
        )
        # Fairness: the re-queued unit now sits BEHIND the older-queued unit.
        assert (
            await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ).unit_id == other
        assert (
            await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ).unit_id == busy

    async def test_release_moves_unit_behind_older_unit(self, conn):
        first, second = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=first, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=second, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed.unit_id == first
        assert (
            await release_unit(conn, unit_id=first, lease_token=claimed.lease_token)
            == STATE_QUEUED
        )
        assert (
            await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ).unit_id == second


# =============================================================================
# Lease-token monotonicity (matrix 3)
# =============================================================================


class TestLeaseToken:
    async def test_strictly_monotonic_across_lifecycle(self, conn):
        """Token monotonicity across pods — affinity grace opted out (0.0) so
        each hop lands on the next pod immediately; the grace is a scheduling
        preference and has no bearing on fencing (see TestAffinityGrace)."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        tokens = []

        c1 = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p1", affinity_grace_seconds=0.0
        )
        tokens.append(c1.lease_token)
        await release_unit(conn, unit_id=u, lease_token=c1.lease_token)
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)  # merge: no token write

        c2 = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p2", affinity_grace_seconds=0.0
        )
        tokens.append(c2.lease_token)
        await _expire(conn, u)
        stolen = await reap_expired(conn, grace_seconds=0.0)
        tokens.append(stolen[0].lease_token)
        await _clear_backoff(conn, u)

        c3 = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p3", affinity_grace_seconds=0.0
        )
        tokens.append(c3.lease_token)
        await complete_unit(
            conn, unit_id=u, lease_token=c3.lease_token, consumed_seq=None
        )
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        assert (await _row(conn, u))["lease_token"] == c3.lease_token  # no reset

        c4 = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p4", affinity_grace_seconds=0.0
        )
        tokens.append(c4.lease_token)

        assert tokens == sorted(tokens)
        assert len(set(tokens)) == len(tokens)  # strictly increasing
        assert tokens == [1, 2, 3, 4, 5]


# =============================================================================
# Input during a leased turn (matrix 5)
# =============================================================================


class TestInputDuringLease:
    async def test_record_input_on_leased_bumps_seq_only(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=3)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        state = await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=8)
        assert state == STATE_LEASED
        row = await _row(conn, u)
        assert row["state"] == STATE_LEASED
        assert row["input_seq"] == 8

    async def test_complete_requeues_when_input_ahead(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=3)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=8)
        state = await complete_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=3
        )
        assert state == STATE_QUEUED
        row = await _row(conn, u)
        assert row["consumed_seq"] == 3
        assert row["attempts_since_completion"] == 0
        assert row["leased_by"] is None and row["leased_until"] is None

    async def test_complete_done_when_input_consumed(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=3)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        state = await complete_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=3
        )
        assert state == STATE_DONE

    async def test_complete_null_consumed_requeues_when_input_present(self, conn):
        """NULL-safe watermark: any recorded input beats a NULL consumed."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)  # no watermark
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed.input_seq is None
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=4)
        state = await complete_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=None
        )
        assert state == STATE_QUEUED  # input 4 must not be swallowed

    async def test_requeued_unit_drains_fifo(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=2)
        await complete_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=1
        )
        again = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert again.unit_id == u
        assert (again.input_seq, again.consumed_seq) == (2, 1)
        assert (
            await complete_unit(
                conn, unit_id=u, lease_token=again.lease_token, consumed_seq=2
            )
            == STATE_DONE
        )


# =============================================================================
# Control watermarks (migration 0119)
# =============================================================================


class TestControlWatermarks:
    async def test_existing_idle_queue_baselines_prior_lane_control_sequence(
        self, conn
    ):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=8)
        row = await _row(conn, u)
        assert (row["control_input_seq"], row["control_consumed_seq"]) == (0, 0)

        await record_control_seq(
            conn,
            unit_id=u,
            unit_kind=SESSION,
            control_seq=4,
            baseline_input_seq=8,
        )
        row = await _row(conn, u)
        assert (row["control_input_seq"], row["control_consumed_seq"]) == (4, 3)

    async def test_existing_pending_control_is_never_skipped_by_new_baseline(
        self, conn
    ):
        u = uuid4()
        await record_control_seq(
            conn,
            unit_id=u,
            unit_kind=SESSION,
            control_seq=1,
            baseline_input_seq=8,
        )
        await record_control_seq(
            conn,
            unit_id=u,
            unit_kind=SESSION,
            control_seq=4,
            baseline_input_seq=8,
        )
        row = await _row(conn, u)
        assert (row["control_input_seq"], row["control_consumed_seq"]) == (4, 0)

    async def test_control_committed_during_lease_forces_completion_requeue(self, conn):
        """A committed control cannot be stranded by a racing completion."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=7)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")

        state = await record_control_seq(
            conn,
            unit_id=u,
            unit_kind=SESSION,
            control_seq=1,
            baseline_input_seq=7,
        )
        assert state == STATE_LEASED

        state = await complete_unit(
            conn,
            unit_id=u,
            lease_token=claimed.lease_token,
            consumed_seq=7,
        )
        assert state == STATE_QUEUED
        row = await _row(conn, u)
        assert (row["control_input_seq"], row["control_consumed_seq"]) == (1, 0)
        assert row["leased_by"] is None and row["leased_until"] is None

    async def test_control_committed_after_completion_revives_done(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=11)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert (
            await complete_unit(
                conn,
                unit_id=u,
                lease_token=claimed.lease_token,
                consumed_seq=11,
            )
            == STATE_DONE
        )

        state = await record_control_seq(
            conn,
            unit_id=u,
            unit_kind=SESSION,
            control_seq=1,
            baseline_input_seq=11,
            fair_key="owner-1",
        )
        assert state == STATE_QUEUED
        row = await _row(conn, u)
        assert row["state"] == STATE_QUEUED
        assert row["fair_key"] == "owner-1"
        assert (row["control_input_seq"], row["control_consumed_seq"]) == (1, 0)

    async def test_control_only_admission_maps_claim_and_read_model_watermarks(
        self, conn
    ):
        """The migration columns reach both public result dataclasses."""
        u = uuid4()
        state = await record_control_seq(
            conn,
            unit_id=u,
            unit_kind=SESSION,
            control_seq=4,
            baseline_input_seq=23,
        )
        assert state == STATE_QUEUED

        before_claim = await queue_depth_for(conn, unit_id=u)
        assert before_claim is not None
        assert (before_claim.input_seq, before_claim.consumed_seq) == (23, 23)
        assert before_claim.has_pending_input is False
        assert (
            before_claim.control_input_seq,
            before_claim.control_consumed_seq,
            before_claim.has_pending_control,
        ) == (4, 3, True)

        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed is not None and claimed.unit_id == u
        assert (claimed.input_seq, claimed.consumed_seq) == (23, 23)
        assert (claimed.control_input_seq, claimed.control_consumed_seq) == (4, 3)


class TestControlFinalization:
    async def _stateless_request_with_receipt(self, conn):
        thread_id = uuid4()
        owner_id = uuid4()
        request_id = uuid4()
        client_request_id = uuid4()
        await conn.execute(
            "INSERT INTO threads (id, user_id, execution_lane) VALUES ($1, $2, $3)",
            thread_id,
            owner_id,
            "stateless",
        )
        await record_control_seq(
            conn,
            unit_id=thread_id,
            unit_kind=SESSION,
            control_seq=1,
            baseline_input_seq=12,
            fair_key=str(owner_id),
        )
        claim = await claim_unit(conn, unit_kind=SESSION, pod_name="owner-pod")
        await conn.execute(
            "INSERT INTO thread_control_requests ("
            "id, thread_id, request_seq, client_request_id, verb, payload, "
            "requested_by"
            ") VALUES ($1, $2, 1, $3, 'mode.set', $4::jsonb, 'owner')",
            request_id,
            thread_id,
            client_request_id,
            '{"mode":"autonomous"}',
        )
        await conn.execute(
            "INSERT INTO thread_events ("
            "thread_id, epoch, seq, kind, payload, control_request_id"
            ") VALUES ($1, 3, 9, 'mode.changed', $2::jsonb, $3)",
            thread_id,
            json.dumps(
                {
                    "request_id": str(request_id),
                    "client_request_id": str(client_request_id),
                    "request_seq": 1,
                    "method": "mode.set",
                    "mode": "autonomous",
                }
            ),
            request_id,
        )
        return thread_id, request_id, claim

    async def test_receipt_finalization_advances_watermark_atomically(self, conn):
        thread_id, request_id, claim = await self._stateless_request_with_receipt(conn)

        async with conn.transaction():
            result = await finalize_control_request(
                conn,
                request_id=request_id,
                lease_token=claim.lease_token,
            )
        assert result == "applied"
        request = await conn.fetchrow(
            "SELECT outcome, journal_epoch, journal_seq, applied_lease_token "
            "FROM thread_control_requests WHERE id = $1",
            request_id,
        )
        assert tuple(request.values()) == ("applied", 3, 9, claim.lease_token)
        queue = await _row(conn, thread_id)
        assert (queue["control_input_seq"], queue["control_consumed_seq"]) == (1, 1)
        assert (
            await conn.fetchval(
                "SELECT permission_mode FROM threads WHERE id = $1", thread_id
            )
            == "autonomous"
        )
        assert (
            await complete_unit(
                conn,
                unit_id=thread_id,
                lease_token=claim.lease_token,
                consumed_seq=12,
            )
            == STATE_DONE
        )

    async def test_control_receipt_is_unique_and_constraints_are_validated(self, conn):
        thread_id, request_id, _claim = await self._stateless_request_with_receipt(conn)

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO thread_events ("
                "thread_id, epoch, seq, kind, payload, control_request_id"
                ") VALUES ($1, 3, 10, 'mode.changed', '{}'::jsonb, $2)",
                thread_id,
                request_id,
            )

        constraints = {
            row["conname"]: row["convalidated"]
            for row in await conn.fetch(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conname = ANY($1::text[])",
                [
                    "valid_narration_mode",
                    "thread_events_control_request_thread_fkey",
                ],
            )
        }
        assert constraints == {
            "valid_narration_mode": True,
            "thread_events_control_request_thread_fkey": True,
        }
        index = await conn.fetchrow(
            "SELECT indisunique, indisvalid FROM pg_index "
            "WHERE indexrelid = 'idx_thread_events_control_request'::regclass"
        )
        assert tuple(index.values()) == (True, True)

    async def test_pinned_control_capability_defaults_closed(self, conn):
        thread_id = uuid4()
        await conn.execute(
            "INSERT INTO threads (id, execution_lane) VALUES ($1, 'pinned')",
            thread_id,
        )
        assert (
            await conn.fetchval(
                "SELECT control_admission_agent_id FROM threads WHERE id = $1",
                thread_id,
            )
            is None
        )

    async def test_old_lease_cannot_finalize_committed_receipt(self, conn):
        thread_id, request_id, claim = await self._stateless_request_with_receipt(conn)
        await conn.execute(
            "UPDATE run_queue SET lease_token = lease_token + 1 WHERE unit_id = $1",
            thread_id,
        )

        async with conn.transaction():
            result = await finalize_control_request(
                conn,
                request_id=request_id,
                lease_token=claim.lease_token,
            )
        assert result == "lost_owner"
        assert (
            await conn.fetchval(
                "SELECT outcome FROM thread_control_requests WHERE id = $1",
                request_id,
            )
            is None
        )
        assert (await _row(conn, thread_id))["control_consumed_seq"] == 0

    async def test_scalar_request_and_watermark_roll_back_together(self, conn):
        thread_id, request_id, claim = await self._stateless_request_with_receipt(conn)

        transaction = conn.transaction()
        await transaction.start()
        assert (
            await finalize_control_request(
                conn,
                request_id=request_id,
                lease_token=claim.lease_token,
            )
            == "applied"
        )
        assert (
            await conn.fetchval(
                "SELECT permission_mode FROM threads WHERE id = $1", thread_id
            )
            == "autonomous"
        )
        assert (await _row(conn, thread_id))["control_consumed_seq"] == 1
        await transaction.rollback()

        assert (
            await conn.fetchval(
                "SELECT permission_mode FROM threads WHERE id = $1", thread_id
            )
            == "supervised"
        )
        assert (
            await conn.fetchval(
                "SELECT outcome FROM thread_control_requests WHERE id = $1",
                request_id,
            )
            is None
        )
        assert (await _row(conn, thread_id))["control_consumed_seq"] == 0

    async def test_pinned_rebind_rejects_former_exact_agent(self, conn):
        thread_id = uuid4()
        owner_id = uuid4()
        old_agent = uuid4()
        new_agent = uuid4()
        request_id = uuid4()
        client_request_id = uuid4()
        await conn.execute(
            "INSERT INTO agents (id, thread_id) VALUES ($1, $2), ($3, $2)",
            old_agent,
            thread_id,
            new_agent,
        )
        await conn.execute(
            "INSERT INTO threads (id, user_id, agent_id, execution_lane) "
            "VALUES ($1, $2, $3, 'pinned')",
            thread_id,
            owner_id,
            new_agent,
        )
        await conn.execute(
            "INSERT INTO thread_control_requests ("
            "id, thread_id, request_seq, client_request_id, verb, payload, "
            "requested_by, accepted_agent_id"
            ") VALUES ($1, $2, 1, $3, 'narration.set', $4::jsonb, 'owner', $5)",
            request_id,
            thread_id,
            client_request_id,
            '{"mode":"silent"}',
            old_agent,
        )
        await conn.execute(
            "INSERT INTO thread_events ("
            "thread_id, epoch, seq, kind, payload, control_request_id"
            ") VALUES ($1, 1, 2, 'narration.changed', $2::jsonb, $3)",
            thread_id,
            json.dumps(
                {
                    "request_id": str(request_id),
                    "client_request_id": str(client_request_id),
                    "request_seq": 1,
                    "method": "narration.set",
                    "mode": "silent",
                }
            ),
            request_id,
        )

        async with conn.transaction():
            result = await finalize_control_request(
                conn,
                request_id=request_id,
                agent_id=old_agent,
            )
        assert result == "lost_owner"
        assert (
            await conn.fetchval(
                "SELECT outcome FROM thread_control_requests WHERE id = $1",
                request_id,
            )
            is None
        )

        # The committed receipt proves the old exact binding applied it. The
        # new exact owner may finish durable convergence without re-journaling;
        # attribution remains with the old writer.
        async with conn.transaction():
            result = await finalize_control_request(
                conn,
                request_id=request_id,
                agent_id=new_agent,
            )
        assert result == "applied"
        terminal = await conn.fetchrow(
            "SELECT outcome, applied_agent_id FROM thread_control_requests "
            "WHERE id = $1",
            request_id,
        )
        assert tuple(terminal.values()) == ("applied", old_agent)
        assert (
            await conn.fetchval(
                "SELECT narration_mode FROM threads WHERE id = $1", thread_id
            )
            == "silent"
        )

    async def test_pinned_rebind_adopts_oldest_unreceipted_without_overtaking(
        self, conn
    ):
        thread_id = uuid4()
        owner_id = uuid4()
        old_agent = uuid4()
        new_agent = uuid4()
        await conn.execute(
            "INSERT INTO agents (id, thread_id) VALUES ($1, $2), ($3, $2)",
            old_agent,
            thread_id,
            new_agent,
        )
        await conn.execute(
            "INSERT INTO threads (id, user_id, agent_id, execution_lane) "
            "VALUES ($1, $2, $3, 'pinned')",
            thread_id,
            owner_id,
            new_agent,
        )
        first_id = uuid4()
        second_id = uuid4()
        await conn.execute(
            "INSERT INTO thread_control_requests ("
            "id, thread_id, request_seq, client_request_id, verb, payload, "
            "requested_by, accepted_agent_id"
            ") VALUES "
            "($1, $3, 1, $4, 'mode.set', $5::jsonb, 'owner', $6), "
            "($2, $3, 2, $7, 'narration.set', $8::jsonb, 'owner', $9)",
            first_id,
            second_id,
            thread_id,
            uuid4(),
            '{"mode":"autonomous"}',
            old_agent,
            uuid4(),
            '{"mode":"silent"}',
            new_agent,
        )

        # Global order wins over per-agent filtering: seq2 is not visible
        # while the older handoff request still belongs to the dead owner.
        assert (
            await fetch_next_control_request(
                conn, thread_id=thread_id, agent_id=new_agent
            )
            is None
        )
        async with conn.transaction():
            assert await adopt_next_pinned_control_request(
                conn, thread_id=thread_id, agent_id=new_agent
            )
        first = await fetch_next_control_request(
            conn, thread_id=thread_id, agent_id=new_agent
        )
        assert first is not None
        assert first.id == first_id
        assert first.request_seq == 1
        assert first.accepted_agent_id == new_agent

    async def test_control_receipt_cannot_link_across_threads(self, conn):
        owner_id = uuid4()
        request_thread = uuid4()
        event_thread = uuid4()
        request_id = uuid4()
        await conn.execute(
            "INSERT INTO threads (id, user_id, execution_lane) "
            "VALUES ($1, $3, 'stateless'), ($2, $3, 'stateless')",
            request_thread,
            event_thread,
            owner_id,
        )
        await conn.execute(
            "INSERT INTO thread_control_requests ("
            "id, thread_id, request_seq, client_request_id, verb, payload, "
            "requested_by"
            ") VALUES ($1, $2, 1, $3, 'mode.set', $4::jsonb, 'owner')",
            request_id,
            request_thread,
            uuid4(),
            '{"mode":"supervised"}',
        )

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO thread_events ("
                "thread_id, epoch, seq, kind, payload, control_request_id"
                ") VALUES ($1, 1, 1, 'mode.changed', $2::jsonb, $3)",
                event_thread,
                json.dumps(
                    {
                        "request_id": str(request_id),
                        "request_seq": 1,
                        "method": "mode.set",
                        "mode": "supervised",
                    }
                ),
                request_id,
            )

    async def test_narration_mode_database_vocabulary_is_closed(self, conn):
        thread_id = uuid4()
        await conn.execute(
            "INSERT INTO threads (id, execution_lane) VALUES ($1, 'stateless')",
            thread_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE threads SET narration_mode = 'future-mode' WHERE id = $1",
                thread_id,
            )


# =============================================================================
# Voluntary release (matrix 7)
# =============================================================================


class TestVoluntaryRelease:
    async def test_attempts_not_reset(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await release_unit(conn, unit_id=u, lease_token=claimed.lease_token)
        assert (await _row(conn, u))["attempts_since_completion"] == 1

    async def test_backoff_blocks_claim_until_cleared(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await release_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, backoff_seconds=3600
        )
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None
        await _clear_backoff(conn, u)  # manual clock control, no sleeping
        again = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert again is not None
        assert again.lease_token == claimed.lease_token + 1

    async def test_error_release_defaults_to_attempts_scaled_backoff(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await release_unit(conn, unit_id=u, lease_token=claimed.lease_token, error=True)
        # attempts=1 → 5s default error backoff.
        assert await _db_true(
            conn,
            "run_after BETWEEN now() + interval '3 seconds' "
            "AND now() + interval '10 seconds' FROM run_queue WHERE unit_id = $1",
            u,
        )

    async def test_clean_release_immediately_claimable(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await release_unit(conn, unit_id=u, lease_token=claimed.lease_token)
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is not None

    async def test_consumed_seq_untouched(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await conn.execute(
            "UPDATE run_queue SET consumed_seq = 42 WHERE unit_id = $1", u
        )
        await release_unit(conn, unit_id=u, lease_token=claimed.lease_token)
        assert (await _row(conn, u))["consumed_seq"] == 42

    async def test_stale_token_returns_none(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert (
            await release_unit(conn, unit_id=u, lease_token=claimed.lease_token - 1)
            is None
        )
        assert (await _row(conn, u))["state"] == STATE_LEASED  # untouched


# =============================================================================
# Attempts / parking / unpark (matrix 8)
# =============================================================================


class TestParking:
    async def _claim_expire_reap(self, conn, u):
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed is not None and claimed.unit_id == u
        await _expire(conn, u)
        stolen = await reap_expired(conn, grace_seconds=0.0)
        assert [s.unit_id for s in stolen] == [u]
        await _clear_backoff(conn, u)
        return stolen[0]

    async def test_reap_parks_at_max_attempts(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await conn.execute(
            "UPDATE run_queue SET max_attempts = 2 WHERE unit_id = $1", u
        )
        first = await self._claim_expire_reap(conn, u)  # attempts 1 < 2
        assert first.state == STATE_QUEUED
        second = await self._claim_expire_reap(conn, u)  # attempts 2 >= 2
        assert second.state == STATE_PARKED
        assert (await _row(conn, u))["state"] == STATE_PARKED
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None

    async def test_unpark_resets_attempts_and_requeues(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await conn.execute(
            "UPDATE run_queue SET max_attempts = 1 WHERE unit_id = $1", u
        )
        await self._claim_expire_reap(conn, u)  # parks immediately
        assert (await _row(conn, u))["state"] == STATE_PARKED
        assert await unpark_unit(conn, unit_id=u) is True
        row = await _row(conn, u)
        assert row["state"] == STATE_QUEUED
        assert row["attempts_since_completion"] == 0
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert claimed is not None and claimed.unit_id == u

    async def test_enqueue_on_parked_records_input_but_stays_parked(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await conn.execute(
            "UPDATE run_queue SET max_attempts = 1 WHERE unit_id = $1", u
        )
        await self._claim_expire_reap(conn, u)
        res = await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=11)
        assert res.status == ENQUEUE_PARKED
        assert res.state == STATE_PARKED
        row = await _row(conn, u)
        assert row["state"] == STATE_PARKED  # no auto-unpark, by decision
        assert row["input_seq"] == 11
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="p1") is None

    async def test_record_input_seq_on_parked_stays_parked(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await conn.execute(
            "UPDATE run_queue SET max_attempts = 1 WHERE unit_id = $1", u
        )
        await self._claim_expire_reap(conn, u)
        state = await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=12)
        assert state == STATE_PARKED
        assert (await _row(conn, u))["input_seq"] == 12

    async def test_unpark_on_non_parked_returns_false(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        assert await unpark_unit(conn, unit_id=u) is False
        assert await unpark_unit(conn, unit_id=uuid4()) is False


# =============================================================================
# Fence semantics (matrix 9, 10, 13)
# =============================================================================


class TestFence:
    async def test_fence_true_while_leased(self, conn, extra_conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ca = await extra_conn()
        async with ca.transaction():
            assert (
                await fence_lease(ca, unit_id=u, lease_token=claimed.lease_token)
                is True
            )

    async def test_fence_false_wrong_token(self, conn, extra_conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        ca = await extra_conn()
        async with ca.transaction():
            assert (
                await fence_lease(ca, unit_id=u, lease_token=claimed.lease_token + 1)
                is False
            )

    async def test_fence_false_after_release(self, conn, extra_conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await release_unit(conn, unit_id=u, lease_token=claimed.lease_token)
        ca = await extra_conn()
        async with ca.transaction():
            assert (
                await fence_lease(ca, unit_id=u, lease_token=claimed.lease_token)
                is False
            )

    async def test_fence_blocks_steal_until_commit_then_steal_lands(
        self, conn, extra_conn
    ):
        """Matrix 9 — the §5.2 FOR SHARE contract, observed end to end."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        await _expire(conn, u)  # reapable, but the persist is in flight

        conn_a = await extra_conn()
        tx = conn_a.transaction()
        await tx.start()
        assert (
            await fence_lease(conn_a, unit_id=u, lease_token=claimed.lease_token)
            is True
        )

        conn_b = await extra_conn()
        reap_task = asyncio.create_task(reap_expired(conn_b, grace_seconds=0.0))
        done, pending = await asyncio.wait({reap_task}, timeout=1.0)
        assert reap_task in pending, "steal must BLOCK behind the persist fence"

        await tx.commit()  # persist lands; the blocked steal may now proceed
        stolen = await asyncio.wait_for(reap_task, timeout=5.0)
        assert [s.unit_id for s in stolen] == [u]
        assert stolen[0].leased_by == "pod-a"
        assert stolen[0].lease_token == claimed.lease_token + 1

        # The old holder is now a zombie: its next fence must fail.
        async with conn_a.transaction():
            assert (
                await fence_lease(conn_a, unit_id=u, lease_token=claimed.lease_token)
                is False
            )

    async def test_zombie_persist_rejected_after_steal(self, conn):
        """Matrix 10: stale token — fence FALSE, heartbeat None, complete None."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await _expire(conn, u)
        stolen = await reap_expired(conn, grace_seconds=0.0)
        assert len(stolen) == 1
        async with conn.transaction():
            assert (
                await fence_lease(conn, unit_id=u, lease_token=claimed.lease_token)
                is False
            )
        assert (
            await heartbeat_unit(conn, unit_id=u, lease_token=claimed.lease_token)
            is None
        )
        assert (
            await complete_unit(
                conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=None
            )
            is None
        )

    async def test_complete_with_stale_token_leaves_row_untouched(self, conn):
        """Matrix 13."""
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=5)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        before = dict(await _row(conn, u))
        assert (
            await complete_unit(
                conn, unit_id=u, lease_token=claimed.lease_token - 1, consumed_seq=5
            )
            is None
        )
        assert dict(await _row(conn, u)) == before


# =============================================================================
# Heartbeat
# =============================================================================


class TestHeartbeat:
    async def test_heartbeat_extends_lease(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p1", lease_ttl_seconds=30
        )
        renewed = await heartbeat_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, lease_ttl_seconds=120
        )
        assert renewed is not None
        assert renewed > claimed.leased_until
        assert await _db_true(
            conn,
            "leased_until > now() + interval '100 seconds' "
            "FROM run_queue WHERE unit_id = $1",
            u,
        )

    async def test_heartbeat_wrong_token_returns_none(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert (
            await heartbeat_unit(conn, unit_id=u, lease_token=claimed.lease_token + 1)
            is None
        )


# =============================================================================
# Reaper (matrix 11 + steal semantics)
# =============================================================================


class TestReaper:
    async def test_steal_requeues_with_backoff_and_bumped_token(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-z")
        await _expire(conn, u)
        stolen = await reap_expired(conn, grace_seconds=0.0)
        assert len(stolen) == 1
        s = stolen[0]
        assert s.unit_id == u
        assert s.state == STATE_QUEUED
        assert s.leased_by == "pod-z"
        assert s.lease_token == claimed.lease_token + 1
        row = await _row(conn, u)
        assert row["leased_by"] is None and row["leased_until"] is None
        assert row["attempts_since_completion"] == 1  # steal never resets attempts
        # Backoff with jitter: 5s × 1 attempt × (1 + U(0, 0.2)) ⇒ (now+5s, now+6s].
        assert await _db_true(
            conn,
            "run_after BETWEEN now() + interval '3 seconds' "
            "AND now() + interval '10 seconds' FROM run_queue WHERE unit_id = $1",
            u,
        )

    async def test_respects_grace_window(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await _expire(conn, u, seconds=10.0)  # expired 10s ago
        assert await reap_expired(conn, grace_seconds=30.0) == []
        assert (await _row(conn, u))["state"] == STATE_LEASED
        stolen = await reap_expired(conn, grace_seconds=5.0)
        assert [s.unit_id for s in stolen] == [u]

    async def test_healthy_lease_not_reaped(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        assert await reap_expired(conn, grace_seconds=0.0) == []

    async def test_wedged_row_does_not_block_other_steals(self, conn, extra_conn):
        """Matrix 11: per-row SKIP LOCKED — a wedged steal blocks only itself."""
        ux, uy = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=ux, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=uy, unit_kind=SESSION)
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        second = await claim_unit(conn, unit_kind=SESSION, pod_name="p2")
        assert {first.unit_id, second.unit_id} == {ux, uy}
        await _expire(conn, ux)
        await _expire(conn, uy)

        wedge = await extra_conn()
        wedge_tx = wedge.transaction()
        await wedge_tx.start()
        await wedge.execute("SELECT 1 FROM run_queue WHERE unit_id = $1 FOR UPDATE", ux)

        reaper = await extra_conn()
        stolen = await asyncio.wait_for(
            reap_expired(reaper, grace_seconds=0.0), timeout=3.0
        )
        assert [s.unit_id for s in stolen] == [uy]
        assert (await _row(conn, ux))["state"] == STATE_LEASED  # untouched

        await wedge_tx.rollback()
        stolen2 = await asyncio.wait_for(
            reap_expired(reaper, grace_seconds=0.0), timeout=3.0
        )
        assert [s.unit_id for s in stolen2] == [ux]

    async def test_unit_kind_filter(self, conn):
        us, uw = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=us, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=uw, unit_kind=WORKER)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await claim_unit(conn, unit_kind=WORKER, pod_name="p1")
        await _expire(conn, us)
        await _expire(conn, uw)
        stolen = await reap_expired(conn, unit_kind=SESSION, grace_seconds=0.0)
        assert [s.unit_id for s in stolen] == [us]
        assert (await _row(conn, uw))["state"] == STATE_LEASED

    async def test_nothing_expired_returns_empty(self, conn):
        assert await reap_expired(conn) == []


# =============================================================================
# Dedup — queued-only (matrix 12)
# =============================================================================


class TestDedup:
    async def test_queued_only_collapse(self, conn):
        first = await enqueue_unit(
            conn, unit_id=uuid4(), unit_kind=BG, dedup_key="cloud_push:t1"
        )
        assert first.status == ENQUEUE_INSERTED
        second = await enqueue_unit(
            conn, unit_id=uuid4(), unit_kind=BG, dedup_key="cloud_push:t1"
        )
        assert second.status == ENQUEUE_DEDUPED
        count = await conn.fetchval(
            "SELECT count(*) FROM run_queue WHERE dedup_key = 'cloud_push:t1'"
        )
        assert count == 1

    async def test_running_plus_pending_coexist(self, conn):
        await enqueue_unit(
            conn, unit_id=uuid4(), unit_kind=BG, dedup_key="cloud_push:t1"
        )
        claimed = await claim_unit(conn, unit_kind=BG, pod_name="p1")
        assert claimed is not None
        third = await enqueue_unit(
            conn, unit_id=uuid4(), unit_kind=BG, dedup_key="cloud_push:t1"
        )
        assert third.status == ENQUEUE_INSERTED  # signal NOT swallowed mid-run
        states = [
            r["state"]
            for r in await conn.fetch(
                "SELECT state FROM run_queue WHERE dedup_key = 'cloud_push:t1' "
                "ORDER BY enqueue_ord"
            )
        ]
        assert states == [STATE_LEASED, STATE_QUEUED]

    async def test_dedup_scoped_by_unit_kind(self, conn):
        a = await enqueue_unit(conn, unit_id=uuid4(), unit_kind=BG, dedup_key="k")
        b = await enqueue_unit(conn, unit_id=uuid4(), unit_kind=WORKER, dedup_key="k")
        assert (a.status, b.status) == (ENQUEUE_INSERTED, ENQUEUE_INSERTED)

    async def test_dedup_unit_id_reuse_raises(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=BG, dedup_key="kA")
        with pytest.raises(ValueError, match="fresh unit_id"):
            await enqueue_unit(conn, unit_id=u, unit_kind=BG, dedup_key="kB")


# =============================================================================
# Claim affinity (matrix 14)
# =============================================================================


class TestClaimAffinity:
    async def test_prefer_overrides_priority_and_age(self, conn):
        old_high, target = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=old_high, unit_kind=SESSION, priority=10)
        await enqueue_unit(conn, unit_id=target, unit_kind=SESSION, priority=0)
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p1", prefer_unit_id=target
        )
        assert claimed.unit_id == target  # same pod continuing its own thread

    async def test_prefer_miss_falls_back_to_general_order(self, conn):
        old_high, target = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=old_high, unit_kind=SESSION, priority=10)
        await enqueue_unit(conn, unit_id=target, unit_kind=SESSION)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p0", prefer_unit_id=target)
        # target now leased → prefer misses → general claim wins on priority.
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p1", prefer_unit_id=target
        )
        assert claimed.unit_id == old_high

    async def test_prefer_missing_unit_falls_back(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p1", prefer_unit_id=uuid4()
        )
        assert claimed is not None and claimed.unit_id == u

    async def test_prefer_respects_run_after(self, conn):
        other, target = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=other, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=target, unit_kind=SESSION, run_after=_future())
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="p1", prefer_unit_id=target
        )
        assert claimed.unit_id == other  # backed-off affinity target not claimable


# =============================================================================
# Affinity grace (§5.3.4) — the DB half of warm-pod reuse: a freshly queued
# unit belongs to its last holder for a bounded window, so the pod that may
# still hold the attached session wins its own re-claim instead of racing.
# =============================================================================


class TestAffinityGrace:
    async def test_claim_records_last_holder(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        row = await _row(conn, u)
        assert row["last_leased_by"] == "pod-a"
        # Survives completion — that is the whole point (leased_by does not).
        await complete_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=1
        )
        row = await _row(conn, u)
        assert row["leased_by"] is None
        assert row["last_leased_by"] == "pod-a"

    async def test_other_pod_cannot_claim_inside_grace(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        # More input arrives during the turn → completion re-queues the unit.
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=2)
        state = await complete_unit(
            conn, unit_id=u, lease_token=first.lease_token, consumed_seq=1
        )
        assert state == STATE_QUEUED
        # A cold pod polling first inside the grace sees nothing...
        assert await claim_unit(conn, unit_kind=SESSION, pod_name="pod-b") is None
        # ...while the warm holder claims it through the general path.
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        assert claimed is not None and claimed.unit_id == u

    async def test_other_pod_claims_after_grace_lapses(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=2)
        await complete_unit(
            conn, unit_id=u, lease_token=first.lease_token, consumed_seq=1
        )
        # The warm pod never came back (crashed, scaled away): a dead holder
        # costs exactly one grace window, not the unit.
        await conn.execute(
            "UPDATE run_queue SET queued_at = now() - interval '1 hour' "
            "WHERE unit_id = $1",
            u,
        )
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-b")
        assert claimed is not None and claimed.unit_id == u

    async def test_grace_is_caller_tunable(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=2)
        await complete_unit(
            conn, unit_id=u, lease_token=first.lease_token, consumed_seq=1
        )
        # Grace 0 = no affinity window at all (the pre-0117 behavior).
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="pod-b", affinity_grace_seconds=0.0
        )
        assert claimed is not None and claimed.unit_id == u

    async def test_never_claimed_unit_is_not_graced(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        # last_leased_by IS NULL → no holder to protect → claimable at once.
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-b")
        assert claimed is not None and claimed.unit_id == u

    async def test_grace_does_not_block_prefer_path(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=2)
        await complete_unit(
            conn, unit_id=u, lease_token=first.lease_token, consumed_seq=1
        )
        claimed = await claim_unit(
            conn, unit_kind=SESSION, pod_name="pod-a", prefer_unit_id=u
        )
        assert claimed is not None and claimed.unit_id == u

    async def test_reaper_steal_clears_affinity(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        await _expire(conn, u)
        stolen = await reap_expired(conn, unit_kind=SESSION)
        assert len(stolen) == 1
        assert (await _row(conn, u))["last_leased_by"] is None
        # A provably-dead holder must not delay the successor by a grace window.
        await _clear_backoff(conn, u)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-b")
        assert claimed is not None and claimed.unit_id == u

    async def test_grace_does_not_starve_other_units(self, conn):
        """A graced unit must not hide unrelated work from a cold pod."""
        held, free = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=held, unit_kind=SESSION, input_seq=1)
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-a")
        await record_input_seq(conn, unit_id=held, unit_kind=SESSION, input_seq=2)
        await complete_unit(
            conn, unit_id=held, lease_token=first.lease_token, consumed_seq=1
        )
        await enqueue_unit(conn, unit_id=free, unit_kind=SESSION, input_seq=1)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-b")
        assert claimed is not None and claimed.unit_id == free


# =============================================================================
# Read models
# =============================================================================


class TestReadModels:
    async def test_queue_depth_missing_unit_is_none(self, conn):
        assert await queue_depth_for(conn, unit_id=uuid4()) is None

    @pytest.mark.parametrize(
        ("input_seq", "consumed_seq", "expected"),
        [
            (5, None, True),
            (5, 3, True),
            (5, 5, False),
            (None, None, False),
            (None, 7, False),
        ],
    )
    async def test_queue_depth_watermark_arithmetic(
        self, conn, input_seq, consumed_seq, expected
    ):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await conn.execute(
            "UPDATE run_queue SET input_seq = $2, consumed_seq = $3 WHERE unit_id = $1",
            u,
            input_seq,
            consumed_seq,
        )
        wm = await queue_depth_for(conn, unit_id=u)
        assert wm.has_pending_input is expected
        assert wm.input_seq == input_seq
        assert wm.consumed_seq == consumed_seq
        assert wm.state == STATE_QUEUED

    async def test_list_active_reports_leased_and_parked(self, conn):
        leased_u, parked_u, queued_u = uuid4(), uuid4(), uuid4()
        for u in (leased_u, parked_u, queued_u):
            await enqueue_unit(conn, unit_id=u, unit_kind=SESSION)
        await conn.execute(
            "UPDATE run_queue SET max_attempts = 1 WHERE unit_id = $1", parked_u
        )
        # Lease the parked-to-be unit first (FIFO) and park it via the reaper.
        first = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-1")
        assert first.unit_id == leased_u
        second = await claim_unit(conn, unit_kind=SESSION, pod_name="pod-2")
        assert second.unit_id == parked_u
        await _expire(conn, parked_u)
        stolen = await reap_expired(conn, grace_seconds=0.0)
        assert [s.state for s in stolen] == [STATE_PARKED]

        active = await list_active(conn)
        assert [r["unit_id"] for r in active["leased"]] == [leased_u]
        assert [r["unit_id"] for r in active["parked"]] == [parked_u]
        leased_row = active["leased"][0]
        assert leased_row["leased_by"] == "pod-1"
        assert leased_row["lease_remaining_seconds"] > 0
        assert queued_u not in {
            r["unit_id"] for r in active["leased"] + active["parked"]
        }

    async def test_list_active_kind_filter(self, conn):
        us, uw = uuid4(), uuid4()
        await enqueue_unit(conn, unit_id=us, unit_kind=SESSION)
        await enqueue_unit(conn, unit_id=uw, unit_kind=WORKER)
        await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await claim_unit(conn, unit_kind=WORKER, pod_name="p1")
        active = await list_active(conn, unit_kind=WORKER)
        assert [r["unit_id"] for r in active["leased"]] == [uw]


# =============================================================================
# record_input_seq admission paths
# =============================================================================


class TestRecordInputSeq:
    async def test_missing_row_creates_queued_unit(self, conn):
        u = uuid4()
        state = await record_input_seq(
            conn, unit_id=u, unit_kind=SESSION, input_seq=1, fair_key="user-9"
        )
        assert state == STATE_QUEUED
        row = await _row(conn, u)
        assert row["input_seq"] == 1
        assert row["fair_key"] == "user-9"
        assert row["lease_token"] == 0

    async def test_done_row_revived_to_queued(self, conn):
        u = uuid4()
        await enqueue_unit(conn, unit_id=u, unit_kind=SESSION, input_seq=1)
        claimed = await claim_unit(conn, unit_kind=SESSION, pod_name="p1")
        await complete_unit(
            conn, unit_id=u, lease_token=claimed.lease_token, consumed_seq=1
        )
        assert (await _row(conn, u))["state"] == STATE_DONE
        state = await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=2)
        assert state == STATE_QUEUED
        assert (await claim_unit(conn, unit_kind=SESSION, pod_name="p1")).unit_id == u

    async def test_watermark_never_regresses(self, conn):
        u = uuid4()
        await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=9)
        state = await record_input_seq(conn, unit_id=u, unit_kind=SESSION, input_seq=5)
        assert state == STATE_QUEUED
        assert (await _row(conn, u))["input_seq"] == 9
