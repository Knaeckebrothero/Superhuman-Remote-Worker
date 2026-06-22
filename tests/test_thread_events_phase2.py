"""Tests for Phase 2 of headless persistent sessions: event log, interrupt
semantics, per-turn lock. DB-integration tests (migration apply, full SSE
replay, prune sweep) live separately and are gated on a running Postgres.

Covers:
  - _broadcast stamps (epoch, seq) cursors and schedules _persist_event.
  - _loop_check_interrupt tri-state mode (also exercised in test_persistent_app).
  - WS interrupt handler picks hard vs graceful based on _tool_inflight.
  - Interrupt mid-stream in persistent_graph: "hard" drops partial AIMessage,
    other modes (graceful, legacy bool) preserve it.
  - Orchestrator per-turn lock: same (thread, turn) shares one Lock,
    different (thread, turn) get distinct Locks, cleanup after timeout.
  - Agent REST endpoints (/api/input, /api/interrupt, /api/approve) routing
    and 503-when-detached behavior.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Section 1 — _broadcast cursor + _persist_event scheduling
# ---------------------------------------------------------------------------


class TestBroadcastCursor:
    """Phase 2 changes: _broadcast pre-increments seq, stamps (epoch, seq)
    into the frame's params, and schedules a fire-and-forget DB write."""

    def setup_method(self):
        import src.api.persistent_app as mod

        mod._subscribers.clear()
        mod._events_epoch = 0
        mod._next_seq = 0
        mod._session = None  # disables the DB write scheduling

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._subscribers.clear()
        mod._events_epoch = 0
        mod._next_seq = 0
        mod._session = None

    def test_broadcast_increments_seq_monotonically(self):
        import src.api.persistent_app as mod

        q = mod._subscribe("c1")
        mod._broadcast("token", {"content": "a"})
        mod._broadcast("token", {"content": "b"})
        mod._broadcast("token", {"content": "c"})

        f1 = q.get_nowait()
        f2 = q.get_nowait()
        f3 = q.get_nowait()
        assert f1["params"]["_seq"] == [0, 1]
        assert f2["params"]["_seq"] == [0, 2]
        assert f3["params"]["_seq"] == [0, 3]
        assert mod._next_seq == 3

    def test_broadcast_uses_current_epoch(self):
        import src.api.persistent_app as mod

        mod._events_epoch = 7
        mod._next_seq = 0
        q = mod._subscribe("c1")
        mod._broadcast("token", {"content": "x"})

        frame = q.get_nowait()
        assert frame["params"]["_seq"] == [7, 1]

    def test_broadcast_keeps_original_params(self):
        import src.api.persistent_app as mod

        q = mod._subscribe("c1")
        mod._broadcast("tool.started", {"tool": "read_file", "id": "tc1"})

        frame = q.get_nowait()
        # _seq added, original keys preserved.
        assert frame["params"]["tool"] == "read_file"
        assert frame["params"]["id"] == "tc1"
        assert "_seq" in frame["params"]

    @pytest.mark.asyncio
    async def test_broadcast_schedules_persist_task_when_session_present(
        self,
    ):
        """When _session is set with a postgres_conn, _broadcast spawns a
        _persist_event task. Without _session it's a pure broadcast."""
        import src.api.persistent_app as mod

        # Fake session with a no-op acquire that records the call.
        recorded = []

        class _FakeConn:
            async def execute(self, *args, **kwargs):
                recorded.append(args)

        class _Acquire:
            async def __aenter__(self):
                return _FakeConn()

            async def __aexit__(self, exc_type, exc, tb):
                return None

        fake_conn = MagicMock()
        fake_conn.acquire = lambda: _Acquire()
        fake_session = MagicMock()
        fake_session.postgres_conn = fake_conn

        mod._session = fake_session
        mod._thread_id = "thread-xyz"
        mod._subscribe("c1")

        mod._broadcast("token", {"content": "hi"})
        # The DB write is scheduled — let the event loop drain.
        await asyncio.sleep(0.05)

        assert recorded, "expected _persist_event to call execute()"
        # First arg is the INSERT SQL; subsequent args bind values.
        sql = recorded[0][0]
        assert "INSERT INTO thread_events" in sql


