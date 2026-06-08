"""Phase 1 of docs/issues/persistent_session_midturn_message_loss.md.

The persistent agent now writes thread_messages straight through its own pool
(``PostgresDB.save_thread_message``) instead of hopping through the orchestrator
REST endpoint. These tests pin the mechanics that the resume fix keys off:

  * the row carries a caller-supplied, idempotent ``id`` (``ON CONFLICT (id)``);
  * non-UUID provider/minted ids are coerced deterministically to the UUID PK;
  * ``seq`` is returned in the same round-trip;
  * the ``threads`` activity/turn bump still happens (no regression vs the
    orchestrator path).
"""

import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.database.postgres_db import (
    PostgresDB,
    _coerce_row_id,
    _THREAD_MSG_ID_NS,
)


def _fake_db(returning):
    """A PostgresDB whose ``acquire()`` yields a fake connection."""
    db = PostgresDB.__new__(PostgresDB)  # bypass __init__/real pool
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=returning)
    conn.execute = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    db.acquire = MagicMock(return_value=cm)
    return db, conn


@pytest.mark.asyncio
async def test_returns_id_and_seq_and_upserts():
    db, conn = _fake_db({"id": "11111111-1111-1111-1111-111111111111", "seq": 42})
    result = await db.save_thread_message(
        thread_id="t1",
        role="ai",
        content="hi",
        turn_number=3,
        id="11111111-1111-1111-1111-111111111111",
    )
    # seq is handed back in the same call (the resume cursor source).
    assert result == {"id": "11111111-1111-1111-1111-111111111111", "seq": 42}

    sql = " ".join(conn.fetchrow.call_args[0][0].split())
    assert "ON CONFLICT (id) DO UPDATE" in sql, "must upsert, not plain insert"
    assert "RETURNING id, seq" in sql, "must return the seq cursor"
    # id is the first bound param (a valid UUID passes through unchanged).
    assert conn.fetchrow.call_args[0][1] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_provider_id_coerced_to_deterministic_uuid():
    """A non-UUID provider id (``chatcmpl-…``) must become a valid, stable UUID
    so the incremental write + turn-complete reconciliation hit one row."""
    db, conn = _fake_db({"id": "x", "seq": 1})
    await db.save_thread_message(
        thread_id="t1", role="ai", content="hi", turn_number=1, id="chatcmpl-abc123"
    )
    bound_id = conn.fetchrow.call_args[0][1]
    # Parses as a UUID (would have raised in asyncpg otherwise).
    uuid.UUID(bound_id)
    # Deterministic: same message id → same row id (idempotent upsert).
    assert bound_id == str(uuid.uuid5(_THREAD_MSG_ID_NS, "chatcmpl-abc123"))


@pytest.mark.asyncio
async def test_none_id_mints_fresh_uuid():
    db, conn = _fake_db({"id": "x", "seq": 1})
    await db.save_thread_message(
        thread_id="t1", role="user", content="hello", turn_number=1
    )
    bound_id = conn.fetchrow.call_args[0][1]
    uuid.UUID(bound_id)  # a valid, freshly-minted UUID


@pytest.mark.asyncio
async def test_bumps_thread_activity():
    db, conn = _fake_db({"id": "x", "seq": 1})
    await db.save_thread_message(thread_id="t1", role="ai", content="hi", turn_number=5)
    # The threads activity/turn update still runs (parity with orchestrator path).
    assert conn.execute.await_count == 1
    upd = " ".join(conn.execute.call_args[0][0].split())
    assert "UPDATE threads" in upd
    assert "last_activity" in upd and "total_turns" in upd


@pytest.mark.asyncio
async def test_component_columns_present():
    db, conn = _fake_db({"id": "x", "seq": 1})
    await db.save_thread_message(
        thread_id="t1",
        role="ai",
        content="hi",
        turn_number=1,
        provider="openai-chat",
        provider_raw={"object": "chat.completion", "id": "cc_1"},
        reasoning="thinking",
        id="11111111-1111-1111-1111-111111111111",
    )
    sql = conn.fetchrow.call_args[0][0]
    assert "provider" in sql and "provider_raw" in sql and "reasoning" in sql
    # JSON-encoded provider_raw reached the positional args.
    assert any(isinstance(a, str) and "cc_1" in a for a in conn.fetchrow.call_args[0])


def test_coerce_row_id_passthrough_and_stability():
    valid = "22222222-2222-2222-2222-222222222222"
    assert _coerce_row_id(valid) == valid  # already a UUID → unchanged
    # Non-UUID → deterministic across calls.
    assert _coerce_row_id("msg_xyz") == _coerce_row_id("msg_xyz")
    # None → fresh + valid, and distinct from the derived ones.
    minted = _coerce_row_id(None)
    uuid.UUID(minted)
    assert minted != _coerce_row_id(None)
