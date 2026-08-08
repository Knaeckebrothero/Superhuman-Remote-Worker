from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.services.session_state_snapshot import (
    build_session_state_snapshot,
)


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SnapshotConn:
    def __init__(
        self,
        *,
        thread: dict | None,
        lifecycle: dict | None = None,
        running: dict | None = None,
        permissions: list[dict] | None = None,
        message_count: int = 0,
        live_turn_count: int = 0,
    ) -> None:
        self.thread = thread
        self.lifecycle = lifecycle
        self.running = running
        self.permissions = permissions or []
        self.message_count = message_count
        self.live_turn_count = live_turn_count
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return _AsyncContext()

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        if "FROM threads" in sql:
            return self.thread
        if "AS latest_kind" in sql:
            return self.lifecycle
        if "SELECT started.payload" in sql:
            return self.running
        raise AssertionError(f"unexpected fetchrow query: {sql}")

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        if "COUNT(*) FROM thread_messages" in sql:
            return self.message_count
        if "MAX(turn_number)" in sql:
            return self.live_turn_count
        raise AssertionError(f"unexpected fetchval query: {sql}")

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        assert "FROM thread_permission_requests" in sql
        return self.permissions


class _SnapshotDB:
    def __init__(self, conn: _SnapshotConn) -> None:
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