# ---------------------------------------------------------------------------
# Section 2 — WS interrupt handler picks hard vs graceful based on tool state
# ---------------------------------------------------------------------------


class TestInterruptModeSelection:
    """The /api/interrupt REST handler (and the WS interrupt branch) picks
    mode = 'graceful' if a tool is currently mid-`ainvoke`, else 'hard'.
    The mode picks the right behavior at the persistent_graph check sites."""

    def setup_method(self):
        import src.api.persistent_app as mod

        mod._loop_interrupt_flag = None
        mod._tool_inflight = False

    def test_loop_on_tool_start_sets_inflight(self):
        import src.api.persistent_app as mod

        async def _run():
            await mod._loop_on_tool_start("read_file", {"path": "/x"}, "tc1")

        asyncio.run(_run())
        assert mod._tool_inflight is True

    def test_loop_on_tool_result_clears_inflight(self):
        import src.api.persistent_app as mod

        mod._tool_inflight = True

        async def _run():
            await mod._loop_on_tool_result("read_file", "ok", "tc1")

        asyncio.run(_run())
        assert mod._tool_inflight is False


# ---------------------------------------------------------------------------
# Section 3 — persistent_graph interrupt semantics: hard drops partial,
#             graceful (and legacy bool) preserves it
# ---------------------------------------------------------------------------


class TestPersistentGraphInterruptModes:
    """Mid-stream interrupt mode controls whether the partial AIMessage is
    appended. The legacy bool API ('check_interrupt returns True') falls
    into the graceful branch — partial is preserved."""

    @pytest.mark.asyncio
    async def test_hard_interrupt_drops_partial_aimessage(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from src.persistent_graph import PersistentLoopCallbacks, _execute_turn

        # First chunk arrives, then interrupt fires before the second.
        chunk1 = AIMessage(content="Half-typed ")
        chunk2 = AIMessage(content="response")

        async def _astream(msgs, **kw):
            yield chunk1
            yield chunk2

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        # Site 1 (pre-LLM): None → continue into the LLM call.
        # Site 2 (mid-astream after chunk1): "hard" → break, drop partial.
        interrupt_seq = [None, "hard", None, None]

        def _check_interrupt():
            return interrupt_seq.pop(0) if interrupt_seq else None

        callbacks = PersistentLoopCallbacks(
            get_user_input=AsyncMock(return_value="hello"),
            on_token=AsyncMock(),
            on_thinking=AsyncMock(),
            on_tool_start=AsyncMock(),
            on_tool_result=AsyncMock(),
            permission_check=AsyncMock(return_value=True),
            on_turn_start=AsyncMock(),
            on_turn_complete=AsyncMock(),
            on_error=AsyncMock(),
            check_interrupt=_check_interrupt,
        )

        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]
        config = MagicMock()
        config.llm.timeout = 600
        config.context_management.max_summary_length = 10000

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=config,
        )

        assert result.interrupted is True
        # Hard interrupt: only system + human message; no partial AI message.
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)

    @pytest.mark.asyncio
    async def test_graceful_interrupt_preserves_partial_aimessage(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from src.persistent_graph import PersistentLoopCallbacks, _execute_turn

        chunk1 = AIMessage(content="Tool calling now")

        async def _astream(msgs, **kw):
            yield chunk1

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        # Site 1: None → enter the LLM call.
        # Site 2 mid-stream: "graceful" → break, but keep the partial.
        interrupt_seq = [None, "graceful", None, None]

        def _check_interrupt():
            return interrupt_seq.pop(0) if interrupt_seq else None

        callbacks = PersistentLoopCallbacks(
            get_user_input=AsyncMock(return_value="hello"),
            on_token=AsyncMock(),
            on_thinking=AsyncMock(),
            on_tool_start=AsyncMock(),
            on_tool_result=AsyncMock(),
            permission_check=AsyncMock(return_value=True),
            on_turn_start=AsyncMock(),
            on_turn_complete=AsyncMock(),
            on_error=AsyncMock(),
            check_interrupt=_check_interrupt,
        )

        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]
        config = MagicMock()
        config.llm.timeout = 600
        config.context_management.max_summary_length = 10000

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=config,
        )

        assert result.interrupted is True
        # Graceful interrupt: partial AI message kept in history.
        assert len(messages) == 3
        assert isinstance(messages[2], AIMessage)


