"""SQL migration runner.

Apply versioned SQL files from ``orchestrator/database/migrations/{app,vector}/``
in lexicographic order, tracked in a ``schema_migrations`` table on each DB.
Design rationale and operational runbook live in ``knowledge-base/knowledge/db_migration.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import time
from pathlib import Path

import asyncpg

from .migration_recovery import NOTX_RECOVERIES, ConcurrentIndexRecovery
from utils.db_url import build_postgres_url

LOCK_ID = 0x5352575F4D4947  # "SRW_MIG" packed into int64.
# A distinct lock is load-bearing for CREATE INDEX CONCURRENTLY. If a second
# runner opens the transactional pass and then waits on the same session lock,
# PostgreSQL's concurrent build can wait for that transaction's old snapshot
# while the transaction waits for the build: a real advisory/virtual-xid
# deadlock. The normal migration lock still serializes transactional passes;
# this one serializes each non-transactional operation through its ledger write.
NOTX_LOCK_ID = 0x5352575F4E5458  # "SRW_NOTX" packed into int64.
NOTX_SUFFIX = ".notx.sql"

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename       TEXT         PRIMARY KEY,
    checksum       TEXT         NOT NULL,
    applied_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    applied_by     TEXT         NOT NULL DEFAULT current_user,
    execution_ms   INTEGER      NOT NULL,
    success        BOOLEAN      NOT NULL DEFAULT TRUE,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS schema_migrations_dirty_idx
    ON schema_migrations(filename) WHERE success = FALSE;
"""

# Columns the runner needs on schema_migrations. ``CREATE TABLE IF NOT
# EXISTS`` no-ops if a table by the same name already exists with a
# different shape, so we explicitly verify after creation. Pre-cutover
# vector_schema.sql shipped a hand-rolled ``schema_migrations(version,
# description)`` table; this check turns that collision into a clear
# error instead of an UndefinedColumnError on the first INSERT.
REQUIRED_COLUMNS = frozenset(
    {
        "filename",
        "checksum",
        "applied_at",
        "applied_by",
        "execution_ms",
        "success",
        "error",
    }
)


class DryRunRollback(Exception):
    """Raised at end of dry-run to force the outer transaction to roll back."""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_notx(path: Path) -> bool:
    # ``.notx.sql`` is two dots; ``Path.suffixes`` returns ['.notx', '.sql'].
    return path.name.endswith(NOTX_SUFFIX)


async def _acquire_notx_lock(conn: asyncpg.Connection) -> None:
    """Acquire the session lock without blocking a concurrent-index build.

    A blocking ``pg_advisory_lock`` call itself holds a virtual transaction ID.
    ``CREATE INDEX CONCURRENTLY`` may wait for that transaction while it waits
    for the advisory lock, producing a deadlock. Short try-lock statements end
    before the build needs to advance to its next phase.
    """
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", NOTX_LOCK_ID):
        await asyncio.sleep(0.05)


def discover(migrations_dir: Path) -> list[Path]:
    """List migrations in apply order. Reject duplicate versions early.

    Normal migrations use ``NNNN_``.  A lowercase suffix (``NNNNa_``) is an
    exceptional interstitial version used only to repair an immutable later
    migration for databases that have not reached it yet.  The interstitial
    itself must also be safe when discovered by databases already past it.
    """
    if not migrations_dir.is_dir():
        raise RuntimeError(f"migrations dir not found: {migrations_dir}")
    files = sorted(
        (
            *migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"),
            *migrations_dir.glob("[0-9][0-9][0-9][0-9][a-z]_*.sql"),
        )
    )
    seen: dict[str, Path] = {}
    for path in files:
        prefix = path.name.split("_", 1)[0]
        if prefix in seen:
            raise RuntimeError(
                f"duplicate migration prefix {prefix!r}: "
                f"{seen[prefix].name} vs {path.name}"
            )
        seen[prefix] = path
    return files


async def _record_failure(
    pool: asyncpg.Pool, filename: str, checksum: str, ms: int, error: str
) -> None:
    """Write the failure row on a fresh connection — the outer txn is rolling back."""
    async with pool.acquire() as failconn:
        await failconn.execute(
            "INSERT INTO schema_migrations(filename, checksum, "
            "execution_ms, success, error) VALUES($1,$2,$3,FALSE,$4) "
            "ON CONFLICT (filename) DO UPDATE SET "
            "checksum = EXCLUDED.checksum, "
            "applied_at = now(), "
            "execution_ms = EXCLUDED.execution_ms, "
            "success = FALSE, "
            "error = EXCLUDED.error",
            filename,
            checksum,
            ms,
            error[:8000],
        )


