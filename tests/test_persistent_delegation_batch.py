"""Persistent-session delegation batch semantics (U5-B).

Delegation-only provider batches fan out concurrently but retain provider
result order.  A batch that mixes ``delegate_agent`` with any other tool is
rejected as a unit before permission or the external-effect boundary.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.workspace_backend import WorkspaceUnavailableError
from src.persistent_graph import PersistentLoopCallbacks, _execute_turn


def _callbacks(**overrides) -> PersistentLoopCallbacks:
    values = {
        "get_user_input": AsyncMock(),
        "on_token": AsyncMock(),
        "on_thinking": AsyncMock(),
        "on_tool_start": AsyncMock(),
        "on_tool_result": AsyncMock(),
        "permission_check": AsyncMock(return_value=True),
        "on_turn_start": AsyncMock(),
        "on_turn_complete": AsyncMock(),
        "on_error": AsyncMock(),
        "check_interrupt": MagicMock(return_value=False),
        "on_tool_execution_start": AsyncMock(),
        "announce_permission_batch": AsyncMock(),
    }
    values.update(overrides)
    return PersistentLoopCallbacks(**values)


def _config() -> MagicMock:
    config = MagicMock()
    config.extra = {}
    config.llm.timeout = 600
    config.memory.enabled = False
    config.memory.observer_interval = 5
    config.context_management.max_summary_length = 10_000
    return config


def _context_manager() -> AsyncMock:
    return AsyncMock(
        ensure_within_limits=AsyncMock(
            side_effect=lambda messages, *args, **kwargs: messages
        )
    )


def _tool(name: str, side_effect) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.args_schema = None
    tool.ainvoke = AsyncMock(side_effect=side_effect)
    return tool


def _tool_call(name: str, call_id: str, **args) -> dict:
    return {"name": name, "args": args, "id": call_id}


def _llm_for_batch(tool_calls: list[dict]) -> AsyncMock:
    responses = iter(
        [
            AIMessage(content="", tool_calls=tool_calls),
            AIMessage(content="done"),
        ]
    )

    async def _astream(_messages, **_kwargs):
        yield next(responses)

    llm = AsyncMock()
    llm.reasoning = None
    llm.astream = _astream
    return llm


async def _run_batch(
    tool_calls: list[dict],
    tools: dict[str, MagicMock],
    *,
    callbacks: PersistentLoopCallbacks,
    tool_context=None,
):
    messages = [SystemMessage(content="sys"), HumanMessage(content="go")]
    result = await _execute_turn(
        llm_with_tools=_llm_for_batch(tool_calls),
        tool_map=tools,
        context_manager=_context_manager(),
        messages=messages,
        callbacks=callbacks,
        llm_timeout=600,
        auxiliary_llm=None,
        config=_config(),
        tool_context=tool_context,
    )
    return result, messages


@pytest.mark.asyncio
async def test_delegation_only_batch_fans_out_and_keeps_result_order():
    """The second child may finish first; provider pairing remains call order."""

    first_started = asyncio.Event()
    second_finished = asyncio.Event()
    completion_order: list[str] = []

    async def _delegate(model_call: dict):
        call_id = model_call["id"]
        if call_id == "child-1":
            first_started.set()
            # Sequential execution deadlocks here and becomes an error result,
            # so this is a behavioral concurrency proof rather than timing math.
            await asyncio.wait_for(second_finished.wait(), timeout=0.5)
            completion_order.append(call_id)
            return "first report"
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        completion_order.append(call_id)
        second_finished.set()
        return "second report"

    delegate = _tool("delegate_agent", _delegate)
    runtime = MagicMock()
    tool_context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
        _fork_source=None,
    )
    callbacks = _callbacks()

    result, messages = await _run_batch(
        [
            _tool_call("delegate_agent", "child-1", prompt="one"),
            _tool_call("delegate_agent", "child-2", prompt="two"),
        ],
        {"delegate_agent": delegate},
        callbacks=callbacks,
        tool_context=tool_context,
    )

    assert completion_order == ["child-2", "child-1"]
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert [(message.tool_call_id, message.content) for message in results] == [
        ("child-1", "first report"),
        ("child-2", "second report"),
    ]
    assert result.tool_calls_made == 2
    runtime.begin_batch.assert_called_once_with(2)
    assert tool_context._fork_source is messages

    # StructuredTool's InjectedToolCallId only works with the full model call;
    # the persistent loop must not use the ordinary args-only invocation here.
    invoked = [awaited.args[0] for awaited in delegate.ainvoke.await_args_list]
    assert [call["id"] for call in invoked] == ["child-1", "child-2"]
    assert all(call["type"] == "tool_call" for call in invoked)


@pytest.mark.asyncio
async def test_mixed_delegation_batch_rejects_every_call_before_effects():
    effects: list[str] = []

    async def _delegate(_args):
        effects.append("delegate")
        return "must not run"

    async def _write(_args):
        effects.append("write")
        return "must not run"

    delegate = _tool("delegate_agent", _delegate)
    write = _tool("write_file", _write)
    runtime = MagicMock()
    tool_context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
        _fork_source=None,
    )
    callbacks = _callbacks()

    result, messages = await _run_batch(
        [
            _tool_call("delegate_agent", "child-1", prompt="one"),
            _tool_call("write_file", "write-1", path="out.txt", content="x"),
        ],
        {"delegate_agent": delegate, "write_file": write},
        callbacks=callbacks,
        tool_context=tool_context,
    )

    assert effects == []
    delegate.ainvoke.assert_not_awaited()
    write.ainvoke.assert_not_awaited()
    callbacks.permission_check.assert_not_awaited()
    callbacks.announce_permission_batch.assert_not_awaited()
    callbacks.on_tool_start.assert_not_awaited()
    callbacks.on_tool_execution_start.assert_not_awaited()
    callbacks.on_tool_result.assert_not_awaited()
    runtime.begin_batch.assert_not_called()
    assert result.tool_calls_made == 0

    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in results] == ["child-1", "write-1"]
    assert all(
        "no tool in this batch was executed" in message.content for message in results
    )
    assert all(
        "cannot be batched with other tools (`write_file`)" in message.content
        for message in results
    )


@pytest.mark.asyncio
async def test_delegation_permissions_do_not_reorder_tool_results():
    async def _delegate(model_call: dict):
        return f"report {model_call['id']}"

    async def _permission(_name, _args, call_id):
        if call_id == "child-2":
            return False
        return True

    delegate = _tool("delegate_agent", _delegate)
    runtime = MagicMock()
    tool_context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
        _fork_source=None,
    )

    result, messages = await _run_batch(
        [
            _tool_call("delegate_agent", "child-1", prompt="one"),
            _tool_call("delegate_agent", "child-2", prompt="two"),
            _tool_call("delegate_agent", "child-3", prompt="three"),
        ],
        {"delegate_agent": delegate},
        callbacks=_callbacks(permission_check=AsyncMock(side_effect=_permission)),
        tool_context=tool_context,
    )

    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert [(message.tool_call_id, message.content) for message in results] == [
        ("child-1", "report child-1"),
        ("child-2", "User declined this tool call."),
        ("child-3", "report child-3"),
    ]
    assert result.tool_calls_made == 2
    runtime.begin_batch.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_non_delegation_batch_keeps_sequential_args_only_execution():
    trace: list[tuple[str, int]] = []

    async def _read(args):
        trace.append(("start", args["number"]))
        await asyncio.sleep(0)
        trace.append(("finish", args["number"]))
        return f"result {args['number']}"

    read = _tool("read_file", _read)
    runtime = MagicMock()
    tool_context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
        _fork_source=None,
    )

    result, messages = await _run_batch(
        [
            _tool_call("read_file", "read-1", number=1),
            _tool_call("read_file", "read-2", number=2),
        ],
        {"read_file": read},
        callbacks=_callbacks(),
        tool_context=tool_context,
    )

    assert trace == [
        ("start", 1),
        ("finish", 1),
        ("start", 2),
        ("finish", 2),
    ]
    assert [awaited.args[0] for awaited in read.ainvoke.await_args_list] == [
        {"number": 1},
        {"number": 2},
    ]
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.content for message in results] == ["result 1", "result 2"]
    assert result.tool_calls_made == 2
    runtime.begin_batch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        WorkspaceUnavailableError("workspace disappeared"),
        asyncio.CancelledError("child cancelled"),
    ],
)
async def test_delegation_failure_cancels_and_joins_siblings_before_unwind(failure):
    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def _delegate(model_call: dict):
        if model_call["id"] == "failing":
            await sibling_started.wait()
            raise failure
        sibling_started.set()
        try:
            await asyncio.Future()
        finally:
            sibling_finished.set()

    delegate = _tool("delegate_agent", _delegate)
    context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=MagicMock(),
        _fork_source=None,
    )

    with pytest.raises(type(failure)):
        await _run_batch(
            [
                _tool_call("delegate_agent", "failing", prompt="fail"),
                _tool_call("delegate_agent", "sibling", prompt="wait"),
            ],
            {"delegate_agent": delegate},
            callbacks=_callbacks(),
            tool_context=context,
        )

    assert sibling_finished.is_set()


@pytest.mark.asyncio
async def test_parent_cancellation_cancels_and_joins_every_delegation():
    both_started = asyncio.Event()
    finished: set[str] = set()
    started: set[str] = set()

    async def _delegate(model_call: dict):
        call_id = model_call["id"]
        started.add(call_id)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Future()
        finally:
            finished.add(call_id)

    delegate = _tool("delegate_agent", _delegate)
    context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=MagicMock(),
        _fork_source=None,
    )
    run = asyncio.create_task(
        _run_batch(
            [
                _tool_call("delegate_agent", "child-1", prompt="one"),
                _tool_call("delegate_agent", "child-2", prompt="two"),
            ],
            {"delegate_agent": delegate},
            callbacks=_callbacks(),
            tool_context=context,
        )
    )
    await both_started.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert finished == {"child-1", "child-2"}


@pytest.mark.asyncio
async def test_interrupt_during_final_permission_prevents_entire_batch():
    interrupted = False
    permission_calls = 0

    async def _permission(_name, _args, _call_id):
        nonlocal interrupted, permission_calls
        permission_calls += 1
        if permission_calls == 2:
            interrupted = True
        return True

    delegate = _tool("delegate_agent", AsyncMock(return_value="must not run"))
    runtime = MagicMock()
    context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
        _fork_source=None,
    )
    callbacks = _callbacks(
        permission_check=AsyncMock(side_effect=_permission),
        check_interrupt=MagicMock(side_effect=lambda: interrupted),
    )

    result, messages = await _run_batch(
        [
            _tool_call("delegate_agent", "child-1", prompt="one"),
            _tool_call("delegate_agent", "child-2", prompt="two"),
        ],
        {"delegate_agent": delegate},
        callbacks=callbacks,
        tool_context=context,
    )

    assert result.interrupted is True
    delegate.ainvoke.assert_not_awaited()
    runtime.begin_batch.assert_not_called()
    events = [message for message in messages if isinstance(message, HumanMessage)]
    assert "2 tool call(s) (delegate_agent, delegate_agent)" in events[-1].content
    assert "they were not executed" in events[-1].content
