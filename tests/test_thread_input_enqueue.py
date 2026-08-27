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

from src.shared.pinned_session_identity import PinnedSessionBinding

THREAD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER = {"id": "user-1", "is_admin": False}
_USE_DB_THREAD = object()


def _pinned_binding() -> PinnedSessionBinding:
    return PinnedSessionBinding(
        thread_id=THREAD_ID,
        runtime_generation="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        agent_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        runtime_attach_token="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        agent_hostname="persistent-aaaaaaaaaaaa",
        pod_namespace="srw",
        pod_uid="pod-uid-a",
        pod_ip="10.0.0.9",
        pod_port=8001,
        agent_status="session",
    )


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
        if "status = 'created'" in q and "status = 'suspended'" in q:
            return THREAD_ID
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
    schedule = MagicMock()
    monkeypatch.setattr(orch_main, "_schedule_stateless_workspace_ensure", schedule)

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
    schedule.assert_not_called()


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
    schedule = MagicMock()

    def _after_commit(thread_id):
        assert thread_id == THREAD_ID
        assert conn.txn_depth == 0

    schedule.side_effect = _after_commit
    monkeypatch.setattr(orch_main, "_schedule_stateless_workspace_ensure", schedule)

    out = await orch_main.thread_input(
        THREAD_ID,
        orch_main.ThreadInputRequest(content="sandbox turn"),
        MagicMock(),
    )

    assert out["accepted"] is True
    assert conn.txn_enters == 1
    schedule.assert_called_once_with(THREAD_ID)


@pytest.mark.asyncio
async def test_awaiting_user_sandbox_input_commits_before_workspace_ensure(monkeypatch):
    """A waiting thread is admitted durably, then wakes the physical workspace."""
    from orchestrator import main as orch_main

    thread = _stateless_thread(status="awaiting_user", metadata=_k8s_sandbox_metadata())
    conn = FakeConn()
    db = FakeDB(thread, conn)
    _patch_common(monkeypatch, orch_main, db)
    observed_depths = []
    schedule = MagicMock(
        side_effect=lambda _thread_id: observed_depths.append(conn.txn_depth)
    )
    monkeypatch.setattr(orch_main, "_schedule_stateless_workspace_ensure", schedule)

    out = await orch_main.thread_input(
        THREAD_ID,
        orch_main.ThreadInputRequest(content="continue from approval"),
        MagicMock(),
    )

    assert out["accepted"] is True
    assert observed_depths == [0]
    admit = next(c for c in conn.calls if c[0] == "fetchval" and "run_queue" in c[1])
    assert admit[3] == 1


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
async def test_stateless_input_rechecks_locked_protected_cloud_before_any_write(
    monkeypatch,
):
    """A protected marker landing after preflight cannot enter run_queue."""
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    preflight = _stateless_thread()
    locked_metadata = _k8s_sandbox_metadata()
    locked_metadata["protected_cloud"] = True
    conn = FakeConn(
        locked_thread=_stateless_thread(metadata=locked_metadata),
    )
    db = FakeDB(preflight, conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID,
            orch_main.ThreadInputRequest(content="must remain pinned"),
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
@pytest.mark.parametrize("field", ["enabled", "conference"])
@pytest.mark.parametrize("value", [None, 0, "", [], {}, "yes", 1])
async def test_stateless_input_refuses_malformed_session_class_before_writes(
    monkeypatch,
    field,
    value,
):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    metadata = {
        "config_override": {
            "workspace": {"backend": "virtual"},
            "officer": {field: value},
        }
    }
    conn = FakeConn()
    db = FakeDB(_stateless_thread(metadata=metadata), conn)
    _patch_common(monkeypatch, orch_main, db)

    with pytest.raises(HTTPException) as exc:
        await orch_main.thread_input(
            THREAD_ID,
            orch_main.ThreadInputRequest(content="must remain pinned"),
            MagicMock(),
        )

    assert exc.value.status_code == 409
    assert conn.calls == []


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
    wake = next(
        c
        for c in conn.calls
        if c[0] == "fetchval"
        and "status = 'created'" in c[1]
        and "status = 'suspended'" in c[1]
    )
    assert wake[3] == 1
    assert conn.calls.index(wake) < next(
        index
        for index, call in enumerate(conn.calls)
        if call[0] == "fetchval" and "INSERT INTO thread_messages" in call[1]
    )
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
    schedule = MagicMock()
    monkeypatch.setattr(orch_main, "_schedule_stateless_workspace_ensure", schedule)

    out = await orch_main.thread_input(
        THREAD_ID, orch_main.ThreadInputRequest(content="lite"), MagicMock()
    )

    assert out["accepted"] is True
    assert conn.txn_enters == 1
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_stateless_workspace_ensure_scheduler_is_single_flight(monkeypatch):
    import asyncio

    from orchestrator import main as orch_main

    started = asyncio.Event()
    release = asyncio.Event()
    ensure = AsyncMock()

    async def _ensure(*_args, **_kwargs):
        started.set()
        await release.wait()

    ensure.side_effect = _ensure
    monkeypatch.setattr(orch_main, "ensure_session_workspace", ensure)
    orch_main._stateless_workspace_ensure_tasks.pop(THREAD_ID, None)

    first = orch_main._schedule_stateless_workspace_ensure(THREAD_ID)
    await started.wait()
    second = orch_main._schedule_stateless_workspace_ensure(THREAD_ID)

    assert second is first
    ensure.assert_awaited_once()
    release.set()
    await first
    await asyncio.sleep(0)
    assert THREAD_ID not in orch_main._stateless_workspace_ensure_tasks


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

    binding = _pinned_binding()
    resolve_spy = AsyncMock(return_value=(pinned, binding))
    monkeypatch.setattr(orch_main, "_resolve_thread_for_forwarding", resolve_spy)
    revalidate_spy = AsyncMock(return_value=binding)
    monkeypatch.setattr(
        orch_main,
        "_revalidate_pinned_forwarding_binding",
        revalidate_spy,
    )
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
    revalidate_spy.assert_awaited_once_with(binding)
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
    binding = _pinned_binding()
    monkeypatch.setattr(
        orch_main,
        "_resolve_thread_for_forwarding",
        AsyncMock(return_value=(pinned, binding)),
    )
    forward_spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(orch_main, "_forward_to_agent", forward_spy)

    out = await orch_main.thread_interrupt(THREAD_ID, MagicMock(), None)
    forward_spy.assert_awaited_once_with(binding, "/api/interrupt", {})
    assert out == {"accepted": True, "agent": {"ok": True}}


@pytest.mark.asyncio
async def test_exact_forward_rechecks_after_client_entry_and_adds_fingerprint(
    monkeypatch,
):
    from orchestrator import main as orch_main

    binding = _pinned_binding()
    order: list[str] = []
    observed: dict = {}

    class _Response:
        status_code = 202
        text = '{"accepted":true}'
        headers = {}

        @staticmethod
        def json():
            return {"accepted": True}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            order.append("client_enter")
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, json):
            order.append("post")
            observed.update(url=url, json=json)
            return _Response()

    async def _revalidate(current):
        order.append("db_recheck")
        assert current is binding
        return binding

    monkeypatch.setattr(orch_main.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        orch_main,
        "_revalidate_pinned_forwarding_binding",
        _revalidate,
    )

    result = await orch_main._forward_to_agent(
        binding,
        "/api/input",
        {"content": "hello", "turn_id": 6},
    )

    assert result == {"accepted": True}
    assert order == ["client_enter", "db_recheck", "post"]
    assert observed == {
        "url": "http://10.0.0.9:8001/api/input",
        "json": {
            "content": "hello",
            "turn_id": 6,
            "session_identity_fingerprint": binding.session_identity_fingerprint,
        },
    }


