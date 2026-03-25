"""Tests for LLM provider routing based on model name prefix."""

import os
from unittest.mock import patch, MagicMock

import pytest

from src.core.loader import (
    _detect_provider,
    _needs_custom_base_url,
    _should_use_reasoning_summary,
    _clamp_reasoning_level,
    _OPENAI_REASONING_LEVELS,
    _create_openai_llm,
    _create_openrouter_llm,
    _create_codex_llm,
    detect_model_family,
)


class TestNeedsCustomBaseUrl:
    """Unit tests for the _needs_custom_base_url helper."""

    def test_openai_prefix_needs_custom_url(self):
        assert _needs_custom_base_url("openai/gpt-oss-120b") is True

    def test_openai_prefix_case_insensitive(self):
        assert _needs_custom_base_url("OpenAI/my-model") is True
        assert _needs_custom_base_url("OPENAI/my-model") is True

    def test_native_gpt_does_not_need_custom_url(self):
        assert _needs_custom_base_url("gpt-5.2-pro") is False
        assert _needs_custom_base_url("gpt-4o") is False
        assert _needs_custom_base_url("gpt-4o-mini") is False

    def test_native_o_series_does_not_need_custom_url(self):
        assert _needs_custom_base_url("o1-preview") is False
        assert _needs_custom_base_url("o3-mini") is False
        assert _needs_custom_base_url("o4-mini") is False

    def test_other_models_do_not_need_custom_url(self):
        assert _needs_custom_base_url("claude-3-opus") is False
        assert _needs_custom_base_url("gemini-pro") is False


def _make_config(**overrides):
    """Create a mock LLMConfig with sensible defaults."""
    config = MagicMock()
    config.model = overrides.get("model", "gpt-4o")
    config.base_url = overrides.get("base_url", None)
    config.api_key = overrides.get("api_key", None)
    config.temperature = overrides.get("temperature", 0.0)
    config.top_p = overrides.get("top_p", None)
    config.top_k = overrides.get("top_k", None)
    config.max_retries = overrides.get("max_retries", 2)
    config.timeout = overrides.get("timeout", None)
    config.reasoning_level = overrides.get("reasoning_level", None)
    config.streaming = overrides.get("streaming", False)
    config.max_output_tokens = overrides.get("max_output_tokens", None)
    return config


# Patches applied to all routing integration tests
_common_patches = [
    patch("src.core.loader.ReasoningChatOpenAI"),
    patch("src.llm.key_ring.KeyRing", new_callable=MagicMock),
]