async def _record_failure_on_connection(
    conn: asyncpg.Connection,
    filename: str,
    checksum: str,
    ms: int,
    error: str,
) -> None:
    """Record a non-transactional failure while its session lock is held."""
    await conn.execute(
        "INSERT INTO schema_migrations(filename, checksum, "
        "execution_ms, success, error) VALUES($1,$2,$3,FALSE,$4) "
        "ON CONFLICT (filename) DO UPDATE SET "
        "checksum = EXCLUDED.checksum, "
        "applied_at = now(), "
        "applied_by = current_user, "
        "execution_ms = EXCLUDED.execution_ms, "
        "success = FALSE, "
        "error = EXCLUDED.error",
        filename,
        checksum,
        ms,
        error[:8000],
    )


async def _record_success_on_connection(
    conn: asyncpg.Connection,
    filename: str,
    checksum: str,
    ms: int,
) -> None:
    """Record a non-transactional success, replacing its dirty row if any."""
    await conn.execute(
        "INSERT INTO schema_migrations"
        "(filename, checksum, execution_ms) VALUES($1,$2,$3) "
        "ON CONFLICT (filename) DO UPDATE SET "
        "checksum = EXCLUDED.checksum, "
        "applied_at = now(), "
        "applied_by = current_user, "
        "execution_ms = EXCLUDED.execution_ms, "
        "success = TRUE, "
        "error = NULL",
        filename,
        checksum,
        ms,
    )


async def _concurrent_index_state(
    conn: asyncpg.Connection,
    recovery: ConcurrentIndexRecovery,
) -> asyncpg.Record | None:
    """Return the visible same-name index state and its canonical shape."""
    return await conn.fetchrow(
        """
        SELECT
            i.indisunique,
            i.indisvalid,
            i.indisready,
            i.indislive,
            i.indnkeyatts,
            i.indnatts,
            am.amname AS access_method,
            i.indrelid = to_regclass($2) AS expected_table,
            ARRAY(
                SELECT pg_get_indexdef(i.indexrelid, position, TRUE)
                FROM generate_series(1, i.indnkeyatts) AS position
                ORDER BY position
            ) AS key_definitions,
            pg_get_expr(i.indpred, i.indrelid) AS predicate
        FROM pg_index AS i
        JOIN pg_class AS index_class ON index_class.oid = i.indexrelid
        JOIN pg_am AS am ON am.oid = index_class.relam
        WHERE i.indexrelid = to_regclass($1)
        """,
        recovery.index_name,
        recovery.table_name,
    )


def _is_expected_concurrent_index(
    state: asyncpg.Record | None,
    recovery: ConcurrentIndexRecovery,
) -> bool:
    """Require both usable catalog flags and the reviewed exact index shape."""
    if state is None:
        return False
    return (
        state["indisunique"]
        and state["indisvalid"]
        and state["indisready"]
        and state["indislive"]
        and state["indnkeyatts"] == len(recovery.key_definitions)
        and state["indnatts"] == len(recovery.key_definitions)
        and state["access_method"] == recovery.access_method
        and state["expected_table"]
        and tuple(state["key_definitions"]) == recovery.key_definitions
        and state["predicate"] == recovery.predicate
    )


async def _require_applied_recovery_dependency(
    conn: asyncpg.Connection,
    migrations_dir: Path,
    filename: str,
) -> Path:
    """Resolve one reviewed replay file and prove its ledger checksum."""
    path = migrations_dir / filename
    if not path.is_file():
        raise RuntimeError(
            f"recovery dependency {filename!r} is missing for {migrations_dir}"
        )
    row = await conn.fetchrow(
        "SELECT checksum, success FROM schema_migrations WHERE filename = $1",
        filename,
    )
    checksum = _checksum(path.read_text())
    if row is None or not row["success"]:
        raise RuntimeError(
            f"recovery dependency {filename!r} is not successfully applied"
        )
    if row["checksum"] != checksum:
        raise RuntimeError(
            f"checksum changed: {filename} (recovery dependencies are immutable)"
        )
    return path


