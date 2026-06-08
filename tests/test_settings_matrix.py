"""Tests for settings matrix: model-family-specific inference defaults."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import src.core.loader as loader
from src.core.loader import (
    LLMConfig,
    PhaseLLMOverride,
    _apply_settings_matrix,
    _load_settings_matrix,
    _parse_llm_config,
    _parse_phase_override,
    detect_reasoning_method,
    load_uploaded_config,
)
from src.core.model_registry import family_of

# Limit-leaf fractions mirror the *_FRACTION constants in src/core/loader.py.
# The four derived leaves are int(base * fraction); model_max_context_tokens is
# the base itself. Tests assert this relationship rather than hand-pinned magics.
FRACTIONS = {
    "context_threshold_tokens": 0.80,
    "summarization_safe_limit": 0.90,
    "summarization_chunk_size": 0.60,
    "message_count_min_tokens": 0.40,
}


def derived(base: int) -> dict:
    """Expected limits block for a given working-window base."""
    out = {k: int(base * f) for k, f in FRACTIONS.items()}
    out["model_max_context_tokens"] = int(base)
    return out


@pytest.fixture(autouse=True)
def clear_settings_matrix_cache():
    """Reset the unified model_config_matrix cache between tests."""
    loader._model_config_matrix_cache.clear()
    yield
    loader._model_config_matrix_cache.clear()


# =============================================================================
# family_of — minimax (registry-backed replacement for detect_model_family)
# =============================================================================


class TestFamilyOfMinimax:
    def test_openrouter_minimax_is_registered(self):
        # `family_of` recognises the openrouter/.../minimax pattern via
        # its sync prefix heuristic — no DB round-trip required.
        assert family_of("openrouter/minimax/minimax-m2.7") == "minimax"

    def test_bare_minimax_via_heuristic(self):
        # family_of's heuristic fallback pattern-matches bare IDs the
        # registry doesn't know, preserving backward compatibility for
        # custom endpoints that don't set an explicit family.
        assert family_of("minimax-m2.7") == "minimax"

    def test_reasoning_method_is_none_for_minimax(self):
        # detect_reasoning_method still flows through family_of internally,
        # so it picks up the registry's family for built-in minimax entries.
        assert detect_reasoning_method("openrouter/minimax/minimax-m2.7") == "none"


# =============================================================================
# _load_settings_matrix
# =============================================================================


class TestLoadSettingsMatrix:
    def test_loads_real_file(self):
        """The real config/model_config_matrix.yaml should expose the
        ``settings`` subsection in the legacy family→params shape."""
        matrix = _load_settings_matrix()
        assert isinstance(matrix, dict)
        assert "default" in matrix
        assert "minimax" in matrix
        assert matrix["minimax"]["temperature"] == 1.0
        # Verify limits sub-dict exists
        assert "limits" in matrix["minimax"]
        assert isinstance(matrix["minimax"]["limits"], dict)
        # Verify default entry has limits — only the working-window base
        # survives in the matrix; the derived leaves are computed at load.
        assert "limits" in matrix["default"]
        assert matrix["default"]["limits"]["model_max_context_tokens"] == 100000
        assert "context_threshold_tokens" not in matrix["default"]["limits"]

    def test_caches_result(self):
        """The unified file is parsed once per path; the projection (the
        ``settings`` subsection extract) re-runs cheaply on each call."""
        first = _load_settings_matrix()
        second = _load_settings_matrix()
        # Same payloads (the projection produces equal dicts), and the parsed
        # cache is shared — so the underlying parse only ran once.
        assert first == second
        base_path = loader.get_project_root() / "config" / "model_config_matrix.yaml"
        assert base_path in loader._model_config_matrix_cache

    def test_missing_file_returns_empty(self):
        """If the unified matrix file is absent, return empty dict."""
        with patch.object(Path, "exists", return_value=False):
            loader._model_config_matrix_cache.clear()
            matrix = _load_settings_matrix()
            assert matrix == {}

    def test_invalid_yaml_returns_empty(self):
        """If the YAML is not a dict, the projection is empty."""
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: None
            mock_open.return_value.read = lambda: "just a string"
            loader._model_config_matrix_cache.clear()
            matrix = _load_settings_matrix()
            assert isinstance(matrix, dict)


# =============================================================================
# _apply_settings_matrix
# =============================================================================


class TestApplySettingsMatrix:
    def test_fills_gaps(self):
        """Settings matrix values fill in keys not set by expert."""
        data = {"llm": {"model": "minimax-m2.7", "temperature": 0.0}}
        _apply_settings_matrix(data, expert_llm_keys={"model"})
        # temperature was NOT in expert_llm_keys, so settings matrix overrides it
        assert data["llm"]["temperature"] == 1.0
        assert data["llm"]["top_p"] == 0.95
        assert "top_k" not in data["llm"]  # M2.7 does not support top_k

    def test_expert_wins(self):
        """Expert-set keys are NOT overridden by settings matrix."""
        data = {"llm": {"model": "minimax-m2.7", "temperature": 0.5}}
        _apply_settings_matrix(data, expert_llm_keys={"model", "temperature"})
        # temperature was in expert_llm_keys, so it stays at 0.5
        assert data["llm"]["temperature"] == 0.5
        # top_p was not in expert, so it gets filled
        assert data["llm"]["top_p"] == 0.95

    def test_unknown_family_gets_defaults(self):
        """Unknown model family gets the 'default' entry values."""
        data = {"llm": {"model": "some-unknown-model", "temperature": 0.0}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        # default entry sets model_max_context_tokens
        assert data["llm"]["model_max_context_tokens"] == 128000
        # default limits applied
        assert data["limits"]["context_threshold_tokens"] == 80000
        assert data["limits"]["model_max_context_tokens"] == 100000

    def test_missing_llm_key(self):
        """No crash if data has no 'llm' key."""
        data = {"agent_id": "test"}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert "llm" not in data or data.get("llm", {}).get("model") is None

    def test_returns_data(self):
        """Returns the mutated data dict."""
        data = {"llm": {"model": "minimax-m2.7"}}
        result = _apply_settings_matrix(data, expert_llm_keys=set())
        assert result is data


# =============================================================================
# _apply_settings_matrix — limits sub-dict
# =============================================================================


class TestApplySettingsMatrixLimits:
    def test_limits_applied(self):
        """Settings matrix limits values are applied from default+family merge."""
        data = {
            "llm": {"model": "minimax-m2.7"},
            "limits": {"message_count_threshold": 300},
        }
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Settings matrix overrides: family values override default
        assert data["limits"]["model_max_context_tokens"] == 170000
        assert data["limits"]["context_threshold_tokens"] == 136000

    def test_limits_key_not_leaked_to_llm(self):
        """The 'limits' sub-dict must NOT be set on data['llm']."""
        data = {"llm": {"model": "minimax-m2.7"}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert "limits" not in data["llm"]

    def test_no_limits_in_matrix_uses_default(self):
        """Family without limits sub-dict: default limits applied."""
        # Pre-populate the unified cache with a synthetic base file so we
        # don't depend on the real config/model_config_matrix.yaml content.
        base_path = loader.get_project_root() / "config" / "model_config_matrix.yaml"
        loader._model_config_matrix_cache[base_path] = {
            "default": {
                "settings": {
                    "model_max_context_tokens": 128000,
                    "limits": {"context_threshold_tokens": 80000},
                },
            },
            "minimax": {"settings": {"temperature": 1.0}},
        }
        data = {
            "llm": {"model": "minimax-m2.7"},
            "limits": {"message_count_threshold": 300},
        }
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Falls back to default limits since minimax has no limits
        assert data["limits"]["context_threshold_tokens"] == 80000

    def test_creates_limits_dict_if_missing(self):
        """If data has no 'limits' key, it gets created via setdefault."""
        data = {"llm": {"model": "minimax-m2.7"}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert "limits" in data
        assert data["limits"]["context_threshold_tokens"] == 136000

    def test_default_entry_used_for_unknown_family(self):
        """Unknown model gets default entry limits."""
        data = {"llm": {"model": "some-unknown-model"}, "limits": {}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert data["limits"]["context_threshold_tokens"] == 80000
        assert data["limits"]["model_max_context_tokens"] == 100000
        assert data["limits"]["summarization_safe_limit"] == 90000

    def test_family_overrides_default(self):
        """Family-specific values override default entry."""
        data = {"llm": {"model": "minimax-m2.7"}, "limits": {}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        # minimax entry overrides default
        assert data["limits"]["context_threshold_tokens"] == 136000
        assert data["limits"]["model_max_context_tokens"] == 170000

    def test_matrix_is_sole_source_for_limits(self):
        """Matrix limits always win — no expert_limits_keys check."""
        data = {
            "llm": {"model": "minimax-m2.7"},
            "limits": {"context_threshold_tokens": 50000},
        }
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Matrix overwrites even pre-existing limits values
        assert data["limits"]["context_threshold_tokens"] == 136000


# =============================================================================
# Per-expert settings matrix
# =============================================================================


class TestPerExpertMatrix:
    def test_expert_matrix_merges_over_base(self, tmp_path):
        """Expert's model_config_matrix.yaml overrides base matrix values.

        The working window is set via limits.model_max_context_tokens (the
        base); the threshold/safe/chunk/msg-min leaves derive from it, so an
        expert resizes the window by overriding the base, not the leaves.
        """
        expert_matrix = {
            "minimax": {
                "settings": {
                    "temperature": 0.7,
                    "limits": {
                        "model_max_context_tokens": 150000,
                    },
                },
            },
        }
        expert_matrix_path = tmp_path / "model_config_matrix.yaml"
        with open(expert_matrix_path, "w") as f:
            yaml.dump(expert_matrix, f)

        data = {"llm": {"model": "minimax-m2.7"}, "limits": {}}
        _apply_settings_matrix(
            data, expert_llm_keys=set(), deployment_dir=str(tmp_path)
        )

        # Expert matrix overrides temperature
        assert data["llm"]["temperature"] == 0.7
        # Expert overrides the working base; the leaves derive from it.
        assert data["limits"] == derived(150000)

    def test_no_expert_matrix_uses_base(self, tmp_path):
        """Missing expert model_config_matrix.yaml falls back to base."""
        # tmp_path exists but has no model_config_matrix.yaml
        data = {"llm": {"model": "minimax-m2.7"}, "limits": {}}
        _apply_settings_matrix(
            data, expert_llm_keys=set(), deployment_dir=str(tmp_path)
        )

        # Values come from base matrix
        assert data["llm"]["temperature"] == 1.0
        assert data["limits"]["context_threshold_tokens"] == 136000


# =============================================================================
# top_p / top_k on dataclasses
# =============================================================================


class TestTopPTopKDataclasses:
    def test_llm_config_defaults_to_none(self):
        config = LLMConfig()
        assert config.top_p is None
        assert config.top_k is None

    def test_llm_config_stores_values(self):
        config = LLMConfig(top_p=0.95, top_k=40)
        assert config.top_p == 0.95
        assert config.top_k == 40

    def test_phase_override_defaults_to_none(self):
        override = PhaseLLMOverride()
        assert override.top_p is None
        assert override.top_k is None

    def test_phase_override_stores_values(self):
        override = PhaseLLMOverride(top_p=0.8, top_k=20)
        assert override.top_p == 0.8
        assert override.top_k == 20

    def test_get_phase_config_merges(self):
        """Phase override merges top_p/top_k correctly."""
        base = LLMConfig(top_p=0.95, top_k=40)
        base.strategic = PhaseLLMOverride(top_p=0.8)
        resolved = base.get_phase_config("strategic")
        assert resolved.top_p == 0.8  # overridden
        assert resolved.top_k == 40  # inherited from base

    def test_get_phase_config_inherits_when_none(self):
        """Phase override with None inherits base values."""
        base = LLMConfig(top_p=0.95, top_k=40)
        base.tactical = PhaseLLMOverride(temperature=0.5)
        resolved = base.get_phase_config("tactical")
        assert resolved.top_p == 0.95
        assert resolved.top_k == 40
        assert resolved.temperature == 0.5

    def test_parse_llm_config_with_top_p_top_k(self):
        data = {"model": "test", "top_p": 0.9, "top_k": 50}
        config = _parse_llm_config(data)
        assert config.top_p == 0.9
        assert config.top_k == 50

    def test_parse_llm_config_without_top_p_top_k(self):
        data = {"model": "test"}
        config = _parse_llm_config(data)
        assert config.top_p is None
        assert config.top_k is None

    def test_parse_phase_override_with_top_p_top_k(self):
        data = {"top_p": 0.8, "top_k": 30}
        override = _parse_phase_override(data)
        assert override.top_p == 0.8
        assert override.top_k == 30

    def test_parse_phase_override_without_top_p_top_k(self):
        data = {"temperature": 0.5}
        override = _parse_phase_override(data)
        assert override.top_p is None
        assert override.top_k is None


# =============================================================================
# Integration: load_agent_config with settings matrix
# =============================================================================


class TestSettingsMatrixIntegration:
    def test_settings_matrix_applied_on_load(self, tmp_path):
        """Full pipeline: load_agent_config applies settings matrix."""
        # Create a minimal expert config that uses minimax but doesn't set temperature
        expert = {
            "$extends": "defaults",
            "agent_id": "test_agent",
            "display_name": "Test Agent",
            "llm": {
                "model": "openai/minimax-m2.7",
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        config = loader.load_agent_config(str(config_file))

        # Settings matrix should have overridden defaults.yaml temperature
        assert config.llm.temperature == 1.0
        assert config.llm.top_p == 0.95
        assert config.llm.top_k is None  # M2.7 does not support top_k

    def test_expert_override_wins_over_matrix(self, tmp_path):
        """Expert config values take priority over settings matrix."""
        expert = {
            "$extends": "defaults",
            "agent_id": "test_agent",
            "display_name": "Test Agent",
            "llm": {
                "model": "openai/minimax-m2.7",
                "temperature": 0.5,  # Expert explicitly sets temperature
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        config = loader.load_agent_config(str(config_file))

        # Expert temperature wins over settings matrix
        assert config.llm.temperature == 0.5
        # top_p still comes from settings matrix
        assert config.llm.top_p == 0.95
        assert config.llm.top_k is None  # M2.7 does not support top_k

    def test_non_minimax_model_gets_default_limits(self, tmp_path):
        """Non-minimax model without specific entry: gets default limits."""
        expert = {
            "$extends": "defaults",
            "agent_id": "test_agent",
            "display_name": "Test Agent",
            "llm": {
                "model": "mistral-large",
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        config = loader.load_agent_config(str(config_file))

        # defaults.yaml temperature preserved (default entry has no temperature)
        assert config.llm.temperature == 0.0
        # But limits come from default entry
        assert config.limits.context_threshold_tokens == 80000
        assert config.limits.model_max_context_tokens == 100000

    def test_settings_matrix_applies_limits(self, tmp_path):
        """Full pipeline: load_agent_config applies limits from settings matrix."""
        expert = {
            "$extends": "defaults",
            "agent_id": "test_agent",
            "display_name": "Test Agent",
            "llm": {
                "model": "openai/minimax-m2.7",
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        config = loader.load_agent_config(str(config_file))

        # Derived from the minimax working base (170000) via the limit fractions.
        assert config.limits.model_max_context_tokens == 170000
        assert config.limits.context_threshold_tokens == 136000  # 170000 * 0.80
        assert config.limits.summarization_safe_limit == 153000  # 170000 * 0.90
        assert config.limits.summarization_chunk_size == 102000  # 170000 * 0.60
        assert config.limits.message_count_min_tokens == 68000  # 170000 * 0.40

    def test_unknown_model_gets_default_limits(self, tmp_path):
        """Unknown model family gets default entry limits."""
        expert = {
            "$extends": "defaults",
            "agent_id": "test_agent",
            "display_name": "Test Agent",
            "llm": {
                "model": "totally-unknown-model",
            },
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        config = loader.load_agent_config(str(config_file))

        assert config.limits.model_max_context_tokens == 100000
        assert config.limits.context_threshold_tokens == 80000  # 100000 * 0.80
        assert config.limits.summarization_safe_limit == 90000  # 100000 * 0.90
        assert config.limits.summarization_chunk_size == 60000  # 100000 * 0.60
        assert config.limits.message_count_min_tokens == 40000  # 100000 * 0.40

    def test_load_agent_config_with_deployment_dir_matrix(self, tmp_path):
        """Expert directory with model_config_matrix.yaml flows through load_agent_config."""
        # Create expert directory with config and unified matrix
        expert_dir = tmp_path / "my_expert"
        expert_dir.mkdir()

        expert = {
            "$extends": "defaults",
            "agent_id": "test_agent",
            "display_name": "Test Agent",
            "llm": {"model": "openai/minimax-m2.7"},
        }
        config_file = expert_dir / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        expert_matrix = {
            "minimax": {
                "settings": {
                    "limits": {
                        "model_max_context_tokens": 150000,
                    },
                },
            },
        }
        with open(expert_dir / "model_config_matrix.yaml", "w") as f:
            yaml.dump(expert_matrix, f)

        config = loader.load_agent_config(
            str(config_file), deployment_dir=str(expert_dir)
        )

        # Expert resizes the working base; the leaves derive from it.
        assert config.limits.model_max_context_tokens == 150000
        assert config.limits.context_threshold_tokens == 120000  # 150000 * 0.80


# =============================================================================
# _load_model_config_matrix_file — unit tests
# =============================================================================


class TestLoadMatrixFile:
    def test_nonexistent_path(self, tmp_path):
        """Nonexistent file returns empty dict."""
        loader._model_config_matrix_cache.clear()
        result = loader._load_model_config_matrix_file(tmp_path / "nope.yaml")
        assert result == {}

    def test_valid_yaml(self, tmp_path):
        """Valid unified YAML is parsed and structured by family."""
        matrix = {
            "minimax": {"settings": {"temperature": 1.0}},
            "deepseek": {"settings": {"top_p": 0.95}},
        }
        path = tmp_path / "matrix.yaml"
        with open(path, "w") as f:
            yaml.dump(matrix, f)
        loader._model_config_matrix_cache.clear()
        result = loader._load_model_config_matrix_file(path)
        assert result == matrix

    def test_non_dict_yaml_returns_empty(self, tmp_path):
        """YAML that parses to a list/string returns empty dict."""
        path = tmp_path / "matrix.yaml"
        path.write_text("- item1\n- item2\n")
        loader._model_config_matrix_cache.clear()
        result = loader._load_model_config_matrix_file(path)
        assert result == {}

    def test_skips_non_dict_entries(self, tmp_path):
        """Entries whose values aren't dicts are skipped."""
        path = tmp_path / "matrix.yaml"
        path.write_text(
            "minimax:\n  settings:\n    temperature: 1.0\nbad_entry: just_a_string\n"
        )
        loader._model_config_matrix_cache.clear()
        result = loader._load_model_config_matrix_file(path)
        assert "minimax" in result
        assert "bad_entry" not in result

    def test_empty_file_returns_empty(self, tmp_path):
        """Empty YAML file returns empty dict."""
        path = tmp_path / "matrix.yaml"
        path.write_text("")
        loader._model_config_matrix_cache.clear()
        result = loader._load_model_config_matrix_file(path)
        assert result == {}

    def test_io_error_returns_empty(self, tmp_path):
        """IO errors during read return empty dict."""
        path = tmp_path / "matrix.yaml"
        path.write_text("good: {settings: {key: value}}")
        loader._model_config_matrix_cache.clear()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = loader._load_model_config_matrix_file(path)
        assert result == {}

    def test_unknown_section_dropped_with_warning(self, tmp_path):
        """Sections that aren't prompts/instructions/settings are ignored."""
        matrix = {
            "minimax": {
                "settings": {"temperature": 1.0},
                "bogus": {"foo": "bar"},
            },
        }
        path = tmp_path / "matrix.yaml"
        with open(path, "w") as f:
            yaml.dump(matrix, f)
        loader._model_config_matrix_cache.clear()
        result = loader._load_model_config_matrix_file(path)
        assert "settings" in result["minimax"]
        assert "bogus" not in result["minimax"]


