"""Tests for prompt matrix resolution: detect_model_family + PromptMatrixResolver."""

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.loader import (
    AgentConfig,
    FileResolver,
    InstructionMatrixResolver,
    LLMConfig,
    MatrixResolver,
    PhaseLLMOverride,
    PromptMatrixResolver,
    PromptResolver,
    detect_model_family,
    detect_reasoning_method,
    get_phase_system_prompt,
    load_base_system_prompt,
    load_instructions,
    load_phase_component,
    load_summarization_prompt,
)


# =============================================================================
# detect_model_family tests
# =============================================================================


class TestDetectModelFamily:
    """Tests for detect_model_family() covering all documented patterns."""

    def test_claude_opus(self):
        assert detect_model_family("claude-opus-4-6") == "claude-opus"
        assert detect_model_family("claude-opus-4-5-20250514") == "claude-opus"

    def test_claude_sonnet(self):
        assert detect_model_family("claude-sonnet-4-5-20250929") == "claude-sonnet"
        assert detect_model_family("claude-sonnet-4-20250514") == "claude-sonnet"

    def test_claude_haiku(self):
        assert detect_model_family("claude-haiku-4-5-20251001") == "claude-haiku"

    def test_gpt5(self):
        assert detect_model_family("gpt-5") == "gpt-5"
        assert detect_model_family("gpt-5.2") == "gpt-5"

    def test_gpt4o(self):
        assert detect_model_family("gpt-4o") == "gpt-4o"
        assert detect_model_family("gpt-4o-mini") == "gpt-4o"

    def test_gpt_oss(self):
        assert detect_model_family("gpt-oss-120b") == "gpt-oss"
        assert detect_model_family("openai/gpt-oss-120b") == "gpt-oss"

    def test_o_series(self):
        assert detect_model_family("o1-preview") == "o-series"
        assert detect_model_family("o3-mini") == "o-series"
        assert detect_model_family("o4-mini") == "o-series"

    def test_gemini(self):
        assert detect_model_family("gemini-2.0-flash") == "gemini"
        assert detect_model_family("gemini-pro") == "gemini"

    def test_deepseek(self):
        assert detect_model_family("deepseek-r1") == "deepseek"
        assert detect_model_family("deepseek-chat") == "deepseek"

    def test_qwen(self):
        assert detect_model_family("qwen-72b") == "qwen"
        assert detect_model_family("qwq-32b") == "qwen"

    def test_llama(self):
        assert detect_model_family("llama-3.3-70b") == "llama"
        assert detect_model_family("meta-llama-3.1-8b") == "llama"

    def test_openrouter_prefix_stripped(self):
        assert detect_model_family("openrouter/anthropic/claude-opus-4") == "claude-opus"
        assert detect_model_family("openrouter/deepseek/deepseek-r1") == "deepseek"
        assert detect_model_family("openrouter/meta-llama/llama-3.3-70b-instruct") == "llama"

    def test_groq_prefix_stripped(self):
        assert detect_model_family("groq/llama-3.3-70b") == "llama"
        assert detect_model_family("groq/deepseek/deepseek-r1-distill") == "deepseek"

    def test_minimax(self):
        assert detect_model_family("minimax-m2.5") == "minimax"
        assert detect_model_family("MiniMax-Text-01") == "minimax"
        assert detect_model_family("openrouter/minimax/minimax-01") == "minimax"

    def test_unknown_model_returns_default(self):
        assert detect_model_family("some-unknown-model") == "default"
        assert detect_model_family("mistral-large") == "default"

    def test_case_insensitive(self):
        assert detect_model_family("Claude-Opus-4-6") == "claude-opus"
        assert detect_model_family("GPT-4o") == "gpt-4o"


# =============================================================================
# detect_reasoning_method tests
# =============================================================================


