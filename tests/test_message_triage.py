"""Tests for ``services.message_triage`` config resolution.

The triage LLM path itself uses httpx and isn't covered here — these tests
pin the DB-backed resolution that replaces the old env-var reads.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.services import message_triage


class TestResolveFromEnv:
    def test_openai_env_works(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        monkeypatch.setenv("LLM_BASE_URL", "https://proxy/v1")
        monkeypatch.setenv("TRIAGE_MODEL", "gpt-4o-mini")

        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            resolved = message_triage._resolve_from_env()

        assert resolved == ("gpt-4o-mini", "https://proxy/v1", "sk-from-env")
        assert any(issubclass(w.category, DeprecationWarning) for w in warned)

    def test_no_keys_returns_none(self, monkeypatch):
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        assert message_triage._resolve_from_env() is None


class TestResolveFromDB:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_default_model(self):
        db = SimpleNamespace(
            get_default_llm_model=AsyncMock(return_value=None),
            get_system_api_key=AsyncMock(return_value=None),
        )
        assert await message_triage._resolve_triage_config(db) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_api_key(self):
        db = SimpleNamespace(
            get_default_llm_model=AsyncMock(return_value="gpt-4o"),
            get_system_api_key=AsyncMock(return_value=None),
        )
        assert await message_triage._resolve_triage_config(db) is None

    @pytest.mark.asyncio
    async def test_uses_registry_base_url_when_resolved(self, monkeypatch):
        from src.core.model_registry import ModelMeta, register_system_lookup

        async def fake_lookup(model_id, capability="chat"):
            return {
                "endpoint_id": "ep-1",
                "base_url": "http://vllm.svc/v1",
                "model_id": model_id,
                "display_name": "Gemma",
                "family": "gemma",
                "context_window": 128000,
                "reasoning_level": None,
            }

        register_system_lookup(fake_lookup)
        try:
            db = SimpleNamespace(
                get_default_llm_model=AsyncMock(
                    return_value="RedHatAI/gemma-4-31B-it-FP8-Dynamic"
                ),
                get_system_api_key=AsyncMock(return_value="sk-sys"),
            )
            model, base_url, api_key = await message_triage._resolve_triage_config(db)
            assert base_url == "http://vllm.svc/v1"
            assert api_key == "sk-sys"
            assert model == "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
        finally:
            register_system_lookup(None)

        # Silence unused import
        _ = ModelMeta

    @pytest.mark.asyncio
    async def test_falls_back_to_openai_when_registry_misses(self):
        db = SimpleNamespace(
            get_default_llm_model=AsyncMock(return_value="gpt-4o"),
            get_system_api_key=AsyncMock(return_value="sk-sys"),
        )
        model, base_url, api_key = await message_triage._resolve_triage_config(db)
        assert model == "gpt-4o"
        assert base_url == "https://api.openai.com/v1"
        assert api_key == "sk-sys"
        db.get_system_api_key.assert_awaited_once_with("openai")

    @pytest.mark.asyncio
    async def test_provider_inference_anthropic(self):
        seen: list[str] = []

        async def capture(provider):
            seen.append(provider)
            return "key"

        db = SimpleNamespace(
            get_default_llm_model=AsyncMock(return_value="claude-opus-4-6"),
            get_system_api_key=AsyncMock(side_effect=capture),
        )
        await message_triage._resolve_triage_config(db)
        assert seen == ["anthropic"]


class TestInferProvider:
    def test_known_prefixes(self):
        assert message_triage._infer_provider("claude-opus-4-6") == "anthropic"
        assert message_triage._infer_provider("groq/moonshotai/kimi") == "groq"
        assert message_triage._infer_provider("gpt-4o") == "openai"
        assert message_triage._infer_provider("gemini-2.5-pro") == "google"

    def test_unknown_defaults_to_openai(self):
        assert message_triage._infer_provider("some-unknown-model") == "openai"
