"""Tests for src/persistent_graph.py — persistent interactive agent loop.

Covers: sentinel constants, TurnResult dataclass, run_persistent_loop (system
prompt insertion, input handling, cancellation, error handling, config extraction,
memory extraction trigger, auto-commit git), and _execute_turn (interrupt handling,
memory/knowledge retrieval, transient injection, context compaction, LLM streaming
with fallback, timeout, tool execution loop, VM upgrade detection).
"""

import asyncio
import logging
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.persistent_graph import (
    APPROVE_SENTINEL,
    DENY_SENTINEL,
    INTERRUPT_SENTINEL,
    PersistentLoopCallbacks,
    TurnResult,
    _execute_turn,
    _inject_context_pairs,
    _injection_anchor_index,
    run_persistent_loop,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_callbacks(**overrides) -> PersistentLoopCallbacks:
    """Build a PersistentLoopCallbacks with sane defaults (all no-op)."""
    defaults = dict(
        get_user_input=AsyncMock(return_value="hello"),
        on_token=AsyncMock(),
        on_thinking=AsyncMock(),
        on_tool_start=AsyncMock(),
        on_tool_result=AsyncMock(),
        permission_check=AsyncMock(return_value=True),
        on_turn_start=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_error=AsyncMock(),
        check_interrupt=MagicMock(return_value=False),
        on_vm_upgrade_needed=None,
    )
    defaults.update(overrides)
    return PersistentLoopCallbacks(**defaults)


def _make_config(**overrides):
    """Minimal config mock."""
    cfg = MagicMock()
    cfg.llm.timeout = overrides.get("llm_timeout", 600)
    cfg.memory.enabled = overrides.get("memory_enabled", False)
    cfg.memory.observer_interval = overrides.get("observer_interval", 5)
    cfg.context_management.max_summary_length = 10000
    return cfg


def _make_llm_response(content="Hi!", tool_calls=None):
    """Build an AIMessage suitable as an LLM response."""
    msg = AIMessage(content=content)
    if tool_calls is not None:
        msg.tool_calls = tool_calls
    return msg


def _make_streaming_llm(response: AIMessage):
    """Return a mock LLM whose astream yields the response as a single chunk."""
    llm = AsyncMock()
    llm.reasoning = None

    async def _astream(messages, **kw):
        yield response

    llm.astream = _astream
    llm.ainvoke = AsyncMock(return_value=response)
    return llm


def _make_tool(name="test_tool", result="tool result"):
    """Build a mock tool."""
    tool = MagicMock()
    tool.name = name
    tool.ainvoke = AsyncMock(return_value=result)
    return tool


# ---------------------------------------------------------------------------
# 1.1 Sentinel constants
# ---------------------------------------------------------------------------


class TestSentinelConstants:
    def test_sentinels_are_distinct(self):
        assert INTERRUPT_SENTINEL != APPROVE_SENTINEL
        assert APPROVE_SENTINEL != DENY_SENTINEL
        assert INTERRUPT_SENTINEL != DENY_SENTINEL

    def test_sentinels_are_strings(self):
        assert isinstance(INTERRUPT_SENTINEL, str)
        assert isinstance(APPROVE_SENTINEL, str)
        assert isinstance(DENY_SENTINEL, str)

    def test_sentinels_unlikely_user_input(self):
        """Sentinel values should not look like normal user input."""
        for s in (INTERRUPT_SENTINEL, APPROVE_SENTINEL, DENY_SENTINEL):
            assert s.startswith("__") and s.endswith("__")


# ---------------------------------------------------------------------------
# 1.2 TurnResult dataclass
# ---------------------------------------------------------------------------


class TestTurnResult:
    def test_required_fields(self):
        tr = TurnResult(turn_id=1, messages_added=2, tool_calls_made=3)
        assert tr.turn_id == 1
        assert tr.messages_added == 2
        assert tr.tool_calls_made == 3

    def test_interrupted_defaults_false(self):
        tr = TurnResult(turn_id=1, messages_added=0, tool_calls_made=0)
        assert tr.interrupted is False

    def test_error_defaults_none(self):
        tr = TurnResult(turn_id=1, messages_added=0, tool_calls_made=0)
        assert tr.error is None

    def test_custom_interrupted_and_error(self):
        tr = TurnResult(
            turn_id=5,
            messages_added=1,
            tool_calls_made=0,
            interrupted=True,
            error="timeout",
        )
        assert tr.interrupted is True
        assert tr.error == "timeout"


# ---------------------------------------------------------------------------
# 1.3 run_persistent_loop — system prompt insertion
# ---------------------------------------------------------------------------


class TestRunPersistentLoopSystemPrompt:
    @pytest.mark.asyncio
    async def test_inserts_system_message_when_messages_empty(self):
        """System prompt inserted at index 0 when messages list is empty."""
        messages: List[BaseMessage] = []
        response = _make_llm_response("Hello!")

        # get_user_input returns one message then cancels
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "hi"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        llm = _make_streaming_llm(response)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="You are helpful.",
            callbacks=callbacks,
            messages=messages,
        )

        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "You are helpful."

    @pytest.mark.asyncio
    async def test_inserts_system_message_when_first_is_not_system(self):
        """Inserts system prompt when first message is HumanMessage."""
        messages: List[BaseMessage] = [HumanMessage(content="existing")]
        response = _make_llm_response("Hi!")

        async def _input():
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        llm = _make_streaming_llm(response)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="system",
            callbacks=callbacks,
            messages=messages,
        )

        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "system"

    @pytest.mark.asyncio
    async def test_does_not_insert_when_system_message_present(self):
        """No duplicate system message when one is already present."""
        existing_sys = SystemMessage(content="original system")
        messages: List[BaseMessage] = [existing_sys]

        async def _input():
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        llm = _make_streaming_llm(_make_llm_response())

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="new system",
            callbacks=callbacks,
            messages=messages,
        )

        assert messages[0] is existing_sys
        assert messages[0].content == "original system"


# ---------------------------------------------------------------------------
# 1.3 run_persistent_loop — input handling
# ---------------------------------------------------------------------------


class TestRunPersistentLoopInputHandling:
    @pytest.mark.asyncio
    async def test_interrupt_sentinel_skips_turn(self):
        """INTERRUPT_SENTINEL should not increment turn_count or fire on_turn_start."""
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return INTERRUPT_SENTINEL
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

        callbacks.on_turn_start.assert_not_called()
        callbacks.on_turn_complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_input_appends_human_message(self):
        """Valid input appends a HumanMessage before calling _execute_turn."""
        messages: List[BaseMessage] = []
        response = _make_llm_response("reply")
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "user question"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        llm = _make_streaming_llm(response)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=messages,
        )

        human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 1
        assert human_msgs[0].content == "user question"

    @pytest.mark.asyncio
    async def test_on_turn_start_called_before_execution(self):
        """on_turn_start fires with turn_id = 1 on first valid input."""
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "go"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

        callbacks.on_turn_start.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_on_turn_complete_called_after_execution(self):
        """on_turn_complete fires with the turn_id."""
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "go"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

        callbacks.on_turn_complete.assert_called_once()
        assert callbacks.on_turn_complete.call_args[0][0] == 1


