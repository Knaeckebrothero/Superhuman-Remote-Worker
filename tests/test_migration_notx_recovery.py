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

from orchestrator.database.migrate import (
    DDL,
    MAINTENANCE_GATES_ENV,
    _concurrent_index_state,
    _force_runner_session_settings,
    _is_expected_concurrent_index,
    _maintenance_gate,
    _runner_owned_transaction_sql,
    discover,
    run_migrations,
)
from orchestrator.database.migration_recovery import NOTX_RECOVERIES


ROOT = Path(__file__).resolve().parents[1]
APP_MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "app"
VECTOR_MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "vector"
AUDIT_MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "audit"
DEDUPE = APP_MIGRATIONS / "0130_jobs_verification_dedupe.sql"
DROP_INDEX = APP_MIGRATIONS / "0131_drop_jobs_verification_uniq.notx.sql"
CREATE_INDEX = APP_MIGRATIONS / "0132_jobs_verification_uniq.notx.sql"
RECOVERY_FILES = (DEDUPE, DROP_INDEX, CREATE_INDEX)

PARENT_ID = UUID("10000000-0000-4000-8000-000000000001")


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


def test_transactional_migration_corpus_is_accepted_by_runner() -> None:
    checked = 0
    for migrations_dir in (APP_MIGRATIONS, VECTOR_MIGRATIONS, AUDIT_MIGRATIONS):
        for path in discover(migrations_dir):
            if path.name.endswith(".notx.sql"):
                continue
            _runner_owned_transaction_sql(path.read_text())
            checked += 1
    assert checked > 0


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
        await conn.execute("DROP TABLE IF EXISTS per_file_path_probe CASCADE")
        await conn.execute("DROP TABLE IF EXISTS wrapped_atomic_probe CASCADE")
        await conn.execute("DROP TABLE IF EXISTS maintenance_gate_probe CASCADE")
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


def _maintenance_stage(tmp_path: Path, *, include_gate: bool, suffix: str) -> Path:
    migrations_dir = tmp_path / suffix
    migrations_dir.mkdir()
    (migrations_dir / "0001_maintenance_base.sql").write_text(
        "CREATE TABLE maintenance_gate_probe (id integer PRIMARY KEY);\n"
    )
    if include_gate:
        (migrations_dir / "0002_maintenance_cutover.sql").write_text(
            "-- maintenance-gate: pinned-runtime-authority-v1\n"
            "ALTER TABLE maintenance_gate_probe ADD COLUMN fenced boolean "
            "NOT NULL DEFAULT true;\n"
        )
    return migrations_dir


def _wrapped_stage(tmp_path: Path, *, suffix: str) -> Path:
    migrations_dir = tmp_path / suffix
    migrations_dir.mkdir()
    (migrations_dir / "0001_wrapped_atomic_probe.sql").write_text(
        "-- transactional: yes\n"
        "BEGIN;\n"
        "CREATE TABLE wrapped_atomic_probe (id integer PRIMARY KEY);\n"
        "COMMIT;\n"
    )
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
async def test_index_catalog_proof_ignores_public_shadow_functions(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    full = _stage(tmp_path, RECOVERY_FILES)
    await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        await conn.execute(
            "CREATE FUNCTION public.to_regclass(text) "
            "RETURNS regclass LANGUAGE sql AS 'SELECT NULL::regclass'; "
            "CREATE FUNCTION public.pg_get_indexdef(oid, integer, boolean) "
            "RETURNS text LANGUAGE sql AS 'SELECT ''shadow''::text'; "
            "CREATE FUNCTION public.pg_get_expr(pg_node_tree, oid) "
            "RETURNS text LANGUAGE sql AS 'SELECT ''shadow''::text'; "
            "CREATE FUNCTION public.generate_series(integer, integer) "
            "RETURNS SETOF integer LANGUAGE sql AS 'SELECT 99'"
        )
    try:
        async with recovery_db.acquire() as conn:
            await _force_runner_session_settings(conn)
            recovery = NOTX_RECOVERIES[CREATE_INDEX.name]
            state = await _concurrent_index_state(conn, recovery)
            assert _is_expected_concurrent_index(state, recovery)
    finally:
        async with recovery_db.acquire() as conn:
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.to_regclass(text); "
                "DROP FUNCTION IF EXISTS "
                "public.pg_get_indexdef(oid, integer, boolean); "
                "DROP FUNCTION IF EXISTS public.pg_get_expr(pg_node_tree, oid); "
                "DROP FUNCTION IF EXISTS public.generate_series(integer, integer)"
            )


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


