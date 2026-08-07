"""Detached-rewind REST endpoint + orchestrator-side rewind SQL."""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_orchestrator_live_readers_filter_tombstones():
    from orchestrator.database import postgres as mod

    for meth in (
        "get_thread_messages_history",
        "get_thread_messages_page",
        "get_thread_message_count",
        "get_officer_last_engagement",
    ):
        src = inspect.getsource(getattr(mod.PostgresDB, meth))
        assert "rewound_at IS NULL" in src, f"{meth} must filter tombstones"


def test_apply_thread_rewind_locks_sweeps_bumps_and_journals():
    from orchestrator.database.postgres import PostgresDB

    class _FakeTxn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeConn:
        def __init__(self):
            self.calls = []

        def transaction(self):
            return _FakeTxn()

        async def execute(self, q, *a):
            self.calls.append(q)

        async def fetchrow(self, q, *a):
            self.calls.append(q)
            return {"id": "33333333-3333-3333-3333-333333333333"}

        async def fetchval(self, q, *a):
            self.calls.append(q)
            if "COUNT" in q:
                return 5
            if "events_epoch" in q:
                return 9
            return 2

    conn = _FakeConn()

    class _FakeAcquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: _FakeAcquire()

    out = asyncio.run(
        db.apply_thread_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=10, actor="user-1"
        )
    )
    assert out["swept"] == 5
    blob = " ".join(conn.calls)
    assert "pg_advisory_xact_lock" in blob
    assert "SET rewound_at = now()" in blob
    assert "INSERT INTO thread_rewinds" in blob
    assert "events_epoch = events_epoch + 1" in blob
    assert "INSERT INTO thread_events" in blob


@pytest.mark.asyncio
async def test_rewind_endpoint_rejects_live_agent(monkeypatch):
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return ({"id": "user-1"}, {"id": thread_id, "agent_id": "agent-9"})

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await orch_main.rewind_thread_detached(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            MagicMock(),
            orch_main.ThreadRewindRequest(message_id="m1", mode="conversation"),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_rewind_endpoint_allows_ended_thread_with_stale_agent_id(monkeypatch):
    """mark_orphaned_threads_ended / agent_update_thread_status's ended branch

    both leave ``agent_id`` populated on real ended threads — only a LIVE
    binding (status not suspended/ended) justifies the 409.
    """
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return (
            {"id": "user-1"},
            {"id": thread_id, "agent_id": "agent-9", "status": "ended"},
        )

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    fake_db = MagicMock()
    fake_db.get_live_thread_message = AsyncMock(
        return_value={"seq": 8, "role": "human", "content": "the prompt"}
    )
    fake_db.apply_thread_rewind = AsyncMock(
        return_value={"rewind_id": "r1", "swept": 3, "surviving_turn": 1}
    )
    monkeypatch.setattr(orch_main, "postgres_db", fake_db)

    out = await orch_main.rewind_thread_detached(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        MagicMock(),
        orch_main.ThreadRewindRequest(message_id="m1", mode="conversation"),
    )
    assert out == {"rewind_id": "r1", "swept": 3, "prompt": "the prompt"}
    fake_db.apply_thread_rewind.assert_awaited_once_with(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=8, actor="user-1"
    )


@pytest.mark.asyncio
async def test_rewind_endpoint_rejects_code_mode(monkeypatch):
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return ({"id": "user-1"}, {"id": thread_id, "agent_id": None})

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await orch_main.rewind_thread_detached(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            MagicMock(),
            orch_main.ThreadRewindRequest(message_id="m1", mode="both"),
        )
    assert exc.value.status_code == 400
    assert "resume" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_rewind_endpoint_happy_path(monkeypatch):
    from orchestrator import main as orch_main

    async def _fake_owner(request, db, thread_id):
        return ({"id": "user-1"}, {"id": thread_id, "agent_id": None})

    monkeypatch.setattr(orch_main, "require_thread_owner", _fake_owner)
    fake_db = MagicMock()
    fake_db.get_live_thread_message = AsyncMock(
        return_value={"seq": 8, "role": "human", "content": "the prompt"}
    )
    fake_db.apply_thread_rewind = AsyncMock(
        return_value={"rewind_id": "r1", "swept": 3, "surviving_turn": 1}
    )
    monkeypatch.setattr(orch_main, "postgres_db", fake_db)

    out = await orch_main.rewind_thread_detached(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        MagicMock(),
        orch_main.ThreadRewindRequest(message_id="m1", mode="conversation"),
    )
    assert out == {"rewind_id": "r1", "swept": 3, "prompt": "the prompt"}
    fake_db.apply_thread_rewind.assert_awaited_once_with(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=8, actor="user-1"
    )
