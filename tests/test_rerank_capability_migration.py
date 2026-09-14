"""Migrations 0240/0241 add ``rerank`` to the locked catalog capability enum."""

from pathlib import Path


MIGRATIONS = Path(__file__).parent.parent / "src/orchestrator/database/migrations/app"


def test_0240_swaps_the_capabilities_check_for_a_rerank_superset():
    sql = " ".join((MIGRATIONS / "0240_rerank_capability.sql").read_text().split())

    assert "DROP CONSTRAINT IF EXISTS models_capabilities_check" in sql
    assert "ADD CONSTRAINT models_capabilities_check CHECK" in sql
    assert "'search', 'fetch', 'rerank'" in sql
    assert "cardinality(capabilities) >= 1" in sql
    # Lock-cheap swap: the CHECK lands NOT VALID inside one transaction.
    assert ") NOT VALID;" in sql
    assert sql.startswith("-- migration:") and "BEGIN;" in sql and "COMMIT;" in sql


def test_0241_validates_the_constraint_0240_added():
    sql = " ".join(
        (MIGRATIONS / "0241_validate_rerank_capability.sql").read_text().split()
    )
    assert "VALIDATE CONSTRAINT models_capabilities_check" in sql


def test_0242_backfills_one_auto_row_per_embedding_provider_and_pins_it():
    """Upgrades stay non-disruptive: the implicit pre-0240 transport (the
    reranker rode the embedding endpoint) becomes a real row + pin, and only
    when the deployment has no rerank row of its own."""
    sql = " ".join(
        (MIGRATIONS / "0242_rerank_rows_from_embedding_rows.sql").read_text().split()
    )
    assert "INSERT INTO models" in sql
    assert "SELECT DISTINCT ON (e.provider_kind, e.provider_ref)" in sql
    assert "'qwen3-reranker-8b'" in sql
    assert "ARRAY['rerank']::TEXT[]" in sql
    assert "'embedding' = ANY (e.capabilities)" in sql
    # Idempotent and admin-respecting: never runs on top of an existing row.
    assert (
        "NOT EXISTS ( SELECT 1 FROM models AS r WHERE 'rerank' = ANY (r.capabilities) )"
        in sql
    )
    assert "seeded_from" in sql and "'migration:0242'" in sql
    # The pin follows the rows and never overwrites an admin's choice.
    assert "'llm.default_rerank_model'" in sql
    assert (
        "NOT EXISTS ( SELECT 1 FROM system_settings WHERE key = 'llm.default_rerank_model' )"
        in sql
    )
    assert sql.startswith("-- migration:") and "BEGIN;" in sql and "COMMIT;" in sql
