"""Persistent-session visibility and loop-guard semantics for U5 children."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from shared.runtime.core.message_markers import PERSIST_ROLE_EVENT, PERSIST_ROLE_KEY
from agent.persistent_graph import (
    PersistentLoopCallbacks,
    TurnResult,
    _execute_turn,
    _inject_context_pairs,
    run_persistent_loop,
)


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
        "persist_message": AsyncMock(),
    }
    values.update(overrides)
    return PersistentLoopCallbacks(**values)


def _config(*, officer: bool = False, max_actions: int = 100) -> MagicMock:
    config = MagicMock()
    config.extra = {}
    config.llm.timeout = 600
    config.llm.model = "test-model"
    config.memory.enabled = False
    config.memory.observer_interval = 5
    config.memory.query = None
    config.context_management.max_summary_length = 10_000
    config.officer.enabled = officer
    config.officer.conference = False
    config.officer.max_actions_per_wake = max_actions
    return config


def _context_manager() -> AsyncMock:
    manager = AsyncMock()
    manager.should_summarize = MagicMock(return_value=False)
    manager.ensure_within_limits = AsyncMock(
        side_effect=lambda messages, *_args, **_kwargs: messages
    )
    return manager


def _tool(name: str, result: str = "ok") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.args_schema = None
    tool.ainvoke = AsyncMock(return_value=result)
    return tool


def _tool_call(name: str, call_id: str) -> dict:
    return {"name": name, "args": {}, "id": call_id}


def _streaming_llm(responses: list[AIMessage], captured: list[list]) -> MagicMock:
    response_iter = iter(responses)

    async def _astream(provider_input, **_kwargs):
        captured.append(list(provider_input))
        yield next(response_iter)

    llm = MagicMock()
    llm.reasoning = None
    llm.astream = _astream
    return llm


@pytest.mark.asyncio
async def test_persistent_loop_stamps_parent_turn_before_execution(monkeypatch):
    tool_context = SimpleNamespace()
    observed_turns: list[int] = []

    async def _execute_stub(**kwargs):
        observed_turns.append(kwargs["tool_context"]._current_turn_count)
        return TurnResult(turn_id=0, messages_added=0, tool_calls_made=0)

    monkeypatch.setattr("agent.persistent_graph._execute_turn", _execute_stub)
    callbacks = _callbacks(
        get_user_input=AsyncMock(
            side_effect=["delegate this", asyncio.CancelledError()]
        )
    )

    await run_persistent_loop(
        llm_with_tools=MagicMock(),
        tools=[],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=[],
        tool_context=tool_context,
        initial_turn_count=6,
    )

    assert observed_turns == [7]
    assert tool_context._current_turn_count == 7


def test_active_child_state_precedes_the_final_product_boundary():
    prepared = [SystemMessage(content="sys"), HumanMessage(content="question")]

    added = _inject_context_pairs(
        prepared,
        [],
        "",
        "",
        active_subagents_block="<active_subagents>one child</active_subagents>",
        product_guide_turn_boundary="<current_request>question</current_request>",
    )

    assert added == 2
    assert isinstance(prepared[-2], HumanMessage)
    assert prepared[-2].content == "<active_subagents>one child</active_subagents>"
    assert prepared[-2].additional_kwargs[PERSIST_ROLE_KEY] == PERSIST_ROLE_EVENT
    assert isinstance(prepared[-1], HumanMessage)
    assert prepared[-1].content == "<current_request>question</current_request>"


@pytest.mark.asyncio
async def test_active_child_state_is_refreshed_per_call_and_never_persisted():
    active_block = "<active_subagents>reviewer is running</active_subagents>"
    runtime = MagicMock()
    runtime.active_subagents_block = MagicMock(side_effect=[active_block, ""])
    tool_context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
    )
    captured: list[list] = []
    llm = _streaming_llm(
        [
            AIMessage(content="", tool_calls=[_tool_call("noop", "call-1")]),
            AIMessage(content="done"),
        ],
        captured,
    )
    callbacks = _callbacks()
    durable_messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="continue"),
    ]

    await _execute_turn(
        llm_with_tools=llm,
        tool_map={"noop": _tool("noop")},
        context_manager=_context_manager(),
        messages=durable_messages,
        callbacks=callbacks,
        llm_timeout=600,
        auxiliary_llm=None,
        config=_config(),
        tool_context=tool_context,
    )

    assert runtime.active_subagents_block.call_count == 2
    assert any(message.content == active_block for message in captured[0])
    assert all(message.content != active_block for message in captured[1])
    assert all(message.content != active_block for message in durable_messages)
    persisted = [call.args[0] for call in callbacks.persist_message.await_args_list]
    assert all(message.content != active_block for message in persisted)


@pytest.mark.asyncio
async def test_empty_retry_archives_each_response_with_its_fresh_provider_input():
    first_block = "<active_subagents>reviewer is running</active_subagents>"
    retry_block = "<active_subagents>reviewer completed</active_subagents>"
    runtime = MagicMock()
    runtime.active_subagents_block = MagicMock(side_effect=[first_block, retry_block])
    tool_context = SimpleNamespace(
        knowledge_bindings=[],
        citation_engine=None,
        subagent_runtime=runtime,
    )
    streamed = AIMessage(
        content="",
        response_metadata={
            "model_name": "empty-attempt",
            "token_usage": {"input_tokens": 11, "output_tokens": 0},
        },
    )
    retried = AIMessage(
        content="recovered",
        response_metadata={
            "model_name": "retry-attempt",
            "token_usage": {"input_tokens": 13, "output_tokens": 2},
        },
    )
    provider_inputs: list[list] = []

    async def _astream(provider_input, **_kwargs):
        provider_inputs.append(list(provider_input))
        yield streamed

    async def _ainvoke(provider_input, **_kwargs):
        provider_inputs.append(list(provider_input))
        return retried

    llm = MagicMock()
    llm.reasoning = True
    llm.astream = _astream
    llm.ainvoke = AsyncMock(side_effect=_ainvoke)
    archive = MagicMock()
    usage = AsyncMock()
    callbacks = _callbacks(archive_llm_call=archive, on_usage=usage)
    context_manager = _context_manager()
    context_manager.record_provider_usage = MagicMock()

    result = await _execute_turn(
        llm_with_tools=llm,
        tool_map={},
        context_manager=context_manager,
        messages=[SystemMessage(content="sys"), HumanMessage(content="continue")],
        callbacks=callbacks,
        llm_timeout=600,
        auxiliary_llm=None,
        config=_config(),
        tool_context=tool_context,
    )

    assert runtime.active_subagents_block.call_count == 2
    assert len(provider_inputs) == 2
    assert archive.call_count == 2
    first_input, first_response, first_metrics = archive.call_args_list[0].args
    retry_input, retry_response, retry_metrics = archive.call_args_list[1].args
    assert first_response is streamed
    assert retry_response is retried
    assert first_input == provider_inputs[0]
    assert retry_input == provider_inputs[1]
    assert first_metrics["model"] == "empty-attempt"
    assert retry_metrics["model"] == "retry-attempt"
    assert [
        call.args[0] for call in context_manager.record_provider_usage.call_args_list
    ] == [11, 13]
    assert [call.args[0]["model"] for call in usage.await_args_list] == [
        "empty-attempt",
        "retry-attempt",
    ]
    assert result.metrics["model"] == "retry-attempt"
    assert any(message.content == first_block for message in first_input)
    assert all(message.content != retry_block for message in first_input)
    assert any(message.content == retry_block for message in retry_input)
    assert all(message.content != first_block for message in retry_input)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["list_agents", "wait_agent"])
async def test_observation_controls_do_not_trip_repeated_action_guard(tool_name):
    captured: list[list] = []
    repeated_calls = [
        AIMessage(content="", tool_calls=[_tool_call(tool_name, f"call-{index}")])
        for index in range(5)
    ]
    llm = _streaming_llm([*repeated_calls, AIMessage(content="done")], captured)
    callbacks = _callbacks()
    durable_messages = [SystemMessage(content="sys"), HumanMessage(content="watch")]

    result = await _execute_turn(
        llm_with_tools=llm,
        tool_map={tool_name: _tool(tool_name)},
        context_manager=_context_manager(),
        messages=durable_messages,
        callbacks=callbacks,
        llm_timeout=600,
        auxiliary_llm=None,
        config=_config(officer=True),
    )

    # Five identical ordinary actions force-end an officer wake. These
    # observation controls instead reach the sixth provider call normally,
    # while remaining counted in the overall action total.
    assert len(captured) == 6
    assert result.tool_calls_made == 5
    assert not any(
        isinstance(message, HumanMessage)
        and str(message.content).startswith("[guard] Wake force-ended")
        for message in durable_messages
    )
