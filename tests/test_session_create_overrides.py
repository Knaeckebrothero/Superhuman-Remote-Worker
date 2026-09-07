"""The create-time override bridge and its warn-phase ignored-key report.

Pins Defect B of
knowledge-base/knowledge/issues/live_settings_silently_dropped_on_stateless_sessions.md:
the New Session form nests ``reasoning_level`` / ``temperature`` under
``config_override.llm`` while ``create_thread`` bridged only the top-level
request fields, so a create-time reasoning pick was dropped.
"""

from __future__ import annotations

import pytest

from orchestrator.services.session_create_overrides import (
    SessionOverrideError,
    bridge_nested_llm_override,
    ignored_override_paths,
)


def _validate(level):
    if level not in {"low", "medium", "high", "xhigh", "max", "none"}:
        raise ValueError(f"bad level {level!r}")
    return level


class TestBridgeNestedLlmOverride:
    def test_nested_reasoning_and_temperature_are_bridged(self):
        rebuilt = {"llm": {"model": "gpt-5.6-sol"}}
        bridged = bridge_nested_llm_override(
            {
                "llm": {
                    "model": "gpt-5.6-sol",
                    "reasoning_level": "max",
                    "temperature": 0.2,
                }
            },
            rebuilt,
            validate_reasoning_level=_validate,
        )
        assert rebuilt["llm"] == {
            "model": "gpt-5.6-sol",
            "reasoning_level": "max",
            "temperature": 0.2,
        }
        assert bridged == ["llm.temperature", "llm.reasoning_level"]

    def test_top_level_field_wins_over_its_nested_twin(self):
        # The caller already bridged the explicit request field; the nested
        # copy (stale form state, or a conflicting API body) must not clobber it.
        rebuilt = {"llm": {"reasoning_level": "high"}}
        bridge_nested_llm_override(
            {"llm": {"reasoning_level": "low"}},
            rebuilt,
            validate_reasoning_level=_validate,
        )
        assert rebuilt["llm"]["reasoning_level"] == "high"

    def test_nested_model_is_bridged_when_no_top_level_model(self):
        rebuilt: dict = {}
        bridge_nested_llm_override(
            {"llm": {"model": "gpt-6-astra"}},
            rebuilt,
            validate_reasoning_level=_validate,
        )
        assert rebuilt == {"llm": {"model": "gpt-6-astra"}}

    def test_nested_reasoning_runs_the_vocabulary_check(self):
        with pytest.raises(ValueError, match="bad level"):
            bridge_nested_llm_override(
                {"llm": {"reasoning_level": "ultra"}},
                {},
                validate_reasoning_level=_validate,
            )

    @pytest.mark.parametrize("bad", ["0.2", True, [0.2]])
    def test_malformed_temperature_is_refused_not_dropped(self, bad):
        with pytest.raises(SessionOverrideError):
            bridge_nested_llm_override(
                {"llm": {"temperature": bad}}, {}, validate_reasoning_level=_validate
            )

    @pytest.mark.parametrize("bad", ["", "   ", 7])
    def test_malformed_model_is_refused(self, bad):
        with pytest.raises(SessionOverrideError):
            bridge_nested_llm_override(
                {"llm": {"model": bad}}, {}, validate_reasoning_level=_validate
            )

    def test_null_and_absent_values_are_not_bridged(self):
        rebuilt: dict = {}
        assert (
            bridge_nested_llm_override(
                {"llm": {"reasoning_level": None}},
                rebuilt,
                validate_reasoning_level=_validate,
            )
            == []
        )
        assert (
            bridge_nested_llm_override(
                None, rebuilt, validate_reasoning_level=_validate
            )
            == []
        )
        assert (
            bridge_nested_llm_override({}, rebuilt, validate_reasoning_level=_validate)
            == []
        )
        # A touched-but-empty llm section is not left behind.
        assert rebuilt.get("llm", {}) == {}


class TestIgnoredOverridePaths:
    def test_reports_nested_keys_the_rebuild_did_not_carry(self):
        sent = {
            "llm": {"model": "m", "base_url": "http://x"},
            "memory": {"enabled": False},
            "interactive": {
                "permission_mode": "autonomous",
                "narration_mode": "silent",
            },
        }
        rebuilt = {
            "llm": {"model": "m"},
            "interactive": {"permission_mode": "autonomous"},
        }
        assert ignored_override_paths(sent, rebuilt) == [
            "interactive.narration_mode",
            "llm.base_url",
            "memory.enabled",
        ]

    def test_a_key_sent_twice_is_not_reported_when_carried(self):
        # The form lifts `model` top-level AND leaves it nested; the rebuild
        # carries it once, so nothing was lost.
        assert (
            ignored_override_paths({"llm": {"model": "m"}}, {"llm": {"model": "m"}})
            == []
        )

    def test_value_mismatch_counts_as_ignored(self):
        # Carried under the same key but with a different value means the
        # caller's value was not what won — report it.
        assert ignored_override_paths(
            {"llm": {"model": "a"}}, {"llm": {"model": "b"}}
        ) == ["llm.model"]

    def test_non_dict_section_is_reported_whole(self):
        assert ignored_override_paths({"skills": ["x"]}, {}) == ["skills"]
        assert ignored_override_paths({"skills": ["x"]}, {"skills": ["x"]}) == []

    def test_empty_and_none_inputs(self):
        assert ignored_override_paths(None, {"llm": {}}) == []
        assert ignored_override_paths({}, {}) == []