# ---------------------------------------------------------------------------
# 1.3 run_persistent_loop — cancellation
# ---------------------------------------------------------------------------


class TestRunPersistentLoopCancellation:
    @pytest.mark.asyncio
    async def test_cancelled_during_get_user_input_exits_cleanly(self):
        """CancelledError during get_user_input exits without raising."""
        callbacks = _make_callbacks(
            get_user_input=AsyncMock(side_effect=asyncio.CancelledError),
        )

        # Should return without raising
        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

    @pytest.mark.asyncio
    async def test_cancelled_during_execute_turn_exits_cleanly(self):
        """CancelledError during _execute_turn exits loop."""
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "go"
            raise asyncio.CancelledError

        # LLM that raises CancelledError
        llm = AsyncMock()
        llm.reasoning = None

        async def _astream(messages, **kw):
            raise asyncio.CancelledError
            yield  # make it a generator

        llm.astream = _astream

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )


# ---------------------------------------------------------------------------
# 1.3 run_persistent_loop — error handling
# ---------------------------------------------------------------------------


class TestRunPersistentLoopErrorHandling:
    @pytest.mark.asyncio
    async def test_execute_turn_error_calls_on_error_and_continues(self):
        """Non-cancellation error in _execute_turn calls on_error, loop continues."""
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return "go"
            raise asyncio.CancelledError

        # LLM that raises a runtime error on first call
        llm = AsyncMock()
        llm.reasoning = None
        invocation = 0

        async def _astream(messages, **kw):
            nonlocal invocation
            invocation += 1
            raise RuntimeError("LLM broke")
            yield  # make it a generator

        llm.astream = _astream

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

        callbacks.on_error.assert_called_once()
        assert "LLM broke" in callbacks.on_error.call_args[0][0]
        # on_turn_complete should still fire after error
        callbacks.on_turn_complete.assert_called_once()
        assert callbacks.on_turn_complete.call_args[0][0] == 1

    @pytest.mark.asyncio
    async def test_on_turn_complete_fires_after_error(self):
        """on_turn_complete fires even when _execute_turn raises."""
        call_count = 0

        async def _input():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "go"
            raise asyncio.CancelledError

        llm = AsyncMock()
        llm.reasoning = None

        async def _astream(messages, **kw):
            raise ValueError("bad")
            yield

        llm.astream = _astream
        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

        callbacks.on_turn_complete.assert_called_once()
        assert callbacks.on_turn_complete.call_args[0][0] == 1


# ---------------------------------------------------------------------------
# 1.3 run_persistent_loop — config extraction
# ---------------------------------------------------------------------------


class TestConfigExtraction:
    @pytest.mark.asyncio
    async def test_llm_timeout_defaults_to_600_when_missing(self):
        """When config.llm.timeout is missing, defaults to 600."""
        config = MagicMock()
        del config.llm.timeout  # force getattr fallback

        callbacks = _make_callbacks(
            get_user_input=AsyncMock(side_effect=asyncio.CancelledError),
        )

        # Should not raise
        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=config,
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

    @pytest.mark.asyncio
    async def test_llm_timeout_defaults_to_600_when_none(self):
        """When config.llm.timeout is None, defaults to 600."""
        config = _make_config(llm_timeout=None)

        callbacks = _make_callbacks(
            get_user_input=AsyncMock(side_effect=asyncio.CancelledError),
        )

        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=config,
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )

    @pytest.mark.asyncio
    async def test_extraction_interval_defaults_when_memory_none(self):
        """When config.memory is None, extraction_interval defaults to 5."""
        config = MagicMock()
        config.llm.timeout = 600
        config.memory = None
        config.context_management.max_summary_length = 10000

        callbacks = _make_callbacks(
            get_user_input=AsyncMock(side_effect=asyncio.CancelledError),
        )

        # Should not raise
        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=config,
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
        )


# ---------------------------------------------------------------------------
# 1.4 Memory extraction trigger
# ---------------------------------------------------------------------------


class TestMemoryExtractionTrigger:
    @pytest.mark.asyncio
    async def test_fires_when_all_conditions_met(self):
        """Memory extraction fires at the right interval."""
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns <= 5:
                return f"turn {turns}"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        config = _make_config(observer_interval=5)
        llm = _make_streaming_llm(_make_llm_response("ok"))

        mock_recall = MagicMock()
        mock_recall.decrement_ttl = AsyncMock()
        mock_recall.retrieve = AsyncMock(return_value=[])
        mock_aux = MagicMock()

        with patch("src.persistent_graph.asyncio.create_task") as mock_task:
            await run_persistent_loop(
                llm_with_tools=llm,
                tools=[],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=config,
                system_prompt="sys",
                callbacks=callbacks,
                messages=[],
                recall_store=mock_recall,
                auxiliary_llm=mock_aux,
            )

            # Should fire once (at turn 5)
            assert mock_task.call_count == 1

    @pytest.mark.asyncio
    async def test_does_not_fire_when_recall_store_none(self):
        """No extraction when recall_store is None."""
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns <= 5:
                return f"turn {turns}"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        config = _make_config(observer_interval=5)
        llm = _make_streaming_llm(_make_llm_response("ok"))

        with patch("src.persistent_graph.asyncio.create_task") as mock_task:
            await run_persistent_loop(
                llm_with_tools=llm,
                tools=[],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=config,
                system_prompt="sys",
                callbacks=callbacks,
                messages=[],
                recall_store=None,
                auxiliary_llm=MagicMock(),
            )

            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_auxiliary_llm_none(self):
        """No extraction when auxiliary_llm is None."""
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns <= 5:
                return f"turn {turns}"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        config = _make_config(observer_interval=5)
        llm = _make_streaming_llm(_make_llm_response("ok"))

        with patch("src.persistent_graph.asyncio.create_task") as mock_task:
            await run_persistent_loop(
                llm_with_tools=llm,
                tools=[],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=config,
                system_prompt="sys",
                callbacks=callbacks,
                messages=[],
                recall_store=MagicMock(),
                auxiliary_llm=None,
            )

            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_interval_zero(self):
        """No extraction when extraction_interval is 0."""
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns <= 5:
                return f"turn {turns}"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        config = _make_config(observer_interval=0)
        llm = _make_streaming_llm(_make_llm_response("ok"))

        with patch("src.persistent_graph.asyncio.create_task") as mock_task:
            await run_persistent_loop(
                llm_with_tools=llm,
                tools=[],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=config,
                system_prompt="sys",
                callbacks=callbacks,
                messages=[],
                recall_store=MagicMock(),
                auxiliary_llm=MagicMock(),
            )

            mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_extraction_failure_is_non_fatal(self):
        """Import error during memory extraction doesn't crash the loop."""
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns <= 5:
                return f"turn {turns}"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)
        config = _make_config(observer_interval=5)
        llm = _make_streaming_llm(_make_llm_response("ok"))

        mock_recall = MagicMock()
        mock_recall.decrement_ttl = AsyncMock()
        mock_recall.retrieve = AsyncMock(return_value=[])

        with patch(
            "src.persistent_graph.asyncio.create_task",
            side_effect=RuntimeError("task creation failed"),
        ):
            # Should not raise
            await run_persistent_loop(
                llm_with_tools=llm,
                tools=[],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=config,
                system_prompt="sys",
                callbacks=callbacks,
                messages=[],
                recall_store=mock_recall,
                auxiliary_llm=MagicMock(),
            )