@pytest.mark.asyncio
async def test_exact_forward_binding_loss_after_client_entry_sends_nothing(
    monkeypatch,
):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    binding = _pinned_binding()
    post = AsyncMock()

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    refusal = HTTPException(
        status_code=409,
        detail={
            "code": "session_binding_invalid",
            "pinned_runtime_generation_contract": 1,
            "session_runtime_generation": binding.runtime_generation,
        },
    )
    monkeypatch.setattr(orch_main.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        orch_main,
        "_revalidate_pinned_forwarding_binding",
        AsyncMock(side_effect=refusal),
    )

    with pytest.raises(HTTPException) as caught:
        await orch_main._forward_to_agent(
            binding,
            "/api/input",
            {"content": "must not move"},
        )

    assert caught.value is refusal
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_identity_mismatch_becomes_generation_bound_refusal(monkeypatch):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    binding = _pinned_binding()

    class _Response:
        status_code = 409
        text = '{"error":"session_identity_mismatch"}'
        headers = {}

        @staticmethod
        def json():
            return {"error": "session_identity_mismatch", "retryable": True}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(orch_main.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        orch_main,
        "_revalidate_pinned_forwarding_binding",
        AsyncMock(return_value=binding),
    )

    with pytest.raises(HTTPException) as caught:
        await orch_main._forward_to_agent(binding, "/api/interrupt", {})

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "session_binding_invalid",
        "message": "This session binding is no longer authoritative.",
        "pinned_runtime_generation_contract": 1,
        "session_runtime_generation": binding.runtime_generation,
    }


@pytest.mark.asyncio
async def test_pinned_input_rechecks_binding_after_turn_lock_before_forward(
    monkeypatch,
):
    from fastapi import HTTPException

    from orchestrator import main as orch_main

    pinned = _stateless_thread(execution_lane="pinned")
    binding = _pinned_binding()
    db = FakeDB(pinned, FakeConn())
    _patch_common(monkeypatch, orch_main, db)
    monkeypatch.setattr(
        orch_main,
        "_resolve_thread_for_forwarding",
        AsyncMock(return_value=(pinned, binding)),
    )
    order: list[str] = []

    class _Lock:
        @staticmethod
        def locked():
            return False

        async def __aenter__(self):
            order.append("lock_enter")
            return self

        async def __aexit__(self, *args):
            order.append("lock_exit")
            return False

    monkeypatch.setattr(orch_main, "_ensure_thread_turn_lock", lambda *_: _Lock())
    monkeypatch.setattr(orch_main, "_schedule_turn_lock_cleanup", MagicMock())
    refusal = HTTPException(
        status_code=409,
        detail={"code": "session_binding_invalid"},
    )

    async def _reject(_binding):
        order.append("binding_recheck")
        raise refusal

    monkeypatch.setattr(
        orch_main,
        "_revalidate_pinned_forwarding_binding",
        _reject,
    )
    forward = AsyncMock()
    monkeypatch.setattr(orch_main, "_forward_to_agent", forward)

    with pytest.raises(HTTPException) as caught:
        await orch_main.thread_input(
            THREAD_ID,
            orch_main.ThreadInputRequest(content="stay exact"),
            MagicMock(),
        )

    assert caught.value is refusal
    assert order == ["lock_enter", "binding_recheck", "lock_exit"]
    forward.assert_not_awaited()
