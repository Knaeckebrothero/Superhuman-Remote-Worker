"""Tests for ``get_citation_engine_config``.

The helper resolves citation config from three sources, in order:
1. Explicit ``overrides`` dict (programmatic callers)
2. Process environment vars (orchestrator-injected at dispatch, or .env
   fallback when the agent runs standalone)
3. Hardcoded defaults

Both override and env paths are first-class; there is no deprecation.
"""

from __future__ import annotations

from src.utils.citation_utils import get_citation_engine_config


class TestOverridesPath:
    def test_overrides_beat_env(self, monkeypatch):
        monkeypatch.setenv("CITATION_LLM_MODEL", "env-model")
        monkeypatch.setenv("CITATION_LLM_URL", "https://env.example/v1")

        cfg = get_citation_engine_config(
            overrides={
                "llm_model": "override-model",
                "llm_url": "https://override.example/v1",
                "db_url": "postgresql://override/db",
                "reasoning_required": "high",
            }
        )
        assert cfg["llm_model"] == "override-model"
        assert cfg["llm_url"] == "https://override.example/v1"
        assert cfg["db_url"] == "postgresql://override/db"
        assert cfg["reasoning_required"] == "high"

    def test_partial_overrides_fall_through_to_env(self, monkeypatch):
        monkeypatch.setenv("CITATION_LLM_MODEL", "env-model")
        monkeypatch.setenv("CITATION_LLM_URL", "https://env/v1")

        cfg = get_citation_engine_config(overrides={"llm_model": "from-override"})

        assert cfg["llm_model"] == "from-override"
        assert cfg["llm_url"] == "https://env/v1"


class TestEnvFallback:
    def test_env_vars_are_read_without_warning(self, monkeypatch):
        monkeypatch.setenv("CITATION_LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("CITATION_LLM_URL", "https://legacy/v1")

        cfg = get_citation_engine_config()

        assert cfg["llm_model"] == "gpt-4o"
        assert cfg["llm_url"] == "https://legacy/v1"

    def test_no_env_no_overrides_falls_back_to_defaults(self, monkeypatch):
        for var in (
            "CITATION_LLM_MODEL",
            "CITATION_LLM_URL",
            "CITATION_DB_URL",
            "VECTOR_DB_URL",
            "DATABASE_URL",
            "CITATION_REASONING_REQUIRED",
        ):
            monkeypatch.delenv(var, raising=False)

        cfg = get_citation_engine_config()
        assert cfg["llm_model"] == "gpt-4"
        assert cfg["llm_url"] is None
        assert cfg["reasoning_required"] == "low"