# =============================================================================
# _load_settings_matrix — caching and expert merge
# =============================================================================


class TestLoadSettingsMatrixCaching:
    def test_base_cache_not_reloaded_with_deployment_dir(self, tmp_path):
        """Base cache is populated once and reused even when deployment_dir
        is given on subsequent calls."""
        loader._model_config_matrix_cache.clear()
        # First call loads base
        _load_settings_matrix()
        base_path = loader.get_project_root() / "config" / "model_config_matrix.yaml"
        first_cache = loader._model_config_matrix_cache.get(base_path)
        assert first_cache is not None

        # Second call with deployment_dir reuses same base cache (the cache
        # entry is keyed by Path and identity-stable across calls).
        _load_settings_matrix(deployment_dir=str(tmp_path))
        assert loader._model_config_matrix_cache.get(base_path) is first_cache

    def test_expert_empty_file_returns_base(self, tmp_path):
        """Expert matrix file that parses to empty returns base unchanged."""
        path = tmp_path / "model_config_matrix.yaml"
        path.write_text("")  # empty -> projection is empty -> base wins
        result = _load_settings_matrix(deployment_dir=str(tmp_path))
        base = _load_settings_matrix()
        assert result == base

    def test_expert_adds_new_family(self, tmp_path):
        """Expert matrix can add a family not in base."""
        expert_matrix = {"custom_model": {"settings": {"temperature": 0.42}}}
        path = tmp_path / "model_config_matrix.yaml"
        with open(path, "w") as f:
            yaml.dump(expert_matrix, f)

        result = _load_settings_matrix(deployment_dir=str(tmp_path))
        # Base families still present
        assert "minimax" in result
        assert "default" in result
        # Expert's new family added
        assert "custom_model" in result
        assert result["custom_model"]["temperature"] == 0.42

    def test_no_deployment_dir_returns_base(self):
        """None deployment_dir returns the base settings projection."""
        result = _load_settings_matrix(deployment_dir=None)
        base = _load_settings_matrix()
        assert result == base


