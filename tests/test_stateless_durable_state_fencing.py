"""Lease identity and lock-order tests for S2 durable session state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.lease_context import LeaseLostError
from src.database.postgres_db import PostgresDB


THREAD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_THREAD_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LEASE = (THREAD_ID, 17)


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _FakeTxn()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 1"

    async def executemany(self, query, args):
        self.calls.append(("executemany", query, tuple(args)))

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return []

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "FROM threads" in query:
            return 1
        if "OFFSET 1 LIMIT 1" in query:
            return "previous-sha"
        return None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "SELECT project_id, metadata, execution_lane" in query:
            return {
                "project_id": None,
                "metadata": {},
                "execution_lane": "stateless",
            }
        if "INSERT INTO thread_messages" in query:
            return {"id": args[0], "seq": 1}
        if "INSERT INTO thread_session_tasks" in query:
            return {
                "task_number": 1,
                "description": "durable task",
                "status": "pending",
                "priority": "medium",
                "notes": "",
                "created_at": None,
                "completed_at": None,
            }
        if "thread_session_runtime_state" in query:
            return {"memory_extraction_turn": 10}
        return None


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


def _db_with(conn: _FakeConn) -> PostgresDB:
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = MagicMock(return_value=_FakeAcquire(conn))
    db.fetch = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value="previous-sha")
    return db


Operation = Callable[[PostgresDB, str], Awaitable[object]]


def _record_turn(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.record_turn_commit(thread_id, "commit-sha")


def _seed_baseline(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.seed_workspace_baseline_commit(thread_id, "baseline-sha")


def _list_turn_commits(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.list_workspace_turn_commits(thread_id)


def _list_tasks(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.list_session_tasks(thread_id)


def _create_task(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.create_session_task(thread_id, "durable task", "medium")


def _start_task(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.start_session_task(thread_id, 1)


def _complete_task(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.complete_session_task(thread_id, 1, "done")


def _claim_cursor(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.claim_memory_extraction_interval(
        thread_id,
        turn_count=10,
        interval=5,
    )


def _list_anchors(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.list_thread_cloud_anchors(thread_id)


def _upsert_anchor(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.upsert_thread_cloud_anchor(
        thread_id,
        "documents/report.pdf",
        {"provider": "nextcloud", "version": "etag-1"},
    )


def _save_message(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.save_thread_message(
        thread_id,
        role="ai",
        content="durable answer",
        turn_number=3,
        id="durable-message",
    )


def _save_messages(db: PostgresDB, thread_id: str) -> Awaitable[object]:
    return db.save_thread_messages(
        thread_id,
        [
            {
                "id": "durable-message",
                "role": "ai",
                "content": "durable answer",
                "turn_number": 3,
            }
        ],
    )


ALL_DURABLE_OPERATIONS: list[object] = [
    pytest.param(_record_turn, id="record-turn-commit"),
    pytest.param(_seed_baseline, id="seed-workspace-baseline"),
    pytest.param(_list_turn_commits, id="list-workspace-turn-commits"),
    pytest.param(_list_tasks, id="list-tasks"),
    pytest.param(_create_task, id="create-task"),
    pytest.param(_start_task, id="start-task"),
    pytest.param(_complete_task, id="complete-task"),
    pytest.param(_claim_cursor, id="claim-memory-cursor"),
    pytest.param(_list_anchors, id="list-cloud-anchors"),
    pytest.param(_upsert_anchor, id="upsert-cloud-anchor"),
    pytest.param(_save_message, id="save-message"),
    pytest.param(_save_messages, id="save-messages"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ALL_DURABLE_OPERATIONS)
async def test_repointed_lease_rejects_every_durable_operation_before_sql(
    operation: Operation,
):
    conn = _FakeConn()
    db = _db_with(conn)

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=(OTHER_THREAD_ID, LEASE[1]),
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            new=AsyncMock(),
        ) as fence,
        patch("src.api.lease_context.mark_current_lease_lost") as mark_lost,
        pytest.raises(LeaseLostError, match="cannot access thread"),
    ):
        await operation(db, THREAD_ID)

    db.acquire.assert_not_called()
    db.fetch.assert_not_awaited()
    db.fetchval.assert_not_awaited()
    fence.assert_not_awaited()
    mark_lost.assert_called_once_with()
    assert conn.calls == []


MUTATING_OPERATIONS: list[object] = [
    pytest.param(_record_turn, id="record-turn-commit"),
    pytest.param(_seed_baseline, id="seed-workspace-baseline"),
    pytest.param(_create_task, id="create-task"),
    pytest.param(_start_task, id="start-task"),
    pytest.param(_complete_task, id="complete-task"),
    pytest.param(_claim_cursor, id="claim-memory-cursor"),
    pytest.param(_upsert_anchor, id="upsert-cloud-anchor"),
    pytest.param(_save_message, id="save-message"),
    pytest.param(_save_messages, id="save-messages"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", MUTATING_OPERATIONS)
async def test_stale_lease_rejection_performs_no_durable_write(operation: Operation):
    conn = _FakeConn()
    db = _db_with(conn)

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=LEASE,
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            new=AsyncMock(side_effect=LeaseLostError("stale")),
        ),
        pytest.raises(LeaseLostError, match="stale"),
    ):
        await operation(db, THREAD_ID)

    sql = [query.lstrip().upper() for _kind, query, _args in conn.calls]
    assert not any(query.startswith(("INSERT", "UPDATE", "DELETE")) for query in sql)


FK_INSERT_OPERATIONS = [
    pytest.param(_create_task, "FOR UPDATE", "thread_session_tasks", id="task"),
    pytest.param(
        _claim_cursor,
        "FOR KEY SHARE",
        "thread_session_runtime_state",
        id="memory-cursor",
    ),
    pytest.param(
        _upsert_anchor,
        "FOR KEY SHARE",
        "thread_cloud_citation_anchors",
        id="cloud-anchor",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,authority_lock,target_table", FK_INSERT_OPERATIONS)
async def test_fk_insert_uses_threads_then_queue_then_write_lock_order(
    operation: Operation,
    authority_lock: str,
    target_table: str,
):
    conn = _FakeConn()
    db = _db_with(conn)

    async def fence(fenced_conn, received_lease):
        assert fenced_conn is conn
        assert received_lease == LEASE
        conn.calls.append(("fence", "run_queue", (received_lease,)))

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=LEASE,
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            side_effect=fence,
        ),
    ):
        await operation(db, THREAD_ID)

    assert [kind for kind, _query, _args in conn.calls] == [
        "fetchval",
        "fence",
        "execute" if target_table == "thread_cloud_citation_anchors" else "fetchrow",
    ]
    assert "FROM threads" in conn.calls[0][1]
    assert authority_lock in conn.calls[0][1]
    assert target_table in conn.calls[2][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,parent_lock_kind,parent_lock_mode,write_kind",
    [
        pytest.param(
            _save_message,
            "fetchval",
            "FOR KEY SHARE",
            "fetchrow",
            id="incremental",
        ),
        pytest.param(
            _save_messages,
            "fetchrow",
            "FOR UPDATE",
            "executemany",
            id="reconcile-batch",
        ),
    ],
)
async def test_message_write_uses_threads_then_queue_then_mutations(
    operation: Operation,
    parent_lock_kind: str,
    parent_lock_mode: str,
    write_kind: str,
):
    conn = _FakeConn()
    db = _db_with(conn)

    async def fence(fenced_conn, received_lease):
        assert fenced_conn is conn
        assert received_lease == LEASE
        conn.calls.append(("fence", "run_queue", (received_lease,)))

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=LEASE,
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            side_effect=fence,
        ),
    ):
        await operation(db, THREAD_ID)

    assert [kind for kind, _query, _args in conn.calls] == [
        parent_lock_kind,
        "fence",
        write_kind,
        "execute",
    ]
    assert "FROM threads" in conn.calls[0][1]
    assert parent_lock_mode in conn.calls[0][1]
    assert "INSERT INTO thread_messages" in conn.calls[2][1]
    assert "UPDATE threads" in conn.calls[3][1]


@pytest.mark.asyncio
async def test_workspace_baseline_is_fenced_then_inserted_once_at_seq_zero():
    conn = _FakeConn()
    db = _db_with(conn)

    async def fence(fenced_conn, received_lease):
        assert fenced_conn is conn
        conn.calls.append(("fence", "run_queue", (received_lease,)))

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=LEASE,
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            side_effect=fence,
        ),
    ):
        await db.seed_workspace_baseline_commit(THREAD_ID, "baseline-sha")

    assert [kind for kind, _query, _args in conn.calls] == ["fence", "execute"]
    query = conn.calls[1][1]
    assert "VALUES ($1, 0, $2)" in query
    assert "ON CONFLICT (thread_id, seq) DO NOTHING" in query
