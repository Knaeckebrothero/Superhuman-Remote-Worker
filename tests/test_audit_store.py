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

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import MIGRATIONS_AUDIT_DIR  # noqa: E402
from orchestrator.services import audit_partitions  # noqa: E402

pytestmark = pytest.mark.asyncio

PARENTS = ("llm_requests", "agent_audit", "chat_history")
AUDIT_INDEXES = {
    "llm_requests": {"llm_requests_job_ts_idx"},
    "agent_audit": {
        "agent_audit_job_id_idx",
        "agent_audit_job_step_idx",
        "agent_audit_pre_id_idx",
    },
    "chat_history": {"chat_history_job_ts_idx"},
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
            # lookahead 4 vs the bootstrap's 2 → 2 new months per parent = 6.
            created = await audit_partitions.ensure_partitions(pool, lookahead_months=4)
            assert len(created) == 6, created
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