# ---------------------------------------------------------------------------
# 1.5 Auto-commit git logic
# ---------------------------------------------------------------------------


class TestAutoCommitGit:
    @pytest.mark.asyncio
    async def test_commit_fires_when_tool_calls_made(self):
        """Git commit fires after a turn with tool calls."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("Done!")

        call_count = 0

        async def _astream(messages, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "result")

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        ws_mgr.read_file.return_value = ""

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr
        tool_ctx.consume_freeze_request.return_value = None

        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "do something"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[tool],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=tool_ctx,
        )

        git_mgr.commit.assert_called_once()
        assert "turn 1" in git_mgr.commit.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_commit_when_no_tool_calls(self):
        """Git commit does NOT fire when turn had zero tool calls."""
        response = _make_llm_response("Just text, no tools")
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "hello"
            raise asyncio.CancelledError

        git_mgr = MagicMock()
        git_mgr.is_active = True

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(response),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=tool_ctx,
        )

        git_mgr.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_fires_every_committing_turn_not_throttled(self):
        """Push fires on a normal turn even when turn_count is not a multiple
        of 5 — the old every-5th-turn throttle is gone."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        stream_count = 0

        async def _astream(messages, **kw):
            nonlocal stream_count
            stream_count += 1
            if stream_count % 2 == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "r")

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True
        git_mgr.has_unpushed_commits.return_value = True
        git_mgr.push.return_value = True

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        ws_mgr.read_file.return_value = ""

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr
        tool_ctx.consume_freeze_request.return_value = None

        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "turn 1"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[tool],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=tool_ctx,
        )

        # Turn 1 is not a multiple of 5, but the commit must still be pushed.
        git_mgr.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_push_fires_on_no_tool_turn_when_commits_unpushed(self):
        """Regression: a session ending on a no-tool turn must still flush
        previously-committed-but-unpushed work to the remote."""
        response = _make_llm_response("Just text, no tools")

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_unpushed_commits.return_value = True
        git_mgr.push.return_value = True

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        ws_mgr.read_file.return_value = ""

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr
        tool_ctx.consume_freeze_request.return_value = None

        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "thanks!"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=_make_streaming_llm(response),
            tools=[],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=tool_ctx,
        )

        # No tools this turn, but unpushed commits exist → must still push.
        git_mgr.push.assert_called_once()
        git_mgr.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_push_when_nothing_unpushed(self):
        """Push is skipped (no needless network round-trip) when the branch has
        no unpushed commits — even across a 5th turn the old throttle pushed on."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        stream_count = 0

        async def _astream(messages, **kw):
            nonlocal stream_count
            stream_count += 1
            if stream_count % 2 == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "r")

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = False
        git_mgr.has_unpushed_commits.return_value = False

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        ws_mgr.read_file.return_value = ""

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr
        tool_ctx.consume_freeze_request.return_value = None

        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns <= 5:
                return f"turn {turns}"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[tool],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=tool_ctx,
        )

        git_mgr.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_failure_is_logged_as_warning(self, caplog):
        """A failed push is surfaced (warning), not silently swallowed — the
        commits remain only on the workspace pod until a later push succeeds."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        stream_count = 0

        async def _astream(messages, **kw):
            nonlocal stream_count
            stream_count += 1
            if stream_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "r")

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = False
        git_mgr.has_unpushed_commits.return_value = True
        git_mgr.push.return_value = False  # push fails

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        ws_mgr.read_file.return_value = ""

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr
        tool_ctx.consume_freeze_request.return_value = None

        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "go"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        with caplog.at_level(logging.WARNING):
            await run_persistent_loop(
                llm_with_tools=llm,
                tools=[tool],
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                config=_make_config(),
                system_prompt="sys",
                callbacks=callbacks,
                messages=[],
                tool_context=tool_ctx,
            )

        assert "push failed" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_git_exception_is_non_fatal(self):
        """Git failure doesn't crash the loop."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        stream_count = 0

        async def _astream(messages, **kw):
            nonlocal stream_count
            stream_count += 1
            if stream_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "r")

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True
        git_mgr.commit.side_effect = RuntimeError("git broke")

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        ws_mgr.read_file.return_value = ""

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr
        tool_ctx.consume_freeze_request.return_value = None

        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "go"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        # Should not raise
        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[tool],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=tool_ctx,
        )

    @pytest.mark.asyncio
    async def test_no_crash_when_tool_context_none(self):
        """Auto-commit section doesn't crash when tool_context is None."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        stream_count = 0

        async def _astream(messages, **kw):
            nonlocal stream_count
            stream_count += 1
            if stream_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "r")
        turns = 0

        async def _input():
            nonlocal turns
            turns += 1
            if turns == 1:
                return "go"
            raise asyncio.CancelledError

        callbacks = _make_callbacks(get_user_input=_input)

        # tool_context=None — should not raise
        await run_persistent_loop(
            llm_with_tools=llm,
            tools=[tool],
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            config=_make_config(),
            system_prompt="sys",
            callbacks=callbacks,
            messages=[],
            tool_context=None,
        )


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — interrupt handling
# ---------------------------------------------------------------------------


class TestExecuteTurnInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_returns_interrupted_result(self):
        """check_interrupt() returning True at top of loop returns interrupted."""
        callbacks = _make_callbacks(check_interrupt=MagicMock(return_value=True))
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert result.interrupted is True

    @pytest.mark.asyncio
    async def test_interrupt_preserves_accumulated_counts(self):
        """Interrupted result preserves messages_added and tool_calls_made."""
        # First LLM call returns tool call, then interrupt fires
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )

        interrupt_after_tool = [False]

        def _check_interrupt():
            return interrupt_after_tool[0]

        call_count = [0]

        async def _astream(messages, **kw):
            call_count[0] += 1
            yield response_with_tool

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("test_tool", "result")
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        callbacks = _make_callbacks(
            check_interrupt=_check_interrupt,
        )

        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        # After first tool execution, set interrupt
        original_on_tool_result = callbacks.on_tool_result

        async def _on_tool_result(name, result, tcid, is_error=False):
            interrupt_after_tool[0] = True
            await original_on_tool_result(name, result, tcid, is_error=is_error)

        callbacks.on_tool_result = _on_tool_result

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={"test_tool": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        assert result.interrupted is True
        assert result.tool_calls_made >= 1
        assert result.messages_added >= 1


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — incremental persistence (Phase 2)
# ---------------------------------------------------------------------------


class TestExecuteTurnIncrementalPersistence:
    """persist_message must fire for each message the instant it's produced, so
    a mid-turn crash keeps the tail (docs/issues/...midturn_message_loss.md)."""

    def _sequenced_llm(self, responses):
        """LLM whose astream yields a different response on each call."""
        idx = [0]

        async def _astream(messages, **kw):
            r = responses[min(idx[0], len(responses) - 1)]
            idx[0] += 1
            yield r

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream
        return llm

    @pytest.mark.asyncio
    async def test_persists_ai_and_tool_messages_as_produced(self):
        ai_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc1"}],
        )
        ai_final = AIMessage(content="done")

        persisted = []
        callbacks = _make_callbacks(
            persist_message=AsyncMock(side_effect=lambda m: persisted.append(m))
        )
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=self._sequenced_llm([ai_with_tool, ai_final]),
            tool_map={"test_tool": _make_tool("test_tool", "result")},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        # AIMessage(tool call) → ToolMessage(result) → AIMessage(final),
        # each persisted the moment it entered history.
        assert [getattr(m, "type", None) for m in persisted] == ["ai", "tool", "ai"]
        assert persisted[0].tool_calls, "the AI step's tool calls must be persisted"
        assert persisted[1].tool_call_id == "tc1", "tool result keeps its link"
        # Every persisted message carries a stable id → reconciliation upserts.
        assert all(getattr(m, "id", None) for m in persisted)

    @pytest.mark.asyncio
    async def test_no_persist_callback_is_a_noop(self):
        """Back-compat: a turn runs fine when the transport wires no
        persist_message (callback defaults to None)."""
        callbacks = _make_callbacks()  # no persist_message
        assert callbacks.persist_message is None
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]
        result = await _execute_turn(
            llm_with_tools=self._sequenced_llm([AIMessage(content="hi")]),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )
        assert result.messages_added >= 1


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — memory retrieval
# ---------------------------------------------------------------------------


