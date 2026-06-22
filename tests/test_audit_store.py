"""Schema + partition-maintenance tests for the Postgres audit store (PR 1).

Spins up an ephemeral PostgreSQL via testcontainers, applies
``migrations/audit/0001_initial.sql`` through the *real* migration runner
(``orchestrator.database.migrate.run_migrations``), then exercises
``orchestrator/services/audit_partitions.py`` against it.

Skips cleanly when the dev dependency (``testcontainers[postgres]``, in
``requirements-dev.txt``) or a container runtime (Docker/Podman) is unavailable,
so the default local ``pytest`` run is unaffected. CI installs the dev deps and
provides Docker, where this suite actually runs.

Each test gets a freshly-created, freshly-migrated database on the shared
container, so partition-creation tests don't interfere with one another.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

# Whole-module skip when the dev dependency isn't installed (the common local
# case). Must precede the orchestrator imports so a plain `pytest` stays green.
pytest.importorskip("testcontainers.postgres")

from types import SimpleNamespace  # noqa: E402

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import MIGRATIONS_AUDIT_DIR  # noqa: E402
from orchestrator.services import audit_partitions, workspace_metering  # noqa: E402
from orchestrator.services.usage_ledger import (  # noqa: E402
    UsageEvent,
    UsageLedger,
    UsageRates,
)

pytestmark = pytest.mark.asyncio

PARENTS = ("llm_requests", "agent_audit", "chat_history", "usage_events")
AUDIT_INDEXES = {
    "llm_requests": {"llm_requests_job_ts_idx"},
    "agent_audit": {
        "agent_audit_job_id_idx",
        "agent_audit_job_step_idx",
        "agent_audit_pre_id_idx",
    },
    "chat_history": {"chat_history_job_ts_idx"},
    "usage_events": {
        "usage_events_dedupe_idx",
        "usage_events_user_ts_idx",
        "usage_events_ref_idx",
    },
}
# Official postgres images are built --with-lz4 (required by the SET COMPRESSION
# statements in the migration). Pin 16 per the package's image recommendation.
AUDIT_IMAGE = "postgres:16"


def _asyncpg_dsn(url: str) -> str:
    """testcontainers returns a SQLAlchemy URL (``postgresql+psycopg2://...``);
    asyncpg wants a bare ``postgresql://`` scheme."""
    import re

    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _swap_db(dsn: str, dbname: str) -> str:
    """Return ``dsn`` repointed at database ``dbname`` (preserving any query)."""
    head, _, tail = dsn.rpartition("/")
    query = ""
    if "?" in tail:
        query = "?" + tail.split("?", 1)[1]
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def pg_dsn():
    """Module-scoped ephemeral Postgres; yields a DSN to its maintenance DB.

    Startup failures (no reachable container runtime) translate to a skip; the
    ``yield`` is deliberately outside the skip-guard so test failures surface as
    failures, not skips.
    """
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(AUDIT_IMAGE)
    try:
        container.start()
    except Exception as exc:  # Docker/Podman not reachable here
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield _asyncpg_dsn(container.get_connection_url())
    finally:
        container.stop()


@asynccontextmanager
async def _audit_pool(base_dsn: str):
    """Create a fresh DB, migrate it via the real runner, yield an asyncpg pool.

    Drops the database on teardown so every test starts from a clean schema.
    """
    dbname = f"audit_t_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(base_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    pool = await asyncpg.create_pool(_swap_db(base_dsn, dbname), min_size=1, max_size=4)
    try:
        await run_migrations(pool, MIGRATIONS_AUDIT_DIR)
        yield pool
    finally:
        await pool.close()
        admin = await asyncpg.connect(base_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()


async def _children(pool, parent: str) -> set[str]:
    rows = await pool.fetch(
        """
        SELECT c.relname AS relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = $1 AND NOT i.inhdetachpending
        """,
        parent,
    )
    return {r["relname"] for r in rows}


class TestAuditSchema:
    """0001_initial.sql applies through the runner and matches the design."""

    async def test_parents_are_partitioned(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            for parent in PARENTS:
                relkind = await pool.fetchval(
                    # relkind is the internal "char" type → asyncpg returns it as
                    # bytes; cast to text so the comparison is str-vs-str.
                    "SELECT relkind::text FROM pg_class WHERE relname = $1",
                    parent,
                )
                # 'p' = partitioned table
                assert relkind == "p", f"{parent} is not a partitioned table"

    async def test_bootstrap_creates_current_plus_n2(self, pg_dsn):
        # Migration bootstraps the current month + 2 lookahead = 3 leaves each.
        async with _audit_pool(pg_dsn) as pool:
            for parent in PARENTS:
                children = await _children(pool, parent)
                assert len(children) == 3, f"{parent} has {children}"

    async def test_leaf_reloptions(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            leaf = next(iter(await _children(pool, "llm_requests")))
            reloptions = await pool.fetchval(
                "SELECT reloptions FROM pg_class WHERE relname = $1", leaf
            )
            assert reloptions is not None, "leaf has no reloptions"
            opts = set(reloptions)
            assert "fillfactor=100" in opts
            assert "autovacuum_freeze_min_age=0" in opts
            assert "autovacuum_vacuum_insert_threshold=10000" in opts

    async def test_expected_indexes_present(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            for parent, expected in AUDIT_INDEXES.items():
                rows = await pool.fetch(
                    """
                    SELECT c.relname AS idx
                    FROM pg_index x
                    JOIN pg_class c ON c.oid = x.indexrelid
                    WHERE x.indrelid = $1::regclass
                    """,
                    parent,
                )
                names = {r["idx"] for r in rows}
                assert expected <= names, f"{parent} missing {expected - names}"

    async def test_jsonb_columns_lz4_compressed(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            # 'l' = lz4 (set explicitly by the migration on the parent columns).
            for parent, col in (
                ("llm_requests", "request"),
                ("agent_audit", "payload"),
                ("chat_history", "inputs"),
                ("usage_events", "details"),
            ):
                attc = await pool.fetchval(
                    # attcompression is the internal "char" type (bytes via
                    # asyncpg); cast to text. 'l' = lz4.
                    """
                    SELECT attcompression::text FROM pg_attribute
                    WHERE attrelid = $1::regclass AND attname = $2
                    """,
                    parent,
                    col,
                )
                assert attc == "l", f"{parent}.{col} compression is {attc!r}, not lz4"

    async def test_agent_audit_check_constraints(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            job = uuid4()
            # Valid pre row → returns its id.
            pre_id = await pool.fetchval(
                "INSERT INTO agent_audit (job_id, step_type) VALUES ($1, 'tool') "
                "RETURNING id",
                job,
            )
            assert pre_id >= 1
            # Valid post row pointing at the pre row.
            await pool.execute(
                "INSERT INTO agent_audit (job_id, step_type, event_phase, pre_id) "
                "VALUES ($1, 'tool', 'post', $2)",
                job,
                pre_id,
            )
            # A post row with no pre_id violates agent_audit_pre_id_check.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    "INSERT INTO agent_audit (job_id, step_type, event_phase) "
                    "VALUES ($1, 'tool', 'post')",
                    job,
                )
            # A pre row WITH a pre_id likewise violates it.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    "INSERT INTO agent_audit (job_id, step_type, pre_id) "
                    "VALUES ($1, 'tool', 1)",
                    job,
                )

    async def test_missing_partition_insert_fails_loudly(self, pg_dsn):
        # The deliberate no-DEFAULT-partition design: a row outside any partition
        # must raise (SQLSTATE 23514), never silently land in a catch-all.
        async with _audit_pool(pg_dsn) as pool:
            far_future = datetime.now(timezone.utc) + timedelta(days=3650)
            with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
                await pool.execute(
                    "INSERT INTO llm_requests "
                    "(job_id, model, timestamp, request, response) "
                    "VALUES ($1, 'm', $2, '{}'::jsonb, '{}'::jsonb)",
                    uuid4(),
                    far_future,
                )
            assert exc.value.sqlstate == "23514"

    async def test_usage_events_partitions_on_ts(self, pg_dsn):
        # 0002 partitions usage_events on `ts` (not `timestamp`). A row whose ts
        # falls outside every monthly partition must fail loudly with 23514 (same
        # no-DEFAULT design), proving the partition key + the audit_partitions
        # `ts`-column path are wired correctly.
        async with _audit_pool(pg_dsn) as pool:
            far_future = datetime.now(timezone.utc) + timedelta(days=3650)
            with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
                await pool.execute(
                    "INSERT INTO usage_events "
                    "(ts, category, resource, quantity, unit, source, source_id) "
                    "VALUES ($1, 'llm', 'm', 1, 'prompt-token', 'litellm', 'r1')",
                    far_future,
                )
            assert exc.value.sqlstate == "23514"

    async def test_usage_events_dedupe_unique(self, pg_dsn):
        # The at-least-once idempotency key (source, source_id, unit, ts): a second
        # identical emit is rejected, so a re-polled spend log / re-emitted close
        # cannot double-count. A different unit on the same source_id is allowed
        # (one row per metered dimension).
        async with _audit_pool(pg_dsn) as pool:
            now = datetime.now(timezone.utc)
            sql = (
                "INSERT INTO usage_events "
                "(ts, category, resource, quantity, unit, source, source_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)"
            )
            await pool.execute(
                sql, now, "llm", "gemma", 100, "prompt-token", "litellm", "req-abc"
            )
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await pool.execute(
                    sql, now, "llm", "gemma", 100, "prompt-token", "litellm", "req-abc"
                )
            await pool.execute(
                sql, now, "llm", "gemma", 50, "completion-token", "litellm", "req-abc"
            )
            n = await pool.fetchval("SELECT count(*) FROM usage_events")
            assert n == 2


class TestAuditPartitions:
    """orchestrator/services/audit_partitions.py against a live audit DB."""

    async def test_ensure_partitions_idempotent(self, pg_dsn):
        # The migration already created current + N+2; ensure_partitions with the
        # default lookahead wants the same set → nothing new, proving catalog
        # truth (not name guessing) drives idempotency.
        async with _audit_pool(pg_dsn) as pool:
            created = await audit_partitions.ensure_partitions(pool)
            assert created == []

    async def test_ensure_partitions_extends_lookahead(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            before = {p: await _children(pool, p) for p in PARENTS}
            # lookahead 4 vs the bootstrap's 2 → 2 new months per parent ×
            # 4 parents = 8.
            created = await audit_partitions.ensure_partitions(pool, lookahead_months=4)
            assert len(created) == 8, created
            for parent in PARENTS:
                after = await _children(pool, parent)
                assert len(after) == 5
                assert before[parent] < after  # strict superset
            # Re-running at the same lookahead is a no-op.
            assert (
                await audit_partitions.ensure_partitions(pool, lookahead_months=4) == []
            )

    async def test_partition_status_shape(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            status = await audit_partitions.partition_status(pool)
            assert set(status) == set(PARENTS)
            for parent, s in status.items():
                assert s["attached"] == 3
                assert s["detach_pending"] == 0
                assert s["awaiting_drop"] == 0
                assert s["last_parent_analyze"] is not None  # migration ANALYZEd
                # Fresh bootstrap sits comfortably above the critical alarm floor.
                assert (
                    s["days_until_unpartitioned"]
                    > audit_partitions._LOOKAHEAD_CRIT_DAYS
                )

    async def test_analyze_parents_force(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            analyzed = await audit_partitions.analyze_parents(pool, force=True)
            assert sorted(analyzed) == sorted(PARENTS)

    async def test_retire_partitions_is_deferred_noop(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            before = {p: await _children(pool, p) for p in PARENTS}
            result = await audit_partitions.retire_partitions(pool)
            assert result.get("deferred") is True
            assert result["detached"] == [] and result["dropped"] == []
            # Nothing detached or dropped.
            for parent in PARENTS:
                assert await _children(pool, parent) == before[parent]

    async def test_maintenance_pass_end_to_end(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            result = await audit_partitions.maintenance_pass(pool)
            assert set(result) == {"created", "analyzed", "retired", "status"}
            assert result["retired"].get("deferred") is True
            assert set(result["status"]) == set(PARENTS)


class TestUsageLedger:
    """orchestrator/services/usage_ledger.py against a live audit DB (Slice 4a)."""

    async def test_record_and_query_unpriced(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))  # rates absent → unpriced
            now = datetime.now(timezone.utc)
            uid, pid, job = uuid4(), uuid4(), uuid4()
            events = [
                UsageEvent(
                    category="llm",
                    resource="gemma",
                    quantity=100,
                    unit="prompt-token",
                    source="litellm",
                    source_id="req1",
                    ts=now,
                    user_id=str(uid),
                    project_id=str(pid),
                    ref_kind="job",
                    ref_id=str(job),
                ),
                UsageEvent(
                    category="llm",
                    resource="gemma",
                    quantity=40,
                    unit="completion-token",
                    source="litellm",
                    source_id="req1",
                    ts=now,
                    user_id=str(uid),
                    project_id=str(pid),
                    ref_kind="job",
                    ref_id=str(job),
                ),
                UsageEvent(
                    category="compute",
                    resource="workspace_pod",
                    quantity=2,
                    unit="vcpu-hour",
                    source="orchestrator",
                    source_id="ws1",
                    ts=now,
                    user_id=str(uid),
                    project_id=str(pid),
                    ref_kind="job",
                    ref_id=str(job),
                ),
            ]
            assert await ledger.record_events(events) == 3
            res = await ledger.query_usage(
                from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1)
            )
            by = {(b["category"], b["unit"]): b for b in res["by_category"]}
            assert by[("llm", "prompt-token")]["quantity"] == 100.0
            assert by[("llm", "completion-token")]["quantity"] == 40.0
            assert by[("compute", "vcpu-hour")]["quantity"] == 2.0
            assert res["total_cost_usd"] == 0.0  # unpriced

    async def test_idempotent_dedupe(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            ev = UsageEvent(
                category="llm",
                resource="gemma",
                quantity=10,
                unit="prompt-token",
                source="litellm",
                source_id="dup",
                ts=now,
            )
            assert await ledger.record_events([ev]) == 1
            assert await ledger.record_events([ev]) == 0  # re-emit deduped
            # Same source_id, different dimension (unit) → distinct row.
            ev2 = UsageEvent(
                category="llm",
                resource="gemma",
                quantity=5,
                unit="completion-token",
                source="litellm",
                source_id="dup",
                ts=now,
            )
            assert await ledger.record_events([ev2]) == 1
            assert await pool.fetchval("SELECT count(*) FROM usage_events") == 2

    async def test_rate_snapshot(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            # usage_rates is an app-DB table; create it on this pool so the
            # resolver has something to read (one DB is fine for the test).
            await pool.execute(
                "CREATE TABLE usage_rates (category text, resource text, "
                "unit text, rate_usd numeric, effective_from timestamptz, "
                "PRIMARY KEY (category, resource, unit, effective_from))"
            )
            now = datetime.now(timezone.utc)
            await pool.execute(
                "INSERT INTO usage_rates "
                "VALUES ('llm','gemma','prompt-token',0.002,$1)",
                now - timedelta(days=10),
            )
            ledger = UsageLedger(pool, UsageRates(pool))
            await ledger.record_events(
                [
                    UsageEvent(
                        category="llm",
                        resource="gemma",
                        quantity=1000,
                        unit="prompt-token",
                        source="litellm",
                        source_id="r",
                        ts=now,
                    )
                ]
            )
            row = await pool.fetchrow(
                "SELECT rate_usd, cost_usd FROM usage_events WHERE source_id='r'"
            )
            assert float(row["rate_usd"]) == 0.002
            assert float(row["cost_usd"]) == 2.0  # 1000 * 0.002, snapshotted

    async def test_partition_gap_falls_back_per_row(self, pg_dsn):
        # A row whose ts falls outside every partition (no DEFAULT partition) would
        # fail the whole batch INSERT; the per-row fallback must still land the good
        # rows and drop only the offender (else the poller blocks forever).
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            good = UsageEvent(
                category="llm",
                resource="g",
                quantity=1,
                unit="prompt-token",
                source="litellm",
                source_id="good",
                ts=now,
            )
            bad = UsageEvent(  # +10y → no partition exists
                category="llm",
                resource="g",
                quantity=1,
                unit="prompt-token",
                source="litellm",
                source_id="bad",
                ts=now + timedelta(days=3650),
            )
            assert await ledger.record_events([good, bad]) == 1
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM usage_events WHERE source_id='good'"
                )
                == 1
            )
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM usage_events WHERE source_id='bad'"
                )
                == 0
            )

    async def test_visibility_owner_filter(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            ua, ub, pshared = uuid4(), uuid4(), uuid4()
            await ledger.record_events(
                [
                    UsageEvent(
                        category="llm",
                        resource="m",
                        quantity=1,
                        unit="prompt-token",
                        source="litellm",
                        source_id="a",
                        ts=now,
                        user_id=str(ua),
                    ),
                    UsageEvent(
                        category="llm",
                        resource="m",
                        quantity=1,
                        unit="prompt-token",
                        source="litellm",
                        source_id="b",
                        ts=now,
                        user_id=str(ub),
                        project_id=str(pshared),
                    ),
                ]
            )
            window = dict(
                from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1)
            )
            # ua sees only their own row.
            res = await ledger.query_usage(owner_user_id=str(ua), **window)
            assert sum(b["events"] for b in res["by_category"]) == 1
            # ua as a member of pshared also sees ub's project-scoped row.
            res2 = await ledger.query_usage(
                owner_user_id=str(ua), visible_project_ids=[str(pshared)], **window
            )
            assert sum(b["events"] for b in res2["by_category"]) == 2

    async def test_unavailable_pool_noop(self):
        # No DB needed: a ledger with no audit pool no-ops (non-load-bearing tier).
        ledger = UsageLedger(None, UsageRates(None))
        assert ledger.is_available is False
        now = datetime.now(timezone.utc)
        ev = UsageEvent(
            category="llm",
            resource="m",
            quantity=1,
            unit="prompt-token",
            source="litellm",
            source_id="x",
            ts=now,
        )
        assert await ledger.record_events([ev]) == 0
        res = await ledger.query_usage(from_ts=now - timedelta(days=1), to_ts=now)
        assert res == {"by_category": [], "total_cost_usd": 0.0}


async def _create_intervals_table(pool) -> None:
    """Create workspace_intervals (app-DB table) on the test pool (0034 DDL)."""
    await pool.execute(
        """
        CREATE TABLE workspace_intervals (
            id BIGSERIAL PRIMARY KEY,
            owner_kind TEXT NOT NULL,
            owner_id UUID NOT NULL,
            tier TEXT,
            cpu_millicores INTEGER NOT NULL,
            mem_bytes BIGINT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ,
            materialized_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await pool.execute(
        "CREATE UNIQUE INDEX workspace_intervals_open_uq "
        "ON workspace_intervals (owner_kind, owner_id) WHERE ended_at IS NULL"
    )


def _owner(kind: str, oid) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, id=str(oid))


async def _noattrib(kind, oid):
    return None


class TestWorkspaceMetering:
    """orchestrator/services/workspace_metering.py against a live DB (Slice 4b)."""

    async def test_parse_cpu(self):
        assert workspace_metering.parse_cpu_millicores("500m") == 500
        assert workspace_metering.parse_cpu_millicores("2") == 2000
        assert workspace_metering.parse_cpu_millicores("2000m") == 2000

    async def test_parse_mem(self):
        assert workspace_metering.parse_mem_bytes("1Gi") == 1024**3
        assert workspace_metering.parse_mem_bytes("512Mi") == 512 * 1024**2
        assert workspace_metering.parse_mem_bytes("1G") == 1000**3

    async def test_open_close_idempotent(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            await _create_intervals_table(pool)
            owner = _owner("job", uuid4())
            kw = dict(tier="sandbox", cpu="500m", memory="1Gi")
            await workspace_metering.open_interval(pool, owner, **kw)
            await workspace_metering.open_interval(pool, owner, **kw)  # deduped
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM workspace_intervals WHERE ended_at IS NULL"
                )
                == 1
            )
            await workspace_metering.close_interval(pool, owner)
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM workspace_intervals WHERE ended_at IS NULL"
                )
                == 0
            )
            # Re-open after close → a fresh interval (suspend/restore semantics).
            await workspace_metering.open_interval(pool, owner, **kw)
            assert await pool.fetchval("SELECT count(*) FROM workspace_intervals") == 2

    async def test_materialize_computes_quantities(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            await _create_intervals_table(pool)
            owner = _owner("job", uuid4())
            uid = uuid4()
            await workspace_metering.open_interval(
                pool, owner, tier="sandbox", cpu="500m", memory="1Gi"
            )
            # Force a 2-hour closed interval.
            await pool.execute(
                "UPDATE workspace_intervals SET "
                "started_at = now() - interval '2 hours', ended_at = now() "
                "WHERE owner_id = $1::uuid",
                str(owner.id),
            )

            async def attrib(kind, oid):
                return {"user_id": str(uid), "project_id": None}

            ledger = UsageLedger(pool, UsageRates(None))
            res = await workspace_metering.materialize_and_reconcile(
                pool, ledger, attrib
            )
            assert res["materialized"] == 1
            rows = await pool.fetch(
                "SELECT unit, quantity, user_id, ref_kind FROM usage_events "
                "WHERE category='compute' ORDER BY unit"
            )
            by = {r["unit"]: r for r in rows}
            # 500m = 0.5 vcpu over 2h → 1.0; 1Gi over 2h → 2.0.
            assert abs(float(by["vcpu-hour"]["quantity"]) - 1.0) < 1e-6
            assert abs(float(by["gib-hour"]["quantity"]) - 2.0) < 1e-6
            assert str(by["vcpu-hour"]["user_id"]) == str(uid)
            assert by["vcpu-hour"]["ref_kind"] == "job"
            assert (
                await pool.fetchval(
                    "SELECT materialized_at FROM workspace_intervals "
                    "WHERE owner_id=$1::uuid",
                    str(owner.id),
                )
                is not None
            )

    async def test_materialize_idempotent(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            await _create_intervals_table(pool)
            owner = _owner("session", uuid4())
            await workspace_metering.open_interval(
                pool, owner, tier="sandbox", cpu="1", memory="2Gi"
            )
            await pool.execute(
                "UPDATE workspace_intervals SET "
                "started_at = now() - interval '1 hour', ended_at = now() "
                "WHERE owner_id=$1::uuid",
                str(owner.id),
            )
            ledger = UsageLedger(pool, UsageRates(None))
            r1 = await workspace_metering.materialize_and_reconcile(
                pool, ledger, _noattrib
            )
            r2 = await workspace_metering.materialize_and_reconcile(
                pool, ledger, _noattrib
            )
            assert r1["materialized"] == 1
            assert r2["materialized"] == 0  # already stamped + ledger dedupes
            assert await pool.fetchval("SELECT count(*) FROM usage_events") == 2

    async def test_reconcile_closes_leaked_open(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            await _create_intervals_table(pool)
            owner = _owner("job", uuid4())
            await workspace_metering.open_interval(
                pool, owner, tier="sandbox", cpu="500m", memory="1Gi"
            )
            # Backdate to 48h ago → a leaked open the reconciler must bound.
            await pool.execute(
                "UPDATE workspace_intervals SET "
                "started_at = now() - interval '48 hours' WHERE owner_id=$1::uuid",
                str(owner.id),
            )
            ledger = UsageLedger(pool, UsageRates(None))
            res = await workspace_metering.materialize_and_reconcile(
                pool, ledger, _noattrib, orphan_after_h=24
            )
            assert res["reconciled"] == 1
            row = await pool.fetchrow(
                "SELECT started_at, ended_at, materialized_at FROM workspace_intervals "
                "WHERE owner_id=$1::uuid",
                str(owner.id),
            )
            assert row["ended_at"] is not None
            # Capped at started_at + 24h, then materialized in the same pass.
            capped_s = (row["ended_at"] - row["started_at"]).total_seconds()
            assert abs(capped_s - 24 * 3600) < 5
            assert row["materialized_at"] is not None
            q = await pool.fetchval(
                "SELECT quantity FROM usage_events WHERE unit='vcpu-hour'"
            )
            assert abs(float(q) - 12.0) < 1e-3  # 0.5 vcpu × 24h
