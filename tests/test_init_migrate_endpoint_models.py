"""Tests for ``orchestrator.init._migrate_endpoint_models_to_catalog``.

The Phase D one-shot promotes system-scoped ``user_llm_endpoint_models``
rows into the admin-curated catalog and drops the legacy table. After it
runs once the table is gone; subsequent runs are no-ops. User-scoped rows
(``llm_endpoints.user_id IS NOT NULL``) are dropped without
migration. ``whisper`` / ``tts`` capabilities don't map to v1 catalog
roles and are skipped.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from orchestrator import init as init_mod


def _make_db(*, exists: bool, rows: list[dict] | None = None):
    """Stub PostgresDB exposing only what the migration touches."""
    conn = AsyncMock()
    # First call → table existence probe; subsequent fetches → SELECT rows.
    conn.fetchval = AsyncMock(return_value=exists)
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.execute = AsyncMock()

    db = MagicMock()
    db._conn = conn

    @asynccontextmanager
    async def _acquire():
        yield conn

    db.acquire = _acquire
    db.create_model = AsyncMock(
        return_value={"id": "00000000-0000-0000-0000-000000000001"}
    )
    return db


def _row(**overrides):
    base = {
        "endpoint_id": UUID("11111111-1111-1111-1111-111111111111"),
        "model_id": "RedHatAI/gemma-4-31B-it-FP8-Dynamic",
        "display_name": "Gemma",
        "family": "gemma",
        "context_window": 32000,
        "reasoning_level": None,
        "enabled": True,
        "capability": "chat",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_noop_when_table_does_not_exist():
    db = _make_db(exists=False)
    await init_mod._migrate_endpoint_models_to_catalog(db)
    db.create_model.assert_not_awaited()
    db._conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_promotes_chat_row_to_catalog_and_drops_table():
    db = _make_db(exists=True, rows=[_row()])
    await init_mod._migrate_endpoint_models_to_catalog(db)

    db.create_model.assert_awaited_once()
    kwargs = db.create_model.await_args.kwargs
    assert kwargs["provider_kind"] == "endpoint"
    assert kwargs["provider_ref"] == str(_row()["endpoint_id"])
    assert kwargs["role"] == "chat"
    assert kwargs["family"] == "gemma"
    assert kwargs["seeded_from"] == "migration:user_llm_endpoint_models"
    assert kwargs["on_conflict_do_nothing"] is True

    # Table is dropped at the end.
    drop_sql = db._conn.execute.await_args.args[0]
    assert "DROP TABLE IF EXISTS user_llm_endpoint_models" in drop_sql


@pytest.mark.asyncio
async def test_promotes_each_catalog_role():
    rows = [
        _row(model_id="aux-1", capability="auxiliary"),
        _row(model_id="emb-1", capability="embedding"),
        _row(model_id="vis-1", capability="vision"),
        _row(model_id="whi-1", capability="whisper"),
        _row(model_id="tts-1", capability="tts"),
    ]
    db = _make_db(exists=True, rows=rows)
    await init_mod._migrate_endpoint_models_to_catalog(db)

    roles_seen = {call.kwargs["role"] for call in db.create_model.await_args_list}
    assert roles_seen == {"auxiliary", "embedding", "vision", "whisper", "tts"}


@pytest.mark.asyncio
async def test_skips_unknown_capability_rows():
    """Rows whose capability isn't a recognized catalog role are dropped with
    the legacy table rather than promoted."""
    rows = [
        _row(model_id="ok-chat", capability="chat"),
        _row(model_id="skip-banana", capability="banana"),
        _row(model_id="skip-blank", capability=""),  # falls to default 'chat'
    ]
    db = _make_db(exists=True, rows=rows)
    await init_mod._migrate_endpoint_models_to_catalog(db)

    promoted_ids = {call.kwargs["model_id"] for call in db.create_model.await_args_list}
    assert promoted_ids == {"ok-chat", "skip-blank"}


@pytest.mark.asyncio
async def test_query_only_selects_system_scoped_rows():
    """The migration JOIN must filter ``e.user_id IS NULL`` so user-scoped
    rows are dropped with the table rather than promoted."""
    db = _make_db(exists=True, rows=[])
    await init_mod._migrate_endpoint_models_to_catalog(db)
    sql = db._conn.fetch.await_args.args[0]
    assert "e.user_id IS NULL" in sql


@pytest.mark.asyncio
async def test_family_falls_back_to_family_of_when_null():
    """When the legacy row's family is null, the migration uses the
    family_of() classifier so the catalog row carries a non-null family."""
    db = _make_db(
        exists=True,
        rows=[_row(model_id="claude-opus-4-7", family=None)],
    )
    await init_mod._migrate_endpoint_models_to_catalog(db)
    kwargs = db.create_model.await_args.kwargs
    # family_of("claude-opus-4-7") → "claude-opus"
    assert kwargs["family"] == "claude-opus"