class TestDetectReasoningMethod:
    """Tests for detect_reasoning_method() auto-detection and explicit override."""

    def test_gpt_oss_returns_prompt(self):
        assert detect_reasoning_method("gpt-oss-120b") == "prompt"
        assert detect_reasoning_method("openai/gpt-oss-120b") == "prompt"

    def test_claude_returns_none(self):
        assert detect_reasoning_method("claude-opus-4-6") == "none"
        assert detect_reasoning_method("claude-sonnet-4-20250514") == "none"
        assert detect_reasoning_method("claude-haiku-4-5-20251001") == "none"

    def test_gemini_returns_none(self):
        assert detect_reasoning_method("gemini-2.0-flash") == "none"

    def test_gpt5_returns_api(self):
        assert detect_reasoning_method("gpt-5") == "api"

    def test_gpt4o_returns_api(self):
        assert detect_reasoning_method("gpt-4o") == "api"

    def test_o_series_returns_api(self):
        assert detect_reasoning_method("o3-mini") == "api"

    def test_deepseek_returns_api(self):
        assert detect_reasoning_method("deepseek-r1") == "api"

    def test_unknown_returns_api(self):
        assert detect_reasoning_method("some-unknown-model") == "api"

    def test_explicit_override(self):
        assert detect_reasoning_method("gpt-oss-120b", explicit_method="none") == "none"
        assert detect_reasoning_method("claude-opus-4-6", explicit_method="prompt") == "prompt"
        assert detect_reasoning_method("gpt-4o", explicit_method="api") == "api"


# =============================================================================
# PromptMatrixResolver tests
# =============================================================================


