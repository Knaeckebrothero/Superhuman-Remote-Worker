"""Static assertions about migration 0201. No database required."""

import pathlib

MIGRATIONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
SQL = MIGRATIONS / "0201_user_ssh_keys.sql"


def test_migration_exists():
    assert SQL.exists(), "0201_user_ssh_keys.sql not created"


def test_declares_its_parent():
    # Filename only — asserting on header column alignment makes the test fail
    # for cosmetic reasons when an implementer follows house format.
    assert "0200_pinned_agent_recycle_authority.sql" in SQL.read_text()


def test_is_transactional_and_bounded():
    body = SQL.read_text()
    assert "BEGIN;" in body and "COMMIT;" in body
    assert "SET LOCAL lock_timeout" in body


def test_fingerprint_is_globally_unique():
    """Per-user uniqueness would let one key identify two people, which makes
    the gateway's fingerprint-to-user lookup ambiguous."""
    body = SQL.read_text()
    assert "UNIQUE (fingerprint_sha256)" in body
    assert "UNIQUE (user_id, fingerprint_sha256)" not in body


def test_cascades_from_users():
    assert "REFERENCES public.users(id) ON DELETE CASCADE" in SQL.read_text()


def test_number_follows_its_declared_parent():
    # Deliberately NOT "is the newest migration": later tasks in this plan add
    # 0190-0193, which would break that assertion two tasks after it is written.
    # Head tracking belongs to test_infrastructure_metering_migrations.py.
    assert int(SQL.name[:4]) == 201
    assert "0200_pinned_agent_recycle_authority.sql" in SQL.read_text()
