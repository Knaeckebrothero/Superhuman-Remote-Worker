"""Tests for LLM provider routing based on model name prefix."""

import os
from unittest.mock import patch, MagicMock

import pytest

from src.core.loader import (
    _should_use_reasoning_summary,
    _clamp_reasoning_level,
    _OPENAI_REASONING_LEVELS,
    _create_openai_llm,
    _create_openrouter_llm,
    _create_codex_llm,
)


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
    """Integration tests for base_url routing in _create_openai_llm.

    Post chunk-6 (models_yaml_removal), base_url resolution collapsed to
    a single source: ``config.base_url`` (dispatcher-injected from the
    catalog row's transport). The legacy YAML fallback that read
    ``LLM_BASE_URL`` for self-hosted "Local" group entries is gone — the
    orchestrator hard-fails at boot when ``LLM_BASE_URL`` is set, so the
    var being present in the test environment doesn't reach the loader.
    Self-hosted models now MUST come through a catalog row whose
    ``provider_kind='endpoint'`` row supplies the base_url at dispatch.
    """

    _LOCAL_MODEL = "RedHatAI/gemma-4-31B-it-FP8-Dynamic"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_self_hosted_model_uses_dispatcher_injected_base_url(self, mock_chat):
        """Self-hosted models receive base_url via dispatcher-injected
        config.base_url — not via the deleted LLM_BASE_URL fallback."""
        mock_chat.return_value = MagicMock()
        config = _make_config(
            model=self._LOCAL_MODEL,
            base_url="http://my-vllm.cluster.local:8080/v1",
        )
        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "http://my-vllm.cluster.local:8080/v1"
        # Regression: the original bug was that the openai/ prefix
        # leaked into the wire name and vLLM 404'd. The bare ID must
        # reach the SDK untouched.
        assert call_kwargs["model"] == self._LOCAL_MODEL
        assert not call_kwargs["model"].startswith("openai/")

    @patch.dict(
        os.environ, {"LLM_BASE_URL": "http://stale-leftover:8080/v1"}, clear=False
    )
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_env_var_does_not_leak_into_native_openai_models(self, mock_chat):
        """A stale LLM_BASE_URL in the test env (the orchestrator boot
        check would refuse to start in production) must NOT reach the
        OpenAI factory — chunk 6 deleted the env-var inheritance path."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.2-pro")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "base_url" not in call_kwargs

    @patch.dict(
        os.environ, {"LLM_BASE_URL": "http://stale-leftover:8080/v1"}, clear=False
    )
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_gpt4o_routes_to_native_openai(self, mock_chat):
        """gpt-4o is native — no base_url override, ever."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-4o")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "base_url" not in call_kwargs

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_explicit_base_url_always_wins(self, mock_chat):
        """Explicit config.base_url is the dispatcher-injection path."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-4o", base_url="http://custom-proxy:9000/v1")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["base_url"] == "http://custom-proxy:9000/v1"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_self_hosted_without_dispatcher_injection_falls_to_native(self, mock_chat):
        """Self-hosted ID with no base_url leaks through to api.openai.com.

        This is intentional post-chunk-6: catch-all behavior at the loader
        is API-OpenAI-default, and the readiness gate (chunk 5) blocks
        catalog-row-less models from being dispatched in the first place.
        Test pins the absence of any env-driven fallback magic."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model=self._LOCAL_MODEL)
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
    def test_gpt5_gets_reasoning_effort(self, mock_chat):
        """gpt-5.2-pro should use Chat Completions reasoning_effort (not Responses API)."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="gpt-5.2-pro", reasoning_level="high")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "high"

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_o3_gets_reasoning_effort(self, mock_chat):
        """o3-mini should use Chat Completions reasoning_effort (not Responses API)."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="o3-mini", reasoning_level="medium")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "medium"

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
    def test_minimal_clamped_to_low(self, mock_chat):
        """minimal should be clamped to low for OpenAI reasoning models."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="o3-mini", reasoning_level="minimal")

        _create_openai_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert "reasoning" not in call_kwargs
        assert call_kwargs["model_kwargs"]["reasoning_effort"] == "low"


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
    def test_reasoning_uses_responses_api_for_native_models(self, mock_chat):
        """Native OpenAI reasoning models use Responses API (Codex proxy requires it)."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/o3-pro", reasoning_level="high")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["reasoning"] == {"effort": "high", "summary": "auto"}
        assert "reasoning_effort" not in call_kwargs.get("model_kwargs", {})

    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_reasoning_uses_chat_completions_for_proxy_models(self, mock_chat):
        """Non-native models (with / in name after prefix strip) use chat completions."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/some-custom/model", reasoning_level="high")

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
        assert call_kwargs["reasoning"] == {"effort": "high", "summary": "auto"}

    @patch.dict(os.environ, {"CODEX_API_KEY": "sk-key1,sk-key2"}, clear=False)
    @patch("src.core.loader.ReasoningChatOpenAI")
    def test_multiple_keys(self, mock_chat):
        """Should support comma-separated keys for rotation."""
        mock_chat.return_value = MagicMock()
        config = _make_config(model="codex/gpt-4o")

        _create_codex_llm(config, limits=None)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["api_key"] == "sk-key1"