# ---------------------------------------------------------------------------
# Section 4 — Orchestrator per-turn lock semantics
# ---------------------------------------------------------------------------


class TestPerTurnLock:
    """The per-turn lock dedupes concurrent POSTs from multi-tab cockpits.
    Two POSTs racing on the same (thread, turn) share one asyncio.Lock; the
    second sees lock.locked() and returns 409."""

    def setup_method(self):
        import orchestrator.main as om

        om._thread_turn_locks.clear()
        om._thread_turn_inflight.clear()

    def teardown_method(self):
        import orchestrator.main as om

        om._thread_turn_locks.clear()
        om._thread_turn_inflight.clear()

    def test_same_key_shares_one_lock(self):
        import orchestrator.main as om

        lock_a = om._ensure_thread_turn_lock("thread-x", 5)
        lock_b = om._ensure_thread_turn_lock("thread-x", 5)
        assert lock_a is lock_b

    def test_different_keys_distinct_locks(self):
        import orchestrator.main as om

        l_thread = om._ensure_thread_turn_lock("thread-x", 5)
        l_other_thread = om._ensure_thread_turn_lock("thread-y", 5)
        l_other_turn = om._ensure_thread_turn_lock("thread-x", 6)
        assert l_thread is not l_other_thread
        assert l_thread is not l_other_turn

    @pytest.mark.asyncio
    async def test_concurrent_acquire_returns_locked_for_second(self):
        import orchestrator.main as om

        lock = om._ensure_thread_turn_lock("thread-x", 1)
        await lock.acquire()
        try:
            # Second caller sees the lock held; the HTTP handler returns
            # 409 based on this signal.
            assert lock.locked() is True
        finally:
            lock.release()
        # After release the next caller can acquire.
        assert lock.locked() is False


# ---------------------------------------------------------------------------
# Section 5 — Agent REST input endpoints reject when no session
# ---------------------------------------------------------------------------


class TestAgentRestInputEndpointsNoSession:
    """All three new REST endpoints must 503 when _session is None — same
    contract as POST /session/detach."""

    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._loop_user_queue = None

    def test_api_input_503_without_session(self):
        from fastapi.testclient import TestClient

        from src.api.persistent_app import create_persistent_app

        app = create_persistent_app("interactive")
        client = TestClient(app)
        resp = client.post("/api/input", json={"content": "hi"})
        assert resp.status_code == 503

    def test_api_interrupt_503_without_session(self):
        from fastapi.testclient import TestClient

        from src.api.persistent_app import create_persistent_app

        app = create_persistent_app("interactive")
        client = TestClient(app)
        resp = client.post("/api/interrupt")
        assert resp.status_code == 503

    def test_api_approve_503_without_session(self):
        from fastapi.testclient import TestClient

        from src.api.persistent_app import create_persistent_app

        app = create_persistent_app("interactive")
        client = TestClient(app)
        resp = client.post("/api/approve", json={"decision": "approve"})
        assert resp.status_code == 503

    def test_api_input_rejects_empty_content(self):
        import asyncio as _asyncio

        from fastapi.testclient import TestClient

        import src.api.persistent_app as mod
        from src.api.persistent_app import create_persistent_app

        # Stand up a minimal session so we get past the 503 gate.
        mod._session = MagicMock()
        mod._loop_user_queue = _asyncio.Queue()
        mod._loop_last_user_content = [""]

        try:
            app = create_persistent_app("interactive")
            client = TestClient(app)
            resp = client.post("/api/input", json={"content": ""})
            assert resp.status_code == 400
        finally:
            mod._session = None
            mod._loop_user_queue = None

    @pytest.mark.asyncio
    async def test_api_input_starts_loop_without_websocket(self):
        """Direct/SSE sessions must not depend on a control WebSocket attach.

        The REST endpoint used by Cockpit should both accept the message and
        ensure the persistent loop is running, otherwise the input sits in
        _loop_user_queue forever.
        """
        import json
        from types import SimpleNamespace

        from starlette.requests import Request

        import src.api.persistent_app as mod

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps({"content": "hello from REST"}).encode(),
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
        )

        seen_inputs: list[str] = []

        async def fake_run_persistent_loop(**kwargs):
            seen_inputs.append(await kwargs["callbacks"].get_user_input())

        mod._thread_id = "thread-rest"
        mod._loop_task = None
        mod._loop_user_queue = asyncio.Queue()
        mod._loop_last_user_content = [""]
        mod._hard_interrupt_event = asyncio.Event()
        mod._session = SimpleNamespace(
            llm_with_tools=object(),
            tools=[],
            context_manager=object(),
            config=SimpleNamespace(
                llm=SimpleNamespace(timeout=1),
                interactive=SimpleNamespace(idle_timeout_minutes=0),
            ),
            system_prompt="system",
            messages=[],
            auxiliary_llm=None,
            recall_store=None,
            knowledge_store=None,
            project_id=None,
            project_ids=[],
            tool_context=None,
            turn_count=0,
            postgres_conn=None,
            memory_extraction_prompt="",
            memory_service=None,  # legacy path (manager flag off)
        )

        try:
            with (
                patch.object(
                    mod,
                    "run_persistent_loop",
                    new=fake_run_persistent_loop,
                ),
                patch.object(mod, "_loop_completion_handler", new=AsyncMock()),
            ):
                response = await mod.handle_api_input(request)
                assert response.status_code == 200
                await asyncio.wait_for(mod._loop_task, timeout=1)

            # Accept-time persistence wraps queue items as {content, id} so
            # the loop reuses the already-persisted row id
            # (session_silent_failure_audit.md #1).
            assert [i["content"] for i in seen_inputs] == ["hello from REST"]
            assert all(i["id"].startswith("msg_") for i in seen_inputs)
        finally:
            mod._session = None
            mod._thread_id = None
            mod._loop_task = None
            mod._loop_user_queue = None
            mod._hard_interrupt_event = None


