"""Pin the idempotent rename of ``user_llm_endpoints`` → ``llm_endpoints``.

This is a structural test: the schema migration must keep the ALTER block
(for upgrades) AND a fresh CREATE TABLE for the new name (for clean
installs). Catches accidental regressions where one or the other is
removed during an unrelated edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "orchestrator" / "database" / "schema.sql"


@pytest.fixture(scope="module")
def schema_text() -> str:
    return SCHEMA_PATH.read_text()


def test_rename_block_is_idempotent(schema_text: str) -> None:
    """The rename block uses information_schema guards so re-running the
    init script on an already-renamed schema is a no-op."""
    assert "ALTER TABLE user_llm_endpoints RENAME TO llm_endpoints" in schema_text
    # Guarded by an information_schema check on the OLD name AND the new name.
    assert "table_name = 'user_llm_endpoints'" in schema_text
    assert "table_name = 'llm_endpoints'" in schema_text


def test_indexes_renamed(schema_text: str) -> None:
    """All three legacy indexes get renamed to drop the user_ prefix."""
    expected = [
        "ALTER INDEX uq_user_llm_endpoint_label_user RENAME TO uq_llm_endpoint_label_user",
        "ALTER INDEX uq_user_llm_endpoint_label_system RENAME TO uq_llm_endpoint_label_system",
        "ALTER INDEX idx_user_llm_endpoints_user RENAME TO idx_llm_endpoints_user",
    ]
    for stmt in expected:
        assert stmt in schema_text, f"missing rename: {stmt}"


def test_fresh_create_uses_new_name(schema_text: str) -> None:
    """Clean installs land on llm_endpoints — the user_llm_endpoints CREATE
    must not survive next to the rename block."""
    assert "CREATE TABLE IF NOT EXISTS llm_endpoints" in schema_text
    assert "CREATE TABLE IF NOT EXISTS user_llm_endpoints" not in schema_text


def test_partial_indexes_rebuilt_under_new_name(schema_text: str) -> None:
    """Fresh installs need the partial unique indexes on the renamed table."""
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_endpoint_label_user" in schema_text
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_endpoint_label_system" in schema_text
    )
    assert "CREATE INDEX IF NOT EXISTS idx_llm_endpoints_user" in schema_text
