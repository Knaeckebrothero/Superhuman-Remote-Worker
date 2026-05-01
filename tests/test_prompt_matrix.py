"""Tests for prompt matrix resolution: family_of + PromptMatrixResolver."""

import textwrap
from unittest.mock import patch


from src.core.loader import (
    AgentConfig,
    LLMConfig,
    PhaseLLMOverride,
    PromptMatrixResolver,
    PromptResolver,
    detect_reasoning_method,
    get_phase_system_prompt,
    load_base_system_prompt,
    load_phase_component,
)
from src.core.model_registry import family_of


# =============================================================================
# family_of tests — registry-backed replacement for detect_model_family
# =============================================================================


class TestFamilyOf:
    """Registry reads `family` from models.yaml for every built-in entry."""

    def test_claude_opus(self):
        assert family_of("claude-opus-4-6") == "claude-opus"

    def test_claude_sonnet(self):
        assert family_of("claude-sonnet-4-5-20250929") == "claude-sonnet"

    def test_gpt4o(self):
        assert family_of("gpt-4o") == "default"  # YAML declares family: default
        assert family_of("gpt-4o-mini") == "default"

    def test_groq_gpt_oss(self):
        assert family_of("groq/gpt-oss-120b") == "gpt-oss"

    def test_gemini(self):
        assert family_of("gemini-2.5-flash") == "gemini"
        assert family_of("gemini-2.5-pro") == "gemini"

    def test_openrouter_minimax(self):
        assert family_of("openrouter/minimax/minimax-m2.7") == "minimax"

    def test_codex_gpt5(self):
        assert family_of("codex/gpt-5.3-codex") == "gpt-5"

    def test_unknown_model_returns_default(self):
        # After heuristic fallback kicks in, unrecognized IDs still fall through
        # to 'default'. Use an ID with no known substring match.
        assert family_of("some-unknown-model") == "default"
        assert family_of("mistral-large") == "default"

    def test_unknown_returns_custom_default(self):
        """Callers can override the 'unknown' fallback."""
        assert family_of("unrecognized-model", default="custom") == "custom"

    def test_heuristic_fallback_for_bare_names(self):
        # Bare IDs (no catalog entry) get heuristic detection — needed
        # for custom endpoints that don't set an explicit family.
        assert family_of("deepseek-r1") == "deepseek"
        assert family_of("qwen-72b") == "qwen"
        assert family_of("llama-3.3-70b") == "llama"


# =============================================================================
# detect_reasoning_method tests — uses family_of() internally
# =============================================================================


