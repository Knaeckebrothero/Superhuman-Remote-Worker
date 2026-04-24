"""Tests for the DB-backed default-builder-model resolution in builder_tools.

Covers the replacement of BUILDER_MODEL / BUILDER_BASE_URL env-var reads
with system_settings + model registry lookups.
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.builder_tools import (
    get_builder_base_url,
    get_builder_model,
    get_builder_provider,
)
from src.core.model_registry import (
    register_custom_lookup,
    register_system_lookup,
)


@pytest.fixture(autouse=True)
def _reset_hooks():
    register_custom_lookup(None)
    register_system_lookup(None)
    yield
    register_custom_lookup(None)
    register_system_lookup(None)


class TestGetBuilderModel:
    @pytest.mark.asyncio
    async def test_returns_db_value_when_configured(self, monkeypatch):
        monkeypatch.delenv("BUILDER_MODEL", raising=False)
        db = AsyncMock()
        db.get_default_llm_model.return_value = "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
        assert await get_builder_model(db) == "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
        db.get_default_llm_model.assert_awaited_once_with("builder")

    @pytest.mark.asyncio
    async def test_falls_back_to_env_with_deprecation(self, monkeypatch):
        monkeypatch.setenv("BUILDER_MODEL", "gpt-4o")
        db = AsyncMock()
        db.get_default_llm_model.return_value = None
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            result = await get_builder_model(db)
        assert result == "gpt-4o"
        assert any(issubclass(w.category, DeprecationWarning) for w in warned)

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("BUILDER_MODEL", raising=False)
        db = AsyncMock()
        db.get_default_llm_model.return_value = None
        assert await get_builder_model(db) is None

    @pytest.mark.asyncio
    async def test_skips_db_when_none(self, monkeypatch):
        """Callers without a DB (legacy paths) skip straight to env."""
        monkeypatch.setenv("BUILDER_MODEL", "claude-opus-4-6")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert await get_builder_model(db=None) == "claude-opus-4-6"


class TestGetBuilderBaseUrl:
    @pytest.mark.asyncio
    async def test_uses_system_endpoint_base_url(self, monkeypatch):
        """Registry-resolved URL beats env vars."""
        monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")

        async def fake_sys(model_id, capability="chat"):
            return {
                "endpoint_id": "aa",
                "base_url": "http://vllm.svc/v1",
                "model_id": model_id,
                "display_name": "Gemma",
                "family": "gemma",
                "context_window": 128000,
                "reasoning_level": None,
                "capability": capability,
            }

        register_system_lookup(fake_sys)
        assert (
            await get_builder_base_url("RedHatAI/gemma-4-31B-it-FP8-Dynamic")
            == "http://vllm.svc/v1"
        )

    @pytest.mark.asyncio
    async def test_builder_base_url_env_fallback_warns(self, monkeypatch):
        """With no registry hit and BUILDER_BASE_URL set, the env path warns."""
        monkeypatch.setenv("BUILDER_BASE_URL", "https://legacy.example/v1")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            url = await get_builder_base_url("totally-unknown-model")
        assert url == "https://legacy.example/v1"
        assert any(issubclass(w.category, DeprecationWarning) for w in warned)

    @pytest.mark.asyncio
    async def test_anthropic_returns_none_without_base_url(self, monkeypatch):
        monkeypatch.delenv("BUILDER_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert await get_builder_base_url("claude-opus-4-6") is None

    @pytest.mark.asyncio
    async def test_openai_falls_back_to_openai_base_url(self, monkeypatch):
        monkeypatch.delenv("BUILDER_BASE_URL", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "https://oai.proxy/v1")
        assert await get_builder_base_url("unknown-openai-id") == "https://oai.proxy/v1"


class TestGetBuilderProvider:
    def test_claude_is_anthropic(self, monkeypatch):
        monkeypatch.delenv("BUILDER_LLM_PROVIDER", raising=False)
        assert get_builder_provider("claude-opus-4-6") == "anthropic"

    def test_codex_is_openai(self, monkeypatch):
        monkeypatch.delenv("BUILDER_LLM_PROVIDER", raising=False)
        assert get_builder_provider("codex/gpt-5.4") == "openai"

    def test_default_is_openai(self, monkeypatch):
        monkeypatch.delenv("BUILDER_LLM_PROVIDER", raising=False)
        assert get_builder_provider("gpt-4o") == "openai"

    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("BUILDER_LLM_PROVIDER", "Groq")
        assert get_builder_provider("claude-opus-4-6") == "groq"
