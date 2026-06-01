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


def test_matrix_load_prefers_override_then_bundled(monkeypatch):
    _reset()
    monkeypatch.setenv("CONFIG_DB_OVERRIDES_ENABLED", "true")
    from src.core.loader import PromptMatrixResolver

    resolver = PromptMatrixResolver(None, "gemma")
    # Stub the bundled path so the test doesn't depend on real config files.
    monkeypatch.setattr(resolver, "resolve_filename", lambda et: "persona.txt")
    monkeypatch.setattr(resolver._file_resolver, "load", lambda fn: "BUNDLED")

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
        }
    ]
    sql = fake_db.fetch.call_args.args[0]
    assert "FROM config_overrides" in sql
    assert "family = $1 OR family IS NULL" in sql
    assert fake_db.fetch.call_args.args[1] == "gemma"
