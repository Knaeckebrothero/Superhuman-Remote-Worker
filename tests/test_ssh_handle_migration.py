import pathlib

MIGRATIONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "orchestrator" / "database" / "migrations" / "app"
)


def _read(name):
    return (MIGRATIONS / name).read_text()


def test_both_exist():
    for name in (
        "0202_threads_ssh_handle.sql",
        "0203_threads_ssh_handle_idx.notx.sql",
    ):
        assert (MIGRATIONS / name).exists(), f"{name} missing"


def test_column_is_nullable_with_no_backfill():
    """Backfill happens lazily in application code; a migration-time UPDATE
    would rewrite every row of a hot table."""
    body = _read("0202_threads_ssh_handle.sql")
    assert "ADD COLUMN IF NOT EXISTS ssh_handle text" in body
    assert "NOT NULL" not in body
    assert "UPDATE threads" not in body


def test_column_migration_retries_the_lock():
    assert "lock_timeout" in _read("0202_threads_ssh_handle.sql")


def test_index_is_concurrent_and_non_transactional():
    """The unique index IS the uniqueness enforcement — there is no follow-up
    ADD CONSTRAINT migration. The runner applies every transactional migration
    before any .notx one, so a follow-up constraint would run before this index
    exists."""
    body = _read("0203_threads_ssh_handle_idx.notx.sql")
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in body
    assert "BEGIN;" not in body, "CONCURRENTLY cannot run inside a transaction"


def test_numbers_are_contiguous_and_ordered():
    # NOT "is the newest": Task 7 adds 0204 immediately after this task.
    for name, number in (
        ("0202_threads_ssh_handle.sql", 202),
        ("0203_threads_ssh_handle_idx.notx.sql", 203),
    ):
        assert int(name[:4]) == number
        assert (MIGRATIONS / name).exists()
