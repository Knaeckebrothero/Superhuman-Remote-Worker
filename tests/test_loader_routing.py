"""Tests for LLM provider routing based on model name prefix."""

import os
from unittest.mock import patch, MagicMock

import pytest

from src.core.loader import (
    _detect_provider,
    _needs_custom_base_url,
    _should_use_reasoning_summary,
    _create_openai_llm,
    _create_openrouter_llm,
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
        assert "model_kwargs" not in call_kwargs or "reasoning_effort" not in call_kwargs.get("model_kwargs", {})

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
        assert _detect_provider("openrouter/meta-llama/llama-3.3-70b-instruct") == "openrouter"

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

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "sk-or-test-key",
        "OPENROUTER_REFERER": "https://my-app.com",
        "OPENROUTER_TITLE": "My Agent",
    }, clear=False)
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
        """Reasoning should go in model_kwargs (no Responses API on OpenRouter)."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openrouter/deepseek/deepseek-r1", reasoning_level="high")

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"

    def test_missing_api_key_raises(self):
        """Should raise ValueError when OPENROUTER_API_KEY is not set."""
        env = os.environ.copy()
        env.pop("OPENROUTER_API_KEY", None)

        with patch.dict(os.environ, env, clear=True):
            config = _make_config(model="openrouter/openai/gpt-4o")
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                _create_openrouter_llm(config, limits=None)

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-key1,sk-or-key2"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_multiple_keys(self, mock_chat):
        """Should support comma-separated keys for rotation."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="openrouter/openai/gpt-4o")

        _create_openrouter_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        # First key should be passed to SDK
        assert call_kwargs["api_key"] == "sk-or-key1"
