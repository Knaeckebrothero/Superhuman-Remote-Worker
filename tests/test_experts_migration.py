"""Structural assertions on migration 0028 (the repo's migration-test idiom:
read the file text, assert on DDL shape — see test_schema_capabilities_migration.py)."""
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "orchestrator/database/migrations/app/0028_experts.sql"
)


def test_migration_file_exists():
    assert MIGRATION.is_file(), "0028_experts.sql must exist"


def test_experts_table_shape():
    sql = MIGRATION.read_text()
    assert "CREATE TABLE IF NOT EXISTS experts" in sql
    assert "expert_type  VARCHAR(10)  NOT NULL CHECK (expert_type IN ('worker', 'session'))" in sql
    assert "owner_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE" in sql
    assert "config       JSONB        NOT NULL DEFAULT '{}'" in sql
    assert "prompts      JSONB        NOT NULL DEFAULT '{}'" in sql
    assert "uq_experts_name_owner" in sql  # personal fork shadows bundled (decision 5)


def test_project_experts_junction_and_one_default_per_type():
    sql = MIGRATION.read_text()
    assert "CREATE TABLE IF NOT EXISTS project_experts" in sql
    assert "default_for     VARCHAR(10) CHECK (default_for IN ('worker', 'session'))" in sql
    assert "uq_project_default_expert" in sql
    assert "WHERE default_for IS NOT NULL" in sql


def test_jobs_expert_id_set_null_on_delete():
    sql = MIGRATION.read_text()
    # History is safe: resolved_config is frozen per job (decision 15).
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS expert_id UUID" in sql
    assert "REFERENCES experts(id) ON DELETE SET NULL NOT VALID" in sql
    assert "VALIDATE CONSTRAINT jobs_expert_id_fkey" in sql


def test_transactional_header_and_wrapping():
    sql = MIGRATION.read_text()
    assert "-- transactional: yes" in sql
    assert "SET LOCAL lock_timeout" in sql
    assert sql.strip().startswith("-- migration:")
    assert "BEGIN;" in sql and "COMMIT;" in sql
