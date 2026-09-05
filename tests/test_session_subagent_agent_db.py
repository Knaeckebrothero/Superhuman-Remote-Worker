"""Agent-side direct writers for thread-parent subagents."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

import agent.database.postgres_db as agent_postgres
from agent.database.postgres_db import PostgresDB
from shared.session_subagent_authority import SessionParentAuthority

PARENT = UUID("aaaaaaaa-1111-4222-8333-444444444444")
CHILD = UUID("bbbbbbbb-1111-4222-8333-444444444444")
GENERATION = UUID("cccccccc-1111-4222-8333-444444444444")
NEXT_GENERATION = UUID("dddddddd-1111-4222-8333-444444444444")
AGENT = UUID("eeeeeeee-1111-4222-8333-444444444444")
ATTACH = UUID("ffffffff-1111-4222-8333-444444444444")

AUTHORITY = SessionParentAuthority(
    execution_lane="pinned",
    parent_thread_id=PARENT,
    agent_id=AGENT,
    pod_uid="pod-uid",
    session_runtime_generation=GENERATION,
    runtime_attach_token=ATTACH,
)


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _conn() -> AsyncMock:
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _db(conn: AsyncMock) -> PostgresDB:
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._queries = {}

    @asynccontextmanager
    async def acquire():
        yield conn

    db.acquire = acquire
    return db


@pytest.fixture(autouse=True)
def authority_gate(monkeypatch):
    gate = AsyncMock(
        return_value={
            "id": PARENT,
            "kind": "session",
            "execution_lane": "pinned",
        }
    )
    monkeypatch.setattr(agent_postgres, "require_session_parent_authority", gate)
    return gate


@pytest.mark.asyncio
async def test_authority_probe_uses_only_captured_session_identity(authority_gate):
    conn = _conn()
    db = _db(conn)

    assert await db.session_parent_authority_current(AUTHORITY) is True

    authority_gate.assert_awaited_once_with(conn, AUTHORITY, parent_thread_id=PARENT)
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_transcript_locks_parent_then_exact_child_generation(
    authority_gate,
):
    conn = _conn()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"runtime_generation": GENERATION, "status": "active"},
            {"id": CHILD, "seq": 8},
        ]
    )
    db = _db(conn)

    saved = await db.save_session_subagent_thread_message(
        str(CHILD),
        parent_thread_id=str(PARENT),
        parent_authority=AUTHORITY,
        runtime_generation=str(GENERATION),
        role="ai",
        content="answer",
        id="provider-message",
        turn_number=2,
    )

    assert saved == {"id": str(CHILD), "seq": 8}
    authority_gate.assert_awaited_once()
    child_call, upsert_call = conn.fetchrow.await_args_list
    child_sql = _compact(child_call.args[0])
    assert "parent_job_id IS NULL" in child_sql
    assert "parent_thread_id = $2::uuid" in child_sql
    assert child_sql.endswith("FOR UPDATE")
    assert child_call.args[1:] == (CHILD, PARENT)
    assert upsert_call.args[1] == agent_postgres._coerce_row_id("provider-message")
    assert upsert_call.args[2] == str(CHILD)


@pytest.mark.asyncio
async def test_stale_or_cross_parent_generation_writes_no_transcript():
    conn = _conn()
    conn.fetchrow = AsyncMock(
        return_value={"runtime_generation": NEXT_GENERATION, "status": "active"}
    )
    db = _db(conn)

    assert (
        await db.save_session_subagent_thread_message(
            str(CHILD),
            parent_thread_id=str(PARENT),
            parent_authority=AUTHORITY,
            runtime_generation=str(GENERATION),
            role="human",
            content="stale",
        )
        is None
    )
    assert conn.fetchrow.await_count == 1
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_fork_seed_is_one_fenced_ordered_batch():
    conn = _conn()
    conn.fetchrow = AsyncMock(
        return_value={"runtime_generation": GENERATION, "status": "created"}
    )
    db = _db(conn)
    messages = [
        {
            "id": "msg-ai",
            "role": "ai",
            "content": "call",
            "tool_calls": [{"id": "call-1", "name": "read_file", "args": {}}],
            "turn_number": 1,
            "provider_raw": {"seed": True},
        },
        {
            "id": "msg-tool",
            "role": "tool",
            "content": "result",
            "tool_call_id": "call-1",
            "turn_number": 1,
        },
    ]

    assert await db.save_session_subagent_thread_messages(
        str(CHILD),
        parent_thread_id=str(PARENT),
        parent_authority=AUTHORITY,
        runtime_generation=str(GENERATION),
        messages=messages,
    )

    _, rows = conn.executemany.await_args.args
    assert [row[2] for row in rows] == ["ai", "tool"]
    assert json.loads(rows[0][4])[0]["id"] == "call-1"
    assert json.loads(rows[0][12]) == {"seed": True}
    assert rows[0][0] == agent_postgres._coerce_row_id("msg-ai")


@pytest.mark.asyncio
async def test_foreground_terminal_update_has_parent_shape_and_background_guard():
    conn = _conn()
    conn.execute.return_value = "UPDATE 1"
    db = _db(conn)

    assert await db.update_session_subagent_thread(
        str(CHILD),
        parent_thread_id=str(PARENT),
        parent_authority=AUTHORITY,
        runtime_generation=str(GENERATION),
        status="ended",
        subagent_status="completed",
        outcome="completed",
        turns=2,
        tokens=10,
        ended=True,
    )

    call = conn.execute.await_args
    sql = _compact(call.args[0])
    assert "parent_job_id IS NULL" in sql
    assert "parent_thread_id = $11::uuid" in sql
    assert "NOT $9::boolean OR COALESCE" in sql
    assert call.args[1:] == (
        str(CHILD),
        "ended",
        "completed",
        "completed",
        2,
        10,
        None,
        None,
        True,
        str(GENERATION),
        str(PARENT),
    )


@pytest.mark.asyncio
async def test_foreground_terminal_retry_adopts_exact_committed_row():
    conn = _conn()
    conn.execute.return_value = "UPDATE 0"
    conn.fetchrow.return_value = {
        "status": "ended",
        "subagent_status": "completed",
        "subagent_outcome": "completed",
        "total_turns": 2,
        "total_tokens": 10,
        "report_path": None,
        "subagent_error": None,
        "run_in_background": "false",
    }
    db = _db(conn)

    assert await db.update_session_subagent_thread(
        str(CHILD),
        parent_thread_id=str(PARENT),
        parent_authority=AUTHORITY,
        runtime_generation=str(GENERATION),
        status="ended",
        subagent_status="completed",
        outcome="completed",
        turns=2,
        tokens=10,
        ended=True,
    )
    retry_sql = _compact(conn.fetchrow.await_args.args[0])
    assert "runtime_generation = $3::uuid" in retry_sql
    assert retry_sql.endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_foreground_terminal_retry_rejects_conflicting_row():
    conn = _conn()
    conn.execute.return_value = "UPDATE 0"
    conn.fetchrow.return_value = {
        "status": "ended",
        "subagent_status": "completed",
        "subagent_outcome": "different",
        "total_turns": 2,
        "total_tokens": 10,
        "report_path": None,
        "subagent_error": None,
        "run_in_background": "false",
    }
    db = _db(conn)

    assert not await db.update_session_subagent_thread(
        str(CHILD),
        parent_thread_id=str(PARENT),
        parent_authority=AUTHORITY,
        runtime_generation=str(GENERATION),
        status="ended",
        subagent_status="completed",
        outcome="completed",
        turns=2,
        tokens=10,
        ended=True,
    )


@pytest.mark.asyncio
async def test_by_call_lookup_is_thread_only_and_authority_fenced():
    conn = _conn()
    conn.fetchrow.return_value = {
        "id": CHILD,
        "parent_job_id": None,
        "parent_thread_id": PARENT,
        "subagent_status": "completed",
    }
    db = _db(conn)

    row = await db.get_session_subagent_thread_by_call(
        str(PARENT), "call-1", parent_authority=AUTHORITY
    )

    assert row["id"] == CHILD
    sql = _compact(conn.fetchrow.await_args.args[0])
    assert "parent_job_id IS NULL" in sql
    assert "parent_thread_id = $1::uuid" in sql
    assert "parent_tool_call_id = $2" in sql
    assert sql.endswith("FOR SHARE")
    assert conn.fetchrow.await_args.args[1:] == (str(PARENT), "call-1")