class TestDetectReasoningMethod:
    """Tests for detect_reasoning_method() auto-detection and explicit override.

    Uses real catalog IDs only: synthetic IDs now return family 'default'
    which maps to reasoning_method='api'.
    """

    def test_groq_gpt_oss_returns_prompt(self):
        assert detect_reasoning_method("groq/gpt-oss-120b") == "prompt"

    def test_claude_returns_none(self):
        assert detect_reasoning_method("claude-opus-4-6") == "none"
        assert detect_reasoning_method("claude-sonnet-4-5-20250929") == "none"

    def test_gemini_returns_none(self):
        assert detect_reasoning_method("gemini-2.5-flash") == "none"

    def test_gpt4o_returns_api(self):
        # gpt-4o has family 'default' in models.yaml → 'api' reasoning method.
        assert detect_reasoning_method("gpt-4o") == "api"

    def test_codex_returns_api(self):
        assert detect_reasoning_method("codex/gpt-5.4") == "api"

    def test_unknown_returns_api(self):
        # Unknown → family 'default' → 'api'.
        assert detect_reasoning_method("some-unknown-model") == "api"

    def test_explicit_override(self):
        assert (
            detect_reasoning_method("groq/gpt-oss-120b", explicit_method="none")
            == "none"
        )
        assert (
            detect_reasoning_method("claude-opus-4-6", explicit_method="prompt")
            == "prompt"
        )
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
        base_matrix = tmp_path / "config" / "model_config_matrix.yaml"
        base_matrix.parent.mkdir(parents=True)
        base_matrix.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                systemprompt: custom_systemprompt.txt
                strategic: custom_strategic.txt
        """)
        )

        with patch.object(
            PromptMatrixResolver, "__init__", lambda self, *a, **kw: None
        ):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "default"
            resolver._prompt_resolver = PromptResolver(None)
            resolver._expert_matrix = {}
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(
                base_matrix
            )

        assert resolver.resolve_filename("systemprompt") == "custom_systemprompt.txt"
        assert resolver.resolve_filename("strategic") == "custom_strategic.txt"
        # Tactical not in matrix, falls to hardcoded default
        assert resolver.resolve_filename("tactical") == "tactical.txt"

    def test_expert_override(self, tmp_path):
        """Expert matrix entries override base matrix entries."""
        # Create base matrix
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                systemprompt: base_system.txt
                strategic: base_strategic.txt
        """)
        )

        # Create expert matrix
        expert_matrix_path = tmp_path / "expert_matrix.yaml"
        expert_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                strategic: expert_strategic.txt
        """)
        )

        with patch.object(
            PromptMatrixResolver, "__init__", lambda self, *a, **kw: None
        ):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = tmp_path
            resolver.model_family = "default"
            resolver._prompt_resolver = PromptResolver(str(tmp_path))
            resolver._expert_matrix = PromptMatrixResolver._load_matrix_from_path(
                expert_matrix_path
            )
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(
                base_matrix_path
            )

        # Expert overrides strategic
        assert resolver.resolve_filename("strategic") == "expert_strategic.txt"
        # Base provides systemprompt (expert doesn't override it)
        assert resolver.resolve_filename("systemprompt") == "base_system.txt"

    def test_model_specific_entry(self, tmp_path):
        """Model-specific entries take priority over default entries."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                systemprompt: systemprompt.txt
                strategic: strategic.txt
            claude-opus:
              prompts:
                systemprompt: systemprompt_claude_opus.txt
        """)
        )

        with patch.object(
            PromptMatrixResolver, "__init__", lambda self, *a, **kw: None
        ):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "claude-opus"
            resolver._prompt_resolver = PromptResolver(None)
            resolver._expert_matrix = {}
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(
                base_matrix_path
            )

        # Model-specific entry wins for systemprompt
        assert (
            resolver.resolve_filename("systemprompt") == "systemprompt_claude_opus.txt"
        )
        # Falls back to default for strategic (no model-specific entry)
        assert resolver.resolve_filename("strategic") == "strategic.txt"

    def test_full_chain_4_levels(self, tmp_path):
        """Exercise the full 4-level fallback chain."""
        base_matrix_path = tmp_path / "base_matrix.yaml"
        base_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                systemprompt: base_default_system.txt
                strategic: base_default_strategic.txt
                tactical: base_default_tactical.txt
                summarization: base_default_summarization.txt
            claude-opus:
              prompts:
                tactical: base_claude_tactical.txt
        """)
        )

        expert_matrix_path = tmp_path / "expert_matrix.yaml"
        expert_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                strategic: expert_default_strategic.txt
            claude-opus:
              prompts:
                systemprompt: expert_claude_system.txt
        """)
        )

        with patch.object(
            PromptMatrixResolver, "__init__", lambda self, *a, **kw: None
        ):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = tmp_path
            resolver.model_family = "claude-opus"
            resolver._prompt_resolver = PromptResolver(str(tmp_path))
            resolver._expert_matrix = PromptMatrixResolver._load_matrix_from_path(
                expert_matrix_path
            )
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(
                base_matrix_path
            )

        # Level 1: Expert model-specific
        assert resolver.resolve_filename("systemprompt") == "expert_claude_system.txt"
        # Level 2: Expert default
        assert resolver.resolve_filename("strategic") == "expert_default_strategic.txt"
        # Level 3: Base model-specific
        assert resolver.resolve_filename("tactical") == "base_claude_tactical.txt"
        # Level 4: Base default
        assert (
            resolver.resolve_filename("summarization")
            == "base_default_summarization.txt"
        )

    def test_load_matrix_invalid_yaml(self, tmp_path):
        """Invalid YAML in matrix file returns empty dict gracefully."""
        matrix_path = tmp_path / "model_config_matrix.yaml"
        matrix_path.write_text(":{invalid yaml")

        # Bypass the per-path cache so the malformed write is actually parsed.
        from src.core import loader as _loader

        _loader._model_config_matrix_cache.pop(matrix_path, None)
        result = PromptMatrixResolver._load_matrix_from_path(matrix_path)
        assert result == {}

    def test_load_matrix_non_dict(self, tmp_path):
        """Non-dict YAML content returns empty dict."""
        matrix_path = tmp_path / "model_config_matrix.yaml"
        matrix_path.write_text("- just\n- a\n- list\n")

        from src.core import loader as _loader

        _loader._model_config_matrix_cache.pop(matrix_path, None)
        result = PromptMatrixResolver._load_matrix_from_path(matrix_path)
        assert result == {}

    def test_load_matrix_nonexistent(self, tmp_path):
        """Non-existent matrix file returns empty dict."""
        from src.core import loader as _loader

        _loader._model_config_matrix_cache.pop(tmp_path / "nope.yaml", None)
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
        base_matrix_path.write_text(
            textwrap.dedent("""\
            default:
              prompts:
                systemprompt: default_system.txt
            claude-opus:
              prompts:
                systemprompt: claude_system.txt
        """)
        )

        with patch.object(
            PromptMatrixResolver, "__init__", lambda self, *a, **kw: None
        ):
            resolver = PromptMatrixResolver.__new__(PromptMatrixResolver)
            resolver.deployment_dir = None
            resolver.model_family = "default"
            resolver._prompt_resolver = PromptResolver(None)
            resolver._expert_matrix = {}
            resolver._base_matrix = PromptMatrixResolver._load_matrix_from_path(
                base_matrix_path
            )

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
            (config_prompts / "systemprompt.txt").write_text(
                "base template {prompt_content}"
            )

            resolver = PromptMatrixResolver(model_family="default")
            result = load_base_system_prompt(resolver)
            assert "base template" in result

    def test_load_phase_component_with_resolver(self, tmp_path):
        """load_phase_component uses PromptMatrixResolver."""
        with patch("src.core.loader.get_project_root", return_value=tmp_path):
            config_prompts = tmp_path / "config" / "prompts"
            config_prompts.mkdir(parents=True)
            (config_prompts / "strategic.txt").write_text(
                "strategic phase {phase_number}"
            )

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
        (expert_dir / "model_config_matrix.yaml").write_text(
            textwrap.dedent("""\
            default:
              prompts:
                strategic: strategic.txt
        """)
        )

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
            tactical=PhaseLLMOverride(
                model="gpt-4o-mini", model_max_context_tokens=32000
            ),
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
        config = LLMConfig(
            model="gpt-4o", temperature=0.3, model_max_context_tokens=128000
        )
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