class TestOpenAILLMRouting:
    """Integration tests for base_url routing in _create_openai_llm."""

    @patch.dict(os.environ, {"LLM_BASE_URL": "http://localhost:8080/v1"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_openai_prefix_uses_llm_base_url(self, mock_chat):
        """openai/ prefixed model should use LLM_BASE_URL."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openai/gpt-oss-120b")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "http://localhost:8080/v1"

    @patch.dict(os.environ, {"LLM_BASE_URL": "http://localhost:8080/v1"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_native_gpt_ignores_llm_base_url(self, mock_chat):
        """Native gpt-* model should NOT use LLM_BASE_URL."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.2-pro")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "base_url" not in call_kwargs

    @patch.dict(os.environ, {"LLM_BASE_URL": "http://localhost:8080/v1"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_gpt4o_ignores_llm_base_url(self, mock_chat):
        """gpt-4o should NOT use LLM_BASE_URL."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-4o")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "base_url" not in call_kwargs

    @patch.dict(os.environ, {"LLM_BASE_URL": "http://localhost:8080/v1"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_explicit_base_url_always_wins(self, mock_chat):
        """Explicit config.base_url overrides everything, even for native models."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-4o", base_url="http://custom-proxy:9000/v1")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "http://custom-proxy:9000/v1"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_no_base_url_env_set(self, mock_chat):
        """When LLM_BASE_URL is not set, even openai/ models get no base_url."""
        mock_chat.return_value = MagicMock()
        # Ensure LLM_BASE_URL is not set
        env = os.environ.copy()
        env.pop("LLM_BASE_URL", None)

        with patch.dict(os.environ, env, clear=True):
            config = _make_config(model="openai/gpt-oss-120b")
            _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "base_url" not in call_kwargs


class TestShouldUseReasoningSummary:
    """Unit tests for the _should_use_reasoning_summary helper."""

    def test_o1_model(self):
        assert _should_use_reasoning_summary("o1-preview") is True
        assert _should_use_reasoning_summary("o1-mini") is True

    def test_o3_model(self):
        assert _should_use_reasoning_summary("o3-mini") is True
        assert _should_use_reasoning_summary("o3-pro") is True

    def test_o4_model(self):
        assert _should_use_reasoning_summary("o4-mini") is True

    def test_gpt5_model(self):
        assert _should_use_reasoning_summary("gpt-5.2-pro") is True
        assert _should_use_reasoning_summary("gpt-5") is True

    def test_case_insensitive(self):
        assert _should_use_reasoning_summary("GPT-5.2-pro") is True
        assert _should_use_reasoning_summary("O3-mini") is True

    def test_proxy_models_excluded(self):
        """Models with / are proxy models and should not use Responses API."""
        assert _should_use_reasoning_summary("openai/gpt-oss-120b") is False
        assert _should_use_reasoning_summary("groq/llama-3") is False

    def test_non_reasoning_models(self):
        assert _should_use_reasoning_summary("gpt-4o") is False
        assert _should_use_reasoning_summary("gpt-4o-mini") is False
        assert _should_use_reasoning_summary("claude-3-opus") is False
        assert _should_use_reasoning_summary("gemini-pro") is False

    def test_deepseek_excluded(self):
        assert _should_use_reasoning_summary("deepseek-reasoner") is False


class TestReasoningSummaryRouting:
    """Integration tests verifying correct reasoning kwargs reach ReasoningChatOpenAI."""

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_gpt5_gets_reasoning_dict(self, mock_chat):
        """gpt-5.2-pro should get reasoning={effort, summary} for Responses API."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.2-pro", reasoning_level="high")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "high", "summary": "auto"}
        # Should NOT have reasoning_effort in model_kwargs
        assert (
            "model_kwargs" not in call_kwargs
            or "reasoning_effort" not in call_kwargs.get("model_kwargs", {})
        )

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_o3_gets_reasoning_dict(self, mock_chat):
        """o3-mini should get reasoning={effort, summary} for Responses API."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="o3-mini", reasoning_level="medium")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "medium", "summary": "auto"}

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_proxy_model_gets_model_kwargs(self, mock_chat):
        """openai/ proxy model should use model_kwargs.reasoning_effort (Chat Completions)."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openai/gpt-oss-120b", reasoning_level="high")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_no_reasoning_when_none(self, mock_chat):
        """reasoning_level='none' should skip both paths."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.2-pro", reasoning_level="none")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert "model_kwargs" not in call_kwargs

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_no_reasoning_when_unset(self, mock_chat):
        """No reasoning_level should skip both paths."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-4o", reasoning_level=None)

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert "model_kwargs" not in call_kwargs


class TestDetectProviderOpenRouter:
    """Unit tests for openrouter/ prefix detection in _detect_provider."""

    def test_openrouter_prefix(self):
        assert _detect_provider("openrouter/anthropic/claude-opus-4") == "openrouter"

    def test_openrouter_prefix_case_insensitive(self):
        assert _detect_provider("OpenRouter/openai/gpt-4o") == "openrouter"
        assert _detect_provider("OPENROUTER/meta-llama/llama-3") == "openrouter"

    def test_openrouter_various_models(self):
        assert _detect_provider("openrouter/deepseek/deepseek-r1") == "openrouter"
        assert _detect_provider("openrouter/openai/gpt-4o-mini") == "openrouter"
        assert (
            _detect_provider("openrouter/meta-llama/llama-3.3-70b-instruct")
            == "openrouter"
        )

    def test_explicit_provider_overrides_prefix(self):
        """Explicit provider should always win over prefix detection."""
        assert _detect_provider("openrouter/some-model", "openai") == "openai"

    def test_other_providers_unchanged(self):
        """Ensure openrouter detection doesn't break existing providers."""
        assert _detect_provider("claude-sonnet-4-20250514") == "anthropic"
        assert _detect_provider("gemini-2.0-flash") == "google"
        assert _detect_provider("groq/llama-3") == "groq"
        assert _detect_provider("gpt-4o") == "openai"


class TestOpenRouterLLMCreation:
    """Integration tests for _create_openrouter_llm."""

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_strips_prefix_and_sets_base_url(self, mock_chat):
        """Should strip openrouter/ prefix and use OpenRouter base URL."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openrouter/anthropic/claude-opus-4")

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["model"] == "anthropic/claude-opus-4"
        assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_explicit_base_url_overrides(self, mock_chat):
        """Explicit config.base_url should override the default OpenRouter URL."""
        mock_chat.return_value = MagicMock()
        config = _make_config(
            model="openrouter/openai/gpt-4o",
            base_url="https://custom-proxy.example.com/v1",
        )

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "https://custom-proxy.example.com/v1"

    @patch.dict(
        os.environ,
        {
            "OPENROUTER_API_KEY": "sk-or-test-key",
            "OPENROUTER_REFERER": "https://my-app.com",
            "OPENROUTER_TITLE": "My Agent",
        },
        clear=False,
    )
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_custom_headers(self, mock_chat):
        """Should pass HTTP-Referer and X-Title headers when env vars are set."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openrouter/openai/gpt-4o")

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["default_headers"] == {
            "HTTP-Referer": "https://my-app.com",
            "X-Title": "My Agent",
        }

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_reasoning_in_model_kwargs(self, mock_chat):
        """Reasoning should use nested reasoning object for OpenRouter."""
        mock_chat.return_value = MagicMock()
        config = _make_config(
            model="openrouter/deepseek/deepseek-r1", reasoning_level="high"
        )

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        # reasoning is a first-class kwarg (not nested in model_kwargs)
        # to avoid LangChain warning about unknown model_kwargs
        assert call_kwargs["reasoning"] == {"effort": "high"}
        assert "reasoning" not in call_kwargs.get("model_kwargs", {})

    def test_missing_api_key_raises(self):
        """Should raise ValueError when OPENROUTER_API_KEY is not set."""
        env = os.environ.copy()
        env.pop("OPENROUTER_API_KEY", None)

        with patch.dict(os.environ, env, clear=True):
            config = _make_config(model="openrouter/openai/gpt-4o")
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                _create_openrouter_llm(config, limits=None)

    @patch.dict(
        os.environ, {"OPENROUTER_API_KEY": "sk-or-key1,sk-or-key2"}, clear=False
    )
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_multiple_keys(self, mock_chat):
        """Should support comma-separated keys for rotation."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openrouter/openai/gpt-4o")

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        # First key should be passed to SDK
        assert call_kwargs["api_key"] == "sk-or-key1"


class TestReasoningLevelClamping:
    """Tests for _clamp_reasoning_level."""

    def test_supported_levels_unchanged(self):
        """low/medium/high pass through for OpenAI."""
        for level in ("low", "medium", "high"):
            assert _clamp_reasoning_level(level, _OPENAI_REASONING_LEVELS) == level

    def test_xhigh_clamped_to_high(self):
        """xhigh -> high for OpenAI."""
        assert _clamp_reasoning_level("xhigh", _OPENAI_REASONING_LEVELS) == "high"

    def test_minimal_clamped_to_low(self):
        """minimal -> low for OpenAI."""
        assert _clamp_reasoning_level("minimal", _OPENAI_REASONING_LEVELS) == "low"

    def test_unknown_level_falls_back_to_high(self):
        """Unknown levels should fall back to high."""
        assert _clamp_reasoning_level("turbo", _OPENAI_REASONING_LEVELS) == "high"


class TestOpenAIReasoningClamping:
    """Integration tests verifying clamping reaches ReasoningChatOpenAI for OpenAI."""

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_xhigh_clamped_to_high(self, mock_chat):
        """xhigh should be clamped to high for OpenAI Chat Completions."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openai/gpt-oss-120b", reasoning_level="xhigh")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_minimal_clamped_to_low_responses_api(self, mock_chat):
        """minimal should be clamped to low for Responses API models."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="o3-mini", reasoning_level="minimal")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "low", "summary": "auto"}


class TestOpenRouterReasoningFormat:
    """Verify OpenRouter gets nested reasoning object without clamping."""

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_xhigh_not_clamped(self, mock_chat):
        """OpenRouter should pass xhigh through without clamping."""
        mock_chat.return_value = MagicMock()
        config = _make_config(
            model="openrouter/deepseek/deepseek-r1", reasoning_level="xhigh"
        )

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "xhigh"}

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_minimal_not_clamped(self, mock_chat):
        """OpenRouter should pass minimal through without clamping."""
        mock_chat.return_value = MagicMock()
        config = _make_config(
            model="openrouter/minimax/minimax-m2.7", reasoning_level="minimal"
        )

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "minimal"}


class TestDetectProviderCodex:
    """Unit tests for codex/ prefix detection in _detect_provider."""

    def test_codex_prefix(self):
        assert _detect_provider("codex/gpt-5.4-pro") == "codex"

    def test_codex_prefix_case_insensitive(self):
        assert _detect_provider("Codex/o3-pro") == "codex"
        assert _detect_provider("CODEX/gpt-4o") == "codex"

    def test_codex_various_models(self):
        assert _detect_provider("codex/gpt-4o") == "codex"
        assert _detect_provider("codex/o3-pro") == "codex"
        assert _detect_provider("codex/gpt-5.4-pro") == "codex"
        assert _detect_provider("codex/o4-mini") == "codex"

    def test_explicit_provider_overrides_codex_prefix(self):
        """Explicit provider should always win over prefix detection."""
        assert _detect_provider("codex/some-model", "openai") == "openai"

    def test_other_providers_unchanged(self):
        """Ensure codex detection doesn't break existing providers."""
        assert _detect_provider("claude-sonnet-4-20250514") == "anthropic"
        assert _detect_provider("gemini-2.0-flash") == "google"
        assert _detect_provider("groq/llama-3") == "groq"
        assert _detect_provider("openrouter/openai/gpt-4o") == "openrouter"
        assert _detect_provider("gpt-4o") == "openai"