def test_maintenance_gate_header_is_exact_and_single() -> None:
    assert (
        _maintenance_gate(
            "-- migration: 0002_cutover.sql\n"
            "-- maintenance-gate: pinned-runtime-authority-v1\n"
        )
        == "pinned-runtime-authority-v1"
    )
    for malformed in (
        "-- maintenance-gate: INVALID_NAME\n",
        "-- MAINTENANCE-GATE: pinned-runtime-authority-v1\n",
        "  -- maintenance-gate: pinned-runtime-authority-v1\n",
    ):
        with pytest.raises(RuntimeError, match="malformed maintenance gate"):
            _maintenance_gate(malformed)
    with pytest.raises(RuntimeError, match="more than one maintenance gate"):
        _maintenance_gate(
            "-- maintenance-gate: first-gate\n-- maintenance-gate: second-gate\n"
        )


def test_runner_strips_only_an_exact_outer_transaction_wrapper() -> None:
    wrapped = (
        "-- transactional: yes\n"
        "BEGIN;\n"
        "DO $body$ BEGIN; PERFORM 'COMMIT;'; END $body$;\n"
        "COMMIT;\n"
    )
    execution_sql = _runner_owned_transaction_sql(wrapped)
    assert "-- transactional: yes" in execution_sql
    assert "DO $body$ BEGIN; PERFORM 'COMMIT;'; END $body$;" in execution_sql
    assert "\nBEGIN;\n" not in execution_sql
    assert not execution_sql.rstrip().endswith("COMMIT;")
    assert _runner_owned_transaction_sql("SELECT 'BEGIN; COMMIT;';\n") == (
        "SELECT 'BEGIN; COMMIT;';\n"
    )
    with pytest.raises(RuntimeError, match="transaction control"):
        _runner_owned_transaction_sql(
            "BEGIN;\nSELECT '\\';\nCOMMIT;\n-- '\nSELECT 1;\nCOMMIT;\n"
        )
    with pytest.raises(RuntimeError, match="transaction control"):
        _runner_owned_transaction_sql(
            "BEGIN;\nSELECT prefix$tag$;\nCOMMIT;\n-- $tag$\nSELECT 1;\nCOMMIT;\n"
        )
    with pytest.raises(RuntimeError, match="transaction control"):
        _runner_owned_transaction_sql(
            "BEGIN;\nSELECT prefix·$tag$;\nCOMMIT;\n-- $tag$\nSELECT 1;\nCOMMIT;\n"
        )
    with pytest.raises(RuntimeError, match="transaction control"):
        _runner_owned_transaction_sql(
            "BEGIN;\nSELECT ·E'\\';\nCOMMIT;\n-- '\nSELECT 1;\nCOMMIT;\n"
        )
    escaped_body = _runner_owned_transaction_sql(
        "BEGIN;\nSELECT E'it\\'s; still one string';\nCOMMIT;\n"
    )
    assert "SELECT E'it\\'s; still one string';" in escaped_body
    for newline in ("\r", "\r\n"):
        commented_wrapper = newline.join(
            (
                "-- transactional: yes",
                "BEGIN;",
                "SELECT 1; -- body",
                "COMMIT;",
                "",
            )
        )
        normalized_body = _runner_owned_transaction_sql(commented_wrapper)
        assert "BEGIN;" not in normalized_body
        assert "COMMIT;" not in normalized_body
        assert "SELECT 1;" in normalized_body
    with pytest.raises(RuntimeError, match="standard_conforming_strings"):
        _runner_owned_transaction_sql(
            "BEGIN;\nSET LOCAL standard_conforming_strings = off;\nCOMMIT;\n"
        )
    with pytest.raises(RuntimeError, match="reset all runner-owned"):
        _runner_owned_transaction_sql("BEGIN;\nRESET ALL;\nCOMMIT;\n")
    with pytest.raises(RuntimeError, match="reset all runner-owned"):
        _runner_owned_transaction_sql("BEGIN;\nRESET/* separator */ALL;\nCOMMIT;\n")
    with pytest.raises(RuntimeError, match="transaction control"):
        _runner_owned_transaction_sql(
            "BEGIN;\nSELECT 1;\n"
            "PREPARE/* separator */TRANSACTION 'srw-test';\nCOMMIT;\n"
        )

    for unsafe in (
        "COMMIT;\n",
        "BEGIN; SELECT 1; ROLLBACK;\n",
        "BEGIN; COMMIT; SELECT 1;\n",
        "START TRANSACTION; SELECT 1; COMMIT;\n",
    ):
        with pytest.raises(RuntimeError, match="transaction control"):
            _runner_owned_transaction_sql(unsafe)


