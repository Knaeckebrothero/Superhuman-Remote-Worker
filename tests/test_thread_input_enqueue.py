"""Stateless-lane enqueue-on-input (stateless_agents.md §5.3.1, M4).

POST /api/persistent/threads/{id}/input on a thread with
``execution_lane='stateless'`` must: persist the human message row
(indistinguishable from the agent's accept-time persist), advance the
run_queue input watermark, and admit the unit — all in ONE transaction on ONE
connection — then answer with the pinned path's response shape (accepted /
turn_id top-level, "queue" object instead of "agent"). The pinned path stays
byte-identical (legacy forward, per-turn lock); the per-turn in-process lock
is skipped ONLY for the stateless lane.

House pattern: direct coroutine calls on orchestrator.main with monkeypatched
module globals (see tests/test_rewind_orchestrator.py).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

THREAD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER = {"id": "user-1", "is_admin": False}
_USE_DB_THREAD = object()


class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.txn_depth += 1
        self._conn.txn_enters += 1
        return self

    async def __aexit__(self, *exc):
        self._conn.txn_depth -= 1
        return False


class FakeConn:
    """Scripted asyncpg connection: records (kind, query, args, txn_depth)."""

    def __init__(
        self,
        *,
        message_seq=41,
        admit_state="queued",
        watermarks=None,
        locked_thread=_USE_DB_THREAD,
    ):
        self.calls = []
        self.txn_depth = 0
        self.txn_enters = 0
        self._message_seq = message_seq
        self._admit_state = admit_state
        self._locked_thread = locked_thread
        self._watermarks = watermarks or {
            "state": "queued",
            "input_seq": message_seq,
            "consumed_seq": None,
            "control_input_seq": 0,
            "control_consumed_seq": 0,
        }

    def transaction(self):
        return _FakeTxn(self)

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q, a, self.txn_depth))
        if "INSERT INTO thread_messages" in q:
            return self._message_seq
        if "run_queue" in q:
            # record_input_seq's one-statement CTE
            return self._admit_state
        return None

    async def execute(self, q, *a):
        self.calls.append(("execute", q, a, self.txn_depth))

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q, a, self.txn_depth))
        if "FROM threads" in q and "FOR UPDATE" in q:
            assert self.txn_depth == 1
            return self._locked_thread
        if "FROM run_queue" in q:
            return dict(self._watermarks)
        return None


class FakeDB:
    def __init__(self, thread, conn):
        self._thread = thread
        self._conn = conn
        if conn._locked_thread is _USE_DB_THREAD:
            conn._locked_thread = thread
        self.get_thread_calls = 0

    async def get_thread(self, tid):
        self.get_thread_calls += 1
        return self._thread

    def acquire(self):
        conn = self._conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _A()


def _stateless_thread(**over):
    thread = {
        "id": THREAD_ID,
        "user_id": "user-1",
        "execution_lane": "stateless",
        "agent_id": None,
        "total_turns": 5,
        "status": "active",
        "metadata": {"config_override": {"workspace": {"backend": "virtual"}}},
    }
    thread.update(over)
    return thread


def _k8s_sandbox_metadata(*, status="ready"):
    generation = "11111111-1111-4111-8111-111111111111"
    workspace = {"status": status, "provisioner": "k8s"}
    metadata = {
        "config_override": {"workspace": {"backend": "sandbox"}},
        "workspace_container": workspace,
    }
    if status == "ready":
        workspace.update(
            {
                "pod_ip": "10.42.0.25",
                "port": 30022,
                "pod_name": "ws-thread-aaaaaaaaaaaa",
                "namespace": "agent-workspaces",
                "_canvas_workspace_generation": generation,
                "_runtime_incarnation": "22222222-2222-4222-8222-222222222222",
            }
        )
        metadata["_workspace_binding"] = {
            "generation": generation,
            "kind": "remote",
            "backing_id": "k8s-pvc:agent-workspaces:pvc-uid",
            "ssh_host_key_fingerprint": "SHA256:trusted",
        }
    return metadata


def _patch_common(monkeypatch, orch_main, db):
    async def _fake_user(request, _db):
        return dict(USER)

    monkeypatch.setattr(orch_main, "require_approved_user", _fake_user)
    monkeypatch.setattr(orch_main, "postgres_db", db)


@pytest.mark.asyncio
async def test_stateless_input_single_transaction_and_response_parity(monkeypatch):
    """Insert + threads bump + record_input_seq share one conn/transaction;
    the watermark read happens post-commit; response carries accepted/turn_id
    top-level with the queue object nested."""
    from orchestrator import main as orch_main

    conn = FakeConn(message_seq=41)
    db = FakeDB(_stateless_thread(), conn)
    _patch_common(monkeypatch, orch_main, db)

    out = await orch_main.thread_input(
        THREAD_ID,
        orch_main.ThreadInputRequest(content="hello queue"),
        MagicMock(),
    )

    # --- transaction shape: exactly one txn; all three writes inside it ---
    assert conn.txn_enters == 1
    insert = next(c for c in conn.calls if "INSERT INTO thread_messages" in c[1])
    bump = next(c for c in conn.calls if "GREATEST(total_turns" in c[1])
    admit = next(c for c in conn.calls if c[0] == "fetchval" and "run_queue" in c[1])
    watermark = next(
        c for c in conn.calls if c[0] == "fetchrow" and "FROM run_queue" in c[1]
    )
    assert insert[3] == 1, "message insert must run inside the transaction"
    assert bump[3] == 1, "threads bump must run inside the transaction"
    assert admit[3] == 1, "record_input_seq must share the SAME transaction"
    assert watermark[3] == 0, "queue_depth read happens after commit"
    order = [conn.calls.index(c) for c in (insert, bump, admit, watermark)]
    assert order == sorted(order), "insert -> bump -> admit -> watermark order"

    # --- message row mirrors the agent's accept-time persist ---
    row_id, thread_id_arg, content, turn_number = insert[2]
    assert thread_id_arg == THREAD_ID
    assert content == "hello queue"
    assert turn_number == 6  # total_turns + 1
    assert "'human'" in insert[1]  # role literal in the mirrored insert
    # row id is the agent's own uuid5 coercion of the minted msg_ id
    from src.database.postgres_db import _coerce_row_id

    raw_msg_id = out["queue"]["message_id"]
    assert raw_msg_id.startswith("msg_") and len(raw_msg_id) == 4 + 24
    assert row_id == _coerce_row_id(raw_msg_id)
    # threads bump args mirror save_thread_message's
    assert bump[2] == (THREAD_ID, 6)

    # --- admission args: unit_id=thread_id, kind, watermark, fair_key ---
    a = admit[2]
    assert a[0] == uuid.UUID(THREAD_ID)
    assert a[1] == "session_turn"
    assert a[2] == 41
    assert a[3] == "user-1"

    # --- response parity ---
    assert out["accepted"] is True
    assert out["turn_id"] == 6
    assert "agent" not in out
    assert out["queue"]["state"] == "queued"
    assert out["queue"]["queue_depth"] == 1
    assert out["queue"]["input_seq"] == 41


@pytest.mark.asyncio
async def test_stateless_input_skips_per_turn_lock(monkeypatch):
    """The in-process per-turn lock dict is replica-unsafe and unnecessary on
    the queue lane — it must not even be consulted."""
    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(_stateless_thread(), conn)
    _patch_common(monkeypatch, orch_main, db)
    lock_spy = MagicMock(side_effect=AssertionError("lock must not be used"))
    monkeypatch.setattr(orch_main, "_ensure_thread_turn_lock", lock_spy)
    forward_spy = AsyncMock(side_effect=AssertionError("no agent forward"))
    monkeypatch.setattr(orch_main, "_forward_to_agent", forward_spy)

    out = await orch_main.thread_input(
        THREAD_ID, orch_main.ThreadInputRequest(content="x"), MagicMock()
    )
    assert out["accepted"] is True
    lock_spy.assert_not_called()
    forward_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_input_rejects_empty_content(monkeypatch):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(_stateless_thread(), conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID, orch_main.ThreadInputRequest(content=""), MagicMock()
        )
    assert exc.value.status_code == 400
    assert conn.calls == []  # nothing persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["vm", "future-tier", None])
async def test_stateless_input_rejects_unsupported_workspace_before_writes(
    monkeypatch, backend
):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    metadata = (
        {"config_override": {"workspace": {"backend": backend}}}
        if backend is not None
        else {}
    )
    conn = FakeConn()
    db = FakeDB(_stateless_thread(metadata=metadata), conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID,
            orch_main.ThreadInputRequest(content="must not queue"),
            MagicMock(),
        )

    assert exc.value.status_code == 409
    assert "virtual/none" in str(exc.value.detail)
    assert conn.calls == []


@pytest.mark.asyncio
async def test_stateless_input_accepts_attested_k8s_sandbox(monkeypatch):
    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(
        _stateless_thread(metadata=_k8s_sandbox_metadata()),
        conn,
    )
    _patch_common(monkeypatch, orch_main, db)

    out = await orch_main.thread_input(
        THREAD_ID,
        orch_main.ThreadInputRequest(content="sandbox turn"),
        MagicMock(),
    )

    assert out["accepted"] is True
    assert conn.txn_enters == 1


@pytest.mark.asyncio
async def test_stateless_input_rechecks_locked_workspace_before_any_write(monkeypatch):
    """A preflight-valid row cannot authorize Docker evidence under lock."""
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    preflight = _stateless_thread()
    locked = _stateless_thread(
        metadata={
            "config_override": {"workspace": {"backend": "sandbox"}},
            "workspace_container": {"status": "ready", "provisioner": "docker"},
        }
    )
    conn = FakeConn(locked_thread=locked)
    db = FakeDB(preflight, conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID,
            orch_main.ThreadInputRequest(content="must not race into the queue"),
            MagicMock(),
        )

    assert exc.value.status_code == 409
    assert conn.txn_enters == 1
    assert len(conn.calls) == 1
    locked_read = conn.calls[0]
    assert locked_read[0] == "fetchrow"
    assert "FROM threads" in locked_read[1]
    assert "FOR UPDATE" in locked_read[1]
    assert locked_read[3] == 1


@pytest.mark.asyncio
async def test_ended_stateless_input_requires_explicit_resume_before_writes(
    monkeypatch,
):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(_stateless_thread(status="ended"), conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException, match="status=ended") as exc:
        await orch_main.thread_input(
            THREAD_ID,
            orch_main.ThreadInputRequest(content="must resume first"),
            MagicMock(),
        )

    assert exc.value.status_code == 409
    assert conn.txn_enters == 1
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_suspended_sandbox_input_commits_then_schedules_workspace_restore(
    monkeypatch,
):
    import asyncio

    from orchestrator import main as orch_main

    thread = _stateless_thread(
        status="suspended",
        metadata=_k8s_sandbox_metadata(status="suspended"),
    )
    conn = FakeConn()
    db = FakeDB(thread, conn)
    _patch_common(monkeypatch, orch_main, db)
    ensure_workspace = AsyncMock()
    monkeypatch.setattr(orch_main, "ensure_session_workspace", ensure_workspace)

    out = await orch_main.thread_input(
        THREAD_ID,
        orch_main.ThreadInputRequest(content="wake and continue"),
        MagicMock(),
    )
    await asyncio.sleep(0)

    assert out["accepted"] is True
    ensure_workspace.assert_awaited_once_with(
        THREAD_ID,
        db=db,
        provisioner=orch_main.container_provisioner,
        suspension=orch_main.workspace_suspension_service,
    )
    admit = next(c for c in conn.calls if c[0] == "fetchval" and "run_queue" in c[1])
    assert admit[3] == 1


@pytest.mark.asyncio
async def test_stateless_input_accepts_none_workspace(monkeypatch):
    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(
        _stateless_thread(
            metadata={"config_override": {"workspace": {"backend": "none"}}}
        ),
        conn,
    )
    _patch_common(monkeypatch, orch_main, db)

    out = await orch_main.thread_input(
        THREAD_ID, orch_main.ThreadInputRequest(content="lite"), MagicMock()
    )

    assert out["accepted"] is True
    assert conn.txn_enters == 1


@pytest.mark.asyncio
async def test_stateless_input_owner_gate(monkeypatch):
    """Same fail-closed owner semantics as _resolve_thread_for_forwarding."""
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(_stateless_thread(user_id="somebody-else"), conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID, orch_main.ThreadInputRequest(content="x"), MagicMock()
        )
    assert exc.value.status_code == 403

    db_missing = FakeDB(None, conn)
    monkeypatch.setattr(orch_main, "postgres_db", db_missing)
    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID, orch_main.ThreadInputRequest(content="x"), MagicMock()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_pinned_thread_takes_legacy_forward_with_lock(monkeypatch):
    """A pinned-lane thread must go through the untouched forwarding path:
    _resolve_thread_for_forwarding + per-turn lock + agent forward, response
    nesting the agent object."""
    import asyncio

    from orchestrator import main as orch_main

    pinned = _stateless_thread(execution_lane="pinned")
    conn = FakeConn()
    db = FakeDB(pinned, conn)
    _patch_common(monkeypatch, orch_main, db)

    agent = {"id": "agent-1", "pod_ip": "10.0.0.9", "pod_port": 8001}
    resolve_spy = AsyncMock(return_value=(pinned, agent))
    monkeypatch.setattr(orch_main, "_resolve_thread_for_forwarding", resolve_spy)
    forward_spy = AsyncMock(
        return_value={"accepted": True, "turn_id": 5, "queue_depth": 1}
    )
    monkeypatch.setattr(orch_main, "_forward_to_agent", forward_spy)
    real_lock = orch_main._ensure_thread_turn_lock
    lock_spy = MagicMock(side_effect=real_lock)
    monkeypatch.setattr(orch_main, "_ensure_thread_turn_lock", lock_spy)
    # Neutralize the 5-minute deferred cleanup task so the loop closes clean.
    monkeypatch.setattr(orch_main, "_schedule_turn_lock_cleanup", MagicMock())

    out = await orch_main.thread_input(
        THREAD_ID, orch_main.ThreadInputRequest(content="hi"), MagicMock()
    )
    await asyncio.sleep(0)

    resolve_spy.assert_awaited_once()
    lock_spy.assert_called_once_with(THREAD_ID, 6)
    forward_spy.assert_awaited_once()
    args = forward_spy.await_args.args
    assert args[1] == "/api/input"
    assert args[2] == {"content": "hi", "turn_id": 6}
    assert out == {"accepted": True, "turn_id": 6, "agent": forward_spy.return_value}
    # The queue lane's transaction machinery must not have been touched.
    assert conn.txn_enters == 0


@pytest.mark.asyncio
async def test_uncorrelated_interrupt_on_stateless_lane_returns_422(monkeypatch):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    conn = FakeConn()
    db = FakeDB(_stateless_thread(), conn)
    _patch_common(monkeypatch, orch_main, db)
    monkeypatch.setattr(
        orch_main,
        "require_thread_owner",
        AsyncMock(return_value=(dict(USER), _stateless_thread())),
    )

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_interrupt(THREAD_ID, MagicMock(), None)
    assert exc.value.status_code == 422
    assert "target_turn_id" in exc.value.detail


@pytest.mark.asyncio
async def test_interrupt_on_pinned_lane_still_forwards(monkeypatch):
    from orchestrator import main as orch_main

    pinned = _stateless_thread(execution_lane="pinned")
    db = FakeDB(pinned, FakeConn())
    _patch_common(monkeypatch, orch_main, db)
    monkeypatch.setattr(
        orch_main,
        "require_thread_owner",
        AsyncMock(return_value=(dict(USER), pinned)),
    )
    agent = {"id": "agent-1", "pod_ip": "10.0.0.9", "pod_port": 8001}
    monkeypatch.setattr(
        orch_main,
        "_resolve_thread_for_forwarding",
        AsyncMock(return_value=(pinned, agent)),
    )
    forward_spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(orch_main, "_forward_to_agent", forward_spy)

    out = await orch_main.thread_interrupt(THREAD_ID, MagicMock(), None)
    forward_spy.assert_awaited_once_with(agent, "/api/interrupt", {})
    assert out == {"accepted": True, "agent": {"ok": True}}