async def _run_concurrent_index_recovery(
    conn: asyncpg.Connection,
    path: Path,
    recovery: ConcurrentIndexRecovery,
) -> None:
    """Converge one explicitly reviewed concurrent-index migration."""
    cleanup_path = await _require_applied_recovery_dependency(
        conn,
        path.parent,
        recovery.cleanup_filename,
    )
    replay_path = await _require_applied_recovery_dependency(
        conn,
        path.parent,
        recovery.replay_filename,
    )

    state = await _concurrent_index_state(conn, recovery)
    if state is not None and not _is_expected_concurrent_index(state, recovery):
        log.warning(
            "recovering unusable or unexpected %s before %s",
            recovery.index_name,
            path.name,
        )
        await conn.execute(cleanup_path.read_text())
        if await _concurrent_index_state(conn, recovery) is not None:
            raise RuntimeError(
                f"{cleanup_path.name} did not remove {recovery.index_name}"
            )

    # Re-run the reviewed, replay-safe dedupe immediately before the build.
    # This closes the ordinary retry gap where new duplicates appeared after
    # 0130 was ledgered but before a prior concurrent build completed.
    await conn.execute(replay_path.read_text())

    state = await _concurrent_index_state(conn, recovery)
    if state is None:
        # Execute the immutable migration bytes, not reconstructed DDL.
        await conn.execute(path.read_text())
        state = await _concurrent_index_state(conn, recovery)

    if not _is_expected_concurrent_index(state, recovery):
        raise RuntimeError(
            f"{path.name} did not produce a unique, valid, ready, live "
            f"{recovery.index_name} with the reviewed shape"
        )


