"""Gate-3 step-1 contract for the one-critic-per-round migrations.

The static checks always run. Real PostgreSQL checks use the existing
``RUN_QUEUE_TEST_DSN`` scratch-DB gate because concurrent index state and
partial-expression uniqueness cannot be represented faithfully by mocks.
The fixture refuses any database whose name does not contain ``test``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
DEDUPE = MIGRATIONS / "0130_jobs_verification_dedupe.sql"
DROP_INDEX = MIGRATIONS / "0131_drop_jobs_verification_uniq.notx.sql"
CREATE_INDEX = MIGRATIONS / "0132_jobs_verification_uniq.notx.sql"

DSN = os.environ.get("RUN_QUEUE_TEST_DSN", "")
requires_postgres = pytest.mark.skipif(
    not DSN,
    reason="RUN_QUEUE_TEST_DSN not set (scratch Postgres required)",
)

PARENT_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("20000000-0000-4000-8000-000000000001")
MISSING = object()


def _statement(path: Path) -> str:
    """Return executable SQL without comments, compacted for assertions."""
    lines = [
        line for line in path.read_text().splitlines() if not line.startswith("--")
    ]
    return " ".join("\n".join(lines).split())


def _assert_scratch_dsn() -> None:
    dbname = DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in dbname:
        pytest.exit(
            f"RUN_QUEUE_TEST_DSN points at database '{dbname}' — refusing: "
            "the scratch database name must contain 'test' (these tests DROP jobs)."
        )


def test_index_ddl_is_the_exact_settled_expression_predicate() -> None:
    sql = _statement(CREATE_INDEX)

    assert sql == (
        "CREATE UNIQUE INDEX CONCURRENTLY jobs_verification_uniq "
        "ON jobs (parent_job_id, (context->>'verification_round')) "
        "WHERE context->>'verification_target' IS NOT NULL "
        "AND jsonb_exists(context, 'verification_round');"
    )
    assert "IF NOT EXISTS" not in sql
    assert "status" not in sql


def test_invalid_shell_cleanup_is_a_separate_earlier_notx_migration() -> None:
    assert _statement(DROP_INDEX) == (
        "DROP INDEX CONCURRENTLY IF EXISTS jobs_verification_uniq;"
    )
    assert DROP_INDEX.name < CREATE_INDEX.name
    assert DROP_INDEX.name.endswith(".notx.sql")
    assert CREATE_INDEX.name.endswith(".notx.sql")


def test_dedupe_contract_preserves_history_and_leaves_exact_predicate() -> None:
    sql = _statement(DEDUPE)

    assert "ORDER BY created_at ASC NULLS LAST, id ASC" in sql
    assert "SET status = 'cancelled', assigned_agent_id = NULL" in sql
    assert "job.context - 'verification_round'" in sql
    assert "'{verification_dedupe}'" in sql
    assert "'original_round', job.context->'verification_round'" in sql
    assert "'winner_job_id', losers.winner_id::text" in sql
    assert "HAVING count(*) > 1" in sql
    assert "RAISE EXCEPTION" in sql


@pytest_asyncio.fixture
async def jobs_db():
    if not DSN:
        pytest.skip("RUN_QUEUE_TEST_DSN not set (scratch Postgres required)")
    _assert_scratch_dsn()

    conn = await asyncpg.connect(DSN, timeout=10)
    await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    await conn.execute(
        """
        CREATE TABLE jobs (
            id UUID PRIMARY KEY,
            parent_job_id UUID,
            context JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'created',
            assigned_agent_id UUID,
            created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    try:
        yield conn
    finally:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
        await conn.close()


async def _insert_job(
    conn: asyncpg.Connection,
    job_id: UUID,
    *,
    round_value: object = MISSING,
    target: UUID | None = PARENT_ID,
    status: str = "created",
    created_at: datetime | None = None,
    assigned_agent_id: UUID | None = None,
) -> None:
    context: dict[str, object] = {"preserved": str(job_id)}
    if target is not None:
        context["verification_target"] = str(target)
    if round_value is not MISSING:
        context["verification_round"] = round_value
    await conn.execute(
        """
        INSERT INTO jobs (
            id, parent_job_id, context, status, assigned_agent_id, created_at
        ) VALUES ($1, $2, $3::jsonb, $4, $5, $6)
        """,
        job_id,
        PARENT_ID,
        json.dumps(context),
        status,
        assigned_agent_id,
        created_at or datetime.now(timezone.utc),
    )


async def _index_health(conn: asyncpg.Connection) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT i.indisunique, i.indisvalid, i.indisready, i.indislive,
               pg_get_expr(i.indpred, i.indrelid) AS predicate
        FROM pg_index AS i
        JOIN pg_class AS c ON c.oid = i.indexrelid
        WHERE c.relname = 'jobs_verification_uniq'
        """
    )


@requires_postgres
@pytest.mark.asyncio
async def test_dedupe_chooses_earliest_created_at_then_id(jobs_db) -> None:
    base = datetime(2026, 8, 10, tzinfo=timezone.utc)
    later_low_id = UUID("00000000-0000-4000-8000-000000000001")
    winner = UUID("00000000-0000-4000-8000-000000000002")
    tied_higher_id = UUID("00000000-0000-4000-8000-000000000003")

    await _insert_job(
        jobs_db,
        later_low_id,
        round_value=4,
        status="processing",
        created_at=base + timedelta(seconds=1),
        assigned_agent_id=AGENT_ID,
    )
    await _insert_job(
        jobs_db,
        tied_higher_id,
        round_value=4,
        status="failed",
        created_at=base,
        assigned_agent_id=AGENT_ID,
    )
    await _insert_job(
        jobs_db,
        winner,
        round_value=4,
        status="completed",
        created_at=base,
        assigned_agent_id=AGENT_ID,
    )

    await jobs_db.execute(DEDUPE.read_text())
    # The SQL itself is replay-safe even though the migration ledger applies it once.
    await jobs_db.execute(DEDUPE.read_text())

    rows = await jobs_db.fetch("SELECT * FROM jobs ORDER BY id")
    by_id = {row["id"]: row for row in rows}
    winner_row = by_id[winner]
    assert winner_row["status"] == "completed"
    assert winner_row["assigned_agent_id"] == AGENT_ID
    assert json.loads(winner_row["context"])["verification_round"] == 4

    for loser_id in (later_low_id, tied_higher_id):
        loser = by_id[loser_id]
        context = json.loads(loser["context"])
        assert loser["status"] == "cancelled"
        assert loser["assigned_agent_id"] is None
        assert "verification_round" not in context
        assert context["verification_target"] == str(PARENT_ID)
        assert context["preserved"] == str(loser_id)
        assert context["verification_dedupe"] == {
            "migration": "0130_jobs_verification_dedupe",
            "reason": "duplicate_parent_round",
            "original_round": 4,
            "winner_job_id": str(winner),
        }

    duplicate_groups = await jobs_db.fetchval(
        """
        SELECT count(*)
        FROM (
            SELECT parent_job_id, context->>'verification_round'
            FROM jobs
            WHERE context->>'verification_target' IS NOT NULL
              AND jsonb_exists(context, 'verification_round')
            GROUP BY parent_job_id, context->>'verification_round'
            HAVING count(*) > 1
        ) AS duplicate_keys
        """
    )
    assert duplicate_groups == 0


@requires_postgres
@pytest.mark.asyncio
async def test_index_is_status_independent_and_excludes_missing_round(jobs_db) -> None:
    winner = UUID("30000000-0000-4000-8000-000000000001")
    await _insert_job(jobs_db, winner, round_value=2, status="cancelled")
    await jobs_db.execute(DROP_INDEX.read_text())
    await jobs_db.execute(CREATE_INDEX.read_text())

    health = await _index_health(jobs_db)
    assert health is not None
    assert health["indisunique"] is True
    assert health["indisvalid"] is True
    assert health["indisready"] is True
    assert health["indislive"] is True
    assert "status" not in health["predicate"]
    assert "jsonb_exists(context, 'verification_round'::text)" in health["predicate"]

    # A terminal row owns the round forever, and status churn never changes
    # index membership or creates a remote 23505 transition hazard.
    await jobs_db.execute(
        "UPDATE jobs SET status = 'completed' WHERE id = $1",
        winner,
    )
    duplicate = UUID("30000000-0000-4000-8000-000000000002")
    with pytest.raises(asyncpg.UniqueViolationError) as exc_info:
        await _insert_job(jobs_db, duplicate, round_value=2, status="created")
    assert exc_info.value.constraint_name == "jobs_verification_uniq"

    # The load-bearing jsonb_exists term excludes absent round keys entirely.
    await _insert_job(
        jobs_db,
        UUID("30000000-0000-4000-8000-000000000003"),
        round_value=MISSING,
    )
    await _insert_job(
        jobs_db,
        UUID("30000000-0000-4000-8000-000000000004"),
        round_value=MISSING,
    )
    assert await jobs_db.fetchval("SELECT count(*) FROM jobs") == 3


@requires_postgres
@pytest.mark.asyncio
async def test_invalid_shell_is_dropped_before_exact_rebuild(jobs_db) -> None:
    first = UUID("40000000-0000-4000-8000-000000000001")
    second = UUID("40000000-0000-4000-8000-000000000002")
    await _insert_job(jobs_db, first, round_value=7)
    await _insert_job(jobs_db, second, round_value=7)

    # A duplicate-blocked concurrent build leaves the dangerous same-name
    # INVALID shell which IF NOT EXISTS would silently accept.
    with pytest.raises(asyncpg.UniqueViolationError):
        await jobs_db.execute(CREATE_INDEX.read_text())
    invalid = await _index_health(jobs_db)
    assert invalid is not None
    assert invalid["indisvalid"] is False

    await jobs_db.execute(DEDUPE.read_text())
    await jobs_db.execute(DROP_INDEX.read_text())
    assert await _index_health(jobs_db) is None

    await jobs_db.execute(CREATE_INDEX.read_text())
    rebuilt = await _index_health(jobs_db)
    assert rebuilt is not None
    assert rebuilt["indisunique"] is True
    assert rebuilt["indisvalid"] is True
    assert rebuilt["indisready"] is True
