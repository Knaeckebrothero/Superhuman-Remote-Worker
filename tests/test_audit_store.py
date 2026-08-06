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
from decimal import Decimal
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
from orchestrator.services.infrastructure_metering.materializer import (  # noqa: E402
    build_usage_plan,
)
from orchestrator.services.usage_ledger import (  # noqa: E402
    StrictUsageConflict,
    StrictUsageExpectation,
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
        "usage_events_project_ts_idx",
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
                    "VALUES ($1, 'llm', 'm', 1, 'prompt-token', 'audit', 'r1')",
                    far_future,
                )
            assert exc.value.sqlstate == "23514"

    async def test_usage_events_dedupe_unique(self, pg_dsn):
        # The at-least-once idempotency key (source, source_id, unit, ts): a second
        # identical emit is rejected, so a re-polled request / re-emitted close
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
                sql, now, "llm", "gemma", 100, "prompt-token", "audit", "req-abc"
            )
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await pool.execute(
                    sql, now, "llm", "gemma", 100, "prompt-token", "audit", "req-abc"
                )
            await pool.execute(
                sql, now, "llm", "gemma", 50, "completion-token", "audit", "req-abc"
            )
            n = await pool.fetchval("SELECT count(*) FROM usage_events")
            assert n == 2

    async def test_usage_events_v2_schema_is_validated_and_probeable(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            expected_columns = {
                "period_start",
                "period_end",
                "measurement_basis",
                "cost_domain",
                "resource_class",
                "attribution_scope",
                "measurement_algorithm",
                "source_capacity_value",
                "source_capacity_unit",
                "source_cluster",
                "source_kind",
                "source_uid",
                "source_lifecycle_id",
                "source_interval_id",
                "event_kind",
                "corrects_source",
                "corrects_source_id",
                "corrects_unit",
                "corrects_ts",
                "correction_group_id",
                "correction_reason",
                "correction_actor_id",
                "discovered_at",
                "payload_hash",
            }
            rows = await pool.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='usage_events'"
            )
            assert expected_columns <= {row["column_name"] for row in rows}

            expected_constraints = {
                "usage_events_period_bounds_v2_check",
                "usage_events_infra_v2_contract_check",
                "usage_events_event_kind_v2_check",
            }
            constraints = await pool.fetch(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid='usage_events'::regclass "
                "AND conname = ANY($1::text[])",
                list(expected_constraints),
            )
            assert {row["conname"] for row in constraints} == expected_constraints
            assert all(row["convalidated"] for row in constraints)

            triggers = await pool.fetch(
                "SELECT tgname, pg_get_triggerdef(oid) AS definition "
                "FROM pg_trigger WHERE tgrelid='usage_events'::regclass "
                "AND NOT tgisinternal"
            )
            trigger_defs = {row["tgname"]: row["definition"] for row in triggers}
            assert (
                "REFERENCING NEW TABLE AS inserted_usage_events"
                in trigger_defs["usage_events_rollup_dirty_days"]
            )
            assert "usage_events_append_only_v2" in trigger_defs
            assert await pool.fetchval(
                "SELECT to_regclass('public.usage_rollup_dirty_days') IS NOT NULL"
            )
            rounding_function = await pool.fetchrow(
                "SELECT provolatile::text AS volatility, "
                "proparallel::text AS parallel, proconfig "
                "FROM pg_proc WHERE oid="
                "'round_half_even_v2(numeric,integer)'::regprocedure"
            )
            assert rounding_function["volatility"] == "i"
            assert rounding_function["parallel"] == "s"
            assert "search_path=pg_catalog" in rounding_function["proconfig"]

    async def test_usage_events_v2_batch_dirtying_and_append_only(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ties = await pool.fetchrow(
                "SELECT "
                "round_half_even_v2(2.5, 0) AS pos_even, "
                "round_half_even_v2(3.5, 0) AS pos_odd, "
                "round_half_even_v2(-2.5, 0) AS neg_even, "
                "round_half_even_v2(-3.5, 0) AS neg_odd, "
                "round_half_even_v2(0.0000000000000000005, 18) AS tiny_even, "
                "round_half_even_v2(0.0000000000000000015, 18) AS tiny_odd"
            )
            assert tuple(ties) == (
                Decimal("2"),
                Decimal("4"),
                Decimal("-2"),
                Decimal("-4"),
                Decimal("0"),
                Decimal("0.000000000000000002"),
            )

            now = datetime.now(timezone.utc)
            period_start = now.replace(hour=1, minute=0, second=0, microsecond=0)
            period_end = period_start + timedelta(hours=1)
            user_id, project_id, ref_id = uuid4(), uuid4(), uuid4()
            lifecycle_id, interval_id = uuid4(), uuid4()
            insert_sql = """
                WITH typed AS (
                    SELECT
                        $1::timestamptz AS period_start,
                        $2::timestamptz AS period_end,
                        $3::uuid AS user_id,
                        $4::uuid AS project_id,
                        $5::uuid AS ref_id,
                        $6::uuid AS lifecycle_id,
                        $7::uuid AS interval_id,
                        $8::text AS ref_kind,
                        $9::text AS source_id
                )
                INSERT INTO usage_events (
                    ts, user_id, project_id, ref_kind, ref_id,
                    category, resource, quantity, unit, rate_usd, cost_usd,
                    source, source_id, details, period_start, period_end,
                    measurement_basis, cost_domain, resource_class,
                    attribution_scope, measurement_algorithm,
                    source_capacity_value, source_capacity_unit,
                    source_cluster, source_kind, source_uid,
                    source_lifecycle_id, source_interval_id, event_kind,
                    payload_hash
                )
                SELECT
                    typed.period_start, typed.user_id, typed.project_id,
                    typed.ref_kind, typed.ref_id,
                    'compute', 'workspace_pod', dimension.quantity,
                    dimension.unit, NULL, NULL,
                    'infra-allocation-v2', typed.source_id, '{}'::jsonb,
                    typed.period_start, typed.period_end,
                    'scheduler-request', 'workload-allocation',
                    'kubernetes-pod', 'customer', 'pod-requests-test-v1',
                    dimension.capacity, dimension.capacity_unit,
                    'cluster-a', 'pod', 'pod-a', typed.lifecycle_id,
                    typed.interval_id, 'usage', dimension.payload_hash
                FROM typed
                CROSS JOIN (
                    VALUES
                        (1::numeric, 'vcpu-hour', 1000::numeric,
                         'millicore', repeat('a', 64)),
                        (4::numeric, 'gib-hour', 4294967296::numeric,
                         'byte', repeat('b', 64))
                ) AS dimension(
                    quantity, unit, capacity, capacity_unit, payload_hash
                )
                ON CONFLICT DO NOTHING
            """
            args = (
                period_start,
                period_end,
                user_id,
                project_id,
                ref_id,
                lifecycle_id,
                interval_id,
                "job",
                "typed-batch",
            )

            assert await pool.execute(insert_sql, *args) == "INSERT 0 2"
            revision = await pool.fetchval(
                "SELECT revision FROM usage_rollup_dirty_days WHERE day=$1",
                period_start.date(),
            )
            assert revision == 1  # one increment for the two-row statement

            assert await pool.execute(insert_sql, *args) == "INSERT 0 0"
            assert (
                await pool.fetchval(
                    "SELECT revision FROM usage_rollup_dirty_days WHERE day=$1",
                    period_start.date(),
                )
                == 1
            )

            invalid_args = (*args[:7], None, "invalid-customer-owner")
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(insert_sql, *invalid_args)

            priced_clone_sql = """
                INSERT INTO usage_events (
                    ts, user_id, project_id, ref_kind, ref_id,
                    category, resource, quantity, unit, rate_usd, cost_usd,
                    source, source_id, details, period_start, period_end,
                    measurement_basis, cost_domain, resource_class,
                    attribution_scope, measurement_algorithm,
                    source_capacity_value, source_capacity_unit,
                    source_cluster, source_kind, source_uid,
                    source_lifecycle_id, source_interval_id, event_kind,
                    payload_hash
                )
                SELECT
                    ts, user_id, project_id, ref_kind, ref_id,
                    category, resource, $2::numeric, unit, $3::numeric,
                    $4::numeric, source, $1, details, period_start, period_end,
                    measurement_basis, cost_domain, resource_class,
                    attribution_scope, measurement_algorithm,
                    $5::numeric, source_capacity_unit,
                    source_cluster, source_kind, source_uid,
                    source_lifecycle_id, source_interval_id, event_kind,
                    repeat('c', 64)
                FROM usage_events
                WHERE source_id='typed-batch' AND unit='vcpu-hour'
                LIMIT 1
            """
            await pool.execute(
                priced_clone_sql,
                "rounded-product",
                Decimal("0.333333333333333333"),
                Decimal("0.1"),
                Decimal("0.033333333333333333"),
                Decimal("1000"),
            )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    priced_clone_sql,
                    "unrounded-product",
                    Decimal("0.333333333333333333"),
                    Decimal("0.1"),
                    Decimal("0.0333333333333333333"),
                    Decimal("1000"),
                )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    priced_clone_sql,
                    "quantity-scale-overflow",
                    Decimal("0.0000000000000000001"),
                    None,
                    None,
                    Decimal("1000"),
                )
            for source_id, quantity, rate, cost, capacity in (
                (
                    "quantity-nan",
                    Decimal("NaN"),
                    None,
                    None,
                    Decimal("1000"),
                ),
                (
                    "quantity-infinity",
                    Decimal("Infinity"),
                    None,
                    None,
                    Decimal("1000"),
                ),
                (
                    "capacity-nan",
                    Decimal("1"),
                    None,
                    None,
                    Decimal("NaN"),
                ),
                (
                    "capacity-infinity",
                    Decimal("1"),
                    None,
                    None,
                    Decimal("Infinity"),
                ),
                (
                    "rate-infinity",
                    Decimal("1"),
                    Decimal("Infinity"),
                    Decimal("0"),
                    Decimal("1000"),
                ),
                (
                    "cost-nan",
                    Decimal("1"),
                    Decimal("0.1"),
                    Decimal("NaN"),
                    Decimal("1000"),
                ),
            ):
                with pytest.raises(asyncpg.exceptions.CheckViolationError):
                    await pool.execute(
                        priced_clone_sql,
                        source_id,
                        quantity,
                        rate,
                        cost,
                        capacity,
                    )

            correction_sql = """
                INSERT INTO usage_events (
                    ts, user_id, project_id, ref_kind, ref_id,
                    category, resource, quantity, unit, rate_usd, cost_usd,
                    source, source_id, details, period_start, period_end,
                    measurement_basis, cost_domain, resource_class,
                    attribution_scope, measurement_algorithm,
                    source_capacity_value, source_capacity_unit,
                    source_cluster, source_kind, source_uid,
                    source_lifecycle_id, source_interval_id, event_kind,
                    corrects_source, corrects_source_id, corrects_unit,
                    corrects_ts, correction_group_id, correction_reason,
                    correction_actor_id, payload_hash
                )
                SELECT
                    ts, user_id, project_id, ref_kind, ref_id,
                    category, resource, -quantity, unit, NULL, NULL,
                    'infra-allocation-correction-v2', $1, details,
                    period_start, period_end, measurement_basis, cost_domain,
                    resource_class, attribution_scope, measurement_algorithm,
                    source_capacity_value, source_capacity_unit,
                    source_cluster, source_kind, source_uid,
                    source_lifecycle_id, source_interval_id, 'correction',
                    source, source_id, $2, $3, $4, 'reviewed correction',
                    $5, repeat('d', 64)
                FROM usage_events
                WHERE source_id='typed-batch' AND unit='vcpu-hour'
                LIMIT 1
            """
            correction_group, correction_actor = uuid4(), uuid4()
            await pool.execute(
                correction_sql,
                "correction-valid",
                "vcpu-hour",
                period_start,
                correction_group,
                correction_actor,
            )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    correction_sql,
                    "correction-wrong-unit",
                    "gib-hour",
                    period_start,
                    uuid4(),
                    correction_actor,
                )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await pool.execute(
                    correction_sql,
                    "correction-wrong-ts",
                    "vcpu-hour",
                    period_start + timedelta(microseconds=1),
                    uuid4(),
                    correction_actor,
                )

            with pytest.raises(asyncpg.exceptions.ObjectNotInPrerequisiteStateError):
                await pool.execute(
                    "UPDATE usage_events SET quantity=2 WHERE source_id=$1",
                    "typed-batch",
                )


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
                # Per-parent on-disk size is reported (whole tree; a fresh
                # bootstrap already carries empty-index metapages, so > 0).
                assert isinstance(s["total_bytes"], int)
                assert s["total_bytes"] >= 0
                # Fresh bootstrap sits comfortably above the critical alarm floor.
                assert (
                    s["days_until_unpartitioned"]
                    > audit_partitions._LOOKAHEAD_CRIT_DAYS
                )

    async def test_partition_status_size_grows_with_rows(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            before = (await audit_partitions.partition_status(pool))["usage_events"][
                "total_bytes"
            ]
            # One raw insert allocates the current-month partition's first heap
            # page → the whole-tree size must strictly grow.
            await pool.execute(
                "INSERT INTO usage_events "
                "(ts, category, resource, quantity, unit, source, source_id) "
                "VALUES (now(), 'llm', 'm', 1, 'prompt-token', 'test', 's-size-1')"
            )
            after = (await audit_partitions.partition_status(pool))["usage_events"][
                "total_bytes"
            ]
            assert after > before

    async def test_analyze_parents_force(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            analyzed = await audit_partitions.analyze_parents(pool, force=True)
            assert sorted(analyzed) == sorted(PARENTS)

    async def test_retire_partitions_is_policy_noop(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            before = {p: await _children(pool, p) for p in PARENTS}
            result = await audit_partitions.retire_partitions(pool)
            # No-auto-deletion policy — permanent no-op, not "deferred".
            assert result.get("policy") == "no-auto-deletion"
            assert result["detached"] == [] and result["dropped"] == []
            for parent in PARENTS:
                assert await _children(pool, parent) == before[parent]

    async def test_maintenance_pass_end_to_end(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            result = await audit_partitions.maintenance_pass(pool)
            assert set(result) == {"created", "analyzed", "retired", "status"}
            assert result["retired"].get("policy") == "no-auto-deletion"
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
                    source="audit",
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
                    quantity=50,
                    unit="cached-prompt-token",
                    source="audit",
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
                    source="audit",
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
            assert await ledger.record_events(events) == 4
            res = await ledger.query_usage(
                from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1)
            )
            by = {(b["category"], b["unit"]): b for b in res["by_category"]}
            assert by[("llm", "prompt-token")]["quantity"] == 100.0
            assert by[("llm", "cached-prompt-token")]["quantity"] == 50.0
            assert by[("llm", "completion-token")]["quantity"] == 40.0
            assert by[("compute", "vcpu-hour")]["quantity"] == 2.0
            assert res["total_cost_usd"] == 0.0  # unpriced
            assert round(res["cache_hit_ratio"], 6) == round(50 / 150, 6)

    async def test_idempotent_dedupe(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            ev = UsageEvent(
                category="llm",
                resource="gemma",
                quantity=10,
                unit="prompt-token",
                source="audit",
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
                source="audit",
                source_id="dup",
                ts=now,
            )
            assert await ledger.record_events([ev2]) == 1
            assert await pool.fetchval("SELECT count(*) FROM usage_events") == 2

    async def test_strict_frozen_infrastructure_batch_round_trip(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            now = datetime.now(timezone.utc)
            start = now.replace(day=1, hour=1, minute=0, second=0, microsecond=0)
            interval_id, lifecycle_id = uuid4(), uuid4()
            owner_id, user_id, project_id = uuid4(), uuid4(), uuid4()
            interval = {
                "id": interval_id,
                "source_cluster": "test-cluster",
                "source_kind": "pod",
                "source_uid": "pod-strict-ledger",
                "source_lifecycle_id": lifecycle_id,
                "revision_no": 1,
                "source_revision": "a" * 64,
                "namespace": "tests",
                "name": "workspace-strict-ledger",
                "category": "compute",
                "resource": "workspace_pod",
                "measurement_basis": "scheduler-request",
                "cost_domain": "workload-allocation",
                "resource_class": "kubernetes-pod",
                "attribution_scope": "customer",
                "owner_kind": "job",
                "owner_id": str(owner_id),
                "user_id": user_id,
                "project_id": project_id,
                "attribution_source": "test-owner",
                "attribution_quality": "exact",
                "backing_resource_uid": None,
                "lifecycle_confidence": "kubernetes-visible",
                "cpu_millicores": 2000,
                "memory_bytes": 4 * 1024**3,
                "storage_bytes": None,
                "capacity_source": "test-requests",
                "capacity_quality": "exact",
                "measurement_algorithm": "pod-requests-test-v1",
                "started_at": start,
                "start_time_source": "test",
                "start_uncertainty_us": 0,
                "ended_at": start + timedelta(hours=1),
                "end_time_source": "test-close",
                "end_uncertainty_us": 0,
                "last_seen_at": start + timedelta(hours=1),
                "last_confirmed_at": start + timedelta(hours=1),
                "materialized_through": start,
                "end_reason": "test",
            }
            plan = build_usage_plan(interval, (), creator_generation=1)
            assert plan is not None
            ledger = UsageLedger(pool, UsageRates(None))
            events = [item.event for item in plan.events]

            first = await ledger.publish_frozen_events(events)
            replay = await ledger.publish_frozen_events(events)

            assert (first.inserted, first.verified) == (2, 2)
            assert (replay.inserted, replay.verified) == (0, 2)
            rows = await pool.fetch(
                "SELECT unit, quantity, payload_hash FROM usage_events "
                "WHERE source='infra-allocation-v2' AND source_id=$1 "
                "ORDER BY unit",
                events[0].payload["source_id"],
            )
            assert [(row["unit"], row["quantity"]) for row in rows] == [
                ("gib-hour", Decimal("4")),
                ("vcpu-hour", Decimal("2")),
            ]
            assert {row["payload_hash"] for row in rows} == {
                event.row_hash for event in events
            }
            assert (
                await pool.fetchval(
                    "SELECT revision FROM usage_rollup_dirty_days "
                    "WHERE day=($1 AT TIME ZONE 'UTC')::date",
                    start,
                )
                == 1
            )

    async def test_strict_legacy_batch_insert_replay_and_conflict(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            user_id, project_id, job_id = uuid4(), uuid4(), uuid4()

            def expectation(
                *,
                source_id: str,
                unit: str,
                quantity: Decimal,
                rate_usd: Decimal,
                cost_usd: Decimal,
            ) -> StrictUsageExpectation:
                fields = {
                    "ts": now,
                    "user_id": user_id,
                    "project_id": project_id,
                    "ref_kind": "job",
                    "ref_id": job_id,
                    "category": "compute",
                    "resource": "workspace_pod",
                    "quantity": quantity,
                    "unit": unit,
                    "rate_usd": rate_usd,
                    "cost_usd": cost_usd,
                    "source": "orchestrator",
                    "source_id": source_id,
                    "details": {
                        "duration_h": "1.000000",
                        "metering_path": "legacy-cutover",
                    },
                }
                return StrictUsageExpectation(
                    source="orchestrator",
                    source_id=source_id,
                    unit=unit,
                    ts=now,
                    expected_fields=fields,
                )

            source_id = f"workspace:{job_id}"
            expected = [
                expectation(
                    source_id=source_id,
                    unit="vcpu-hour",
                    quantity=Decimal("2"),
                    rate_usd=Decimal("0.10"),
                    cost_usd=Decimal("0.20"),
                ),
                expectation(
                    source_id=source_id,
                    unit="gib-hour",
                    quantity=Decimal("4"),
                    rate_usd=Decimal("0.02"),
                    cost_usd=Decimal("0.08"),
                ),
            ]

            first = await ledger.publish_expected_events(expected)
            replay = await ledger.publish_expected_events(expected)

            assert (first.expected, first.inserted, first.verified) == (2, 2, 2)
            assert (replay.expected, replay.inserted, replay.verified) == (2, 0, 2)
            rows = await pool.fetch(
                "SELECT unit, quantity, rate_usd, cost_usd, event_kind, "
                "payload_hash FROM usage_events "
                "WHERE source='orchestrator' AND source_id=$1 ORDER BY unit",
                source_id,
            )
            assert [
                (
                    row["unit"],
                    row["quantity"],
                    row["rate_usd"],
                    row["cost_usd"],
                    row["event_kind"],
                    row["payload_hash"],
                )
                for row in rows
            ] == [
                (
                    "gib-hour",
                    Decimal("4"),
                    Decimal("0.02"),
                    Decimal("0.08"),
                    None,
                    None,
                ),
                (
                    "vcpu-hour",
                    Decimal("2"),
                    Decimal("0.10"),
                    Decimal("0.20"),
                    None,
                    None,
                ),
            ]

            conflicting_fields = dict(expected[0].expected_fields)
            conflicting_fields["quantity"] = Decimal("3")
            conflict = StrictUsageExpectation(
                source=expected[0].source,
                source_id=expected[0].source_id,
                unit=expected[0].unit,
                ts=expected[0].ts,
                expected_fields=conflicting_fields,
            )
            fresh_source_id = f"workspace:{uuid4()}"
            fresh = expectation(
                source_id=fresh_source_id,
                unit="vcpu-hour",
                quantity=Decimal("1"),
                rate_usd=Decimal("0.10"),
                cost_usd=Decimal("0.10"),
            )

            with pytest.raises(StrictUsageConflict, match="quantity"):
                await ledger.publish_expected_events([fresh, conflict])

            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM usage_events "
                    "WHERE source='orchestrator' AND source_id=$1",
                    fresh_source_id,
                )
                == 0
            )
            assert await pool.fetchval(
                "SELECT quantity FROM usage_events "
                "WHERE source='orchestrator' AND source_id=$1 "
                "AND unit='vcpu-hour' AND ts=$2",
                source_id,
                now,
            ) == Decimal("2")

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
                        source="audit",
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
                source="audit",
                source_id="good",
                ts=now,
            )
            bad = UsageEvent(  # +10y → no partition exists
                category="llm",
                resource="g",
                quantity=1,
                unit="prompt-token",
                source="audit",
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
                        source="audit",
                        source_id="a",
                        ts=now,
                        user_id=str(ua),
                    ),
                    UsageEvent(
                        category="llm",
                        resource="m",
                        quantity=1,
                        unit="prompt-token",
                        source="audit",
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

    async def test_query_grouped_by_user_and_model(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            ua, ub, pid = uuid4(), uuid4(), uuid4()

            def ev(uid, model, qty, unit, sid):
                return UsageEvent(
                    category="llm",
                    resource=model,
                    quantity=qty,
                    unit=unit,
                    source="audit",
                    source_id=sid,
                    ts=now,
                    user_id=str(uid),
                    project_id=str(pid),
                )

            await ledger.record_events(
                [
                    ev(ua, "gemma", 100, "prompt-token", "a1"),
                    ev(ua, "gemma", 30, "completion-token", "a1"),
                    ev(ub, "opus", 200, "prompt-token", "b1"),
                ]
            )
            window = dict(
                from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1)
            )
            by_user = await ledger.query_grouped(group_by="user", **window)
            keys = {r["key"] for r in by_user}
            assert keys == {str(ua), str(ub)}
            ua_prompt = next(
                r
                for r in by_user
                if r["key"] == str(ua) and r["unit"] == "prompt-token"
            )
            assert ua_prompt["quantity"] == 100.0 and ua_prompt["events"] == 1
            by_model = await ledger.query_grouped(group_by="model", **window)
            assert {r["key"] for r in by_model} == {"gemma", "opus"}

    async def test_query_grouped_nonadmin_scoped_to_self(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            me, other, shared = uuid4(), uuid4(), uuid4()

            def ev(uid, sid):
                return UsageEvent(
                    category="llm",
                    resource="gemma",
                    quantity=10,
                    unit="prompt-token",
                    source="audit",
                    source_id=sid,
                    ts=now,
                    user_id=str(uid),
                    project_id=str(shared),
                )

            await ledger.record_events([ev(me, "m1"), ev(other, "o1")])
            window = dict(
                from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1)
            )
            # Non-admin (owner set) + a shared visible project must STILL only see self.
            rows = await ledger.query_grouped(
                group_by="user",
                owner_user_id=str(me),
                visible_project_ids=[str(shared)],
                **window,
            )
            assert {r["key"] for r in rows} == {str(me)}

    async def test_query_grouped_rejects_bad_dimension(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            with pytest.raises(ValueError):
                await ledger.query_grouped(
                    group_by="evil", from_ts=now - timedelta(days=1), to_ts=now
                )

    async def test_query_timeseries_buckets_by_day_and_key(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            # Days 15/16 of the current month are always inside the bootstrapped
            # current-month partition — no month-boundary flake.
            base = datetime.now(timezone.utc).replace(
                day=15, hour=12, minute=0, second=0, microsecond=0
            )
            d_a, d_b = base, base + timedelta(days=1)
            uid, pid = uuid4(), uuid4()

            def ev(model, qty, unit, sid, ts):
                return UsageEvent(
                    category="llm",
                    resource=model,
                    quantity=qty,
                    unit=unit,
                    source="audit",
                    source_id=sid,
                    ts=ts,
                    user_id=str(uid),
                    project_id=str(pid),
                )

            await ledger.record_events(
                [
                    ev("opus", 100, "prompt-token", "b1", d_b),
                    ev("opus", 25, "cached-prompt-token", "b1", d_b),
                    ev("opus", 20, "completion-token", "b2", d_b),
                    ev("opus", 40, "prompt-token", "a1", d_a),
                    ev("opus", 2, "vcpu-hour", "a2", d_a),  # compute → not a token
                    ev("gemma", 10, "prompt-token", "a3", d_a),
                ]
            )
            window = dict(
                from_ts=base - timedelta(days=2), to_ts=base + timedelta(days=2)
            )
            rows = await ledger.query_timeseries(group_by="model", **window)
            by = {(r["day"], r["key"]): r for r in rows}
            day_a, day_b = d_a.date().isoformat(), d_b.date().isoformat()
            # tokens = prompt + cached prompt + completion summed; rows ascend by day
            assert by[(day_b, "opus")]["tokens"] == 145.0
            assert by[(day_b, "opus")]["events"] == 3
            assert (
                by[(day_a, "opus")]["tokens"] == 40.0
            )  # vcpu-hour excluded from tokens
            assert by[(day_a, "opus")]["events"] == 2  # but still counted as an event
            assert by[(day_a, "gemma")]["tokens"] == 10.0
            assert [r["day"] for r in rows] == sorted(r["day"] for r in rows)
            with pytest.raises(ValueError):
                await ledger.query_timeseries(group_by="evil", **window)

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
            source="audit",
            source_id="x",
            ts=now,
        )
        assert await ledger.record_events([ev]) == 0
        res = await ledger.query_usage(from_ts=now - timedelta(days=1), to_ts=now)
        assert res == {
            "by_category": [],
            "total_cost_usd": 0.0,
            "cache_hit_ratio": 0.0,
        }


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


async def _ensure_month_partition(
    pool, *, parent: str = "usage_events", months_back: int = 1
) -> None:
    """Attach ``parent``'s partition ``months_back`` UTC months in the past.

    The 0001/0002 bootstraps create only the current month + N+2 lookahead (the
    forward-only contract owned by audit_partitions), and retention is a no-op by
    policy so a long-running DB always already has older months. Two kinds of test
    need a BACKDATED partition to exist so the insert doesn't 23514 and drop the
    row: a leaked-interval reconcile stamps its event at ``now() - orphan_after_h``
    (up to 24h back → previous month on the 1st), and the rollup catch-up drill
    seeds events several months back. Create them here so the test is
    deterministic regardless of the calendar date. Mirrors the migration's own
    bootstrap DO block (UTC month boundaries), shifted ``months_back`` back.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL timezone = 'UTC'")
            await conn.execute(
                f"""
                DO $$
                DECLARE
                    m    DATE := (date_trunc('month', now())
                                  - interval '{int(months_back)} month')::date;
                    part TEXT := '{parent}_p' || to_char(m, 'YYYY_MM');
                BEGIN
                    EXECUTE format(
                        'CREATE TABLE IF NOT EXISTS %I PARTITION OF {parent} '
                        'FOR VALUES FROM (%L) TO (%L)',
                        part, m, (m + interval '1 month')::date
                    );
                END $$;
                """
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
            # The reconcile stamps its event at now()-24h; on the first day of a
            # month that falls into the previous month, which the migration's
            # forward-only bootstrap doesn't create. Attach it so the assertion
            # below is calendar-independent (see _ensure_month_partition).
            await _ensure_month_partition(pool)
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


class TestBreakdownFold:
    """Pure (key, unit) → per-key folding + label merge used by /api/usage/breakdown."""

    def test_fold_groups_units_under_key(self):
        from orchestrator.main import _fold_breakdown

        rows = [
            {
                "key": "u1",
                "unit": "prompt-token",
                "quantity": 100.0,
                "cost_usd": 0.0,
                "events": 2,
            },
            {
                "key": "u1",
                "unit": "completion-token",
                "quantity": 30.0,
                "cost_usd": 0.0,
                "events": 2,
            },
            {
                "key": "u1",
                "unit": "cached-prompt-token",
                "quantity": 25.0,
                "cost_usd": 0.0,
                "events": 1,
            },
            {
                "key": "u2",
                "unit": "prompt-token",
                "quantity": 50.0,
                "cost_usd": 0.0,
                "events": 1,
            },
        ]
        folded = _fold_breakdown(rows)
        assert folded["u1"]["units"]["prompt-token"]["quantity"] == 100.0
        assert folded["u1"]["events"] == 5  # summed across units
        assert folded["u1"]["cache_hit_ratio"] == 0.2
        assert folded["u2"]["units"]["prompt-token"]["events"] == 1

    def test_merge_labels_falls_back_to_key(self):
        from orchestrator.main import _fold_breakdown, _merge_labels

        folded = _fold_breakdown(
            [
                {
                    "key": "u1",
                    "unit": "prompt-token",
                    "quantity": 1.0,
                    "cost_usd": 0.0,
                    "events": 1,
                },
                {
                    "key": "u2",
                    "unit": "prompt-token",
                    "quantity": 1.0,
                    "cost_usd": 0.0,
                    "events": 1,
                },
            ]
        )
        out = _merge_labels(folded, {"u1": {"label": "Alice", "is_admin": True}})
        by_key = {r["key"]: r for r in out}
        assert by_key["u1"]["label"] == "Alice" and by_key["u1"]["is_admin"] is True
        assert by_key["u2"]["label"] == "u2"  # unknown id → key as label

    def test_build_timeseries_pivots_and_orders(self):
        from orchestrator.main import _build_timeseries

        rows = [
            {
                "day": "2026-06-01",
                "key": "opus",
                "tokens": 100.0,
                "cost_usd": 0.0,
                "events": 1,
            },
            {
                "day": "2026-06-02",
                "key": "opus",
                "tokens": 50.0,
                "cost_usd": 0.0,
                "events": 1,
            },
            {
                "day": "2026-06-01",
                "key": "gemma",
                "tokens": 10.0,
                "cost_usd": 0.0,
                "events": 3,
            },
        ]
        out = _build_timeseries(rows, {"opus": {"label": "Opus 4"}})
        assert out["days"] == ["2026-06-01", "2026-06-02"]  # sorted union of buckets
        # gemma leads despite fewer tokens — series order is total events desc (3 > 2)
        assert [s["key"] for s in out["series"]] == ["gemma", "opus"]
        opus = next(s for s in out["series"] if s["key"] == "opus")
        assert opus["events"] == 2 and len(opus["points"]) == 2
        assert opus["label"] == "Opus 4"  # label map applied
        gemma = next(s for s in out["series"] if s["key"] == "gemma")
        assert gemma["label"] == "gemma"  # unknown key → raw key as label
