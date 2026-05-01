"""Tests for ``orchestrator.init._seed_models_from_helm``.

The seed step reads ``helm/values.yaml``'s ``llm.seed.systemModels[]`` block
(plus a legacy ``config/models.yaml`` bridge that drops out in chunk 7) and
delegates to :func:`orchestrator.seed.llm_config.seed`. These tests pin:
provider-gating (skip rows for unseeded providers), the ``local`` provider
is excluded from the legacy bridge, the auxiliary / vision / embedding helper
lists map to non-chat capabilities, and re-runs are no-ops thanks to
``ON CONFLICT DO NOTHING``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import init as init_mod


def _fake_db(*, seeded_providers: list[str] | None = None):
    db = MagicMock()
    db.list_system_api_keys = AsyncMock(
        return_value=[{"provider": p} for p in (seeded_providers or [])]
    )
    db.get_system_api_key = AsyncMock(return_value=None)
    db.create_model = AsyncMock(
        return_value={"id": "00000000-0000-0000-0000-000000000001"}
    )
    return db


@pytest.mark.asyncio
async def test_no_keys_seeded_inserts_nothing():
    """No system_api_keys rows present → no catalog rows inserted."""
    db = _fake_db(seeded_providers=[])
    await init_mod._seed_models_from_helm(db)
    db.create_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_openai_seeded_only_openai_rows_inserted():
    """One provider seeded → only that provider's catalog rows are inserted.

    The legacy bridge consumes ``config/models.yaml`` which ships at least
    two openai chat entries (gpt-4o, gpt-4o-mini) plus auxiliary/vision/
    embedding entries. Anthropic / Google / Groq / OpenRouter rows must be
    skipped.
    """
    db = _fake_db(seeded_providers=["openai"])
    await init_mod._seed_models_from_helm(db)

    inserted_providers = {
        call.kwargs["provider_ref"] for call in db.create_model.await_args_list
    }
    assert inserted_providers == {"openai"}, (
        f"Expected only openai inserts, got {inserted_providers}"
    )
    assert db.create_model.await_count >= 2  # at least gpt-4o + gpt-4o-mini chat rows


@pytest.mark.asyncio
async def test_local_provider_never_seeded():
    """The 'local' YAML provider routes through llm_endpoints, not
    system_api_keys — it must never produce catalog rows even if 'local'
    somehow ends up in the seeded provider set."""
    db = _fake_db(seeded_providers=["local", "openai"])
    await init_mod._seed_models_from_helm(db)

    inserted_providers = {
        call.kwargs["provider_ref"] for call in db.create_model.await_args_list
    }
    assert "local" not in inserted_providers


@pytest.mark.asyncio
async def test_helper_lists_map_to_non_chat_capabilities():
    """auxiliary_models / vision_models / embedding_models YAML keys must
    insert with the corresponding capability (not 'chat').

    With openai seeded, the YAML contributes openai entries to all three
    helper lists. We assert at least one of each non-chat capability appears.
    """
    db = _fake_db(seeded_providers=["openai"])
    await init_mod._seed_models_from_helm(db)

    inserted_capabilities = {
        call.kwargs["capability"] for call in db.create_model.await_args_list
    }
    assert {"chat", "auxiliary", "vision", "embedding"}.issubset(inserted_capabilities)


@pytest.mark.asyncio
async def test_all_inserts_pass_on_conflict_do_nothing():
    """Every insert must use on_conflict_do_nothing=True so re-runs are
    safe and admin edits are never clobbered."""
    db = _fake_db(seeded_providers=["openai"])
    await init_mod._seed_models_from_helm(db)

    for call in db.create_model.await_args_list:
        assert call.kwargs.get("on_conflict_do_nothing") is True


@pytest.mark.asyncio
async def test_seeded_from_breadcrumb_set():
    """All inserts must carry seeded_from='helm:llm.seed' so the catalog can
    distinguish helm-seed from admin-added rows."""
    db = _fake_db(seeded_providers=["openai"])
    await init_mod._seed_models_from_helm(db)

    breadcrumbs = {
        call.kwargs.get("seeded_from") for call in db.create_model.await_args_list
    }
    assert breadcrumbs == {"helm:llm.seed"}


@pytest.mark.asyncio
async def test_idempotent_when_create_model_returns_none():
    """When all rows already exist (create_model returns None), the function
    completes without error and reports zero inserts."""
    db = _fake_db(seeded_providers=["openai"])
    db.create_model = AsyncMock(return_value=None)  # simulate ON CONFLICT skip

    # Should not raise.
    await init_mod._seed_models_from_helm(db)
    # Still attempted the inserts.
    assert db.create_model.await_count >= 2