async def run_migrations(
    pool: asyncpg.Pool,
    migrations_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Apply pending migrations; refuse to run if anything looks wrong.

    Raises:
        RuntimeError: dirty row, checksum drift, missing applied file, or
            duplicate prefixes on disk.
        Exception: re-raised from the failing migration after recording the
            failure on the dirty row.
    """
    files = discover(migrations_dir)
    if not files:
        log.warning("no migrations in %s", migrations_dir)
        return

    transactional = [p for p in files if not _is_notx(p)]
    non_transactional = [p for p in files if _is_notx(p)]

    async with pool.acquire() as conn:
        await conn.execute(DDL)

        existing_columns = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'schema_migrations' "
                "AND table_schema = ANY(current_schemas(FALSE))"
            )
        }
        missing = REQUIRED_COLUMNS - existing_columns
        if missing:
            raise RuntimeError(
                f"schema_migrations table exists but is missing columns "
                f"{sorted(missing)}; this is most likely a legacy table "
                f"from before the migration runner. Drop it manually "
                f"(DROP TABLE schema_migrations) and let the runner "
                f"recreate it. See knowledge-base/knowledge/db_migration.md §Operational "
                f"runbook."
            )

        # Transactional pass — wrapped under the advisory lock + outer txn.
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)

                dirty_rows = await conn.fetch(
                    "SELECT filename, checksum, error FROM schema_migrations "
                    "WHERE success = FALSE ORDER BY filename"
                )
                unrecoverable_dirty = [
                    row for row in dirty_rows if row["filename"] not in NOTX_RECOVERIES
                ]
                if unrecoverable_dirty:
                    dirty = unrecoverable_dirty[0]
                    raise RuntimeError(
                        f"dirty migration {dirty['filename']!r}: "
                        f"{dirty['error']!s}; manual repair required "
                        f"(see knowledge-base/knowledge/db_migration.md §Operational runbook)"
                    )
                for dirty in dirty_rows:
                    path = next(
                        (item for item in files if item.name == dirty["filename"]),
                        None,
                    )
                    if path is None:
                        raise RuntimeError(
                            f"dirty recoverable migration {dirty['filename']!r} "
                            "is missing on disk"
                        )
                    if dirty["checksum"] != _checksum(path.read_text()):
                        raise RuntimeError(
                            f"checksum changed: {path.name} "
                            "(dirty recoverable migrations are immutable)"
                        )
                    log.warning(
                        "dirty migration %s has an explicit automatic "
                        "recovery contract; retrying",
                        dirty["filename"],
                    )

                applied = {
                    r["filename"]: r["checksum"]
                    for r in await conn.fetch(
                        "SELECT filename, checksum FROM schema_migrations "
                        "WHERE success = TRUE ORDER BY filename"
                    )
                }

                for path in files:
                    if path.name in applied:
                        if applied[path.name] != _checksum(path.read_text()):
                            raise RuntimeError(
                                f"checksum changed: {path.name} "
                                f"(applied migrations are immutable; "
                                f"write a superseding migration instead)"
                            )

                stray = set(applied) - {p.name for p in files}
                if stray:
                    raise RuntimeError(f"applied but missing on disk: {sorted(stray)}")

                pending_tx = [p for p in transactional if p.name not in applied]

                if not pending_tx and not [
                    p for p in non_transactional if p.name not in applied
                ]:
                    log.info(
                        "schema up to date (%d applied) in %s",
                        len(applied),
                        migrations_dir.name,
                    )
                    return

                if pending_tx:
                    log.info(
                        "applying %d transactional migration(s) in %s",
                        len(pending_tx),
                        migrations_dir.name,
                    )
                for path in pending_tx:
                    sql = path.read_text()
                    log.info("→ %s", path.name)
                    t0 = time.monotonic()
                    try:
                        await conn.execute(sql)
                    except Exception as exc:
                        ms = int((time.monotonic() - t0) * 1000)
                        await _record_failure(
                            pool, path.name, _checksum(sql), ms, str(exc)
                        )
                        raise
                    ms = int((time.monotonic() - t0) * 1000)
                    await conn.execute(
                        "INSERT INTO schema_migrations"
                        "(filename, checksum, execution_ms) "
                        "VALUES($1,$2,$3)",
                        path.name,
                        _checksum(sql),
                        ms,
                    )
                    log.info("✓ %s (%d ms)", path.name, ms)

                if dry_run:
                    raise DryRunRollback()
        except DryRunRollback:
            log.info("dry-run: transactional pass rolled back")
            return

    # Non-transactional pass — run outside any txn, one connection per migration
    # so the runner survives the kinds of operations that demand it (CREATE
    # INDEX CONCURRENTLY, ALTER SYSTEM, etc.). A session advisory lock spans
    # the physical operation, catalog verification (when registered), and its
    # ledger write. Skipped on dry-run.
    if dry_run:
        if non_transactional:
            log.info(
                "dry-run: skipping %d non-transactional migration(s) "
                "(cannot be rolled back)",
                len(non_transactional),
            )
        return

    for path in non_transactional:
        async with pool.acquire() as conn:
            await _acquire_notx_lock(conn)
            try:
                # A second runner may have completed this migration while this
                # connection waited for the session lock, so re-read the row.
                applied_row = await conn.fetchrow(
                    "SELECT checksum FROM schema_migrations "
                    "WHERE filename = $1 AND success = TRUE",
                    path.name,
                )
                sql = path.read_text()
                checksum = _checksum(sql)
                if applied_row:
                    if applied_row["checksum"] != checksum:
                        raise RuntimeError(
                            f"checksum changed: {path.name} "
                            "(applied migrations are immutable; write a "
                            "superseding migration instead)"
                        )
                    continue

                recovery = NOTX_RECOVERIES.get(path.name)
                log.info("→ %s (non-transactional)", path.name)
                t0 = time.monotonic()
                try:
                    if recovery is None:
                        await conn.execute(sql)
                    else:
                        await _run_concurrent_index_recovery(conn, path, recovery)
                except Exception as exc:
                    ms = int((time.monotonic() - t0) * 1000)
                    await _record_failure_on_connection(
                        conn,
                        path.name,
                        checksum,
                        ms,
                        str(exc),
                    )
                    raise
                ms = int((time.monotonic() - t0) * 1000)
                await _record_success_on_connection(
                    conn,
                    path.name,
                    checksum,
                    ms,
                )
                log.info("✓ %s (%d ms)", path.name, ms)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", NOTX_LOCK_ID)


async def _main() -> int:
    """Entrypoint for ``python -m orchestrator.database.migrate``.

    Used by CI for the dry-run gate and for ad-hoc local runs against a
    DATABASE_URL. The orchestrator itself drives the runner via lifespan.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=str(Path(__file__).parent / "migrations" / "app"),
        help="Migrations directory (default: ./migrations/app)",
    )
    parser.add_argument(
        "--database-url",
        default=build_postgres_url("POSTGRES", fallback_env="DATABASE_URL"),
        help=(
            "Postgres URL (defaults to a DSN built from "
            "POSTGRES_USER/PASSWORD/HOST/PORT/DB, falling back to $DATABASE_URL)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apply transactional migrations then roll back",
    )
    args = parser.parse_args()

    if not args.database_url:
        parser.error(
            "--database-url is required (or set POSTGRES_USER/PASSWORD plus "
            "POSTGRES_HOST/PORT/DB, or $DATABASE_URL)"
        )

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    pool = await asyncpg.create_pool(args.database_url, min_size=1, max_size=2)
    try:
        await run_migrations(pool, Path(args.dir), dry_run=args.dry_run)
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
