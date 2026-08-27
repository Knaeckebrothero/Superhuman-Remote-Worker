"""Safety contract for the manually run historical LLM pricing backfill."""

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts/backfill_llm_pricing.py"


def test_backfill_uses_idempotent_additive_corrections_not_mutation() -> None:
    source = SCRIPT.read_text()

    assert "UPDATE usage_events" not in source
    assert "INSERT INTO usage_events" in source
    assert "llm-pricing-correction-v1" in source
    assert "unpriced-reversal" in source
    assert "priced-replacement" in source
    assert "ON CONFLICT (source, source_id, unit, ts) DO NOTHING" in source
    assert "original.quantity * original.quantity_sign" in source