# =============================================================================
# _apply_settings_matrix — edge cases
# =============================================================================


class TestApplySettingsMatrixEdgeCases:
    def test_empty_matrix_no_default(self):
        """Empty matrix (no 'default' key) applies nothing."""
        base_path = loader.get_project_root() / "config" / "model_config_matrix.yaml"
        loader._model_config_matrix_cache[base_path] = {}
        data = {"llm": {"model": "some-model", "temperature": 0.5}, "limits": {}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Nothing changed
        assert data["llm"]["temperature"] == 0.5
        assert data["limits"] == {}

    def test_family_is_default_skips_family_merge(self):
        """When detect_model_family returns 'default', no double-apply of default entry."""
        base_path = loader.get_project_root() / "config" / "model_config_matrix.yaml"
        loader._model_config_matrix_cache[base_path] = {
            "default": {
                "settings": {
                    "model_max_context_tokens": 128000,
                    "limits": {"context_threshold_tokens": 80000},
                },
            },
        }
        data = {"llm": {"model": "some-unknown-model"}, "limits": {}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Default entry applied exactly once
        assert data["llm"]["model_max_context_tokens"] == 128000
        assert data["limits"]["context_threshold_tokens"] == 80000

    def test_preserves_non_model_dependent_limits(self):
        """Behavioral limits (message_count_threshold, tool_retry_count) not overwritten."""
        data = {
            "llm": {"model": "minimax-m2.7"},
            "limits": {
                "message_count_threshold": 300,
                "tool_retry_count": 5,
            },
        }
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Matrix doesn't have these keys, so they stay
        assert data["limits"]["message_count_threshold"] == 300
        assert data["limits"]["tool_retry_count"] == 5
        # But matrix limits are also applied
        assert data["limits"]["context_threshold_tokens"] == 136000

    def test_all_real_families_have_base_only(self):
        """Every family carries limits.model_max_context_tokens (the working
        base) and must NOT hand-author the derived leaves (they're computed)."""
        matrix = _load_settings_matrix()
        derived_leaves = {
            "context_threshold_tokens",
            "summarization_safe_limit",
            "summarization_chunk_size",
            "message_count_min_tokens",
        }
        for family, settings in matrix.items():
            assert "limits" in settings, f"Family '{family}' missing limits"
            limits = settings["limits"]
            assert "model_max_context_tokens" in limits, (
                f"Family '{family}' missing limits.model_max_context_tokens (base)"
            )
            leaked = derived_leaves & set(limits)
            assert not leaked, (
                f"Family '{family}' hand-authors derived leaves: {leaked}"
            )


# =============================================================================
# Per-expert settings matrix — additional scenarios
# =============================================================================


class TestPerExpertMatrixExtended:
    def test_expert_overrides_default_entry(self, tmp_path):
        """Expert matrix can override values in the 'default' entry."""
        expert_matrix = {
            "default": {
                "settings": {
                    "model_max_context_tokens": 256000,  # nominal / API ceiling
                    "limits": {"model_max_context_tokens": 200000},  # working base
                },
            },
        }
        path = tmp_path / "model_config_matrix.yaml"
        with open(path, "w") as f:
            yaml.dump(expert_matrix, f)

        data = {"llm": {"model": "some-unknown-model"}, "limits": {}}
        _apply_settings_matrix(
            data, expert_llm_keys=set(), deployment_dir=str(tmp_path)
        )

        # Nominal (top-level) override flows to llm; leaves derive from the base.
        assert data["llm"]["model_max_context_tokens"] == 256000
        assert data["limits"] == derived(200000)

    def test_expert_adds_new_family_with_limits(self, tmp_path):
        """Expert can define a new family that the base doesn't have."""
        expert_matrix = {
            "my_custom_model": {
                "settings": {
                    "temperature": 0.3,
                    "model_max_context_tokens": 50000,
                    "limits": {
                        "context_threshold_tokens": 30000,
                        "model_max_context_tokens": 40000,
                        "summarization_safe_limit": 35000,
                        "summarization_chunk_size": 25000,
                        "message_count_min_tokens": 20000,
                    },
                },
            },
        }
        path = tmp_path / "model_config_matrix.yaml"
        with open(path, "w") as f:
            yaml.dump(expert_matrix, f)

        # Override the base cache entry to a synthetic minimal default so
        # this test is independent of the real base file's defaults.
        base_path = loader.get_project_root() / "config" / "model_config_matrix.yaml"
        loader._model_config_matrix_cache[base_path] = {
            "default": {
                "settings": {"limits": {"context_threshold_tokens": 80000}},
            },
        }
        data = {"llm": {"model": "my_custom_model-v1"}, "limits": {}}
        # Since detect_model_family won't know "my_custom_model", it returns "default"
        # So this tests that the default entry is used (expert's new family ignored
        # unless detect_model_family returns it)
        _apply_settings_matrix(
            data, expert_llm_keys=set(), deployment_dir=str(tmp_path)
        )
        assert data["limits"]["context_threshold_tokens"] == 80000

    def test_expert_override_of_derived_leaf_is_ignored(self, tmp_path):
        """A derived leaf set in an expert matrix is clobbered by the derivation
        (the base, not the leaf, is the knob). Overriding the base is the way."""
        expert_matrix = {
            "deepseek": {
                "settings": {
                    "limits": {
                        # The hand-set leaf is ignored; the base wins and derives.
                        "context_threshold_tokens": 42000,
                        "model_max_context_tokens": 160000,
                    },
                },
            },
        }
        path = tmp_path / "model_config_matrix.yaml"
        with open(path, "w") as f:
            yaml.dump(expert_matrix, f)

        data = {"llm": {"model": "deepseek-v4-pro"}, "limits": {}}
        _apply_settings_matrix(
            data, expert_llm_keys=set(), deployment_dir=str(tmp_path)
        )

        # The 42000 leaf is discarded; everything derives from base 160000.
        assert data["limits"] == derived(160000)
        assert data["limits"]["context_threshold_tokens"] == 128000  # 160000 * 0.80


# =============================================================================
# All model families — verify real matrix values
# =============================================================================


class TestRealMatrixFamilies:
    """Verify each family's limits from the real settings_matrix.yaml file."""

    @pytest.mark.parametrize(
        "model,base",
        [
            ("minimax-m2.7", 170000),  # M2.7: 204K nominal, 170K working base
            ("o3-mini", 170000),
            ("deepseek-v4-pro", 200000),  # V4 Pro: 1M nominal, 200K working base
            ("deepseek-v4-flash", 200000),  # shares the deepseek family
            ("gemini-2.0-flash", 200000),
            ("gpt-oss-120b", 110000),
            ("some-unknown-model", 100000),  # default entry
        ],
    )
    def test_family_limits(self, model, base):
        data = {"llm": {"model": model}, "limits": {}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Every leaf derives from the family's working base.
        assert data["limits"] == derived(base)

    @pytest.mark.parametrize(
        "model,expected_temp",
        [
            ("minimax-m2.7", 1.0),
            ("o3-mini", 1.0),
        ],
    )
    def test_family_temperature(self, model, expected_temp):
        data = {"llm": {"model": model, "temperature": 0.0}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert data["llm"]["temperature"] == expected_temp

    def test_deepseek_top_p(self):
        data = {"llm": {"model": "deepseek-v4-pro"}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert data["llm"]["top_p"] == 0.95

    def test_gemini_top_k(self):
        data = {"llm": {"model": "gemini-2.0-flash"}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        assert data["llm"]["top_k"] == 40
        assert data["llm"]["top_p"] == 0.95


# =============================================================================
# load_uploaded_config integration
# =============================================================================


class TestUploadedConfigMatrix:
    def test_uploaded_config_gets_matrix_limits(self, tmp_path):
        """load_uploaded_config applies settings matrix limits."""
        uploaded = {
            "agent_id": "uploaded_agent",
            "display_name": "Uploaded Agent",
            "llm": {"model": "deepseek-v4-pro"},
        }
        config_file = tmp_path / "uploaded.yaml"
        with open(config_file, "w") as f:
            yaml.dump(uploaded, f)

        merged = load_uploaded_config(config_file)

        # Deepseek (V4) limits derived from the matrix base (200000).
        assert merged["limits"]["model_max_context_tokens"] == 200000
        assert merged["limits"]["context_threshold_tokens"] == 160000  # 200000 * 0.80

    def test_uploaded_config_llm_keys_respected(self, tmp_path):
        """Uploaded config's explicit llm keys are not overridden by matrix."""
        uploaded = {
            "agent_id": "uploaded_agent",
            "display_name": "Uploaded Agent",
            "llm": {"model": "minimax-m2.7", "temperature": 0.3},
        }
        config_file = tmp_path / "uploaded.yaml"
        with open(config_file, "w") as f:
            yaml.dump(uploaded, f)

        merged = load_uploaded_config(config_file)

        # Uploaded temperature preserved
        assert merged["llm"]["temperature"] == 0.3
        # Matrix top_p applied
        assert merged["llm"]["top_p"] == 0.95

    def test_uploaded_unknown_model_gets_default(self, tmp_path):
        """Uploaded config with unknown model gets default entry."""
        uploaded = {
            "agent_id": "uploaded_agent",
            "display_name": "Uploaded Agent",
            "llm": {"model": "mystery-model-7b"},
        }
        config_file = tmp_path / "uploaded.yaml"
        with open(config_file, "w") as f:
            yaml.dump(uploaded, f)

        merged = load_uploaded_config(config_file)

        assert merged["limits"]["context_threshold_tokens"] == 80000
        assert merged["limits"]["model_max_context_tokens"] == 100000


# =============================================================================
# Context-window base resolution + per-model override (the derivation feature)
# =============================================================================


class TestContextWindowBaseResolution:
    def test_no_override_uses_conservative_family_base(self):
        """Without a per-model override the base is the family's conservative
        limits.model_max_context_tokens — NOT the (much larger) nominal. The
        nominal stays on the llm block as the Layer-1 ceiling."""
        data = {"llm": {"model": "minimax-m3"}}
        _apply_settings_matrix(data, expert_llm_keys=set())
        # Base is the 200k working window, not the 1M nominal — the M3 trap.
        assert data["limits"] == derived(200000)
        assert data["llm"]["model_max_context_tokens"] == 1000000

    def test_explicit_override_becomes_base_and_survives_matrix(self):
        """A per-model window present in expert_llm_keys (the dispatch-injected
        case) becomes the base, survives the matrix re-apply, and re-derives
        every leaf."""
        data = {"llm": {"model": "minimax-m3", "model_max_context_tokens": 32000}}
        _apply_settings_matrix(
            data, expert_llm_keys={"model", "model_max_context_tokens"}
        )
        # Injected window is NOT overwritten by the family nominal.
        assert data["llm"]["model_max_context_tokens"] == 32000
        # Every leaf derives from the 32k working window.
        assert data["limits"] == derived(32000)

    def test_override_above_family_base_is_honored_no_clamp(self):
        """The admin field is the cap: a 512k override beats the family's
        conservative 200k base with no clamp."""
        data = {"llm": {"model": "claude-opus-4-8", "model_max_context_tokens": 512000}}
        _apply_settings_matrix(
            data, expert_llm_keys={"model", "model_max_context_tokens"}
        )
        assert data["limits"] == derived(512000)

    def test_falsy_override_falls_back_to_family_base(self):
        """A falsy override (0) falls back to the family base rather than
        producing zero-sized limits (belt-and-suspenders for the dispatch guard)."""
        data = {"llm": {"model": "minimax-m3", "model_max_context_tokens": 0}}
        _apply_settings_matrix(
            data, expert_llm_keys={"model", "model_max_context_tokens"}
        )
        assert data["limits"] == derived(200000)

    def test_per_model_window_in_config_drives_limits_end_to_end(self, tmp_path):
        """Full parse: a model_max_context_tokens in the config llm block (same
        mechanism dispatch uses via override) drives config.limits."""
        expert = {
            "$extends": "defaults",
            "agent_id": "a",
            "display_name": "A",
            "llm": {"model": "some-self-hosted", "model_max_context_tokens": 32000},
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(expert, f)

        config = loader.load_agent_config(str(config_file))

        assert config.llm.model_max_context_tokens == 32000
        assert config.limits.model_max_context_tokens == 32000
        assert config.limits.context_threshold_tokens == 25600  # 32000 * 0.80
        assert config.limits.summarization_safe_limit == 28800  # 32000 * 0.90
        assert config.limits.summarization_chunk_size == 19200  # 32000 * 0.60
        assert config.limits.message_count_min_tokens == 12800  # 32000 * 0.40
