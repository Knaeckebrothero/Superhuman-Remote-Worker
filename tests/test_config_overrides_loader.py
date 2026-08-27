"""Tests for DB-backed prompt overrides in src/core/loader.py (v1).

Pure unit tests — no DB. Exercise the process-local override map, the
flag gate, the _db_lookup precedence (family > global), and the
MatrixResolver.load hook.
"""

import pytest

import src.core.loader as loader


def _reset():
    loader.clear_config_overrides()


def test_db_lookup_returns_none_when_flag_off(monkeypatch):
    _reset()
    monkeypatch.delenv("CONFIG_DB_OVERRIDES_ENABLED", raising=False)
    loader.set_config_overrides(
        [
            {"family": "gemma", "kind": "prompts", "name": "persona", "content": "X"},
        ]
    )
    assert loader._db_lookup("prompts", "gemma", "persona") is None


def test_db_lookup_family_specific_hit(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "prompts",
                "name": "persona",
                "content": "GEMMA",
            },
        ]
    )
    assert loader._db_lookup("prompts", "gemma", "persona") == "GEMMA"
    assert (
        loader._db_lookup("prompts", "gpt_5", "persona") is None
    )  # other family: miss


def test_db_lookup_global_fallback_and_precedence(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "1")
    loader.set_config_overrides(
        [
            {"family": None, "kind": "prompts", "name": "persona", "content": "GLOBAL"},
            {
                "family": "gemma",
                "kind": "prompts",
                "name": "persona",
                "content": "GEMMA",
            },
        ]
    )
    assert (
        loader._db_lookup("prompts", "gpt_5", "persona") == "GLOBAL"
    )  # falls back to global
    assert (
        loader._db_lookup("prompts", "gemma", "persona") == "GEMMA"
    )  # family beats global


def test_clear_overrides(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {"family": "gemma", "kind": "prompts", "name": "persona", "content": "X"},
        ]
    )
    loader.clear_config_overrides()
    assert loader._db_lookup("prompts", "gemma", "persona") is None


def test_matrix_load_prefers_override_then_bundled(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import PromptMatrixResolver

    resolver = PromptMatrixResolver(None, "gemma")
    # Stub the bundled path so the test doesn't depend on real config files.
    # load() reads bundled content via _resolve_path().read_text() (location-
    # primary resolution), so point that at a temp file rather than the shipped
    # persona.txt.
    bundled = tmp_path / "persona.txt"
    bundled.write_text("BUNDLED", encoding="utf-8")
    monkeypatch.setattr(resolver, "_resolve_path", lambda et: bundled)

    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "prompts",
                "name": "persona",
                "content": "OVERRIDE",
            },
        ]
    )
    assert resolver.load("persona") == "OVERRIDE"  # override wins, no file read
    assert (
        resolver.load("persona", bundled_only=True) == "BUNDLED"
    )  # bypasses overrides


@pytest.mark.asyncio
async def test_config_overrides_namespace_lists_family_and_global():
    from unittest.mock import AsyncMock, MagicMock

    from src.database.postgres_db import ConfigOverridesNamespace

    fake_db = MagicMock()
    fake_db.fetch = AsyncMock(
        return_value=[
            {
                "family": "gemma",
                "kind": "prompts",
                "name": "persona",
                "content": "X",
                "content_format": "text",
                "value_json": None,
            },
        ]
    )
    fake_db._row_to_dict = lambda r: dict(r)

    ns = ConfigOverridesNamespace(fake_db)
    rows = await ns.list_overrides_for_family("gemma")

    assert rows == [
        {
            "family": "gemma",
            "kind": "prompts",
            "name": "persona",
            "content": "X",
            "content_format": "text",
            "value_json": None,
        }
    ]
    sql = fake_db.fetch.call_args.args[0]
    assert "FROM config_overrides" in sql
    assert "value_json" in sql
    assert "family = $1 OR family IS NULL" in sql
    assert fake_db.fetch.call_args.args[1] == "gemma"