class TestPromptMatrixResolver:
    """Tests for PromptMatrixResolver fallback chain."""

    def test_default_fallback_no_matrix_files(self, tmp_path):
        """Without any matrix files, hardcoded defaults are used."""
        resolver = PromptMatrixResolver(
            deployment_dir=str(tmp_path),
            model_family="claude-opus",
        )
        assert resolver.resolve_filename("systemprompt") == "systemprompt.txt"
        assert resolver.resolve_filename("strategic") == "strategic.txt"
        assert resolver.resolve_filename("tactical") == "tactical.txt"
        assert resolver.resolve_filename("summarization") == "summarization_prompt.txt"
        # Note: "instructions" moved to InstructionMatrixResolver

    def test_base_matrix_default_resolution(self, tmp_path):
        """Base matrix default entries are used when no expert matrix exists."""
        # Create base matrix
        base_matrix = tmp_path / "config" / "prompt_matrix.yaml"
        base_matrix.parent.mkdir(parents=True)
        base_matrix.write_text(textwrap.dedent("""\
            default:
              systemprompt: custom_systemprompt.txt
              strategic: custom_strategic.txt
        """))

        with patch.object(PromptMatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "default"
            resolver._prompt_resolver = PromptResolver(None)
            resolver._expert_matrix = {}
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(base_matrix)

        assert resolver.resolve_filename("systemprompt") == "custom_systemprompt.txt"
        assert resolver.resolve_filename("strategic") == "custom_strategic.txt"
        # Tactical not in matrix, falls to hardcoded default
        assert resolver.resolve_filename("tactical") == "tactical.txt"

    def test_expert_override(self, tmp_path):
        """Expert matrix entries override base matrix entries."""
        # Create base matrix
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(textwrap.dedent("""\
            default:
              systemprompt: base_system.txt
              strategic: base_strategic.txt
        """))

        # Create expert matrix
        expert_matrix_path = tmp_path / "expert_matrix.yaml"
        expert_matrix_path.write_text(textwrap.dedent("""\
            default:
              strategic: expert_strategic.txt
        """))

        with patch.object(PromptMatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = tmp_path
            resolver.model_family = "default"
            resolver._prompt_resolver = PromptResolver(str(tmp_path))
            resolver._expert_matrix = PromptMatrixResolver._load_matrix_from_path(expert_matrix_path)
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(base_matrix_path)

        # Expert overrides strategic
        assert resolver.resolve_filename("strategic") == "expert_strategic.txt"
        # Base provides systemprompt (expert doesn't override it)
        assert resolver.resolve_filename("systemprompt") == "base_system.txt"

    def test_model_specific_entry(self, tmp_path):
        """Model-specific entries take priority over default entries."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(textwrap.dedent("""\
            default:
              systemprompt: systemprompt.txt
              strategic: strategic.txt
            claude-opus:
              systemprompt: systemprompt_claude_opus.txt
        """))

        with patch.object(PromptMatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "claude-opus"
            resolver._prompt_resolver = PromptResolver(None)
            resolver._expert_matrix = {}
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(base_matrix_path)

        # Model-specific entry wins for systemprompt
        assert resolver.resolve_filename("systemprompt") == "systemprompt_claude_opus.txt"
        # Falls back to default for strategic (no model-specific entry)
        assert resolver.resolve_filename("strategic") == "strategic.txt"

    def test_full_chain_4_levels(self, tmp_path):
        """Exercise the full 4-level fallback chain."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(textwrap.dedent("""\
            default:
              systemprompt: base_default_system.txt
              strategic: base_default_strategic.txt
              tactical: base_default_tactical.txt
              summarization: base_default_summarization.txt
            claude-opus:
              tactical: base_claude_tactical.txt
        """))

        expert_matrix_path = tmp_path / "expert_matrix.yaml"
        expert_matrix_path.write_text(textwrap.dedent("""\
            default:
              strategic: expert_default_strategic.txt
            claude-opus:
              systemprompt: expert_claude_system.txt
        """))

        with patch.object(PromptMatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = tmp_path
            resolver.model_family = "claude-opus"
            resolver._prompt_resolver = PromptResolver(str(tmp_path))
            resolver._expert_matrix = PromptMatrixResolver._load_matrix_from_path(expert_matrix_path)
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(base_matrix_path)

        # Level 1: Expert model-specific
        assert resolver.resolve_filename("systemprompt") == "expert_claude_system.txt"
        # Level 2: Expert default
        assert resolver.resolve_filename("strategic") == "expert_default_strategic.txt"
        # Level 3: Base model-specific
        assert resolver.resolve_filename("tactical") == "base_claude_tactical.txt"
        # Level 4: Base default
        assert resolver.resolve_filename("summarization") == "base_default_summarization.txt"

    def test_load_matrix_invalid_yaml(self, tmp_path):
        """Invalid YAML in matrix file returns empty dict gracefully."""
        matrix_path = tmp_path / "prompt_matrix.yaml"
        matrix_path.write_text(":{invalid yaml")

        result = PromptMatrixResolver._load_matrix_from_path(matrix_path)
        assert result == {}

    def test_load_matrix_non_dict(self, tmp_path):
        """Non-dict YAML content returns empty dict."""
        matrix_path = tmp_path / "prompt_matrix.yaml"
        matrix_path.write_text("- just\n- a\n- list\n")

        result = PromptMatrixResolver._load_matrix_from_path(matrix_path)
        assert result == {}

    def test_load_matrix_nonexistent(self, tmp_path):
        """Non-existent matrix file returns empty dict."""
        result = PromptMatrixResolver._load_matrix_from_path(tmp_path / "nope.yaml")
        assert result == {}

    def test_unknown_prompt_type_fallback(self, tmp_path):
        """Unknown prompt types fall through to hardcoded default pattern."""
        resolver = PromptMatrixResolver(
            deployment_dir=str(tmp_path),
            model_family="default",
        )
        # Unknown type gets "{type}.txt" fallback
        assert resolver.resolve_filename("something_new") == "something_new.txt"

    def test_default_model_family_skips_model_levels(self, tmp_path):
        """When model_family is 'default', levels 1 and 3 are skipped."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(textwrap.dedent("""\
            default:
              systemprompt: default_system.txt
            claude-opus:
              systemprompt: claude_system.txt
        """))

        with patch.object(PromptMatrixResolver, "__init__", lambda self, *a, **kw: None):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "default"
            resolver._prompt_resolver = PromptResolver(None)
            resolver._expert_matrix = {}
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(base_matrix_path)

        # Should use default, not claude-opus
        assert resolver.resolve_filename("systemprompt") == "default_system.txt"


# =============================================================================
# Default resolution tests (matrix-only, no legacy fallback)
# =============================================================================


class TestDefaultResolution:
    """Verify that functions work correctly with the matrix-only resolution path."""

    def test_load_base_system_prompt_with_resolver(self, tmp_path):
        """load_base_system_prompt uses PromptMatrixResolver."""
        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text("base template {prompt_content}")

            resolver = PromptMatrixResolver(model_family="default")
            result = load_base_system_prompt(resolver)
            assert "base template" in result

    def test_load_phase_component_with_resolver(self, tmp_path):
        """load_phase_component uses PromptMatrixResolver."""
        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "strategic.txt").write_text("strategic phase {phase_number}")

            resolver = PromptMatrixResolver(model_family="default")
            result = load_phase_component(is_strategic=True, matrix_resolver=resolver)
            assert "strategic phase" in result

    def test_get_phase_system_prompt_no_model(self, tmp_path):
        """Without model param, default model family is used."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test Agent",
        )

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text(
                "{agent_display_name} {prompt_content}"
            )
            (config_prompts / "strategic.txt").write_text("phase {phase_number}")

            result = get_phase_system_prompt(
                config=config,
                is_strategic=True,
                phase_number=1,
            )
            assert "Test Agent" in result
            assert "phase 1" in result

    def test_get_phase_system_prompt_with_model(self, tmp_path):
        """With model param, model family is detected and used."""
        config = AgentConfig(
            agent_id="test",
            display_name="Test Agent",
        )

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text(
                "{agent_display_name} {prompt_content}"
            )
            (config_prompts / "strategic.txt").write_text("phase {phase_number}")

            result = get_phase_system_prompt(
                config=config,
                is_strategic=True,
                phase_number=1,
                model="claude-opus-4-6",
            )
            assert "Test Agent" in result
            assert "phase 1" in result
            # Claude models should not have reasoning directive injected
            assert "Reasoning:" not in result


# =============================================================================
# Integration: PromptMatrixResolver.load() with real files
# =============================================================================


class TestPromptMatrixResolverLoad:
    """Test that load() correctly resolves filenames and reads files."""

    def test_load_from_expert_directory(self, tmp_path):
        """Expert file takes priority in file search."""
        # Create expert directory with custom file
        expert_dir = tmp_path / "expert"
        expert_dir.mkdir()
        (expert_dir / "strategic.txt").write_text("expert strategic content")
        (expert_dir / "prompt_matrix.yaml").write_text(textwrap.dedent("""\
            default:
              strategic: strategic.txt
        """))

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            # Also create base files
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "strategic.txt").write_text("base strategic content")

            resolver = PromptMatrixResolver(
                deployment_dir=str(expert_dir),
                model_family="default",
            )
            result = resolver.load("strategic")
            assert result == "expert strategic content"

    def test_load_falls_through_to_framework(self, tmp_path):
        """When expert dir doesn't have the file, framework dir is used."""
        expert_dir = tmp_path / "expert"
        expert_dir.mkdir()
        # No strategic.txt in expert dir, but matrix references it

        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text("framework systemprompt")
            # No base matrix file either — hardcoded defaults will be used

            resolver = PromptMatrixResolver(
                deployment_dir=str(expert_dir),
                model_family="default",
            )
            result = resolver.load("systemprompt")
            assert result == "framework systemprompt"

    def test_exists_returns_true_for_resolvable(self, tmp_path):
        """exists() returns True when the file can be found."""
        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "systemprompt.txt").write_text("exists")

            resolver = PromptMatrixResolver(model_family="default")
            assert resolver.exists("systemprompt") is True

    def test_exists_returns_false_for_missing(self, tmp_path):
        """exists() returns False when file cannot be found."""
        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)

            resolver = PromptMatrixResolver(model_family="default")
            assert resolver.exists("nonexistent_type") is False


