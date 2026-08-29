"""Unit tests for `_effective_models_from_layers` (Layer 3 effective-model display).

The pure helper mirrors the dispatch precedence so the create-form picker can
show the model that will actually run when left untouched. Since U1 an expert
has ONE model (``llm.model``) plus the roster-wide subagent model
(``subagents.llm.model``): the per-phase tiers are gone, a legacy fragment is
read through the loader's compat mapping (a stored ``llm.strategic`` pin
surfaces as ``model``, a stored ``llm.subagent`` as the ``subagent`` slot), and
``strategic`` / ``tactical`` are kept equal to ``model`` as deprecated aliases
until the cockpit reads ``model`` (U1 WP6). See
knowledge-base/knowledge/issues/loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md (Layer 3)
and knowledge-base/knowledge/features/universal_experts_and_subagents.md §1.1.
"""

from __future__ import annotations

import copy
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

SLOTS = {"model", "subagent", "session", "strategic", "tactical"}


def _all_equal(result: dict, expected: dict) -> bool:
    return set(result) == SLOTS and all(result[s] == expected for s in SLOTS)


class TestEffectiveModelsFromLayers:
    def test_model_agnostic_expert_uses_account_default(self):
        """A bundled, model-agnostic expert ({}) resolves to the user's account
        default_model in every slot; the aliases equal ``model``."""
        r = eff({}, "acct-model", "sys-model")
        assert _all_equal(r, {"model": "acct-model", "source": "account_default"})

    def test_no_account_default_falls_to_system(self):
        r = eff({}, None, "sys-model")
        assert _all_equal(r, {"model": "sys-model", "source": "system_default"})

    def test_expert_top_level_pin_wins_everywhere(self):
        r = eff({"model": "exp-model"}, "acct", "sys")
        assert _all_equal(r, {"model": "exp-model", "source": "expert"})

    def test_legacy_strategic_pin_surfaces_as_model(self):
        """A stored pre-U1 ``llm.strategic`` pin is the expert's one model now:
        it surfaces as ``model`` (and so in every slot), not as a phase slot."""
        r = eff({"strategic": {"model": "strat"}}, "acct", "sys")
        assert _all_equal(r, {"model": "strat", "source": "expert"})

    def test_legacy_tactical_pin_lifts_when_no_strategic(self):
        r = eff({"tactical": {"model": "tac"}}, "acct", "sys")
        assert _all_equal(r, {"model": "tac", "source": "expert"})

    def test_legacy_strategic_beats_legacy_tactical(self):
        r = eff({"strategic": {"model": "s"}, "tactical": {"model": "t"}}, None, "sys")
        assert _all_equal(r, {"model": "s", "source": "expert"})

    def test_explicit_model_beats_a_legacy_phase_pin(self):
        """The July incident rule: an explicit ``llm.model`` wins over a phase
        pin in the same fragment (the pin is dropped, never a second slot)."""
        r = eff({"model": "top", "tactical": {"model": "tac"}}, "acct", "sys")
        assert _all_equal(r, {"model": "top", "source": "expert"})

    def test_subagent_slot_reads_the_roster_wide_pin(self):
        """``subagents.llm.model`` is the "subagent model" picker since U1: a
        real pin shows as the ``subagent`` slot; ``model`` is unaffected."""
        r = eff({"model": "top"}, "acct", "sys", {"llm": {"model": "haiku"}})
        assert r["subagent"] == {"model": "haiku", "source": "expert"}
        assert r["model"] == {"model": "top", "source": "expert"}
        assert r["session"] == r["strategic"] == r["tactical"] == r["model"]

    def test_subagent_inherit_is_the_parent_model(self):
        """``inherit`` is not a selection: the children run on the expert's
        model, so the slot reports that (with its provenance)."""
        r = eff({}, "acct", "sys", {"llm": {"model": "inherit"}})
        assert r["subagent"] == {"model": "acct", "source": "account_default"}
        r = eff({"model": "top"}, None, None, {"llm": {"model": "inherit"}})
        assert r["subagent"] == {"model": "top", "source": "expert"}

    def test_subagent_slot_falls_back_to_model_without_a_pin(self):
        for subagents in (None, {}, {"roster": {"x": {"$ref": "critic"}}}, {"llm": {}}):
            r = eff({"model": "top"}, "acct", "sys", subagents)
            assert r["subagent"] == {"model": "top", "source": "expert"}

    def test_legacy_llm_subagent_tier_feeds_the_subagent_slot(self):
        """A stored pre-U1 ``llm.subagent`` reader pin is ``subagents.llm``
        after the compat mapping — the same slot the light runner reads."""
        r = eff({"subagent": {"model": "reader"}}, "acct", None)
        assert r["subagent"] == {"model": "reader", "source": "expert"}
        assert r["model"] == {"model": "acct", "source": "account_default"}

    def test_explicit_subagents_llm_beats_the_legacy_tier(self):
        r = eff({"subagent": {"model": "old"}}, "acct", None, {"llm": {"model": "new"}})
        assert r["subagent"] == {"model": "new", "source": "expert"}

    def test_none_expert_llm_is_safe(self):
        r = eff(None, "acct", "sys")
        assert r["model"] == {"model": "acct", "source": "account_default"}
        assert r["session"] == r["model"]

    def test_no_defaults_at_all_yields_null_model(self):
        """No expert pin, no account default, no registry default → model is
        None and the UI shows a bare 'Default'."""
        r = eff({}, None, None)
        assert _all_equal(r, {"model": None, "source": "system_default"})

    def test_inputs_are_not_mutated(self):
        llm = {"strategic": {"model": "strat"}, "subagent": {"model": "reader"}}
        subagents = {"llm": {"model": "inherit"}}
        before = (copy.deepcopy(llm), copy.deepcopy(subagents))
        eff(llm, "acct", "sys", subagents)
        assert (llm, subagents) == before