# ---------------------------------------------------------------------------
# Structured (settings / guardrails) override accessors — Phase B.
# ---------------------------------------------------------------------------


def test_settings_override_assembles_and_merges(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {
                "family": None,
                "kind": "settings",
                "name": "temperature",
                "value_json": 0.7,
            },
            {
                "family": "gemma",
                "kind": "settings",
                "name": "temperature",
                "value_json": 1.0,
            },
            {
                "family": "gemma",
                "kind": "settings",
                "name": "limits.context_threshold_tokens",
                "value_json": 120000,
            },
        ]
    )
    assert loader._settings_override_for("gemma") == {
        "temperature": 1.0,
        "limits": {"context_threshold_tokens": 120000},
    }
    assert loader._settings_override_for("other") == {"temperature": 0.7}


def test_guardrails_override_merges(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {
                "family": None,
                "kind": "guardrails",
                "name": "guardrails",
                "value_json": {"nudges": {"a": 1}},
            },
            {
                "family": "gemma",
                "kind": "guardrails",
                "name": "guardrails",
                "value_json": {"tool_examples": {"b": 2}},
            },
        ]
    )
    assert loader._guardrails_override_for("gemma") == {
        "nudges": {"a": 1},
        "tool_examples": {"b": 2},
    }


def test_structured_overrides_empty_when_flag_off(monkeypatch):
    _reset()
    monkeypatch.delenv("CONFIG_DB_OVERRIDES_ENABLED", raising=False)
    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "settings",
                "name": "temperature",
                "value_json": 1.0,
            }
        ]
    )
    assert loader._settings_override_for("gemma") == {}
    assert loader._guardrails_override_for("gemma") == {}


def test_set_overrides_parses_jsonb_string(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "settings",
                "name": "temperature",
                "value_json": "1.0",
            }
        ]
    )
    assert loader._settings_override_for("gemma") == {"temperature": 1.0}


def test_resolve_model_settings_applies_override(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "settings",
                "name": "temperature",
                "value_json": 1.0,
            }
        ]
    )
    assert loader.resolve_model_settings("google/gemma-4-31b")["temperature"] == 1.0
    assert (
        loader.resolve_model_settings("google/gemma-4-31b", bundled_only=True)[
            "temperature"
        ]
        != 1.0
    )


def test_resolve_guardrails_applies_override(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "guardrails",
                "name": "guardrails",
                "value_json": {"nudges": {"extra": "be careful"}},
            }
        ]
    )
    assert (
        loader.resolve_guardrails("google/gemma-4-31b")["nudges"]["extra"]
        == "be careful"
    )
    assert (
        loader.resolve_guardrails("google/gemma-4-31b", bundled_only=True)
        .get("nudges", {})
        .get("extra")
        is None
    )


def test_apply_settings_overrides_mutates_config(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import (
        AgentConfig,
        LimitsConfig,
        LLMConfig,
        apply_settings_overrides,
    )

    cfg = AgentConfig(
        agent_id="t",
        display_name="t",
        llm=LLMConfig(model="google/gemma-4-31b", temperature=0.0),
        limits=LimitsConfig(),
    )
    loader.set_config_overrides(
        [
            {
                "family": "gemma",
                "kind": "settings",
                "name": "temperature",
                "value_json": 1.0,
            },
            {
                "family": "gemma",
                "kind": "settings",
                "name": "limits.context_threshold_tokens",
                "value_json": 123000,
            },
        ]
    )
    assert apply_settings_overrides(cfg) is True
    assert cfg.llm.temperature == 1.0
    assert cfg.limits.context_threshold_tokens == 123000


def test_apply_settings_overrides_noop_without_rows(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import (
        AgentConfig,
        LimitsConfig,
        LLMConfig,
        apply_settings_overrides,
    )

    cfg = AgentConfig(
        agent_id="t",
        display_name="t",
        llm=LLMConfig(model="google/gemma-4-31b", temperature=0.3),
        limits=LimitsConfig(),
    )
    assert apply_settings_overrides(cfg) is False
    assert cfg.llm.temperature == 0.3
