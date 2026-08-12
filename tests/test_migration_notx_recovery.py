"""Real-Postgres coverage for reviewed non-transactional recovery recipes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from orchestrator.database.migrate import run_migrations


ROOT = Path(__file__).resolve().parents[1]
APP_MIGRATIONS = ROOT / "orchestrator/database/migrations/app"
DEDUPE = APP_MIGRATIONS / "0130_jobs_verification_dedupe.sql"
DROP_INDEX = APP_MIGRATIONS / "0131_drop_jobs_verification_uniq.notx.sql"
CREATE_INDEX = APP_MIGRATIONS / "0132_jobs_verification_uniq.notx.sql"
RECOVERY_FILES = (DEDUPE, DROP_INDEX, CREATE_INDEX)

PARENT_ID = UUID("10000000-0000-4000-8000-000000000001")


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest.fixture(scope="module")
def recovery_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    container = testcontainers.PostgresContainer("postgres:15")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for notx recovery test: {exc}")
    try:
        yield _asyncpg_dsn(container.get_connection_url())
    finally:
        container.stop()


@pytest_asyncio.fixture
async def recovery_db(recovery_pg_dsn: str):
    pool = await asyncpg.create_pool(recovery_pg_dsn, min_size=1, max_size=6)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        await conn.execute(
            """
            CREATE TABLE jobs (
                id UUID PRIMARY KEY,
                parent_job_id UUID,
                context JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'created',
                assigned_agent_id UUID,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    try:
        yield pool
    finally:
        await pool.close()


def _stage(tmp_path: Path, files: tuple[Path, ...]) -> Path:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir(exist_ok=True)
    for source in files:
        (migrations_dir / source.name).write_bytes(source.read_bytes())
    return migrations_dir


async def _insert_critic(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    round_number: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO jobs (id, parent_job_id, context)
        VALUES ($1, $2, $3::jsonb)
        """,
        job_id,
        PARENT_ID,
        json.dumps(
            {
                "verification_target": str(PARENT_ID),
                "verification_round": round_number,
            }
        ),
    )


async def _index_health(conn: asyncpg.Connection) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT i.indisunique, i.indisvalid, i.indisready, i.indislive,
               pg_get_indexdef(i.indexrelid, 1, TRUE) AS first_key,
               pg_get_indexdef(i.indexrelid, 2, TRUE) AS second_key,
               pg_get_expr(i.indpred, i.indrelid) AS predicate
        FROM pg_index AS i
        WHERE i.indexrelid = to_regclass('jobs_verification_uniq')
        """
    )


@pytest.mark.asyncio
async def test_standard_retry_repairs_dirty_invalid_0132_and_new_duplicates(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    full = _stage(tmp_path, RECOVERY_FILES)
    winner = UUID("20000000-0000-4000-8000-000000000001")
    loser = UUID("20000000-0000-4000-8000-000000000002")
    async with recovery_db.acquire() as conn:
        # Inject the live race at DDL-command start: the runner has already
        # replayed 0130, but both critics land before PostgreSQL scans jobs.
        # The first ordinary run must create its own INVALID shell and dirty
        # ledger row; the test never edits migration-ledger state by hand.
        await conn.execute(
            f"""
            CREATE FUNCTION inject_verification_race() RETURNS event_trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF current_query() LIKE
                   '%CREATE UNIQUE INDEX CONCURRENTLY jobs_verification_uniq%'
                THEN
                    INSERT INTO jobs (id, parent_job_id, context)
                    VALUES
                        ('{winner}'::uuid, '{PARENT_ID}'::uuid,
                         '{{"verification_target": "{PARENT_ID}",
                            "verification_round": 7}}'::jsonb),
                        ('{loser}'::uuid, '{PARENT_ID}'::uuid,
                         '{{"verification_target": "{PARENT_ID}",
                            "verification_round": 7}}'::jsonb);
                END IF;
            END
            $$;
            CREATE EVENT TRIGGER inject_verification_race
                ON ddl_command_start
                WHEN TAG IN ('CREATE INDEX')
                EXECUTE FUNCTION inject_verification_race();
            """
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await run_migrations(recovery_db, full)

        failed_health = await _index_health(conn)
        assert failed_health is not None
        assert failed_health["indisvalid"] is False
        failed_ledger = await conn.fetchrow(
            "SELECT checksum, success, error FROM schema_migrations "
            "WHERE filename = $1",
            CREATE_INDEX.name,
        )
        assert (
            failed_ledger["checksum"]
            == hashlib.sha256(CREATE_INDEX.read_bytes()).hexdigest()
        )
        assert failed_ledger["success"] is False
        assert "duplicated" in failed_ledger["error"]

        await conn.execute("DROP EVENT TRIGGER inject_verification_race")
        await conn.execute("DROP FUNCTION inject_verification_race()")

    await run_migrations(recovery_db, full)
    await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        health = await _index_health(conn)
        assert health is not None
        assert health["indisunique"] is True
        assert health["indisvalid"] is True
        assert health["indisready"] is True
        assert health["indislive"] is True
        assert health["first_key"] == "parent_job_id"
        assert health["second_key"] == "(context ->> 'verification_round'::text)"
        assert (
            "jsonb_exists(context, 'verification_round'::text)" in health["predicate"]
        )

        ledger = await conn.fetchrow(
            "SELECT checksum, success, error FROM schema_migrations "
            "WHERE filename = $1",
            CREATE_INDEX.name,
        )
        assert (
            ledger["checksum"] == hashlib.sha256(CREATE_INDEX.read_bytes()).hexdigest()
        )
        assert ledger["success"] is True
        assert ledger["error"] is None
        rows = await conn.fetch("SELECT id, status, context FROM jobs ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["id"] == winner
        assert json.loads(rows[0]["context"])["verification_round"] == 7
        assert rows[1]["id"] == loser
        assert rows[1]["status"] == "cancelled"
        loser_context = json.loads(rows[1]["context"])
        assert "verification_round" not in loser_context
        assert loser_context["verification_dedupe"]["winner_job_id"] == str(winner)


@pytest.mark.asyncio
async def test_standard_retry_accepts_only_exact_healthy_unledgered_index(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    through_cleanup = _stage(tmp_path, (DEDUPE, DROP_INDEX))
    await run_migrations(recovery_db, through_cleanup)

    async with recovery_db.acquire() as conn:
        await _insert_critic(conn, job_id=uuid4(), round_number=2)
        # Simulate process death after the exact DDL committed but before its
        # ledger INSERT. A blind re-execution would raise DuplicateTableError.
        await conn.execute(CREATE_INDEX.read_text())
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename=$1)",
            CREATE_INDEX.name,
        )

    full = _stage(tmp_path, RECOVERY_FILES)
    await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT success FROM schema_migrations WHERE filename=$1",
            CREATE_INDEX.name,
        )
        health = await _index_health(conn)
        assert health is not None
        assert all(
            health[name]
            for name in ("indisunique", "indisvalid", "indisready", "indislive")
        )


@pytest.mark.asyncio
async def test_valid_wrong_shape_is_rebuilt_instead_of_recorded_green(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    through_cleanup = _stage(tmp_path, (DEDUPE, DROP_INDEX))
    await run_migrations(recovery_db, through_cleanup)

    async with recovery_db.acquire() as conn:
        await conn.execute(
            "CREATE UNIQUE INDEX jobs_verification_uniq ON jobs (status)"
        )
        wrong_health = await _index_health(conn)
        assert wrong_health is not None
        assert wrong_health["indisvalid"] is True
        assert wrong_health["first_key"] == "status"

    full = _stage(tmp_path, RECOVERY_FILES)
    await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        health = await _index_health(conn)
        assert health is not None
        assert health["first_key"] == "parent_job_id"
        assert health["second_key"] == "(context ->> 'verification_round'::text)"
        assert await conn.fetchval(
            "SELECT success FROM schema_migrations WHERE filename=$1",
            CREATE_INDEX.name,
        )


@pytest.mark.asyncio
async def test_concurrent_runners_serialize_notx_operation_and_ledger(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    through_dedupe = _stage(tmp_path, (DEDUPE,))
    await run_migrations(recovery_db, through_dedupe)

    full = _stage(tmp_path, RECOVERY_FILES)
    await asyncio.gather(
        run_migrations(recovery_db, full),
        run_migrations(recovery_db, full),
    )

    async with recovery_db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE filename=$1 AND success",
                CREATE_INDEX.name,
            )
            == 1
        )
        health = await _index_health(conn)
        assert health is not None
        assert all(
            health[name]
            for name in ("indisunique", "indisvalid", "indisready", "indislive")
        )