# =============================================================================
# LLMConfig.get_phase_config() — model_max_context_tokens merge tests
# =============================================================================


class TestPhaseConfigContextTokens:
    """Tests for model_max_context_tokens merge in get_phase_config()."""

    def test_base_value_inherited_when_no_override(self):
        """Phase config inherits base model_max_context_tokens when override omits it."""
        config = LLMConfig(model="gpt-4o", model_max_context_tokens=128000)
        phase = config.get_phase_config("strategic")
        assert phase.model_max_context_tokens == 128000

    def test_override_replaces_base(self):
        """Phase override's model_max_context_tokens replaces base value."""
        config = LLMConfig(
            model="gpt-4o",
            model_max_context_tokens=128000,
            tactical=PhaseLLMOverride(model="gpt-4o-mini", model_max_context_tokens=32000),
        )
        phase = config.get_phase_config("tactical")
        assert phase.model_max_context_tokens == 32000
        assert phase.model == "gpt-4o-mini"

    def test_override_none_keeps_base(self):
        """When override has model_max_context_tokens=None, base is kept."""
        config = LLMConfig(
            model="gpt-4o",
            model_max_context_tokens=128000,
            strategic=PhaseLLMOverride(temperature=0.5),
        )
        phase = config.get_phase_config("strategic")
        assert phase.model_max_context_tokens == 128000
        assert phase.temperature == 0.5

    def test_base_none_override_sets(self):
        """When base has no model_max_context_tokens, override can set it."""
        config = LLMConfig(
            model="gpt-4o",
            tactical=PhaseLLMOverride(model_max_context_tokens=32000),
        )
        assert config.model_max_context_tokens is None
        phase = config.get_phase_config("tactical")
        assert phase.model_max_context_tokens == 32000

    def test_no_override_returns_self(self):
        """Phase without override returns self (identity)."""
        config = LLMConfig(model="gpt-4o", model_max_context_tokens=128000)
        phase = config.get_phase_config("tactical")
        assert phase is config

    def test_resolved_config_has_no_phase_fields(self):
        """Resolved phase config has strategic/tactical/summarization=None."""
        config = LLMConfig(
            model="gpt-4o",
            model_max_context_tokens=128000,
            strategic=PhaseLLMOverride(model_max_context_tokens=200000),
        )
        phase = config.get_phase_config("strategic")
        assert phase.strategic is None
        assert phase.tactical is None
        assert phase.summarization is None


