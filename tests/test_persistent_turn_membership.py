"""Turn membership + the pinned live request through a session turn.

knowledge-base/knowledge/issues/stateless_turn_settlement_crashes_after_midturn_compaction.md:
the persistent loop stamps every message a turn appends (input included)
and pins the input for the turn's duration so a mid-turn summary re-seats
it verbatim; the pin comes off before settlement, the stamp stays.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from shared.runtime.core.message_markers import (
    PROTECTED_KEY,
    PROTECTED_TURN_INPUT,
    turn_membership,
)
from agent.persistent_graph import PersistentLoopCallbacks, run_persistent_loop


def _config() -> MagicMock:
    config = MagicMock()
    config.llm.timeout = 30
    config.memory.enabled = False
    config.memory.observer_interval = 5
    config.context_management.max_summary_length = 10_000
    config.officer.enabled = False
    return config


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


def _llm(*replies: str) -> MagicMock:
    queue = iter(replies)

    async def _astream(_messages, **_kwargs):
        yield AIMessage(content=next(queue))

    llm = MagicMock(reasoning=None)
    llm.astream = _astream
    return llm


@pytest.mark.asyncio
async def test_turn_input_is_pinned_while_the_turn_runs_and_unpinned_after():
    pins_seen_at_compaction = []
    manager = MagicMock()
    manager.compaction_runs = 0

    async def _bound(messages, *_a, **_k):
        # What the summariser would see: the live input carries the pin.
        pins_seen_at_compaction.append(
            [
                m.additional_kwargs.get(PROTECTED_KEY)
                for m in messages
                if isinstance(m, HumanMessage)
            ]
        )
        return messages

    manager.ensure_within_limits = _bound
    messages: list = []
    callbacks = _callbacks(["first request"])

    await run_persistent_loop(
        llm_with_tools=_llm("done"),
        tools=[],
        context_manager=manager,
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=messages,
    )

    assert pins_seen_at_compaction and pins_seen_at_compaction[0] == [
        PROTECTED_TURN_INPUT
    ]
    inputs = [m for m in messages if isinstance(m, HumanMessage)]
    assert len(inputs) == 1
    assert PROTECTED_KEY not in inputs[0].additional_kwargs
    assert turn_membership(inputs[0]) == 1
    answers = [m for m in messages if isinstance(m, AIMessage)]
    assert answers and turn_membership(answers[-1]) == 1
    callbacks.on_turn_complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_each_turn_stamps_its_own_messages():
    manager = MagicMock()
    manager.compaction_runs = 0
    manager.ensure_within_limits = AsyncMock(side_effect=lambda m, *a, **k: m)
    messages: list = []
    callbacks = _callbacks(["one", "two"])

    await run_persistent_loop(
        llm_with_tools=_llm("a1", "a2"),
        tools=[],
        context_manager=manager,
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=messages,
    )

    stamps = [
        turn_membership(m) for m in messages if isinstance(m, (HumanMessage, AIMessage))
    ]
    assert stamps == [1, 1, 2, 2]
    # Only the live turn's input is pinned; a finished turn's is not.
    assert all(
        PROTECTED_KEY not in m.additional_kwargs
        for m in messages
        if isinstance(m, HumanMessage)
    )
    assert callbacks.on_turn_complete.await_count == 2