# ---------------------------------------------------------------------------
# Section 6 — No-cursor SSE replay anchors past the last completed turn
# ---------------------------------------------------------------------------


class TestNoCursorReplayStart:
    """A fresh SSE attach (no cached cursor — e.g. opening the session on a
    second device) must NOT replay the whole epoch from seq 0. The client has
    already loaded the thread's completed turns from REST history; replaying
    them as live frames re-renders the last assistant turn twice, split by a
    spurious "SESSION RESUMED" divider (the cold-attach twin of the
    gone_beyond_horizon duplicate render). The replay must start just past the
    last turn-terminal event so only the in-flight turn is replayed."""

    @pytest.mark.asyncio
    async def test_anchors_past_last_terminal_event(self):
        import orchestrator.main as om

        captured = {}

        class _Conn:
            async def fetchval(self, sql, *args):
                captured["sql"] = sql
                captured["args"] = args
                # Simulate MAX(seq) of the epoch's terminal events.
                return 7

        start = await om._no_cursor_replay_start(_Conn(), "thread-x", 3)

        # Replay only seq > 7 (the in-flight turn), NOT the whole epoch.
        assert start == 7
        # The anchor must filter on turn-terminal kinds, not every event —
        # otherwise it would stop at the last token and still re-replay the
        # completed turn's text.
        assert "turn.completed" in captured["sql"]
        assert "turn.error" in captured["sql"]
        assert captured["args"] == ("thread-x", 3)

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_turn_has_finished(self):
        """First turn still in flight (no terminal event yet) → replay from 0
        so the in-flight first turn, absent from REST history, is delivered."""
        import orchestrator.main as om

        class _Conn:
            async def fetchval(self, sql, *args):
                return 0  # COALESCE(MAX(seq), 0) with no terminal rows

        start = await om._no_cursor_replay_start(_Conn(), "thread-x", 0)
        assert start == 0

    @pytest.mark.asyncio
    async def test_coerces_null_max_to_zero(self):
        import orchestrator.main as om

        class _Conn:
            async def fetchval(self, sql, *args):
                return None

        start = await om._no_cursor_replay_start(_Conn(), "thread-x", 0)
        assert start == 0