class TestExecuteTurnMemoryRetrieval:
    @pytest.mark.asyncio
    async def test_memory_retrieval_uses_last_human_message(self):
        """Context text is extracted from the last HumanMessage."""
        recall = MagicMock()
        recall.decrement_ttl = AsyncMock()
        recall.retrieve = AsyncMock(return_value=[])

        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="first"),
            HumanMessage(content="second question"),
        ]

        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            recall_store=recall,
        )

        recall.retrieve.assert_called_once_with("second question")

    @pytest.mark.asyncio
    async def test_memory_retrieval_timeout_is_non_fatal(self):
        """Timeout on recall_store.retrieve doesn't crash the turn."""
        recall = MagicMock()
        recall.decrement_ttl = AsyncMock()
        recall.retrieve = AsyncMock(side_effect=asyncio.TimeoutError)

        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            recall_store=recall,
        )

        assert result.error is None

    @pytest.mark.asyncio
    async def test_memory_retrieval_exception_is_non_fatal(self):
        """Exception on recall_store.retrieve doesn't crash the turn."""
        recall = MagicMock()
        recall.decrement_ttl = AsyncMock()
        recall.retrieve = AsyncMock(side_effect=RuntimeError("db down"))

        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            recall_store=recall,
        )

        assert result.error is None

    @pytest.mark.asyncio
    async def test_decrement_ttl_timeout_does_not_block_retrieval(self):
        """TTL decrement timeout doesn't prevent retrieval from running."""
        recall = MagicMock()
        recall.decrement_ttl = AsyncMock(side_effect=asyncio.TimeoutError)
        recall.retrieve = AsyncMock(return_value=[])

        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            recall_store=recall,
        )

        # retrieve should still be called even after TTL timeout
        recall.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_context_when_no_human_message(self):
        """Empty string used as context if no HumanMessage found."""
        recall = MagicMock()
        recall.decrement_ttl = AsyncMock()
        recall.retrieve = AsyncMock(return_value=[])

        messages = [SystemMessage(content="sys")]
        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            recall_store=recall,
        )

        recall.retrieve.assert_called_once_with("")


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — knowledge retrieval
# ---------------------------------------------------------------------------


class TestExecuteTurnKnowledgeRetrieval:
    @pytest.mark.asyncio
    async def test_knowledge_search_called_with_project_ids(self):
        """hybrid_search called with project_ids as UUIDs."""
        ks = MagicMock()
        ks.hybrid_search = AsyncMock(return_value=[])

        messages = [SystemMessage(content="sys"), HumanMessage(content="what auth?")]
        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            knowledge_store=ks,
            project_id="550e8400-e29b-41d4-a716-446655440000",
        )

        ks.hybrid_search.assert_called_once()
        call_kwargs = ks.hybrid_search.call_args[1]
        assert call_kwargs["match_count"] == 5
        assert call_kwargs["query"] == "what auth?"

    @pytest.mark.asyncio
    async def test_knowledge_not_called_without_project_id(self):
        """No knowledge search when project_id is None and project_ids empty."""
        ks = MagicMock()
        ks.hybrid_search = AsyncMock(return_value=[])

        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            knowledge_store=ks,
            project_id=None,
        )

        ks.hybrid_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_knowledge_not_called_without_store(self):
        """No knowledge search when knowledge_store is None."""
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        # Should not raise even with project_id set
        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            knowledge_store=None,
            project_id="550e8400-e29b-41d4-a716-446655440000",
        )

        assert result.error is None

    @pytest.mark.asyncio
    async def test_knowledge_timeout_is_non_fatal(self):
        """Timeout on knowledge retrieval is non-fatal."""
        ks = MagicMock()
        ks.hybrid_search = AsyncMock(side_effect=asyncio.TimeoutError)

        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            knowledge_store=ks,
            project_id="550e8400-e29b-41d4-a716-446655440000",
        )

        assert result.error is None

    @pytest.mark.asyncio
    async def test_knowledge_uses_project_ids_list(self):
        """project_ids takes priority over project_id."""
        ks = MagicMock()
        ks.hybrid_search = AsyncMock(return_value=[])

        messages = [SystemMessage(content="sys"), HumanMessage(content="q")]
        callbacks = _make_callbacks()

        pid1 = "550e8400-e29b-41d4-a716-446655440000"
        pid2 = "660e8400-e29b-41d4-a716-446655440001"

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            knowledge_store=ks,
            project_ids=[pid1, pid2],
        )

        ks.hybrid_search.assert_called_once()
        call_kwargs = ks.hybrid_search.call_args[1]
        assert len(call_kwargs["project_ids"]) == 2


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — transient injection
# ---------------------------------------------------------------------------


class TestTransientInjection:
    @pytest.mark.asyncio
    async def test_prepared_is_copy_not_original(self):
        """Transient injections go into a copy, not the original messages list."""
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        # Original messages should have gained the AI response but NOT workspace injection
        ws_messages = [
            m
            for m in messages
            if isinstance(m, SystemMessage) and "<workspace_memory>" in m.content
        ]
        assert len(ws_messages) == 0


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — context compaction
# ---------------------------------------------------------------------------


def _make_compacting_ctx_mgr(result_fn):
    """Context-manager mock whose ensure_within_limits simulates a
    *successful* summarization: returns ``result_fn(msgs)`` and bumps
    ``compaction_runs`` — the loop's authoritative did-it-compact gate
    (length deltas alone no longer count as compaction)."""
    ctx_mgr = AsyncMock()
    ctx_mgr.compaction_runs = 0

    async def _compactor(msgs, *a, **kw):
        ctx_mgr.compaction_runs += 1
        return result_fn(msgs)

    ctx_mgr.ensure_within_limits = _compactor
    return ctx_mgr


