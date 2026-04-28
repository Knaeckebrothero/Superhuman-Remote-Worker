"""Tests for the stage-5 config resolution in browser._get_browser_llm.

The function prefers ``context.config['browser']`` (populated from job
``config_override`` at dispatch) and falls back to env vars with a
``DeprecationWarning`` whenever one is used.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _call(context=None):
    """Invoke _get_browser_llm without actually constructing a ChatOpenAI."""
    with patch("langchain_openai.ChatOpenAI") as chat_cls:
        chat_cls.return_value = SimpleNamespace(name="stub")
        from src.tools.research.browser import _get_browser_llm

        _get_browser_llm(context)
        assert chat_cls.called
        return chat_cls.call_args.kwargs


def _ctx(browser_cfg):
    return SimpleNamespace(config={"browser": browser_cfg} if browser_cfg else {})


class TestFromContext:
    def test_config_override_wins(self, monkeypatch):
        monkeypatch.setenv("BROWSER_LLM_MODEL", "env-model")
        monkeypatch.setenv("BROWSER_LLM_BASE_URL", "https://env/v1")
        ctx = _ctx(
            {
                "model": "cfg-model",
                "base_url": "http://cfg.svc/v1",
                "api_key": "cfg-key",
            }
        )
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            kwargs = _call(ctx)
        assert kwargs["model"] == "cfg-model"
        assert kwargs["base_url"] == "http://cfg.svc/v1"
        assert kwargs["api_key"] == "cfg-key"
        assert not any(issubclass(w.category, DeprecationWarning) for w in warned)

    def test_partial_override_falls_through_to_env_with_warning(self, monkeypatch):
        monkeypatch.setenv("BROWSER_LLM_BASE_URL", "https://env/v1")
        ctx = _ctx({"model": "cfg-model"})
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            kwargs = _call(ctx)
        assert kwargs["model"] == "cfg-model"
        assert kwargs["base_url"] == "https://env/v1"
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "BROWSER_LLM_BASE_URL" in str(w.message)
            for w in warned
        )


class TestEnvFallback:
    def test_model_from_env_warns(self, monkeypatch):
        monkeypatch.setenv("BROWSER_LLM_MODEL", "gpt-legacy")
        monkeypatch.delenv("BROWSER_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            kwargs = _call(None)
        assert kwargs["model"] == "gpt-legacy"
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "BROWSER_LLM_MODEL" in str(w.message)
            for w in warned
        )

    def test_no_env_no_context_uses_default(self, monkeypatch):
        for var in (
            "BROWSER_LLM_MODEL",
            "BROWSER_LLM_BASE_URL",
            "BROWSER_LLM_API_KEY",
            "OPENAI_API_KEY",
            "LLM_BASE_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        kwargs = _call(None)
        assert kwargs["model"] == "gpt-4o"
        assert "base_url" not in kwargs

    def test_llm_base_url_legacy_branch_only_for_openai_prefix(self, monkeypatch):
        monkeypatch.delenv("BROWSER_LLM_BASE_URL", raising=False)
        monkeypatch.setenv("LLM_BASE_URL", "https://legacy/v1")

        # gpt-4o does NOT start with "openai/", so legacy branch is skipped.
        kwargs = _call(_ctx({"model": "gpt-4o"}))
        assert "base_url" not in kwargs

        # openai/gpt-oss-120b does, so legacy branch fires (with warning).
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            kwargs = _call(_ctx({"model": "openai/gpt-oss-120b"}))
        assert kwargs["base_url"] == "https://legacy/v1"
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "LLM_BASE_URL" in str(w.message)
            for w in warned
        )


@pytest.mark.parametrize("env_var", ["BROWSER_LLM_MODEL", "BROWSER_LLM_BASE_URL"])
def test_deprecation_message_mentions_override(monkeypatch, env_var):
    monkeypatch.setenv(env_var, "legacy-value")
    if env_var == "BROWSER_LLM_MODEL":
        monkeypatch.delenv("BROWSER_LLM_BASE_URL", raising=False)
    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")
        _call(None)
    depr = [w for w in warned if issubclass(w.category, DeprecationWarning)]
    assert any(env_var in str(w.message) for w in depr)