@pytest.mark.asyncio
async def test_wrapped_transactional_migration_dry_run_rolls_back_schema_and_ledger(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = _wrapped_stage(tmp_path, suffix="wrapped-dry-run")

    await run_migrations(recovery_db, migrations_dir, dry_run=True)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('wrapped_atomic_probe') IS NULL")
        assert await conn.fetchval("SELECT to_regclass('schema_migrations') IS NULL")


@pytest.mark.asyncio
async def test_failing_dry_run_leaves_preexisting_ledger_unchanged(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "failing-dry-run"
    migrations_dir.mkdir()
    (migrations_dir / "0001_existing_history.sql").write_text("SELECT 1;\n")
    await run_migrations(recovery_db, migrations_dir)

    async with recovery_db.acquire() as conn:
        ledger_before = await conn.fetch(
            "SELECT filename, checksum, success, error "
            "FROM public.schema_migrations ORDER BY filename"
        )

    (migrations_dir / "0002_failing_probe.sql").write_text(
        "CREATE TABLE failing_dry_run_probe (id integer PRIMARY KEY);\nSELECT 1 / 0;\n"
    )
    with pytest.raises(asyncpg.DivisionByZeroError):
        await run_migrations(recovery_db, migrations_dir, dry_run=True)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT to_regclass('public.failing_dry_run_probe') IS NULL"
        )
        assert (
            await conn.fetch(
                "SELECT filename, checksum, success, error "
                "FROM public.schema_migrations ORDER BY filename"
            )
            == ledger_before
        )


@pytest.mark.asyncio
async def test_dry_run_does_not_repair_a_missing_preexisting_ledger_index(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "dry-run-missing-ledger-index"
    migrations_dir.mkdir()
    (migrations_dir / "0001_probe.sql").write_text("SELECT 1;\n")
    await run_migrations(recovery_db, migrations_dir)
    async with recovery_db.acquire() as conn:
        await conn.execute("DROP INDEX public.schema_migrations_dirty_idx")
        assert await conn.fetchval(
            "SELECT pg_catalog.to_regclass"
            "('public.schema_migrations_dirty_idx') IS NULL"
        )

    await run_migrations(recovery_db, migrations_dir, dry_run=True)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT pg_catalog.to_regclass"
            "('public.schema_migrations_dirty_idx') IS NULL"
        )
        assert await conn.fetchval(
            "SELECT success FROM public.schema_migrations WHERE filename=$1",
            "0001_probe.sql",
        )


@pytest.mark.asyncio
async def test_wrapped_migration_and_ledger_roll_back_together_on_ledger_failure(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = _wrapped_stage(tmp_path, suffix="wrapped-ledger-failure")
    async with recovery_db.acquire() as conn:
        await conn.execute(DDL)
        await conn.execute(
            "ALTER TABLE schema_migrations ADD CONSTRAINT reject_wrapped_probe "
            "CHECK (filename <> '0001_wrapped_atomic_probe.sql')"
        )

    with pytest.raises(asyncpg.CheckViolationError):
        await run_migrations(recovery_db, migrations_dir)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('wrapped_atomic_probe') IS NULL")
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename=$1)",
            "0001_wrapped_atomic_probe.sql",
        )


@pytest.mark.asyncio
async def test_runner_forces_standard_strings_and_records_failure_with_pool_size_one(
    recovery_db: asyncpg.Pool,
    recovery_pg_dsn: str,
    tmp_path: Path,
) -> None:
    async with recovery_db.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS standard_strings_probe CASCADE")
    migrations_dir = tmp_path / "standard-strings-off"
    migrations_dir.mkdir()
    filename = "0001_standard_strings_probe.sql"
    (migrations_dir / filename).write_text(
        "BEGIN;\n"
        "CREATE TABLE standard_strings_probe (id integer PRIMARY KEY);\n"
        "SELECT '\\';$x$';\n"
        "COMMIT;\n"
        "SELECT $x$$x$;\n"
        "-- $x$\n"
        "SELECT 1;\n"
        "COMMIT;\n"
    )
    # Pin the exact adversarial environment: this connection would parse the
    # ordinary string with legacy backslash escapes unless the runner forces
    # the setting to match its lexer before executing. A one-slot pool also
    # proves failure recording does not recursively acquire and deadlock.
    narrow_pool = await asyncpg.create_pool(
        recovery_pg_dsn,
        min_size=1,
        max_size=1,
        server_settings={"standard_conforming_strings": "off"},
    )
    try:
        with pytest.raises(asyncpg.PostgresError):
            await asyncio.wait_for(
                run_migrations(narrow_pool, migrations_dir), timeout=10
            )
        async with narrow_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT to_regclass('standard_strings_probe') IS NULL"
            )
            dirty = await conn.fetchrow(
                "SELECT success, error FROM schema_migrations WHERE filename=$1",
                filename,
            )
            assert dirty is not None and dirty["success"] is False
            assert dirty["error"]
    finally:
        await narrow_pool.close()


