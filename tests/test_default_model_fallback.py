"""Tests for ``PostgresDB.resolve_default_for_role``.

The resolver returns the admin pin when it points at an enabled catalog row,
falls back to the first-enabled-alphabetical catalog row when the pin is
missing or dangling, and returns None only when no pin exists AND no enabled
catalog row is available. Non-catalog kinds (whisper, tts) bypass validation
and return the pin verbatim.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.database.postgres import PostgresDB


def _db(
    *,
    pin: str | None = None,
    catalog_resolves_to: dict | None = None,
    alphabetical: list[dict] | None = None,
) -> PostgresDB:
    """Build a stub PostgresDB exposing only the methods resolve_default_for_role
    consults. Avoids spinning up an actual connection pool."""
    db = PostgresDB.__new__(PostgresDB)
    db.get_default_llm_model = AsyncMock(return_value=pin)
    db.resolve_catalog_model = AsyncMock(return_value=catalog_resolves_to)
    db.list_models_by_role_alphabetical = AsyncMock(return_value=alphabetical or [])
    return db


@pytest.mark.asyncio
async def test_returns_pin_when_pin_resolves_to_enabled_catalog_row():
    db = _db(
        pin="claude-opus-4-7",
        catalog_resolves_to={"model_id": "claude-opus-4-7", "enabled": True},
    )
    assert await db.resolve_default_for_role("auxiliary") == "claude-opus-4-7"
    db.list_models_by_role_alphabetical.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_alphabetical_first_when_pin_is_missing_from_catalog():
    db = _db(
        pin="ghost-model-not-in-catalog",
        catalog_resolves_to=None,  # resolver returned no row
        alphabetical=[
            {"model_id": "alpha-model", "display_label": "Alpha"},
            {"model_id": "zeta-model", "display_label": "Zeta"},
        ],
    )
    assert await db.resolve_default_for_role("auxiliary") == "alpha-model"


@pytest.mark.asyncio
async def test_returns_alphabetical_first_when_pin_resolves_to_disabled_row():
    db = _db(
        pin="legacy-model",
        catalog_resolves_to={"model_id": "legacy-model", "enabled": False},
        alphabetical=[{"model_id": "alpha-model", "display_label": "Alpha"}],
    )
    assert await db.resolve_default_for_role("auxiliary") == "alpha-model"


@pytest.mark.asyncio
async def test_returns_alphabetical_first_when_no_pin_set():
    db = _db(
        pin=None,
        alphabetical=[{"model_id": "alpha-model", "display_label": "Alpha"}],
    )
    assert await db.resolve_default_for_role("auxiliary") == "alpha-model"
    # Catalog validation is skipped entirely when no pin exists.
    db.resolve_catalog_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_when_no_pin_and_no_catalog_rows():
    db = _db(pin=None, alphabetical=[])
    assert await db.resolve_default_for_role("auxiliary") is None


@pytest.mark.asyncio
async def test_each_catalog_role_resolves_independently():
    db = _db(
        pin=None,
        alphabetical=[{"model_id": "alpha", "display_label": "Alpha"}],
    )
    for role in ("chat", "auxiliary", "embedding", "vision"):
        assert await db.resolve_default_for_role(role) == "alpha"
        db.list_models_by_role_alphabetical.assert_awaited_with(role)


@pytest.mark.asyncio
async def test_whisper_kind_returns_pin_verbatim():
    """Whisper isn't in the v1 catalog role enum — pin passes through with
    no validation and no fallback."""
    db = _db(pin="whisper-1")
    assert await db.resolve_default_for_role("whisper") == "whisper-1"
    db.resolve_catalog_model.assert_not_awaited()
    db.list_models_by_role_alphabetical.assert_not_awaited()


@pytest.mark.asyncio
async def test_whisper_kind_returns_none_when_no_pin():
    """Without a pin AND outside the catalog roles, the resolver has nothing
    to fall back to and must return None."""
    db = _db(pin=None)
    assert await db.resolve_default_for_role("tts") is None
    db.resolve_catalog_model.assert_not_awaited()
    db.list_models_by_role_alphabetical.assert_not_awaited()