# =============================================================================
# LLM reuse — full config equality tests
# =============================================================================


class TestLLMReuseEquality:
    """Tests that LLM reuse compares full config, not just model name."""

    def test_same_model_same_settings_are_equal(self):
        """Identical configs should be equal (enabling reuse)."""
        config = LLMConfig(model="gpt-4o", temperature=0.3, model_max_context_tokens=128000)
        strategic = config.get_phase_config("strategic")
        tactical = config.get_phase_config("tactical")
        assert strategic == tactical

    def test_same_model_different_context_tokens_not_equal(self):
        """Same model but different context tokens should NOT be equal."""
        config = LLMConfig(
            model="gpt-4o",
            model_max_context_tokens=128000,
            tactical=PhaseLLMOverride(model_max_context_tokens=32000),
        )
        strategic = config.get_phase_config("strategic")
        tactical = config.get_phase_config("tactical")
        assert strategic != tactical

    def test_same_model_different_temperature_not_equal(self):
        """Same model but different temperature should NOT be equal."""
        config = LLMConfig(
            model="gpt-4o",
            temperature=0.3,
            tactical=PhaseLLMOverride(temperature=0.0),
        )
        strategic = config.get_phase_config("strategic")
        tactical = config.get_phase_config("tactical")
        assert strategic != tactical

    def test_different_models_not_equal(self):
        """Different models are obviously not equal."""
        config = LLMConfig(
            model="gpt-4o",
            tactical=PhaseLLMOverride(model="gpt-4o-mini"),
        )
        strategic = config.get_phase_config("strategic")
        tactical = config.get_phase_config("tactical")
        assert strategic != tactical
