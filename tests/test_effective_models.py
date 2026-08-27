"""Unit tests for `_effective_models_from_layers` (Layer 3 effective-model display).

The pure helper mirrors the dispatch precedence + the agent's get_phase_config
fallback so the create-form picker can show the model that will actually run when
left untouched. See
knowledge-base/knowledge/issues/loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md (Layer 3).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add orchestrator/ to sys.path so its top-level modules import bare.
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402

eff = main._effective_models_from_layers


class TestEffectiveModelsFromLayers:
    def test_model_agnostic_expert_uses_account_default(self):
        """A bundled, model-agnostic expert ({}) resolves to the user's account
        default_model across all slots."""
        r = eff({}, "acct-model", "sys-model")
        assert r["strategic"] == {"model": "acct-model", "source": "account_default"}
        assert r["tactical"] == {"model": "acct-model", "source": "account_default"}
        assert r["session"] == {"model": "acct-model", "source": "account_default"}

    def test_no_account_default_falls_to_system(self):
        r = eff({}, None, "sys-model")
        assert all(
            s == {"model": "sys-model", "source": "system_default"} for s in r.values()
        )

    def test_expert_top_level_pin_wins_everywhere(self):
        r = eff({"model": "exp-model"}, "acct", "sys")
        assert all(s == {"model": "exp-model", "source": "expert"} for s in r.values())

    def test_expert_strategic_pin_only(self):
        """A strategic pin applies to strategic; tactical + session fall through
        to the account/system default (mirrors get_phase_config)."""
        r = eff({"strategic": {"model": "strat"}}, "acct", "sys")
        assert r["strategic"] == {"model": "strat", "source": "expert"}
        assert r["tactical"] == {"model": "acct", "source": "account_default"}
        assert r["session"] == {"model": "acct", "source": "account_default"}

    def test_expert_both_phase_pins(self):
        r = eff({"strategic": {"model": "s"}, "tactical": {"model": "t"}}, None, "sys")
        assert r["strategic"] == {"model": "s", "source": "expert"}
        assert r["tactical"] == {"model": "t", "source": "expert"}
        assert r["session"] == {"model": "sys", "source": "system_default"}

    def test_top_level_pin_plus_one_phase_pin(self):
        """Top-level expert pin is the floor for unpinned phases; an explicit
        phase pin still overrides it."""
        r = eff({"model": "top", "tactical": {"model": "tac"}}, "acct", "sys")
        assert r["strategic"] == {"model": "top", "source": "expert"}
        assert r["tactical"] == {"model": "tac", "source": "expert"}
        assert r["session"] == {"model": "top", "source": "expert"}

    def test_none_expert_llm_is_safe(self):
        r = eff(None, "acct", "sys")
        assert r["session"] == {"model": "acct", "source": "account_default"}

    def test_no_defaults_at_all_yields_null_model(self):
        """No expert pin, no account default, no registry default → model is
        None and the UI shows a bare 'Default'."""
        r = eff({}, None, None)
        assert r["session"] == {"model": None, "source": "system_default"}
