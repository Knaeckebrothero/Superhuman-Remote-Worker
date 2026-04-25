"""Tests for ``orchestrator.init._seed_llm_keys_from_env``.

The init step builds a payload from ``SEED_<PROVIDER>_API_KEY`` env vars and
hands it to ``orchestrator.seed.llm_config.seed`` — the same insert-only
function the helm seeder Job uses. These tests pin: env-driven payload
construction, the legacy ``vision`` slot is excluded, and re-runs are
no-ops when rows already exist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import init as init_mod


def _fake_db(*, existing_providers: list[str] | None = None):
    db = MagicMock()
    db.list_system_api_keys = AsyncMock(
        return_value=[{"provider": p} for p in (existing_providers or [])]
    )
    db.list_system_llm_endpoints = AsyncMock(return_value=[])
    db.upsert_system_api_key = AsyncMock()
    return db


def _fake_db_with_defaults(
    *,
    openrouter_key: str | None = None,
    existing_defaults: dict[str, str] | None = None,
):
    """Mock for the openrouter-default path. Tracks set_default_llm_model
    calls so tests can assert which slots got pinned."""
    db = MagicMock()
    db.get_system_api_key = AsyncMock(
        side_effect=lambda provider: openrouter_key
        if provider == "openrouter"
        else None
    )
    defaults = dict(existing_defaults or {})
    db.get_default_llm_model = AsyncMock(side_effect=defaults.get)
    db.set_default_llm_model = AsyncMock()
    db._defaults_track = defaults
    return db


@pytest.mark.asyncio
async def test_no_seed_env_vars_is_noop(monkeypatch):
    """With no SEED_* env vars set, the seeder isn't invoked."""
    for provider in ("openai", "anthropic", "google", "groq", "openrouter", "tavily"):
        monkeypatch.delenv(f"SEED_{provider.upper()}_API_KEY", raising=False)

    db = _fake_db()
    await init_mod._seed_llm_keys_from_env(db)
    db.upsert_system_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_provider_seeded(monkeypatch):
    for provider in ("openai", "anthropic", "google", "groq", "openrouter", "tavily"):
        monkeypatch.delenv(f"SEED_{provider.upper()}_API_KEY", raising=False)
    monkeypatch.setenv("SEED_OPENAI_API_KEY", "sk-test")

    db = _fake_db()
    await init_mod._seed_llm_keys_from_env(db)

    db.upsert_system_api_key.assert_awaited_once()
    kwargs = db.upsert_system_api_key.await_args.kwargs
    assert kwargs["provider"] == "openai"
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["seeded_from"] == "helm:llm.seed"


@pytest.mark.asyncio
async def test_existing_row_skipped(monkeypatch):
    for provider in ("openai", "anthropic", "google", "groq", "openrouter", "tavily"):
        monkeypatch.delenv(f"SEED_{provider.upper()}_API_KEY", raising=False)
    monkeypatch.setenv("SEED_OPENAI_API_KEY", "sk-rotated")

    db = _fake_db(existing_providers=["openai"])
    await init_mod._seed_llm_keys_from_env(db)

    # Existing row wins — admin-UI rotation is never clobbered by
    # subsequent boots.
    db.upsert_system_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_vision_legacy_slot_never_seeded(monkeypatch):
    """``vision`` is in VALID_SYSTEM_API_KEY_PROVIDERS for legacy reasons but
    must never be seeded — vision keys flow through the per-endpoint inline
    api_key on a custom endpoint row."""
    for provider in ("openai", "anthropic", "google", "groq", "openrouter", "tavily"):
        monkeypatch.delenv(f"SEED_{provider.upper()}_API_KEY", raising=False)
    monkeypatch.setenv("SEED_VISION_API_KEY", "should-be-ignored")

    db = _fake_db()
    await init_mod._seed_llm_keys_from_env(db)
    db.upsert_system_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_providers_seeded(monkeypatch):
    for provider in ("openai", "anthropic", "google", "groq", "openrouter", "tavily"):
        monkeypatch.delenv(f"SEED_{provider.upper()}_API_KEY", raising=False)
    monkeypatch.setenv("SEED_OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("SEED_ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("SEED_TAVILY_API_KEY", "tvly-x")

    db = _fake_db()
    await init_mod._seed_llm_keys_from_env(db)

    assert db.upsert_system_api_key.await_count == 3
    seeded_providers = {
        call.kwargs["provider"] for call in db.upsert_system_api_key.await_args_list
    }
    assert seeded_providers == {"openai", "anthropic", "tavily"}


# ---------------------------------------------------------------------------
# OpenRouter default-pinning
# ---------------------------------------------------------------------------


class TestApplyOpenrouterDefaults:
    @pytest.mark.asyncio
    async def test_no_openrouter_key_is_noop(self):
        db = _fake_db_with_defaults(openrouter_key=None)
        await init_mod._apply_openrouter_defaults(db)
        db.set_default_llm_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pins_auxiliary_and_embedding_when_slots_empty(self):
        db = _fake_db_with_defaults(openrouter_key="sk-or-v1-xxx")
        await init_mod._apply_openrouter_defaults(db)
        assert db.set_default_llm_model.await_count == 2
        kinds_pinned = {
            call.args[0] for call in db.set_default_llm_model.await_args_list
        }
        assert kinds_pinned == {"auxiliary", "embedding"}

    @pytest.mark.asyncio
    async def test_does_not_clobber_existing_default(self):
        db = _fake_db_with_defaults(
            openrouter_key="sk-or-v1-xxx",
            existing_defaults={"auxiliary": "groq/admin-pinned"},
        )
        await init_mod._apply_openrouter_defaults(db)
        # Only embedding gets set — auxiliary already had a value.
        assert db.set_default_llm_model.await_count == 1
        only_call = db.set_default_llm_model.await_args_list[0]
        assert only_call.args[0] == "embedding"

    @pytest.mark.asyncio
    async def test_pinned_auxiliary_routes_through_openrouter(self):
        db = _fake_db_with_defaults(openrouter_key="sk-or-v1-xxx")
        await init_mod._apply_openrouter_defaults(db)
        for call in db.set_default_llm_model.await_args_list:
            kind, model = call.args
            assert model.startswith("openrouter/"), (
                f"expected openrouter/ prefix on {kind} default, got {model}"
            )