def _thread(**overrides):
    row = {
        "id": "thread-1",
        "permission_mode": "supervised",
        "metadata": {},
        "status": "active",
        "execution_lane": "pinned",
        "events_epoch": 3,
        "events_seq_hwm": 41,
        "queue_state": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_snapshot_has_full_lane_free_shape_and_normalizes_durable_rows():
    conn = _SnapshotConn(
        thread=_thread(
            metadata=json.dumps(
                {
                    "config_override": {
                        "interactive": {"narration_mode": "silent"},
                        "llm": {"model": "stored-model", "api_key": "never-return"},
                    },
                    "execution_lane": "must-not-return",
                }
            )
        ),
        lifecycle={
            "latest_kind": "turn.started",
            "latest_turn_id": "7",
            "latest_turn_start_seq": 39,
        },
        running={
            "payload": json.dumps(
                {"id": "tool-7", "tool": "run_command", "args": {"cmd": "ls"}}
            )
        },
        permissions=[
            {
                "id": "approval-1",
                "tool_call_id": "tool-8",
                "tool_name": "write_file",
                "tool_args": '{"path":"notes.md"}',
            },
            {
                "id": "approval-2",
                "tool_call_id": "tool-9",
                "tool_name": "run_command",
                "tool_args": "not-json",
            },
        ],
        message_count=12,
        live_turn_count=7,
    )

    result = await build_session_state_snapshot(
        _SnapshotDB(conn),
        "thread-1",
        resolved_config={
            "agent": {
                "interactive": {
                    "permission_mode": "autonomous",
                    "narration_mode": "verbose",
                },
                "llm": {
                    "model": "effective-model",
                    "temperature": Decimal("0.25"),
                    "api_key": "also-never-return",
                },
            },
        },
    )

    assert result == {
        "thread_id": "thread-1",
        # The first-class thread column is the durable permission authority.
        "permission_mode": "supervised",
        "narration_mode": "verbose",
        "turn_count": 7,
        "turn_in_flight": True,
        "message_count": 12,
        "model": "effective-model",
        "temperature": 0.25,
        "running_tool": {
            "id": "tool-7",
            "tool": "run_command",
            "args": {"cmd": "ls"},
        },
        "pending_permissions": [
            {
                "id": "tool-8",
                "approval_id": "approval-1",
                "tool": "write_file",
                "args": {"path": "notes.md"},
            },
            {
                "id": "tool-9",
                "approval_id": "approval-2",
                "tool": "run_command",
                "args": {},
            },
        ],
        "event_cursor": {"epoch": 3, "seq": 41},
        "replay_cursor": {"epoch": 3, "seq": 38},
        "snapshot_source": "durable_journal",
    }
    encoded = json.dumps(result)
    assert "execution_lane" not in encoded
    assert "api_key" not in encoded
    assert "never-return" not in encoded

    lifecycle_call = next(call for call in conn.calls if "AS latest_kind" in call[0])
    assert lifecycle_call[1][1] == 3
    assert lifecycle_call[1][2] == [
        "turn.started",
        "turn.completed",
        "turn.error",
        "turn.interrupted",
        "turn.parked",
        "ready",
    ]


@pytest.mark.asyncio
async def test_snapshot_explicitly_clears_idle_runtime_and_pending_state():
    conn = _SnapshotConn(
        thread=_thread(metadata={"config_override": {}}),
        lifecycle={
            "latest_kind": "turn.completed",
            "latest_turn_id": "7",
            "latest_turn_start_seq": 35,
        },
        running=None,
        permissions=[],
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_in_flight"] is False
    assert result["running_tool"] is None
    assert result["pending_permissions"] == []
    assert result["narration_mode"] == "auto"
    assert result["model"] is None
    assert result["temperature"] is None


@pytest.mark.asyncio
async def test_snapshot_returns_none_if_thread_disappears_inside_read():
    conn = _SnapshotConn(thread=None)
    assert await build_session_state_snapshot(_SnapshotDB(conn), "thread-gone") is None
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_snapshot_uses_live_turn_after_rewind_and_queue_done_clears_stale_edge():
    conn = _SnapshotConn(
        thread=_thread(execution_lane="stateless", queue_state="done"),
        lifecycle={
            "latest_kind": "turn.started",
            "latest_turn_id": "4",
            "latest_turn_start_seq": 41,
        },
        running={"payload": {"id": "stale-tool", "tool": "run_command", "args": {}}},
        live_turn_count=4,
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_count"] == 4
    assert result["turn_in_flight"] is False
    assert result["running_tool"] is None
    # Do not replay the stale start when the queue proves its terminal edge
    # was the journal write that went missing.
    assert result["replay_cursor"] == {"epoch": 3, "seq": 41}


@pytest.mark.asyncio
async def test_stale_queue_row_never_clears_a_pinned_runtime_snapshot():
    conn = _SnapshotConn(
        thread=_thread(execution_lane="pinned", queue_state="done"),
        lifecycle={
            "latest_kind": "turn.started",
            "latest_turn_id": "8",
            "latest_turn_start_seq": 41,
        },
        running={"payload": {"id": "live-tool", "tool": "run_command", "args": {}}},
        live_turn_count=8,
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_in_flight"] is True
    assert result["running_tool"] == {
        "id": "live-tool",
        "tool": "run_command",
        "args": {},
    }


@pytest.mark.asyncio
async def test_unknown_stateless_queue_state_never_claims_runtime_is_idle():
    conn = _SnapshotConn(
        thread=_thread(execution_lane="stateless", queue_state="future-state"),
        lifecycle={
            "latest_kind": "turn.started",
            "latest_turn_id": "8",
            "latest_turn_start_seq": 41,
        },
        running={"payload": {"id": "live-tool", "tool": "run_command", "args": {}}},
        live_turn_count=8,
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_in_flight"] is True
    assert result["running_tool"] is not None


@pytest.mark.asyncio
async def test_ready_is_a_durable_terminal_observation_for_turn_and_tool_state():
    conn = _SnapshotConn(
        thread=_thread(execution_lane="pinned", queue_state=None),
        lifecycle={
            "latest_kind": "ready",
            "latest_turn_id": "8",
            "latest_turn_start_seq": 39,
        },
        running=None,
        live_turn_count=8,
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_in_flight"] is False


@pytest.mark.asyncio
async def test_queued_future_inputs_do_not_advance_the_active_runtime_turn():
    conn = _SnapshotConn(
        thread=_thread(execution_lane="stateless", queue_state="leased"),
        lifecycle={
            "latest_kind": "turn.started",
            "latest_turn_id": "8",
            "latest_turn_start_seq": 36,
        },
        # Durable admission has already numbered two future human rows 9/10.
        live_turn_count=10,
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_count"] == 8
    assert result["turn_in_flight"] is True
    assert result["replay_cursor"] == {"epoch": 3, "seq": 35}


@pytest.mark.asyncio
async def test_turn_count_fallback_uses_only_execution_produced_rows():
    conn = _SnapshotConn(
        thread=_thread(events_seq_hwm=77),
        lifecycle=None,
        live_turn_count=6,
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_count"] == 6
    assert result["replay_cursor"] == {"epoch": 3, "seq": 77}
    fallback_sql = next(sql for sql, _args in conn.calls if "MAX(turn_number)" in sql)
    assert "role NOT IN ('human', 'event')" in fallback_sql


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ended", "suspended", "idle", "future-status"])
async def test_terminal_thread_status_clears_stale_runtime_edges(status):
    conn = _SnapshotConn(
        thread=_thread(status=status),
        lifecycle={
            "latest_kind": "turn.started",
            "latest_turn_id": "9",
            "latest_turn_start_seq": 40,
        },
        running={"payload": {"id": "stale", "tool": "run_command", "args": {}}},
    )

    result = await build_session_state_snapshot(_SnapshotDB(conn), "thread-1")

    assert result is not None
    assert result["turn_in_flight"] is False
    assert result["running_tool"] is None


@pytest.mark.asyncio
async def test_config_resolver_receives_the_row_captured_with_the_event_cursor():
    conn = _SnapshotConn(
        thread=_thread(
            metadata={"config_override": {"llm": {"model": "captured"}}},
            events_seq_hwm=52,
        ),
        lifecycle=None,
    )
    resolver = AsyncMock(
        return_value={"agent": {"llm": {"model": "captured", "api_key": "secret"}}}
    )

    result = await build_session_state_snapshot(
        _SnapshotDB(conn), "thread-1", config_resolver=resolver
    )

    assert result is not None
    resolver.assert_awaited_once_with(
        {
            **_thread(
                metadata={"config_override": {"llm": {"model": "captured"}}},
                events_seq_hwm=52,
            )
        },
        {"config_override": {"llm": {"model": "captured"}}},
    )
    assert result["model"] == "captured"
    assert result["event_cursor"] == {"epoch": 3, "seq": 52}
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_owner_gated_route_resolves_only_safe_display_config():
    import main as orchestrator_main

    thread = _thread(
        id="thread-1",
        user_id="user-1",
        metadata={"config_override": {}},
    )
    snapshot = {
        "thread_id": "thread-1",
        "pending_permissions": [],
        "event_cursor": {"epoch": 3, "seq": 41},
    }
    owner = AsyncMock(return_value=({"id": "user-1"}, thread))
    resolve = AsyncMock(
        return_value={"agent": {"llm": {"model": "effective", "api_key": "secret"}}}
    )

    async def _build_with_captured_resolver(_db, _thread_id, *, config_resolver):
        captured = {
            **thread,
            "metadata": {"config_override": {"llm": {"model": "captured"}}},
        }
        resolved = await config_resolver(captured, captured["metadata"])
        assert resolved == {
            "agent": {"llm": {"model": "effective", "api_key": "secret"}}
        }
        return snapshot

    build = AsyncMock(side_effect=_build_with_captured_resolver)

    with (
        patch.object(orchestrator_main, "require_thread_owner", owner),
        patch.object(orchestrator_main, "_resolve_session_config", resolve),
        patch.object(orchestrator_main, "build_session_state_snapshot", build),
    ):
        response = MagicMock()
        response.headers = {}
        result = await orchestrator_main.get_thread_session_state(
            "thread-1", MagicMock(), response
        )

    assert result is snapshot
    assert response.headers["Cache-Control"] == "private, no-store"
    owner.assert_awaited_once()
    resolve.assert_awaited_once_with(
        {**thread, "metadata": {"config_override": {"llm": {"model": "captured"}}}},
        {"config_override": {"llm": {"model": "captured"}}},
    )
    build.assert_awaited_once_with(
        orchestrator_main.postgres_db,
        "thread-1",
        config_resolver=ANY,
    )


@pytest.mark.asyncio
async def test_owner_gated_route_returns_404_if_thread_vanishes_after_auth():
    import main as orchestrator_main

    with (
        patch.object(
            orchestrator_main,
            "require_thread_owner",
            AsyncMock(return_value=({"id": "user-1"}, _thread())),
        ),
        patch.object(
            orchestrator_main,
            "_resolve_session_config",
            AsyncMock(return_value=None),
        ),
        patch.object(
            orchestrator_main,
            "build_session_state_snapshot",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await orchestrator_main.get_thread_session_state(
                "thread-gone", MagicMock(), MagicMock()
            )
    assert exc.value.status_code == 404
