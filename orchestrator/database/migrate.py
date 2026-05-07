"""SQL migration runner.

Apply numbered SQL files from ``orchestrator/database/migrations/{app,vector}/``
in lexicographic order, tracked in a ``schema_migrations`` table on each DB.
Design rationale and operational runbook live in ``docs/db_migration.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import time
from pathlib import Path

import asyncpg

from orchestrator.utils.db_url import build_postgres_url

LOCK_ID = 0x5352575F4D4947  # "SRW_MIG" packed into int64.
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


def discover(migrations_dir: Path) -> list[Path]:
    """List migrations in apply order. Reject obviously malformed names early."""
    if not migrations_dir.is_dir():
        raise RuntimeError(f"migrations dir not found: {migrations_dir}")
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
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
                f"recreate it. See docs/db_migration.md §Operational "
                f"runbook."
            )

        # Transactional pass — wrapped under the advisory lock + outer txn.
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)

                dirty = await conn.fetchrow(
                    "SELECT filename, error FROM schema_migrations "
                    "WHERE success = FALSE LIMIT 1"
                )
                if dirty:
                    raise RuntimeError(
                        f"dirty migration {dirty['filename']!r}: "
                        f"{dirty['error']!s}; manual repair required "
                        f"(see docs/db_migration.md §Operational runbook)"
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
    # INDEX CONCURRENTLY, ALTER SYSTEM, etc.). Skipped on dry-run.
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
            applied_row = await conn.fetchrow(
                "SELECT 1 FROM schema_migrations "
                "WHERE filename = $1 AND success = TRUE",
                path.name,
            )
            if applied_row:
                continue

            sql = path.read_text()
            log.info("→ %s (non-transactional)", path.name)
            t0 = time.monotonic()
            try:
                await conn.execute(sql)
            except Exception as exc:
                ms = int((time.monotonic() - t0) * 1000)
                await _record_failure(pool, path.name, _checksum(sql), ms, str(exc))
                raise
            ms = int((time.monotonic() - t0) * 1000)
            await conn.execute(
                "INSERT INTO schema_migrations"
                "(filename, checksum, execution_ms) VALUES($1,$2,$3)",
                path.name,
                _checksum(sql),
                ms,
            )
            log.info("✓ %s (%d ms)", path.name, ms)


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
