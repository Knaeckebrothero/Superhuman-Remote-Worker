"""Tests for the stage-5 refactor of ``get_citation_engine_config``.

The helper moved from pure env-var reads to an overrides-first resolution
so the orchestrator can inject citation model/URL via ``config_override``.
Env vars stay as a deprecated fallback; these tests pin both paths.
"""

from __future__ import annotations

import warnings

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

    def test_partial_overrides_still_fall_through_to_env(self, monkeypatch):
        monkeypatch.setenv("CITATION_LLM_MODEL", "env-model")
        monkeypatch.setenv("CITATION_LLM_URL", "https://env/v1")

        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            cfg = get_citation_engine_config(overrides={"llm_model": "from-override"})

        assert cfg["llm_model"] == "from-override"
        assert cfg["llm_url"] == "https://env/v1"
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "CITATION_LLM_URL" in str(w.message)
            for w in warned
        )


class TestEnvFallback:
    def test_env_vars_trigger_deprecation(self, monkeypatch):
        monkeypatch.setenv("CITATION_LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("CITATION_LLM_URL", "https://legacy/v1")

        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            cfg = get_citation_engine_config()

        assert cfg["llm_model"] == "gpt-4o"
        assert cfg["llm_url"] == "https://legacy/v1"
        depr = [w for w in warned if issubclass(w.category, DeprecationWarning)]
        assert any("CITATION_LLM_MODEL" in str(w.message) for w in depr)
        assert any("CITATION_LLM_URL" in str(w.message) for w in depr)

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
