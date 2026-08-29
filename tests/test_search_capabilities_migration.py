"""Migration 0205 expands the locked catalog capability enum."""

from pathlib import Path


MIGRATION = (
    Path(__file__).parent.parent
    / "orchestrator/database/migrations/app/0212_search_fetch_capabilities.sql"
)


def test_migration_replaces_capabilities_check_with_search_and_fetch():
    sql = " ".join(MIGRATION.read_text().split())

    assert "DROP CONSTRAINT IF EXISTS models_capabilities_check" in sql
    assert "ADD CONSTRAINT models_capabilities_check CHECK" in sql
    assert "'search', 'fetch'" in sql
    assert "cardinality(capabilities) >= 1" in sql
