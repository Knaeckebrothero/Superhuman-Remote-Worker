"""Tests for ``main._create_builder_llm`` after the DB-resolver migration.

Pre-migration: the function fell through ``BUILDER_*_API_KEY`` →
``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` env vars when no custom/system
endpoint matched. Post-migration: the bare-name env reads are gone for
built-in catalog models — the key is resolved from ``system_api_keys`` by
inferred provider, and a missing row raises ``RuntimeError`` with a
seed-hint message. ``codex/*`` keeps its ``CODEX_API_KEY`` env path
because codex auth is user-bound via the proxy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mirror test_admin_providers_api.py's pattern for importing main.
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402


@pytest.fixture
def patched_main(monkeypatch):
    """Patch the heavy collaborators of ``_create_builder_llm`` so the test
    only exercises the api-key resolution branch."""

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(return_value=None), raising=True
    )
    monkeypatch.setattr(
        main, "resolve_builder_settings", MagicMock(return_value={}), raising=True
    )
    monkeypatch.setattr(
        main,
        "get_builder_base_url",
        AsyncMock(return_value="https://api.example/v1"),
        raising=True,
    )

    # Capture the LLMConfig that the function would feed to ``create_llm``.
    captured = {}

    def fake_create_llm(config):
        captured["config"] = config
        return "fake-llm"

    # ``create_llm`` is imported lazily inside _create_builder_llm via
    # ``from src.core.loader import LLMConfig, create_llm`` so we patch on
    # the source module.
    from src.core import loader as core_loader

    monkeypatch.setattr(core_loader, "create_llm", fake_create_llm, raising=True)
    return captured


class TestProviderInference:
    @pytest.mark.asyncio
    async def test_claude_resolves_anthropic_key(self, patched_main, monkeypatch):
        get_key = AsyncMock(return_value="sk-ant-resolved")
        monkeypatch.setattr(main.postgres_db, "get_system_api_key", get_key)
        await main._create_builder_llm("claude-opus-4-6")
        get_key.assert_awaited_once_with("anthropic")
        assert patched_main["config"].api_key == "sk-ant-resolved"

    @pytest.mark.asyncio
    async def test_openrouter_resolves_openrouter_key(self, patched_main, monkeypatch):
        get_key = AsyncMock(return_value="sk-or-v1-x")
        monkeypatch.setattr(main.postgres_db, "get_system_api_key", get_key)
        await main._create_builder_llm("openrouter/anthropic/claude")
        get_key.assert_awaited_once_with("openrouter")
        assert patched_main["config"].api_key == "sk-or-v1-x"

    @pytest.mark.asyncio
    async def test_gpt_resolves_openai_key(self, patched_main, monkeypatch):
        get_key = AsyncMock(return_value="sk-openai")
        monkeypatch.setattr(main.postgres_db, "get_system_api_key", get_key)
        await main._create_builder_llm("gpt-4o")
        get_key.assert_awaited_once_with("openai")
        assert patched_main["config"].api_key == "sk-openai"


class TestCodexStaysOnEnv:
    @pytest.mark.asyncio
    async def test_codex_uses_env(self, patched_main, monkeypatch):
        # If get_system_api_key were called, it would blow up — codex must
        # never reach the DB lookup.
        bad_lookup = AsyncMock(side_effect=RuntimeError("codex must not hit DB"))
        monkeypatch.setattr(main.postgres_db, "get_system_api_key", bad_lookup)
        monkeypatch.setenv("CODEX_API_KEY", "codex-token-xyz")
        await main._create_builder_llm("codex/gpt-5.4")
        bad_lookup.assert_not_called()
        assert patched_main["config"].api_key == "codex-token-xyz"

    @pytest.mark.asyncio
    async def test_codex_default_when_unset(self, patched_main, monkeypatch):
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setattr(
            main.postgres_db,
            "get_system_api_key",
            AsyncMock(side_effect=RuntimeError("codex must not hit DB")),
        )
        await main._create_builder_llm("codex/gpt-5.4")
        # Empty/unset env still produces a placeholder so a keyless local
        # codex proxy doesn't raise — matches pre-migration behavior.
        assert patched_main["config"].api_key == "not-needed"


class TestMissingKeyRaisesWithSeedHint:
    @pytest.mark.asyncio
    async def test_missing_anthropic_row_raises(self, patched_main, monkeypatch):
        monkeypatch.setattr(
            main.postgres_db,
            "get_system_api_key",
            AsyncMock(return_value=None),
        )
        with pytest.raises(RuntimeError) as exc:
            await main._create_builder_llm("claude-sonnet-4-6")
        assert "anthropic" in str(exc.value)
        assert "SEED_ANTHROPIC_API_KEY" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_openai_row_raises(self, patched_main, monkeypatch):
        monkeypatch.setattr(
            main.postgres_db,
            "get_system_api_key",
            AsyncMock(return_value=None),
        )
        with pytest.raises(RuntimeError) as exc:
            await main._create_builder_llm("gpt-4o")
        assert "SEED_OPENAI_API_KEY" in str(exc.value)


class TestEndpointRowStillWins:
    """When the model resolves to a custom/system endpoint, the DB lookup
    is skipped and the inline endpoint key is used."""

    @pytest.mark.asyncio
    async def test_endpoint_key_used_directly(self, patched_main, monkeypatch):
        endpoint_meta = MagicMock()
        endpoint_meta.origin = "system"
        endpoint_meta.endpoint_id = "ep-1"
        monkeypatch.setattr(
            main, "_resolve_model", AsyncMock(return_value=endpoint_meta)
        )
        monkeypatch.setattr(
            main.postgres_db,
            "get_user_llm_endpoint",
            AsyncMock(
                return_value={
                    "label": "Custom",
                    "base_url": "http://vllm.svc/v1",
                    "api_key": "ep-key",
                }
            ),
        )
        bad_lookup = AsyncMock(
            side_effect=RuntimeError("endpoint path must not hit system_api_keys")
        )
        monkeypatch.setattr(main.postgres_db, "get_system_api_key", bad_lookup)

        await main._create_builder_llm("RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        bad_lookup.assert_not_called()
        assert patched_main["config"].api_key == "ep-key"
        assert patched_main["config"].base_url == "http://vllm.svc/v1"
