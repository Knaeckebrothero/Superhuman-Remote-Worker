"""LF-5: interrupted tool turns cannot wedge a live persistent session."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import BadRequestError

from src.persistent_graph import PersistentLoopCallbacks, run_persistent_loop


def _config() -> MagicMock:
    config = MagicMock()
    config.llm.timeout = 30
    config.memory.enabled = False
    config.memory.observer_interval = 5
    config.context_management.max_summary_length = 10_000
    config.officer.enabled = False
    return config


def _context_manager() -> MagicMock:
    manager = MagicMock()
    manager.ensure_within_limits = AsyncMock(
        side_effect=lambda messages, *_a, **_k: messages
    )
    return manager


def _callbacks(inputs, **overrides) -> PersistentLoopCallbacks:
    queue = iter(inputs)

    async def _input():
        try:
            return next(queue)
        except StopIteration:
            raise asyncio.CancelledError from None

    defaults = {
        "get_user_input": _input,
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
    defaults.update(overrides)
    return PersistentLoopCallbacks(**defaults)


def _bad_request(message: str) -> BadRequestError:
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://provider.invalid/v1/responses"),
        json={"error": {"message": message, "type": "invalid_request_error"}},
    )
    return BadRequestError(
        message,
        response=response,
        body={"message": message, "type": "invalid_request_error"},
    )


@pytest.mark.asyncio
async def test_interruption_after_persist_self_repairs_the_next_in_process_turn():
    """The exact post-persist/pre-tool fault leaves the same process usable."""

    seen_inputs: list[list] = []
    calls = 0

    async def _astream(provider_input, **_kwargs):
        nonlocal calls
        calls += 1
        seen_inputs.append(list(provider_input))
        if calls == 1:
            yield AIMessage(
                content="",
                tool_calls=[{"id": "call_orphan", "name": "inspect", "args": {}}],
            )
        else:
            yield AIMessage(content="Recovered without a restart.")

    llm = MagicMock(reasoning=None)
    llm.astream = _astream
    tool = MagicMock(name="inspect")
    tool.name = "inspect"
    tool.ainvoke = AsyncMock(return_value="must not run")

    seam_calls = 0

    async def _fault_seam(_response):
        nonlocal seam_calls
        seam_calls += 1
        return seam_calls == 1

    persisted: list = []

    async def _persist(message):
        persisted.append(message)

    messages: list = []
    callbacks = _callbacks(
        ["first turn", "next turn"],
        persist_message=_persist,
        after_assistant_tool_calls_persisted=_fault_seam,
    )
    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[tool],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=messages,
    )

    assert calls == 2
    tool.ainvoke.assert_not_awaited()
    assert any(
        isinstance(message, AIMessage) and message.tool_calls for message in persisted
    ), "the real assistant tool-call response must remain durably truthful"
    assert not any(isinstance(message, ToolMessage) for message in persisted)
    assert any(
        isinstance(message, HumanMessage)
        and "were not executed" in str(message.content)
        for message in persisted
    )
    second_call_ids = {
        tool_call["id"]
        for message in seen_inputs[1]
        if isinstance(message, AIMessage)
        for tool_call in (message.tool_calls or [])
    }
    assert "call_orphan" not in second_call_ids
    assert any(
        isinstance(message, AIMessage)
        and message.content == "Recovered without a restart."
        for message in messages
    )


@pytest.mark.asyncio
async def test_two_equivalent_pairing_400s_escalate_once_and_stop_spend():
    error = _bad_request("No tool output found for function call call_first")
    provider_calls = 0

    async def _astream(_messages, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        # A changed volatile call id is still the same invariant failure.
        call_id = "call_first" if provider_calls == 1 else "call_second"
        raise _bad_request(f"No tool output found for function call {call_id}")
        yield  # pragma: no cover - make this an async generator

    llm = MagicMock(reasoning=None)
    llm.astream = _astream
    on_error = AsyncMock()
    callbacks = _callbacks(["one", "two", "three"], on_error=on_error)

    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=[],
    )

    assert error.status_code == 400  # pins the injected provider shape
    assert provider_calls == 2, "the queued third turn must spend no LLM call"
    surfaced = [str(call.args[0]) for call in on_error.await_args_list]
    assert sum("Session halted after two consecutive" in text for text in surfaced) == 1
    assert callbacks.on_turn_complete.await_count == 2


@pytest.mark.asyncio
async def test_non_pairing_provider_400_keeps_normal_error_behavior(monkeypatch):
    monkeypatch.setattr("src.persistent_graph._SESSION_LLM_RETRY_BASE_DELAY", 0.0)
    provider_calls = 0

    async def _astream(_messages, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        # Two complete ordinary turns exhaust the existing three-attempt
        # generic retry budget. Unlike pairing errors they are surfaced
        # normally and never enter the LF-5 circuit.
        if provider_calls <= 6:
            raise _bad_request("Invalid schema for response_format")
        yield AIMessage(content="third turn still ran")

    llm = MagicMock(reasoning=None)
    llm.astream = _astream
    on_error = AsyncMock()
    messages: list = []
    callbacks = _callbacks(["one", "two", "three"], on_error=on_error)

    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=messages,
    )

    assert provider_calls == 7
    assert all(
        "Session halted after two consecutive" not in str(call.args[0])
        for call in on_error.await_args_list
    )
    assert on_error.await_count == 2
    assert all(
        "Invalid schema for response_format" in str(call.args[0])
        for call in on_error.await_args_list
    )
    assert any(
        isinstance(message, AIMessage) and message.content == "third turn still ran"
        for message in messages
    )


@pytest.mark.asyncio
async def test_normal_paired_tool_traffic_is_unchanged():
    provider_inputs: list[list] = []
    provider_calls = 0

    async def _astream(messages, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        provider_inputs.append(list(messages))
        if provider_calls == 1:
            yield AIMessage(
                content="",
                tool_calls=[{"id": "call_ok", "name": "inspect", "args": {}}],
            )
        else:
            yield AIMessage(content="done")

    llm = MagicMock(reasoning=None)
    llm.astream = _astream
    tool = MagicMock()
    tool.name = "inspect"
    tool.ainvoke = AsyncMock(return_value="result")
    messages: list = []

    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[tool],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=_callbacks(["run it"]),
        messages=messages,
    )

    tool.ainvoke.assert_awaited_once_with({})
    second_call_ids = {
        tool_call["id"]
        for message in provider_inputs[1]
        if isinstance(message, AIMessage)
        for tool_call in (message.tool_calls or [])
    }
    second_result_ids = {
        message.tool_call_id
        for message in provider_inputs[1]
        if isinstance(message, ToolMessage)
    }
    assert second_call_ids == second_result_ids == {"call_ok"}