class TestCodexModelFamilyDetection:
    """Verify detect_model_family strips codex/ prefix correctly."""

    def test_codex_gpt5_family(self):
        assert detect_model_family("codex/gpt-5.4-pro") == "gpt-5"

    def test_codex_o_series_family(self):
        assert detect_model_family("codex/o3-pro") == "o-series"
        assert detect_model_family("codex/o4-mini") == "o-series"

    def test_codex_gpt4o_family(self):
        assert detect_model_family("codex/gpt-4o") == "gpt-4o"

    def test_codex_does_not_affect_other_prefixes(self):
        """Existing prefix stripping should still work."""
        assert detect_model_family("openrouter/openai/gpt-4o") == "gpt-4o"
        assert detect_model_family("groq/llama-3.3-70b") == "llama"
        assert detect_model_family("openai/gpt-oss-120b") == "gpt-oss"


class TestCodexLLMCreation:
    """Integration tests for _create_codex_llm."""

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_strips_prefix_and_sets_default_base_url(self, mock_chat):
        """Should strip codex/ prefix and use default CLIProxyAPI base URL."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-5.4-pro")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["model"] == "gpt-5.4-pro"
        assert call_kwargs["base_url"] == "http://localhost:8317/v1"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_explicit_base_url_overrides(self, mock_chat):
        """Explicit config.base_url should override env and default."""
        mock_chat.return_value = MagicMock()
        config = _make_config(
            model="codex/gpt-4o",
            base_url="http://custom-proxy:9000/v1",
        )

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "http://custom-proxy:9000/v1"

    @patch.dict(os.environ, {"CODEX_BASE_URL": "http://remote:8317/v1"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_env_base_url_overrides_default(self, mock_chat):
        """CODEX_BASE_URL env var should override the default."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-4o")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "http://remote:8317/v1"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_default_api_key_is_not_needed(self, mock_chat):
        """When no env var set, API key should default to 'not-needed'."""
        mock_chat.return_value = MagicMock()
        env = os.environ.copy()
        env.pop("CODEX_API_KEY", None)

        with patch.dict(os.environ, env, clear=True):
            config = _make_config(model="codex/gpt-5.4-pro")
            _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["api_key"] == "not-needed"

    @patch.dict(os.environ, {"CODEX_API_KEY": "sk-codex-test"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_explicit_api_key_from_env(self, mock_chat):
        """CODEX_API_KEY env var should be used when set."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-4o")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["api_key"] == "sk-codex-test"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_reasoning_uses_chat_completions(self, mock_chat):
        """Reasoning should use model_kwargs.reasoning_effort for proxy."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/o3-pro", reasoning_level="high")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_reasoning_clamped(self, mock_chat):
        """xhigh should be clamped to high for Codex (OpenAI limits)."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-5.4-pro", reasoning_level="xhigh")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"

    @patch.dict(os.environ, {"CODEX_API_KEY": "sk-key1,sk-key2"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_multiple_keys(self, mock_chat):
        """Should support comma-separated keys for rotation."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-4o")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["api_key"] == "sk-key1"
