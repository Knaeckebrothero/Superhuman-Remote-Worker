"""Pin the v2 ``models`` schema (capabilities[] only).

Structural test (no real DB). Asserts the table declaration and indexes
match the v2 shape and that no orphan references to the dropped
singular ``capability`` column survive in the schema. Catches accidental
regressions during unrelated schema edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "orchestrator" / "database" / "schema.sql"


@pytest.fixture(scope="module")
def schema_text() -> str:
    return SCHEMA_PATH.read_text()


def test_models_table_declares_capabilities_array(schema_text: str) -> None:
    """The CREATE TABLE body declares capabilities TEXT[] NOT NULL with the
    enum CHECK inline — no separate migration block needed for fresh
    installs."""
    assert "capabilities TEXT[] NOT NULL CHECK (" in schema_text
    assert "cardinality(capabilities) >= 1" in schema_text
    assert (
        "capabilities <@ ARRAY['chat', 'auxiliary', 'embedding', 'vision', 'whisper', 'tts']::TEXT[]"
        in schema_text
    )


def test_unique_constraint_is_v2(schema_text: str) -> None:
    """One physical model is one row — UNIQUE keys on
    (provider_kind, provider_ref, model_id) only."""
    assert "uq_model_provider_v2 UNIQUE (provider_kind, provider_ref, model_id)" in (
        schema_text
    )


def test_gin_index_on_capabilities(schema_text: str) -> None:
    """GIN index supports ``$1 = ANY(capabilities)`` and unnest fan-out."""
    assert (
        "CREATE INDEX IF NOT EXISTS idx_models_capabilities_enabled" in schema_text
    )
    assert "USING GIN (capabilities) WHERE enabled = TRUE" in schema_text


def test_no_singular_capability_column(schema_text: str) -> None:
    """The legacy singular column is fully purged from the schema. Catches
    a regression where someone re-adds it without thinking through the
    fallout for the array-aware accessors."""
    # The CREATE TABLE body must not declare a singular `capability` column.
    assert "capability TEXT NOT NULL CHECK (capability IN" not in schema_text
    # No legacy idx_models_capability_enabled (was dropped in v2).
    assert "idx_models_capability_enabled" not in schema_text
    # No legacy uq_model_provider (4-tuple); only v2.
    assert "uq_model_provider UNIQUE" not in schema_text


def test_no_orphan_capability_references_in_models_section(schema_text: str) -> None:
    """The migration blocks that referenced the singular column have been
    removed. Asserting on the absence of their landmark statements catches
    accidental re-introduction during merges."""
    # The role→capability rename block.
    assert "RENAME COLUMN role TO capability" not in schema_text
    # The capability-CHECK widen migration.
    assert "models_role_check" not in schema_text
    assert (
        "ADD CONSTRAINT models_capability_check\n"
        "        CHECK (capability IN" not in schema_text
    )
