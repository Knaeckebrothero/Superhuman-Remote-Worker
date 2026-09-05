"""HF-4 — dispatcher query correctness with the partial index present.

Exercises PostgresDB.get_dispatchable_jobs against a real Postgres (test-
containers) with the 0046 partial index applied, covering every WHERE branch:
status gate, assigned-agent gate, freeze gate, cloud-baseline 'seeding' skip,
explicit failed-loop-baseline skip, legacy-loop compatibility, the recursive
ancestor-cascade guard, and the priority/created ordering.

This is a functional (result) assertion, not a plan assertion — the EXPLAIN
before/after adoption proof is a separate one-shot artifact. Applying the
migration's own DDL here also guards it against becoming malformed SQL.
"""

import re
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

_MIGRATION = Path(
    "src/orchestrator/database/migrations/app/0046_jobs_dispatchable_partial_idx.notx.sql"
)


def _index_ddl() -> str:
    """The migration's CREATE INDEX, minus CONCURRENTLY (can't run in a txn)."""
    body = "\n".join(
        ln
        for ln in _MIGRATION.read_text().splitlines()
        if not ln.lstrip().startswith("--")
    )
    stmt = body[body.index("CREATE INDEX") :].strip().rstrip(";")
    return re.sub(r"CONCURRENTLY\s+", "", stmt)


def _u(n: int) -> UUID:
    return UUID(f"{n:032x}")


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY,
                description text,
                status text NOT NULL,
                config_name text,
                config_override jsonb,
                assigned_agent_id uuid,
                user_id uuid,
                project_id uuid,
                parent_job_id uuid,
                priority int NOT NULL DEFAULT 0,
                runner_kind text NOT NULL DEFAULT 'user',
                -- Gate 1: the legacy dispatcher serves pinned jobs only.
                execution_lane text NOT NULL DEFAULT 'pinned',
                branch_name text,
                repo_name text,
                context jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                freeze_data jsonb
            )
        """)
        await conn.execute("TRUNCATE jobs")
        await conn.execute(_index_ddl())
    yield d
    await d.close()


async def _insert(db, n, status, **cols):
    keys = ["id", "status", *cols.keys()]
    vals = [_u(n), status, *cols.values()]
    ph = ", ".join(f"${i + 1}" for i in range(len(vals)))
    async with db.acquire() as conn:
        await conn.execute(f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({ph})", *vals)


@pytest.mark.asyncio
async def test_index_exists_from_migration(db):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_jobs_dispatchable'"
        )
    assert row is not None, "0046 migration DDL must create idx_jobs_dispatchable"


@pytest.mark.asyncio
async def test_dispatchable_set_and_ordering(db):
    # Dispatchable: created + paused, no agent, no freeze, not seeding, clear chain
    await _insert(db, 1, "created", priority=5)
    await _insert(db, 2, "paused", priority=10)
    # Non-dispatchable branches:
    await _insert(db, 3, "processing")  # status gate
    await _insert(db, 4, "completed")  # status gate
    await _insert(db, 5, "created", assigned_agent_id=_u(999))  # already assigned
    await _insert(
        db, 6, "created", freeze_data='{"freeze_type": "phase_boundary"}'
    )  # frozen
    await _insert(
        db, 7, "created", context='{"cloud_baseline": {"state": "seeding"}}'
    )  # baseline still seeding
    await _insert(
        db,
        8,
        "created",
        priority=4,
        context='{"loop_id": "legacy-loop"}',
    )  # legacy loop created before cloud baselines
    await _insert(
        db,
        9,
        "created",
        context=('{"loop_id": "new-loop", "cloud_baseline": {"state": "failed"}}'),
    )  # explicit new-loop baseline failure is fail-closed
    await _insert(
        db,
        10,
        "created",
        priority=3,
        context='{"cloud_baseline": {"state": "failed"}}',
    )  # ordinary Mode A seed failure remains best-effort
    await _insert(
        db, 11, "created", priority=100, execution_lane="stateless"
    )  # run_queue owns this row; legacy dispatch must never see it
    await _insert(
        db, 12, "paused", priority=200, execution_lane="future-lane"
    )  # unknown runtime classes fail closed instead of falling back to legacy

    got = await db.get_dispatchable_jobs(limit=50)
    ids = [str(r["id"]) for r in got]

    assert ids == [str(_u(2)), str(_u(1)), str(_u(8)), str(_u(10))], (
        "clean pinned jobs dispatch in priority order; stateless jobs stay out"
    )
    # The managed repository authority keys on repo_name; a projection that
    # drops it dispatches every job with a credential-less remote.
    assert all("repo_name" in dict(r) for r in got)


@pytest.mark.asyncio
async def test_cascade_guard_blocks_children_of_paused_ancestors(db):
    # Parent P paused -> its child is cascade-blocked (but P itself, being a bare
    # paused job with no blocking ancestor, is dispatchable — preempted jobs
    # resume). Parent Q completed -> its child is dispatchable.
    await _insert(db, 20, "paused")  # blocking parent (dispatchable itself)
    await _insert(db, 21, "created", parent_job_id=_u(20))  # cascade-blocked child
    await _insert(db, 30, "completed")  # non-blocking parent (terminal)
    await _insert(db, 31, "created", parent_job_id=_u(30))  # dispatchable child

    got = await db.get_dispatchable_jobs(limit=50)
    ids = {str(r["id"]) for r in got}

    assert str(_u(20)) in ids, (
        "a bare paused job (no blocking ancestor) is itself dispatchable"
    )
    assert str(_u(31)) in ids, "child of a completed parent is dispatchable"
    assert str(_u(21)) not in ids, "child of a paused parent is cascade-blocked"
    assert str(_u(30)) not in ids, (
        "a completed parent is not dispatchable (status gate)"
    )
