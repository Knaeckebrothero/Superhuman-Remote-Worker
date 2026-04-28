"""Tests for ``PostgresDB.resolve_default_for_role``.

The resolver returns the admin pin when it points at an enabled catalog row,
falls back to the first-enabled-alphabetical catalog row when the pin is
missing or dangling, and returns None only when no pin exists AND no enabled
catalog row is available. Every catalog role — chat, auxiliary, embedding,
vision, whisper, tts — goes through the same validation path.
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
    for role in ("chat", "auxiliary", "embedding", "vision", "whisper", "tts"):
        assert await db.resolve_default_for_role(role) == "alpha"
        db.list_models_by_role_alphabetical.assert_awaited_with(role)


@pytest.mark.asyncio
async def test_whisper_pin_validates_against_catalog():
    """Whisper joined the catalog in v1.1 — pin is now validated against
    ``resolve_catalog_model`` like every other role."""
    db = _db(
        pin="whisper-1",
        catalog_resolves_to={"model_id": "whisper-1", "enabled": True},
    )
    assert await db.resolve_default_for_role("whisper") == "whisper-1"
    db.resolve_catalog_model.assert_awaited_once_with("whisper-1", role="whisper")


@pytest.mark.asyncio
async def test_tts_returns_none_when_no_pin_and_no_catalog_rows():
    """Without a pin and without catalog rows, tts resolves to None — same
    contract as the other catalog roles."""
    db = _db(pin=None, alphabetical=[])
    assert await db.resolve_default_for_role("tts") is None