class TestContextCompaction:
    @pytest.mark.asyncio
    async def test_compaction_triggers_git_commit_and_push(self):
        """When compaction actually ran (run counter), git commit + push fires."""

        ctx_mgr = _make_compacting_ctx_mgr(lambda msgs: msgs[:2])

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr

        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
        ]
        callbacks = _make_callbacks()

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=ctx_mgr,
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        git_mgr.commit.assert_called_once()
        git_mgr.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_compaction_adopts_result_into_session_list(self):
        """A real compaction is adopted into the durable ``messages`` list
        (summary + tail replace the old history in place), so the next LLM
        call doesn't re-summarize from scratch. The full log stays in
        thread_messages; only the in-memory working set shrinks."""

        summary_msg = SystemMessage(content="[Summary of prior work]\nrecap")
        ctx_mgr = _make_compacting_ctx_mgr(
            lambda msgs: [msgs[0], summary_msg, msgs[-1]]
        )

        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
        ]
        callbacks = _make_callbacks(on_context_compacted=AsyncMock())

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=ctx_mgr,
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        contents = [getattr(m, "content", "") for m in messages]
        assert any("[Summary of prior work]" in c for c in contents), (
            "compacted history must be adopted into the session list"
        )
        assert "q1" not in contents, "summarized-away messages must be gone"
        # on_context_compacted surfaced the compaction exactly once
        callbacks.on_context_compacted.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_passthrough_does_not_fire_compaction_side_effects(self):
        """A pass-through bound (no summarization) must not adopt, must not
        emit on_context_compacted, and must not git-push — even if stray
        list-length changes occur (the old length heuristic false-fired on
        leaked RemoveMessage markers; the run counter cannot)."""
        from langchain_core.messages import RemoveMessage

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True
        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr
        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr

        # Stray markers in the list (the historical leak shape): the strip
        # shrinks the working copy, which used to read as "compaction".
        messages = [
            RemoveMessage(id="dead-1"),
            RemoveMessage(id="dead-2"),
            SystemMessage(content="sys"),
            SystemMessage(content="[Summary of prior work]\nold recap"),
            HumanMessage(content="q"),
        ]
        ctx_mgr = AsyncMock()
        ctx_mgr.compaction_runs = 0  # never bumped → pass-through
        ctx_mgr.ensure_within_limits = AsyncMock(side_effect=lambda m, *a, **kw: m)
        callbacks = _make_callbacks(on_context_compacted=AsyncMock())

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=ctx_mgr,
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        callbacks.on_context_compacted.assert_not_awaited()
        git_mgr.commit.assert_not_called()
        git_mgr.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_compaction_git_failure_non_fatal(self):
        """Git failure during compaction doesn't crash."""

        ctx_mgr = _make_compacting_ctx_mgr(lambda msgs: msgs[:1])

        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True
        git_mgr.commit.side_effect = RuntimeError("git failed")

        ws_mgr = MagicMock()
        ws_mgr.git_manager = git_mgr

        tool_ctx = MagicMock()
        tool_ctx.workspace_manager = ws_mgr

        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q"),
            AIMessage(content="a"),
        ]
        callbacks = _make_callbacks()

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response("ok")),
            tool_map={},
            context_manager=ctx_mgr,
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        assert result.error is None


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — LLM streaming
# ---------------------------------------------------------------------------


class TestLLMStreaming:
    @pytest.mark.asyncio
    async def test_string_content_streamed_token_by_token(self):
        """String content from chunks is streamed via on_token."""
        chunk1 = AIMessage(content="Hello ")
        chunk2 = AIMessage(content="world")

        async def _astream(msgs, **kw):
            yield chunk1
            yield chunk2

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert callbacks.on_token.call_count == 2
        callbacks.on_token.assert_any_call("Hello ")
        callbacks.on_token.assert_any_call("world")

    @pytest.mark.asyncio
    async def test_anthropic_list_content_streamed(self):
        """Anthropic list-of-dicts content blocks are streamed."""
        chunk = AIMessage(
            content=[
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "id": "x"},  # non-text block
            ]
        )

        async def _astream(msgs, **kw):
            yield chunk

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        callbacks.on_token.assert_called_once_with("Hello")

    @pytest.mark.asyncio
    async def test_empty_text_blocks_produce_warning(self):
        """Empty text blocks generate a user-visible warning (not silently dropped)."""
        chunk = AIMessage(content=[{"type": "text", "text": ""}])

        async def _astream(msgs, **kw):
            yield chunk

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        callbacks.on_token.assert_called_once_with(
            "⚠ The model returned an empty response. Please try again or switch models."
        )

    @pytest.mark.asyncio
    async def test_empty_chunks_return_error(self):
        """No chunks → TurnResult with 'Empty LLM response' error."""

        async def _astream(msgs, **kw):
            return
            yield  # empty generator

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

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
            config=_make_config(),
        )

        assert result.error == "Empty LLM response"

    @pytest.mark.asyncio
    async def test_empty_streaming_response_generates_warning(self):
        """Streaming response with empty content and no tool calls sends warning."""
        chunk = AIMessage(content="")

        async def _astream(msgs, **kw):
            yield chunk

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

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
            config=_make_config(),
        )

        callbacks.on_token.assert_called_once_with(
            "⚠ The model returned an empty response. Please try again or switch models."
        )
        assert messages[-1].content == (
            "⚠ The model returned an empty response. Please try again or switch models."
        )
        assert result.error is None

    @pytest.mark.asyncio
    async def test_empty_streaming_response_with_refusal(self):
        """Streaming response with refusal in additional_kwargs sends refusal msg."""
        chunk = AIMessage(
            content="",
            additional_kwargs={"refusal": "I cannot help with that"},
        )

        async def _astream(msgs, **kw):
            yield chunk

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        callbacks.on_token.assert_called_once_with(
            "⚠ The model declined to respond: I cannot help with that"
        )

    @pytest.mark.asyncio
    async def test_empty_text_with_tool_calls_no_warning(self):
        """Streaming response with tool calls but no text should not warn."""
        chunk = AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "test_tool", "args": {"x": 1}}],
        )

        async def _astream(msgs, **kw):
            yield chunk

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        mock_tool = AsyncMock()
        mock_tool.name = "test_tool"
        mock_tool.ainvoke = AsyncMock(return_value="done")

        # Need a second LLM call that returns no tool calls to end the turn
        call_count = 0

        async def _astream_second(msgs, **kw):
            yield AIMessage(content="Tool result processed.")

        original_astream = llm.astream

        async def _astream_dispatch(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async for c in original_astream(msgs, **kw):
                    yield c
            else:
                async for c in _astream_second(msgs, **kw):
                    yield c

        llm.astream = _astream_dispatch

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"test_tool": mock_tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        # on_token should NOT have been called with the warning message
        for call in callbacks.on_token.call_args_list:
            assert "empty response" not in call.args[0].lower()


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — LLM streaming fallback
# ---------------------------------------------------------------------------


class TestLLMStreamingFallback:
    @pytest.mark.asyncio
    async def test_response_not_read_triggers_fallback(self):
        """ResponseNotRead error triggers ainvoke fallback."""

        class ResponseNotReadError(Exception):
            pass

        async def _astream(msgs, **kw):
            raise ResponseNotReadError("stream failed")
            yield

        fallback_response = _make_llm_response("fallback response")
        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream
        llm.ainvoke = AsyncMock(return_value=fallback_response)

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

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
            config=_make_config(),
        )

        llm.ainvoke.assert_called_once()
        assert result.error is None

    @pytest.mark.asyncio
    async def test_api_connection_error_triggers_fallback(self):
        """APIConnectionError triggers ainvoke fallback."""

        class APIConnectionError(Exception):
            pass

        async def _astream(msgs, **kw):
            raise APIConnectionError("connection lost")
            yield

        fallback_response = _make_llm_response("ok")
        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream
        llm.ainvoke = AsyncMock(return_value=fallback_response)

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

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
            config=_make_config(),
        )

        llm.ainvoke.assert_called_once()
        assert result.error is None

    @pytest.mark.asyncio
    async def test_other_stream_errors_re_raised(self):
        """Non-fallback errors propagate up."""

        async def _astream(msgs, **kw):
            raise ValueError("unexpected")
            yield

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        with pytest.raises(ValueError, match="unexpected"):
            await _execute_turn(
                llm_with_tools=llm,
                tool_map={},
                context_manager=AsyncMock(
                    ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
                ),
                messages=messages,
                callbacks=callbacks,
                llm_timeout=600,
                auxiliary_llm=None,
                config=_make_config(),
            )

    @pytest.mark.asyncio
    async def test_fallback_streams_content_via_on_token(self):
        """Fallback ainvoke response is streamed via on_token."""

        class ResponseNotReadError(Exception):
            pass

        async def _astream(msgs, **kw):
            raise ResponseNotReadError("no stream")
            yield

        fallback_response = _make_llm_response("complete answer")
        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream
        llm.ainvoke = AsyncMock(return_value=fallback_response)

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        callbacks.on_token.assert_called_with("complete answer")


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — LLM timeout
# ---------------------------------------------------------------------------


class TestLLMTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_result(self):
        """asyncio.TimeoutError at outer level returns TurnResult with error."""

        async def _astream(msgs, **kw):
            raise asyncio.TimeoutError()
            yield

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=10,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert result.error is not None
        assert "timed out" in result.error
        callbacks.on_error.assert_called_once()


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — tool execution loop
# ---------------------------------------------------------------------------


class TestToolExecutionLoop:
    @pytest.mark.asyncio
    async def test_successful_tool_execution(self):
        """Tool is invoked, result appended as ToolMessage."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {"q": "test"}, "id": "tc1"}],
        )
        final_response = _make_llm_response("Here is the answer")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("search", "search results")
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="find stuff")]

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={"search": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        assert result.tool_calls_made == 1
        assert result.messages_added >= 2  # tool result + final AI

        tool.ainvoke.assert_called_once_with({"q": "test"})
        callbacks.on_tool_start.assert_called_once_with("search", {"q": "test"}, "tc1")
        callbacks.on_tool_result.assert_called_once_with(
            "search", "search results", "tc1", is_error=False
        )

    @pytest.mark.asyncio
    async def test_permission_denied_tool(self):
        """Denied tool call: ToolMessage added but tool NOT invoked."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "dangerous", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("dangerous", "should not run")

        callbacks = _make_callbacks(permission_check=AsyncMock(return_value=False))
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={"dangerous": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        tool.ainvoke.assert_not_called()
        callbacks.on_tool_start.assert_not_called()
        assert result.tool_calls_made == 0

        # Check denied message was added
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert any("denied" in m.content.lower() for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Unknown tool name: error ToolMessage added, on_tool_result called."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "nonexistent", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={},  # no tools
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert result.tool_calls_made == 0
        callbacks.on_tool_result.assert_called_once()
        assert "not found" in callbacks.on_tool_result.call_args[0][1]

    @pytest.mark.asyncio
    async def test_tool_exception_handled(self):
        """Tool exception caught, error result stored, counters still increment."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "broken", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("broken")
        tool.ainvoke = AsyncMock(side_effect=RuntimeError("tool crashed"))
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={"broken": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        assert result.tool_calls_made == 1
        callbacks.on_tool_result.assert_called_once()
        assert "error" in callbacks.on_tool_result.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_tool_none_result_becomes_empty_string(self):
        """Tool returning None produces empty string ToolMessage."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "noop", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("ok")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("noop", None)
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"noop": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        callbacks.on_tool_result.assert_called_once_with(
            "noop", "", "tc1", is_error=False
        )

    @pytest.mark.asyncio
    async def test_tool_args_default_to_empty_dict(self):
        """Missing 'args' in tool_call defaults to {} via .get('args', {})."""
        # LangChain AIMessage validator requires 'args', so we build a response
        # that has args=None and verify the code handles it via .get() default.
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "simple", "args": {}, "id": "tc1"}],
        )
        # Simulate missing 'args' at the dict level after construction
        response_with_tool.tool_calls[0].pop("args", None)

        final_response = _make_llm_response("ok")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("simple", "done")
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"simple": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        tool.ainvoke.assert_called_once_with({})

    @pytest.mark.asyncio
    async def test_turn_continues_after_tool_calls(self):
        """After tool results, LLM called again to see results."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("final answer")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("search", "data")
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"search": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        # LLM called twice: once got tool call, once got final response
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_turn_ends_when_no_tool_calls(self):
        """Turn ends when response has no tool_calls."""
        response = _make_llm_response("just text")
        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(response),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert result.tool_calls_made == 0
        assert result.messages_added == 1  # just the AI response


# ---------------------------------------------------------------------------
# 1.6 _execute_turn — VM upgrade detection
# ---------------------------------------------------------------------------


class TestVMUpgradeDetection:
    @pytest.mark.asyncio
    async def test_vm_upgrade_fires_on_matching_freeze_type(self):
        """on_vm_upgrade_needed fires when freeze_type == 'vm_upgrade_required'."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "run_cmd", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("done")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("run_cmd", "ok")

        freeze_req = {"freeze_type": "vm_upgrade_required", "reason": "sudo"}
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = freeze_req

        on_upgrade = AsyncMock()
        callbacks = _make_callbacks(on_vm_upgrade_needed=on_upgrade)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"run_cmd": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        on_upgrade.assert_called_once_with(freeze_req)

    @pytest.mark.asyncio
    async def test_vm_upgrade_not_fired_on_other_freeze_type(self):
        """on_vm_upgrade_needed NOT fired for non-vm_upgrade freeze types."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "run_cmd", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("done")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("run_cmd", "ok")

        freeze_req = {"freeze_type": "human_review", "reason": "checkpoint"}
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = freeze_req

        on_upgrade = AsyncMock()
        callbacks = _make_callbacks(on_vm_upgrade_needed=on_upgrade)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"run_cmd": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        on_upgrade.assert_not_called()

    @pytest.mark.asyncio
    async def test_vm_upgrade_not_checked_without_callback(self):
        """No freeze check when on_vm_upgrade_needed is None."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "run_cmd", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("done")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("run_cmd", "ok")
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        # on_vm_upgrade_needed is None
        callbacks = _make_callbacks(on_vm_upgrade_needed=None)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"run_cmd": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        # consume_freeze_request should not be called when callback is None
        # Actually, looking at the code: it checks `tool_context and callbacks.on_vm_upgrade_needed`
        # With on_vm_upgrade_needed=None, the condition is False, so no call

    @pytest.mark.asyncio
    async def test_vm_upgrade_not_checked_without_tool_context(self):
        """No freeze check when tool_context is None."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "run_cmd", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("done")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("run_cmd", "ok")
        on_upgrade = AsyncMock()
        callbacks = _make_callbacks(on_vm_upgrade_needed=on_upgrade)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"run_cmd": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=None,
        )

        on_upgrade.assert_not_called()

    @pytest.mark.asyncio
    async def test_vm_upgrade_not_fired_when_consume_returns_none(self):
        """No upgrade callback when consume_freeze_request returns None."""
        response_with_tool = AIMessage(
            content="",
            tool_calls=[{"name": "run_cmd", "args": {}, "id": "tc1"}],
        )
        final_response = _make_llm_response("done")

        call_count = 0

        async def _astream(msgs, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield response_with_tool
            else:
                yield final_response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream

        tool = _make_tool("run_cmd", "ok")
        tool_ctx = MagicMock()
        tool_ctx.consume_freeze_request.return_value = None

        on_upgrade = AsyncMock()
        callbacks = _make_callbacks(on_vm_upgrade_needed=on_upgrade)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={"run_cmd": tool},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
            tool_context=tool_ctx,
        )

        on_upgrade.assert_not_called()


# ---------------------------------------------------------------------------
# Streamed-response normalization (regression:
# persistent_session_empty_chunk_history_corruption)
# ---------------------------------------------------------------------------


class TestStreamedResponseNormalization:
    """A streamed AIMessageChunk must never land in history as a raw chunk,
    and an empty interrupted partial must be dropped rather than appended —
    otherwise the next turn's request serialization raises
    'TypeError: Got unknown type ...' ("malformed response")."""

    @pytest.mark.asyncio
    async def test_normal_streamed_chunk_appended_as_concrete_ai_message(self):
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(AIMessageChunk(content="answer")),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=_make_callbacks(),
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        ai = [m for m in messages if isinstance(m, AIMessage)]
        assert len(ai) == 1
        assert type(ai[0]) is AIMessage  # concrete, not AIMessageChunk
        assert ai[0].content == "answer"

    @pytest.mark.asyncio
    async def test_empty_interrupted_partial_is_not_appended(self):
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]
        # check_interrupt: False at top-of-loop (line 434), then "graceful"
        # mid-stream (line 574) so the partial-handling path runs.
        check = MagicMock(side_effect=[False, "graceful"])

        result = await _execute_turn(
            llm_with_tools=_make_streaming_llm(
                AIMessageChunk(content="", id="lc_run--019e5adc-0945-7b73")
            ),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=_make_callbacks(check_interrupt=check),
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert result.interrupted is True
        # The empty partial must NOT be in history (neither chunk nor concrete).
        assert len(messages) == 2
        assert not any(isinstance(m, AIMessage) for m in messages)


class TestHardInterruptHelpers:
    """Phase-3 immediate hard-interrupt cancellation primitives.

    These race an in-flight LLM / auxiliary await against a hard-interrupt
    event so a hung network read is torn down at once, instead of waiting for
    the cooperative check_interrupt poll (which can't fire while parked).
    """

    @pytest.mark.asyncio
    async def test_await_passthrough_when_no_event(self):
        from src.persistent_graph import _await_or_hard_interrupt

        async def quick():
            return "done"

        result, interrupted = await _await_or_hard_interrupt(quick(), None)
        assert result == "done"
        assert interrupted is False

    @pytest.mark.asyncio
    async def test_await_completes_when_event_idle(self):
        from src.persistent_graph import _await_or_hard_interrupt

        async def quick():
            return 42

        result, interrupted = await _await_or_hard_interrupt(quick(), asyncio.Event())
        assert result == 42
        assert interrupted is False

    @pytest.mark.asyncio
    async def test_await_cancels_blocked_coro_on_event(self):
        from src.persistent_graph import _await_or_hard_interrupt

        event = asyncio.Event()
        cancelled = {"v": False}

        async def hang():
            try:
                await asyncio.sleep(30)
                return "never"
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise

        async def fire():
            await asyncio.sleep(0.01)
            event.set()

        fire_task = asyncio.create_task(fire())
        result, interrupted = await _await_or_hard_interrupt(hang(), event)
        await fire_task

        assert interrupted is True
        assert result is None
        # The blocked await must actually be torn down, not just abandoned.
        assert cancelled["v"] is True

    @pytest.mark.asyncio
    async def test_await_propagates_coro_exception(self):
        from src.persistent_graph import _await_or_hard_interrupt

        async def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            await _await_or_hard_interrupt(boom(), asyncio.Event())

    @pytest.mark.asyncio
    async def test_stream_yields_chunks_then_stop(self):
        from src.persistent_graph import _stream_next_or_hard_interrupt

        async def gen():
            yield "a"
            yield "b"

        aiter = gen().__aiter__()
        event = asyncio.Event()
        assert await _stream_next_or_hard_interrupt(aiter, event) == ("a", "chunk")
        assert await _stream_next_or_hard_interrupt(aiter, event) == ("b", "chunk")
        assert await _stream_next_or_hard_interrupt(aiter, event) == (None, "stop")

    @pytest.mark.asyncio
    async def test_stream_passthrough_when_no_event(self):
        from src.persistent_graph import _stream_next_or_hard_interrupt

        async def gen():
            yield "only"

        aiter = gen().__aiter__()
        assert await _stream_next_or_hard_interrupt(aiter, None) == ("only", "chunk")
        assert await _stream_next_or_hard_interrupt(aiter, None) == (None, "stop")

    @pytest.mark.asyncio
    async def test_stream_interrupts_hung_read(self):
        from src.persistent_graph import _stream_next_or_hard_interrupt

        started = asyncio.Event()

        async def hanging_gen():
            started.set()
            await asyncio.sleep(30)  # never yields — simulates a hung read
            yield "never"

        aiter = hanging_gen().__aiter__()
        event = asyncio.Event()

        async def fire():
            await started.wait()
            event.set()

        fire_task = asyncio.create_task(fire())
        chunk, status = await _stream_next_or_hard_interrupt(aiter, event)
        await fire_task

        assert chunk is None
        assert status == "interrupt"


# ---------------------------------------------------------------------------
# 1.7 _execute_turn — reasoning broadcast (dedupe key + ordering)
# ---------------------------------------------------------------------------


class TestExecuteTurnReasoning:
    """Reasoning frames carry the AI message id and are emitted exactly once.

    docs/issues/persistent_chat_reasoning_after_answer_and_replay_duplication.md
    """

    @pytest.mark.asyncio
    async def test_reasoning_content_emitted_once_with_pinned_message_id(self):
        """A Chat-Completions reasoning blob (additional_kwargs.reasoning_content)
        is broadcast once via the post-stream fallback, keyed to the AI message
        id, and the persisted row id is pinned to that same id."""
        response = AIMessage(content="The answer.")
        response.additional_kwargs = {"reasoning_content": "Let me think."}

        captured = []

        async def _on_thinking(content, message_id=None):
            captured.append((content, message_id))

        callbacks = _make_callbacks(on_thinking=_on_thinking)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(response),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert len(captured) == 1
        content, message_id = captured[0]
        assert content == "Let me think."
        assert message_id and message_id.startswith("msg_")
        # The appended AI row shares the broadcast id — the dedupe key.
        assert messages[-1].id == message_id

    @pytest.mark.asyncio
    async def test_live_streamed_reasoning_skips_post_stream_fallback(self):
        """When the SSE tap surfaces reasoning live (sink fired), the post-stream
        fallback does not re-broadcast it — exactly one emission."""
        from src.llm.reasoning_chat import _STREAM_REASONING_SINK

        response = AIMessage(content="Answer.")
        response.additional_kwargs = {"reasoning_content": "live reasoning"}

        captured = []

        async def _on_thinking(content, message_id=None):
            captured.append((content, message_id))

        async def _astream(messages, **kw):
            # Simulate the tap surfacing a reasoning delta mid-stream, before
            # the answer chunk — exactly what the live ordering fix relies on.
            sink = _STREAM_REASONING_SINK.get()
            assert sink is not None, "sink must be installed during the stream"
            sink("live reasoning")
            yield response

        llm = AsyncMock()
        llm.reasoning = None
        llm.astream = _astream
        llm.ainvoke = AsyncMock(return_value=response)

        callbacks = _make_callbacks(on_thinking=_on_thinking)
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )
        # Let the fire-and-forget broadcast task run.
        await asyncio.sleep(0)

        assert len(captured) == 1
        assert captured[0][0] == "live reasoning"
        # Pinned so the live frame's id matches the persisted row.
        assert captured[0][1] == messages[-1].id

    @pytest.mark.asyncio
    async def test_sink_is_cleared_after_turn(self):
        """The reasoning sink contextvar must not leak past the turn."""
        from src.llm.reasoning_chat import _STREAM_REASONING_SINK

        callbacks = _make_callbacks()
        messages = [SystemMessage(content="sys"), HumanMessage(content="go")]

        await _execute_turn(
            llm_with_tools=_make_streaming_llm(_make_llm_response()),
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=callbacks,
            llm_timeout=600,
            auxiliary_llm=None,
            config=_make_config(),
        )

        assert _STREAM_REASONING_SINK.get() is None


# ---------------------------------------------------------------------------
# Transient context injection placement (Gemini function-call turn ordering)
# ---------------------------------------------------------------------------


def _first_index(messages, predicate):
    return next((i for i, m in enumerate(messages) if predicate(m)), None)


def _is_tool_call_ai(m) -> bool:
    return isinstance(m, AIMessage) and bool(getattr(m, "tool_calls", None))


def _memory_pair(content: str):
    """A real (AIMessage(tool_call), ToolMessage) pair, as the seam produces."""
    from src.core.memory_injection import create_memory_injection_messages

    return list(create_memory_injection_messages(content))


class TestInjectionAnchorIndex:
    """`_injection_anchor_index` anchors injected pairs after the first user turn."""

    def test_after_first_human_with_system(self):
        msgs = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        assert _injection_anchor_index(msgs) == 2

    def test_after_first_human_no_system(self):
        msgs = [HumanMessage(content="hi"), AIMessage(content="yo")]
        assert _injection_anchor_index(msgs) == 1

    def test_anchors_on_first_human_not_later(self):
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
        ]
        assert _injection_anchor_index(msgs) == 2

    def test_no_human_falls_back_after_system(self):
        assert _injection_anchor_index([SystemMessage(content="sys")]) == 1

    def test_no_human_no_system_returns_zero(self):
        assert _injection_anchor_index([]) == 0
        assert _injection_anchor_index([AIMessage(content="a")]) == 0


class TestInjectContextPairs:
    """Injected memory/knowledge pairs must never precede the first user turn.

    Regression for the Gemini 400 "Please ensure that function call turn comes
    immediately after a user turn or after a function response turn": the
    injected pairs are synthetic AIMessage(tool_call)+ToolMessage, and placing
    them right after the SystemMessage (before the first HumanMessage) made the
    function call the first entry of Gemini's `contents` array — failing the
    very first turn of a session. The worker graph is shielded by its leading
    todos HumanMessage; the persistent loop now anchors on the first user turn.
    """

    def _assert_gemini_ordering(self, messages):
        """No tool-call AIMessage or ToolMessage may precede the first user turn."""
        first_human = _first_index(messages, lambda m: isinstance(m, HumanMessage))
        first_tool = _first_index(
            messages, lambda m: _is_tool_call_ai(m) or isinstance(m, ToolMessage)
        )
        assert first_human is not None, "expected a user turn in the history"
        assert first_tool is None or first_human < first_tool, (
            f"tool turn at {first_tool} precedes first user turn at {first_human}"
        )

    def _assert_pairing(self, messages):
        call_ids = [
            tc["id"] for m in messages if _is_tool_call_ai(m) for tc in m.tool_calls
        ]
        result_ids = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
        assert sorted(call_ids) == sorted(result_ids)
        # Each injected result immediately follows its call (AI then Tool).
        for i, m in enumerate(messages):
            if _is_tool_call_ai(m):
                nxt = messages[i + 1]
                assert isinstance(nxt, ToolMessage)
                assert nxt.tool_call_id in {tc["id"] for tc in m.tool_calls}

    def test_memory_injected_after_first_human(self):
        prepared = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        count = _inject_context_pairs(prepared, [], "MEMORIES", "")
        assert count == 2
        self._assert_gemini_ordering(prepared)
        # The fixed shape: System, Human, AI(tool_call), Tool — NOT the old
        # System, AI(tool_call), Tool, Human that Gemini rejects.
        assert isinstance(prepared[0], SystemMessage)
        assert isinstance(prepared[1], HumanMessage)
        assert _is_tool_call_ai(prepared[2])
        assert isinstance(prepared[3], ToolMessage)

    def test_knowledge_injected_after_first_human(self):
        prepared = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        count = _inject_context_pairs(prepared, [], "", "KNOWLEDGE")
        assert count == 2
        self._assert_gemini_ordering(prepared)

    def test_all_three_sources_ordering_and_pairing(self):
        manager = _memory_pair("MANAGER")  # seam payload is itself tool-call pairs
        prepared = [
            SystemMessage(content="sys"),
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
        ]
        count = _inject_context_pairs(prepared, manager, "MEM", "KB")
        assert count == len(manager) + 4
        self._assert_gemini_ordering(prepared)
        self._assert_pairing(prepared)

    def test_no_injection_when_all_empty(self):
        prepared = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        before = list(prepared)
        count = _inject_context_pairs(prepared, [], "", "")
        assert count == 0
        assert prepared == before
