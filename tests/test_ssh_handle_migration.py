import pathlib

from orchestrator.database.migrate import discover

MIGRATIONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)

COLUMN = "0202_threads_ssh_handle.sql"
INDEX = "0203_threads_ssh_handle_idx.notx.sql"


def _read(name):
    return (MIGRATIONS / name).read_text()


def _statements(name):
    """The migration's SQL with comment lines stripped.

    Whole-file substring assertions are a false-failure trap on files this
    heavily commented — 0202 has to explain the deferred-fence drain, and
    prose about a NOT NULL column would otherwise fail a test about the DDL.
    """
    return "\n".join(
        line for line in _read(name).splitlines() if not line.lstrip().startswith("--")
    )


def test_both_exist():
    for name in (COLUMN, INDEX):
        assert (MIGRATIONS / name).exists(), f"{name} missing"


def test_column_is_nullable_with_no_backfill():
    """Backfill happens lazily in application code; a migration-time UPDATE
    would rewrite every row of a hot table."""
    sql = _statements(COLUMN)
    assert "ADD COLUMN IF NOT EXISTS ssh_handle text" in sql
    # Whole-file, not just the ADD COLUMN clause: a later `ALTER COLUMN
    # ssh_handle SET NOT NULL` in a separate statement is exactly what this
    # test exists to catch. Comment-stripping alone keeps prose from tripping
    # it, so there is no reason to narrow the span further.
    assert "NOT NULL" not in sql
    assert "UPDATE threads" not in sql
    assert "UPDATE public.threads" not in sql


def test_column_migration_retries_the_lock():
    assert "lock_timeout" in _statements(COLUMN)


def test_column_migration_drains_the_deferred_fence():
    """0202 must fire 0185's threads_agent_reciprocity_fence before its ALTER.

    0191 UPDATEs public.threads while that fence is installed DEFERRABLE
    INITIALLY DEFERRED, and the runner applies every transactional migration in
    ONE transaction — so on any upgrade whose unapplied span still reaches back
    past 0191, those AFTER-row events are still pending here and Postgres
    refuses "ALTER TABLE ... because it has pending trigger events", aborting
    the pass and hard-failing boot. A fresh database is immune only because
    0191's UPDATE matches no rows, so the schema snapshot cannot catch this.

    The end-to-end proof is
    test_infrastructure_metering_migrations.py::
    test_0185_serializes_real_predecessor_rows_with_lane_changes, which replays
    the live migrations directory against a threads table that HAS rows. This
    test exists because that coverage is incidental to that test's purpose and
    was narrowed away once already.
    """
    sql = _statements(COLUMN)
    drain = "SET CONSTRAINTS public.threads_agent_reciprocity_fence IMMEDIATE"
    restore = "SET CONSTRAINTS public.threads_agent_reciprocity_fence DEFERRED"
    assert drain in sql, "0202 must drain the deferred fence before ALTER TABLE"
    assert restore in sql, "0202 must restore the fence's declared DEFERRED timing"
    assert sql.index(drain) < sql.index("ALTER TABLE public.threads"), (
        "the drain must precede the ALTER, or the pending events still block it"
    )
    assert sql.index("ALTER TABLE public.threads") < sql.index(restore), (
        "the restore must follow the ALTER"
    )
    assert "SET CONSTRAINTS ALL" not in sql, (
        "scope the drain to the one constraint: SET CONSTRAINTS ALL would also "
        "change officer_ticket_claim_job_integrity's declared IMMEDIATE timing "
        "for every migration appended after this one"
    )


def test_index_is_concurrent_and_non_transactional():
    """The unique index IS the uniqueness enforcement — there is no follow-up
    ADD CONSTRAINT migration. The runner applies every transactional migration
    before any .notx one, so a follow-up constraint would run before this index
    exists."""
    sql = _statements(INDEX)
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in sql
    assert "BEGIN;" not in sql, "CONCURRENTLY cannot run inside a transaction"


def test_index_refuses_if_not_exists():
    """Per 0132's runbook: IF NOT EXISTS reports success against the INVALID
    same-name shell a failed concurrent build leaves behind, which would record
    0203 as applied while uniqueness went unenforced. It buys nothing on the
    success path either — the runner skips a .notx migration the ledger already
    records as applied. A duplicate handle would route SSH to the wrong
    workspace."""
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS" not in _statements(INDEX)


def test_index_is_partial():
    """Backfill is lazy, so nearly every existing row keeps a NULL handle. A
    full index would store an entry per thread row and add write amplification
    to a hot table for nothing; the partial predicate enforces exactly the same
    uniqueness, since NULLs are distinct under the default NULLS DISTINCT."""
    assert "WHERE ssh_handle IS NOT NULL" in _statements(INDEX)


def test_discover_accepts_the_directory_and_orders_our_pair():
    """A duplicate migration prefix hard-fails boot, so assert against the real
    rule rather than a reimplementation of it: discover() raises on duplicates,
    and it — not the filename — decides what counts as a version (0092 and
    0092z are distinct interstitials, not a collision).

    Not hypothetical: 0202/0203 were first written as 0190/0191 and collided
    with upstream migrations of those numbers after a rebase.
    """
    names = [path.name for path in discover(MIGRATIONS)]
    assert names.count(COLUMN) == 1
    assert names.count(INDEX) == 1
    assert names.index(COLUMN) < names.index(INDEX)
    # Not "is the newest": Task 7 adds 0204 immediately after this task.
    assert names.index(COLUMN) == names.index("0201_user_ssh_keys.sql") + 1