@pytest.mark.asyncio
async def test_runner_reasserts_public_search_path_before_each_file(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "per-file-session-settings"
    migrations_dir.mkdir()
    (migrations_dir / "0001_change_path.sql").write_text(
        "SET SESSION search_path = pg_catalog;\n"
    )
    (migrations_dir / "0002_create_probe.sql").write_text(
        "CREATE TABLE per_file_path_probe (id integer PRIMARY KEY);\n"
    )

    await run_migrations(recovery_db, migrations_dir)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT to_regclass('public.per_file_path_probe') IS NOT NULL"
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.schema_migrations WHERE success"
            )
            == 2
        )


@pytest.mark.asyncio
async def test_search_path_cannot_hide_restored_public_schema_from_gate(
    recovery_db: asyncpg.Pool,
    recovery_pg_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MAINTENANCE_GATES_ENV, raising=False)
    async with recovery_db.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('public.jobs') IS NOT NULL")
    migrations_dir = _maintenance_stage(
        tmp_path,
        include_gate=True,
        suffix="hidden-public-schema",
    )
    narrow_pool = await asyncpg.create_pool(
        recovery_pg_dsn,
        min_size=1,
        max_size=1,
        server_settings={"search_path": "pg_catalog"},
    )
    try:
        with pytest.raises(RuntimeError, match="maintenance-gated migration"):
            await run_migrations(narrow_pool, migrations_dir)
        async with narrow_pool.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass('public.jobs') IS NOT NULL")
            assert await conn.fetchval(
                "SELECT to_regclass('public.maintenance_gate_probe') IS NULL"
            )
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM public.schema_migrations WHERE success)"
            )
    finally:
        await narrow_pool.close()


@pytest.mark.asyncio
async def test_concurrent_runners_serialize_wrapped_schema_and_single_ledger_row(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = _wrapped_stage(tmp_path, suffix="wrapped-concurrent")

    await asyncio.gather(
        run_migrations(recovery_db, migrations_dir),
        run_migrations(recovery_db, migrations_dir),
    )

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT to_regclass('wrapped_atomic_probe') IS NOT NULL"
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE filename=$1 AND success",
                "0001_wrapped_atomic_probe.sql",
            )
            == 1
        )


@pytest.mark.asyncio
async def test_runner_lock_calls_ignore_public_shadow_functions(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = _wrapped_stage(tmp_path, suffix="shadowed-lock-builtins")
    async with recovery_db.acquire() as conn:
        await conn.execute(
            "CREATE FUNCTION public.pg_try_advisory_lock(bigint) "
            "RETURNS boolean LANGUAGE sql AS 'SELECT false'"
        )
        await conn.execute(
            "CREATE FUNCTION public.pg_advisory_xact_lock(bigint) "
            "RETURNS void LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'shadow lock called'; END $$"
        )
        await conn.execute(
            "CREATE FUNCTION public.pg_advisory_unlock(bigint) "
            "RETURNS boolean LANGUAGE sql AS 'SELECT false'"
        )
    try:
        await asyncio.wait_for(
            asyncio.gather(
                run_migrations(recovery_db, migrations_dir),
                run_migrations(recovery_db, migrations_dir),
            ),
            timeout=10,
        )

        async with recovery_db.acquire() as conn:
            assert await conn.fetchval(
                "SELECT to_regclass('public.wrapped_atomic_probe') IS NOT NULL"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM public.schema_migrations "
                    "WHERE filename=$1 AND success",
                    "0001_wrapped_atomic_probe.sql",
                )
                == 1
            )
    finally:
        async with recovery_db.acquire() as conn:
            await conn.execute(
                "DROP FUNCTION IF EXISTS public.pg_try_advisory_lock(bigint); "
                "DROP FUNCTION IF EXISTS public.pg_advisory_xact_lock(bigint); "
                "DROP FUNCTION IF EXISTS public.pg_advisory_unlock(bigint)"
            )


