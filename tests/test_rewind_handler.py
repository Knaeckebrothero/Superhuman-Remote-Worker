"""Turn→commit mapping wiring (Task 4) + the rewind WS handler (Task 5)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from src.persistent_graph import PersistentLoopCallbacks


def _minimal_callbacks(**overrides):
    """Build the callbacks dataclass with every required field stubbed."""
    import inspect

    kwargs = {}
    for name, param in inspect.signature(PersistentLoopCallbacks).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = AsyncMock()
    kwargs.update(overrides)
    return PersistentLoopCallbacks(**kwargs)


def test_callbacks_accept_on_workspace_commit():
    spy = AsyncMock()
    cbs = _minimal_callbacks(on_workspace_commit=spy)
    assert cbs.on_workspace_commit is spy


def test_on_workspace_commit_defaults_none():
    cbs = _minimal_callbacks()
    assert cbs.on_workspace_commit is None


def test_loop_on_workspace_commit_records_via_conn(monkeypatch):
    from src.api import persistent_app as app_mod

    conn = MagicMock()
    conn.record_turn_commit = AsyncMock()
    session = MagicMock()
    session.postgres_conn = conn
    monkeypatch.setattr(app_mod, "_session", session)
    monkeypatch.setattr(app_mod, "_thread_id", "tid-1")

    asyncio.run(app_mod._loop_on_workspace_commit("sha42"))
    conn.record_turn_commit.assert_awaited_once_with("tid-1", "sha42")


def test_loop_on_workspace_commit_tolerates_no_session(monkeypatch):
    from src.api import persistent_app as app_mod

    monkeypatch.setattr(app_mod, "_session", None)
    asyncio.run(app_mod._loop_on_workspace_commit("sha42"))  # must not raise


def _mk_session(messages, git_active=False):
    conn = MagicMock()
    conn.get_live_message = AsyncMock()
    conn.apply_rewind = AsyncMock(
        return_value={"rewind_id": "r-1", "swept": 4, "surviving_turn": 2}
    )
    conn.resolve_restore_commit = AsyncMock(return_value="sha-target")
    git_mgr = MagicMock()
    git_mgr.is_active = git_active
    git_mgr.commit = MagicMock(return_value=True)
    git_mgr.get_current_commit = MagicMock(side_effect=["sha-snap", "sha-restore"])
    git_mgr.restore_tree = MagicMock(return_value=True)
    ws_mgr = SimpleNamespace(git_manager=git_mgr if git_active else None)
    session = SimpleNamespace(
        messages=messages,
        turn_count=9,
        postgres_conn=conn,
        workspace_manager=ws_mgr,
    )
    return session, conn, git_mgr


def _human(msg_id, text):
    m = HumanMessage(content=text)
    m.id = msg_id
    return m


def _run_rewind(app_mod, ws, data):
    asyncio.run(app_mod._handle_rewind(ws, data))


def _patched_app(monkeypatch, session, *, turn_open=False):
    from src.api import persistent_app as app_mod

    monkeypatch.setattr(app_mod, "_session", session)
    monkeypatch.setattr(app_mod, "_thread_id", "tid-1")
    monkeypatch.setattr(app_mod, "_turn_event_open", turn_open)
    monkeypatch.setattr(app_mod, "_tool_inflight", False)
    monkeypatch.setattr(app_mod, "_loop_user_queue", asyncio.Queue())
    monkeypatch.setattr(app_mod, "_loop_interrupt_flag", None)
    monkeypatch.setattr(app_mod, "_hard_interrupt_event", asyncio.Event())
    monkeypatch.setattr(
        app_mod, "_resolve_event_journal_epoch", AsyncMock(return_value=7)
    )
    monkeypatch.setattr(app_mod, "_restore_session_messages", AsyncMock())
    monkeypatch.setattr(app_mod, "_broadcast", MagicMock())
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)
    return app_mod, ws_sent


def test_rewind_shallow_truncates_in_place(monkeypatch):
    target = _human("msg_target", "redo this")
    msgs = [
        _human("msg_old", "earlier"),
        AIMessage(content="ok"),
        target,
        AIMessage(content="bad path"),
    ]
    session, conn, _ = _mk_session(msgs)
    conn.get_live_message.return_value = {
        "seq": 42,
        "role": "human",
        "content": "redo this",
    }
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "msg_target", "mode": "conversation", "request_id": "rq1"},
    )

    assert len(session.messages) == 2  # truncated at the target, inclusive
    assert session.turn_count == 2  # from apply_rewind surviving_turn
    conn.apply_rewind.assert_awaited_once()
    assert conn.apply_rewind.await_args.kwargs["from_seq"] == 42
    app_mod._restore_session_messages.assert_not_awaited()
    acks = [p for m, p in ws_sent if m == "rewind.ack"]
    assert acks and acks[0]["prompt"] == "redo this"
    assert acks[0]["request_id"] == "rq1"
    app_mod._broadcast.assert_called_once()
    assert app_mod._broadcast.call_args.args[0] == "rewind.done"


def test_rewind_deep_falls_back_to_rehydrate(monkeypatch):
    # Target id is NOT in the in-memory list (compacted away / restored prefix).
    msgs = [_human("msg_other", "x"), AIMessage(content="y")]
    session, conn, _ = _mk_session(msgs)
    conn.get_live_message.return_value = {"seq": 5, "role": "human", "content": "old"}
    # Nothing survives this rewind, so an empty post-rehydrate transcript is the
    # legitimate outcome (not the amnesia case covered by
    # test_rewind_deep_rehydrate_failure_sends_error_not_ack below) — the
    # empty+retry post-condition must not fire.
    conn.apply_rewind.return_value = {
        "rewind_id": "r-1",
        "swept": 2,
        "surviving_turn": 0,
    }
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "msg_gone", "mode": "conversation", "request_id": "rq2"},
    )

    assert session.messages == []  # cleared…
    app_mod._restore_session_messages.assert_awaited_once()  # …and rehydrated
    acks = [p for m, p in ws_sent if m == "rewind.ack"]
    assert acks  # legitimate empty transcript still acks normally


def test_rewind_deep_rehydrate_failure_sends_error_not_ack(monkeypatch):
    # Same shape as the deep-rewind test above, but surviving_turn=2 (the
    # _mk_session/apply_rewind default): turns are supposed to survive, yet
    # the (mocked, side-effect-free) rehydrate leaves messages empty — the
    # amnesia case Finding 1 guards against. DB sweep is already committed;
    # the initiator must get an error instead of a false-success ack.
    msgs = [_human("msg_other", "x"), AIMessage(content="y")]
    session, conn, _ = _mk_session(msgs)
    conn.get_live_message.return_value = {"seq": 5, "role": "human", "content": "old"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "msg_gone", "mode": "conversation", "request_id": "rq3"},
    )

    assert app_mod._restore_session_messages.await_count == 2  # initial + one retry
    assert not [p for m, p in ws_sent if m == "rewind.ack"]
    errors = [p for m, p in ws_sent if m == "error"]
    assert errors
    app_mod._broadcast.assert_called_once()
    assert app_mod._broadcast.call_args.args[0] == "rewind.done"


def test_rewind_validates_target_before_interrupting(monkeypatch):
    session, conn, _ = _mk_session([_human("m", "x")])
    conn.get_live_message.return_value = None  # invalid target
    app_mod, ws_sent = _patched_app(monkeypatch, session, turn_open=True)
    from src.api import persistent_app as pa

    pa._loop_user_queue.put_nowait("queued-input")

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "m", "mode": "conversation", "request_id": "r"},
    )

    errors = [p for m, p in ws_sent if m == "error"]
    assert errors
    assert pa._loop_interrupt_flag is None  # interrupt path never entered
    assert pa._loop_user_queue.qsize() == 1  # drain never ran — item still queued


def test_rewind_code_mode_requires_git(monkeypatch):
    session, conn, _ = _mk_session([_human("m", "x")], git_active=False)
    conn.get_live_message.return_value = {"seq": 3, "role": "human", "content": "x"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod, MagicMock(), {"message_id": "m", "mode": "code", "request_id": "r"}
    )

    errors = [p for m, p in ws_sent if m == "error"]
    assert errors and "version" in errors[0]["message"].lower()
    conn.apply_rewind.assert_not_awaited()


def test_rewind_both_git_failure_aborts_before_sweep(monkeypatch):
    target = _human("m", "x")
    session, conn, git = _mk_session([target], git_active=True)
    conn.get_live_message.return_value = {"seq": 3, "role": "human", "content": "x"}
    git.restore_tree = MagicMock(return_value=False)
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod, MagicMock(), {"message_id": "m", "mode": "both", "request_id": "r"}
    )

    conn.apply_rewind.assert_not_awaited()  # sweep gated behind git success
    assert session.messages == [target]  # memory untouched
    assert [m for m, _ in ws_sent if m == "error"]


def test_rewind_rejects_non_human_target(monkeypatch):
    session, conn, _ = _mk_session([_human("m", "x")])
    conn.get_live_message.return_value = {"seq": 4, "role": "ai", "content": "resp"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "m", "mode": "conversation", "request_id": "r"},
    )

    conn.apply_rewind.assert_not_awaited()
    assert [m for m, _ in ws_sent if m == "error"]


def test_rewind_drains_pending_queue(monkeypatch):
    target = _human("m", "x")
    session, conn, _ = _mk_session([target])
    conn.get_live_message.return_value = {"seq": 3, "role": "human", "content": "x"}
    app_mod, ws_sent = _patched_app(monkeypatch, session)
    from src.api import persistent_app as pa

    pa._loop_user_queue.put_nowait("queued-input")

    _run_rewind(
        app_mod,
        MagicMock(),
        {"message_id": "m", "mode": "conversation", "request_id": "r"},
    )

    assert pa._loop_user_queue.empty()


def test_compact_boundary_maps_to_keep_recent(monkeypatch):
    """boundary_message_id=X → keep_recent_override counts non-injection
    messages from X (inclusive) to the end."""
    from src.api import persistent_app as app_mod

    target = _human("msg_b", "keep from here")
    msgs = [
        _human("msg_a", "old"),
        AIMessage(content="old reply"),
        target,
        AIMessage(content="recent reply"),
    ]
    captured = {}

    async def _fake_summarize(**kwargs):
        captured.update(kwargs)
        return list(kwargs["messages"])

    ctx_mgr = MagicMock()
    ctx_mgr.summarize_and_compact = AsyncMock(side_effect=_fake_summarize)
    ctx_mgr.compaction_runs = 0
    session = SimpleNamespace(
        messages=msgs,
        turn_count=4,
        context_manager=ctx_mgr,
        auxiliary_llm=MagicMock(),
        config=SimpleNamespace(
            context_management=SimpleNamespace(max_summary_length=10000)
        ),
        workspace_manager=None,
        postgres_conn=MagicMock(),
    )
    monkeypatch.setattr(app_mod, "_session", session)
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)

    asyncio.run(app_mod._handle_compact(MagicMock(), "", boundary_message_id="msg_b"))

    assert ctx_mgr.summarize_and_compact.await_count == 1
    assert captured["keep_recent_override"] == 2  # target + 1 later message


def test_compact_boundary_unknown_id_errors(monkeypatch):
    from src.api import persistent_app as app_mod

    ctx_mgr = MagicMock()
    ctx_mgr.summarize_and_compact = AsyncMock()
    session = SimpleNamespace(
        messages=[_human("msg_a", "x")],
        context_manager=ctx_mgr,
        auxiliary_llm=MagicMock(),
        config=SimpleNamespace(
            context_management=SimpleNamespace(max_summary_length=10000)
        ),
        workspace_manager=None,
        postgres_conn=MagicMock(),
    )
    monkeypatch.setattr(app_mod, "_session", session)
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)

    asyncio.run(
        app_mod._handle_compact(MagicMock(), "", boundary_message_id="msg_missing")
    )

    ctx_mgr.summarize_and_compact.assert_not_awaited()
    assert [m for m, _ in ws_sent if m == "error"]


def test_compact_boundary_excludes_injections_from_keep_count(monkeypatch):
    """Workspace injection messages must not be counted in keep_recent_override."""
    from src.api import persistent_app as app_mod
    from src.core.workspace_injection import create_instruction_tool_messages

    # Create real instruction injection messages (AIMessage + ToolMessage pair)
    pre_injection_ai, pre_injection_tool = create_instruction_tool_messages(
        ".instructions", "pre-boundary instructions"
    )
    post_injection_ai, post_injection_tool = create_instruction_tool_messages(
        ".instructions", "post-boundary instructions"
    )

    target = _human("msg_target", "keep from here")
    msgs = [
        _human("msg_a", "old"),
        AIMessage(content="old reply"),
        pre_injection_ai,
        pre_injection_tool,
        target,
        AIMessage(content="recent reply"),
        post_injection_ai,
        post_injection_tool,
    ]
    captured = {}

    async def _fake_summarize(**kwargs):
        captured.update(kwargs)
        return list(kwargs["messages"])

    ctx_mgr = MagicMock()
    ctx_mgr.summarize_and_compact = AsyncMock(side_effect=_fake_summarize)
    ctx_mgr.compaction_runs = 0
    session = SimpleNamespace(
        messages=msgs,
        turn_count=8,
        context_manager=ctx_mgr,
        auxiliary_llm=MagicMock(),
        config=SimpleNamespace(
            context_management=SimpleNamespace(max_summary_length=10000)
        ),
        workspace_manager=None,
        postgres_conn=MagicMock(),
    )
    monkeypatch.setattr(app_mod, "_session", session)
    ws_sent = []

    async def _fake_ws_send(ws, method, params):
        ws_sent.append((method, params))

    monkeypatch.setattr(app_mod, "_ws_send", _fake_ws_send)

    asyncio.run(
        app_mod._handle_compact(MagicMock(), "", boundary_message_id="msg_target")
    )

    assert ctx_mgr.summarize_and_compact.await_count == 1
    # keep_recent should be 2: the target + the one non-injection msg after it
    # (post_injection_ai and post_injection_tool must NOT be counted)
    assert captured["keep_recent_override"] == 2