@pytest.mark.asyncio
async def test_dry_run_catalog_checks_ignore_public_to_regclass_shadow(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "shadowed-catalog-builtins"
    migrations_dir.mkdir()
    (migrations_dir / "0001_existing_history.sql").write_text("SELECT 1;\n")
    await run_migrations(recovery_db, migrations_dir)

    async with recovery_db.acquire() as conn:
        history_before = await conn.fetch(
            "SELECT filename, checksum, success, error "
            "FROM public.schema_migrations ORDER BY filename"
        )
        await conn.execute(
            "CREATE FUNCTION public.to_regclass(text) "
            "RETURNS regclass LANGUAGE sql AS 'SELECT NULL::regclass'"
        )
    (migrations_dir / "0002_catalog_probe.sql").write_text(
        "CREATE TABLE shadow_catalog_probe (id integer PRIMARY KEY);\n"
    )
    try:
        await run_migrations(recovery_db, migrations_dir, dry_run=True)

        async with recovery_db.acquire() as conn:
            assert await conn.fetchval(
                "SELECT pg_catalog.to_regclass('public.shadow_catalog_probe') IS NULL"
            )
            assert (
                await conn.fetch(
                    "SELECT filename, checksum, success, error "
                    "FROM public.schema_migrations ORDER BY filename"
                )
                == history_before
            )
    finally:
        async with recovery_db.acquire() as conn:
            await conn.execute("DROP FUNCTION IF EXISTS public.to_regclass(text)")


@pytest.mark.asyncio
async def test_fresh_chain_applies_maintenance_gate_without_acknowledgement(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MAINTENANCE_GATES_ENV, raising=False)
    full = _maintenance_stage(tmp_path, include_gate=True, suffix="fresh")
    async with recovery_db.acquire() as conn:
        # A real from-zero app schema has no application relation. The module
        # fixture's synthetic jobs table belongs to the unrelated notx tests.
        await conn.execute("DROP TABLE jobs")

    await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='maintenance_gate_probe' AND column_name='fenced')"
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE success")
            == 2
        )


@pytest.mark.asyncio
async def test_schema_without_ledger_is_not_treated_as_fresh(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MAINTENANCE_GATES_ENV, raising=False)
    full = _maintenance_stage(tmp_path, include_gate=True, suffix="restored")

    # ``jobs`` exists but schema_migrations does not: this models a restored
    # schema_current or damaged legacy ledger, not a safe from-zero install.
    with pytest.raises(RuntimeError, match="maintenance-gated migration"):
        await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('jobs') IS NOT NULL")
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE success)"
        )


@pytest.mark.asyncio
async def test_preexisting_empty_ledger_is_not_treated_as_fresh(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MAINTENANCE_GATES_ENV, raising=False)
    full = _maintenance_stage(tmp_path, include_gate=True, suffix="empty-ledger")
    async with recovery_db.acquire() as conn:
        await conn.execute("DROP TABLE jobs")
        await conn.execute(DDL)

    with pytest.raises(RuntimeError, match="maintenance-gated migration"):
        await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE success)"
        )
        assert await conn.fetchval(
            "SELECT to_regclass('maintenance_gate_probe') IS NULL"
        )


@pytest.mark.asyncio
async def test_existing_chain_requires_exact_maintenance_acknowledgement(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MAINTENANCE_GATES_ENV, raising=False)
    base = _maintenance_stage(tmp_path, include_gate=False, suffix="base")
    full = _maintenance_stage(tmp_path, include_gate=True, suffix="full")
    await run_migrations(recovery_db, base)

    with pytest.raises(
        RuntimeError,
        match="requires MIGRATION_MAINTENANCE_GATES to include "
        "'pinned-runtime-authority-v1'",
    ):
        await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='maintenance_gate_probe' AND column_name='fenced')"
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE success")
            == 1
        )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE NOT success)"
        )

    monkeypatch.setenv(
        MAINTENANCE_GATES_ENV,
        "unrelated-gate, pinned-runtime-authority-v1",
    )
    await run_migrations(recovery_db, full)

    async with recovery_db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='maintenance_gate_probe' AND column_name='fenced')"
        )


@pytest.mark.asyncio
async def test_nontransactional_maintenance_gate_is_refused(
    recovery_db: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "notx-gate"
    migrations_dir.mkdir()
    (migrations_dir / "0001_cutover.notx.sql").write_text(
        "-- maintenance-gate: pinned-runtime-authority-v1\nSELECT 1;\n"
    )
    with pytest.raises(RuntimeError, match="must be transactional"):
        await run_migrations(recovery_db, migrations_dir)
