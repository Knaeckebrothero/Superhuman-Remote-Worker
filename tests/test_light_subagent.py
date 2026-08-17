"""Unit tests for the light-subagent ReAct harness (Phase 1).

Covers knowledge-base/knowledge/issues/delegation_light_mode_missing.md Phase 1: run_light_subagent
returns final text, executes tool calls, stops at each cap, uses a fresh message
list, and handles the empty-tools path — all with a fake LLM + fake tools, no
infra.
"""

import asyncio

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import pytest

from src.tools.delegation.light_runner import (
    _EMPTY_RESULT,
    _message_text,
    run_light_subagent,
)


# --- fakes ------------------------------------------------------------------


class ScriptedLLM:
    """Fake LLM returning a pre-scripted sequence of AIMessages.

    Records every `ainvoke` argument so tests can assert on the message list the
    reader built (fresh context, tool results fed back, etc.).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of message-lists passed to ainvoke

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="[default final]")


@tool
def echo_tool(text: str) -> str:
    """Echo the given text back."""
    return f"echoed: {text}"


def _tool_call(name="echo_tool", args=None, call_id="call_1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {"text": "hi"}, "id": call_id}],
    )


# --- tests ------------------------------------------------------------------


class TestMessageText:
    def test_string_content(self):
        assert _message_text(AIMessage(content="hello")) == "hello"

    def test_block_content(self):
        msg = AIMessage(
            content=[
                {"type": "text", "text": "part1 "},
                {"type": "text", "text": "part2"},
            ]
        )
        assert _message_text(msg) == "part1 part2"

    def test_empty_content(self):
        assert _message_text(AIMessage(content="")) == ""


class TestRunLightSubagent:
    @pytest.mark.asyncio
    async def test_returns_final_text_no_tools(self):
        """A reader with no tools answers in one turn and returns its text."""
        llm = ScriptedLLM([AIMessage(content="final answer")])
        out = await run_light_subagent("do X", "", tools=[], llm=llm)
        assert out == "final answer"
        assert len(llm.calls) == 1

    @pytest.mark.asyncio
    async def test_executes_tool_calls_then_returns(self):
        """Reader calls a tool, sees its result, then returns a final answer."""
        llm = ScriptedLLM([_tool_call(), AIMessage(content="synthesized")])
        out = await run_light_subagent("do X", "", tools=[echo_tool], llm=llm)
        assert out == "synthesized"
        assert len(llm.calls) == 2
        # The tool result must be fed back into the reader's own context.
        second_turn = llm.calls[1]
        tool_msgs = [m for m in second_turn if isinstance(m, ToolMessage)]
        assert any("echoed: hi" in (m.content or "") for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_fresh_message_list_no_parent_history(self):
        """First turn is exactly System preamble + Human task — no history."""
        llm = ScriptedLLM([AIMessage(content="done")])
        await run_light_subagent(
            "READ THE THING", "shared background", tools=[], llm=llm
        )
        first_turn = llm.calls[0]
        assert len(first_turn) == 2
        assert isinstance(first_turn[0], SystemMessage)
        assert "READ THE THING" in first_turn[1].content  # the task
        # Shared context rides in the system preamble, not as parent history.
        assert "shared background" in first_turn[0].content

    @pytest.mark.asyncio
    async def test_stops_at_iteration_cap(self):
        """Never-terminating reader stops at max_iterations, then synthesizes."""
        llm = ScriptedLLM([_tool_call(), _tool_call(), AIMessage(content="SYNTH")])
        out = await run_light_subagent(
            "loop", "", tools=[echo_tool], llm=llm, max_iterations=2
        )
        assert out == "SYNTH"
        # 2 loop turns + 1 forced synthesis turn.
        assert len(llm.calls) == 3
        # The synthesis turn carries the tool-free instruction.
        assert "final answer now" in llm.calls[2][-1].content

    @pytest.mark.asyncio
    async def test_stops_at_token_cap(self):
        """Exceeding max_tokens after a tool turn forces synthesis."""
        llm = ScriptedLLM([_tool_call(), AIMessage(content="TOKENSYNTH")])
        out = await run_light_subagent(
            "big", "", tools=[echo_tool], llm=llm, max_iterations=10, max_tokens=1
        )
        assert out == "TOKENSYNTH"
        assert len(llm.calls) == 2  # 1 tool turn + 1 synthesis (token cap tripped)

    @pytest.mark.asyncio
    async def test_iteration_cap_falls_back_to_last_text(self):
        """If synthesis yields nothing, the last real text is returned."""
        # Both loop turns produce text alongside tool calls; synthesis returns "".
        llm = ScriptedLLM(
            [
                AIMessage(
                    content="partial finding", tool_calls=_tool_call().tool_calls
                ),
                AIMessage(content=""),  # forced-synthesis turn, empty
            ]
        )
        out = await run_light_subagent(
            "loop", "", tools=[echo_tool], llm=llm, max_iterations=1
        )
        assert out == "partial finding"

    @pytest.mark.asyncio
    async def test_empty_result_marker(self):
        """A reader that says nothing and calls nothing returns the marker."""
        llm = ScriptedLLM([AIMessage(content="")])
        out = await run_light_subagent("noop", "", tools=[], llm=llm)
        assert out == _EMPTY_RESULT

    @pytest.mark.asyncio
    async def test_tool_call_with_no_tools_returns_text(self):
        """If the model calls a tool but the reader has none, return its text."""
        llm = ScriptedLLM(
            [AIMessage(content="I tried", tool_calls=_tool_call().tool_calls)]
        )
        out = await run_light_subagent("x", "", tools=[], llm=llm)
        assert out == "I tried"
        assert len(llm.calls) == 1  # no second turn — nothing to service the call

    @pytest.mark.asyncio
    async def test_turn_tool_calls_run_concurrently(self):
        """Multiple tool calls in ONE reader turn execute concurrently.

        Deterministic (no timing): both calls must reach an asyncio.Barrier(2)
        for it to release. If they ran sequentially the first would block
        forever and wait_for would time out.
        """
        barrier = asyncio.Barrier(2)

        @tool
        async def wait_tool(idx: int) -> str:
            """Wait on a shared barrier, then return."""
            await barrier.wait()
            return f"released {idx}"

        two_calls = AIMessage(
            content="",
            tool_calls=[
                {"name": "wait_tool", "args": {"idx": 1}, "id": "a"},
                {"name": "wait_tool", "args": {"idx": 2}, "id": "b"},
            ],
        )
        llm = ScriptedLLM([two_calls, AIMessage(content="done")])
        out = await asyncio.wait_for(
            run_light_subagent("x", "", tools=[wait_tool], llm=llm), timeout=2
        )
        assert out == "done"

    @pytest.mark.asyncio
    async def test_tool_error_is_isolated_not_fatal(self):
        """A raised tool becomes an error ToolMessage; the reader continues."""

        @tool
        def boom(x: str) -> str:
            """Always fails."""
            raise RuntimeError("kaboom")

        llm = ScriptedLLM(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "boom", "args": {"x": "1"}, "id": "e1"}],
                ),
                AIMessage(content="recovered"),
            ]
        )
        out = await run_light_subagent("x", "", tools=[boom], llm=llm)
        assert out == "recovered"
        # The reader saw the error as a ToolMessage on its next turn.
        tool_msgs = [m for m in llm.calls[1] if isinstance(m, ToolMessage)]
        assert any("kaboom" in (m.content or "") for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_preamble_includes_role_port_and_format(self):
        """Role, port block, and return-format all land in the system preamble."""
        llm = ScriptedLLM([AIMessage(content="ok")])
        await run_light_subagent(
            "task",
            "ctx",
            tools=[],
            llm=llm,
            port_block="=== SUBAGENT ENVIRONMENT ===\nports 8100-8199",
            role="source reader",
            expected_return_format="a bulleted list",
        )
        preamble = llm.calls[0][0].content
        assert "source reader" in preamble
        assert "8100-8199" in preamble
        assert "a bulleted list" in preamble


class SlowScriptedLLM(ScriptedLLM):
    """ScriptedLLM with a per-call delay before each response."""

    def __init__(self, responses, delays):
        super().__init__(responses)
        self._delays = list(delays)

    async def ainvoke(self, messages):
        delay = self._delays.pop(0) if self._delays else 0
        if delay:
            await asyncio.sleep(delay)
        return await super().ainvoke(messages)


@tool
async def sleepy_tool(text: str) -> str:
    """Sleep long, then echo."""
    await asyncio.sleep(5.0)
    return f"slow: {text}"


class TestWallClockDeadline:
    """The reader self-terminates at timeout_seconds with partial results.

    Covers the job-472ea457 failure mode: a deep reader outliving the parent
    graph's delegation batch watchdog must instead synthesize and return.
    """

    @pytest.mark.asyncio
    async def test_slow_llm_turn_is_cut_off_and_synthesized(self):
        """An LLM turn that overruns the deadline is cancelled → synthesis."""
        # The first call is cancelled mid-sleep, so it never consumes a
        # scripted response — the queue holds only the synthesis reply.
        llm = SlowScriptedLLM(
            [AIMessage(content="PARTIAL SYNTH")],
            delays=[5.0, 0],
        )
        out = await run_light_subagent("x", "", tools=[], llm=llm, timeout_seconds=0.1)
        assert out == "PARTIAL SYNTH"
        # The synthesis turn names the reason.
        assert "time limit" in llm.calls[-1][-1].content

    @pytest.mark.asyncio
    async def test_slow_tool_calls_cut_off_with_paired_tool_messages(self):
        """A tool turn that overruns is cancelled; every pending tool_call gets
        a synthetic ToolMessage before synthesis (strict-provider pairing)."""
        llm = ScriptedLLM([_tool_call("sleepy_tool"), AIMessage(content="TOOLSYNTH")])
        out = await run_light_subagent(
            "x", "", tools=[sleepy_tool], llm=llm, timeout_seconds=0.2
        )
        assert out == "TOOLSYNTH"
        # Synthesis turn saw a ToolMessage answering the cancelled call.
        synth_turn = llm.calls[-1]
        paired = [
            m
            for m in synth_turn
            if isinstance(m, ToolMessage) and m.tool_call_id == "call_1"
        ]
        assert len(paired) == 1
        assert "ran out of time" in paired[0].content

    @pytest.mark.asyncio
    async def test_zero_timeout_means_unbounded(self):
        """timeout_seconds=0 (the pure-harness default) adds no deadline."""
        llm = ScriptedLLM([_tool_call(), AIMessage(content="done")])
        out = await run_light_subagent(
            "x", "", tools=[echo_tool], llm=llm, timeout_seconds=0
        )
        assert out == "done"

    @pytest.mark.asyncio
    async def test_hung_synthesis_falls_back_to_last_text(self, monkeypatch):
        """Even the forced-synthesis call is bounded; on overrun the reader
        still returns the last text it produced instead of hanging."""
        from src.tools.delegation import light_runner

        monkeypatch.setattr(light_runner, "_SYNTHESIS_TIMEOUT_SECONDS", 0.05)
        llm = SlowScriptedLLM(
            [
                AIMessage(
                    content="partial finding", tool_calls=_tool_call().tool_calls
                ),
                AIMessage(content="never returned"),
            ],
            delays=[0, 5.0],  # loop turn instant, synthesis turn hangs
        )
        out = await run_light_subagent(
            "x", "", tools=[echo_tool], llm=llm, max_iterations=1
        )
        assert out == "partial finding"


class _FlakyLLM:
    """Fake LLM raising scripted errors before returning scripted messages."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.call_count = 0

    async def ainvoke(self, messages):
        self.call_count += 1
        outcome = self._outcomes.pop(0) if self._outcomes else AIMessage(content="done")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Status(Exception):
    """Provider error carrying an HTTP status, like the openai/anthropic SDKs."""

    def __init__(self, status_code, message=""):
        super().__init__(message or f"Error code: {status_code}")
        self.status_code = status_code


class TestReaderLLMRetry:
    """Readers had NO retry, so one transient blip killed a whole fan-out.

    Both readers of critic job 37c418d2 died on a single 408 stream-disconnect:
    the parent execute node's classify+retry never covered them, because a light
    reader is a graph-less in-process harness.
    knowledge-history/done/llm_retry_and_fallback_reimplemented_per_call_site.md
    """

    @pytest.mark.asyncio
    async def test_transient_stream_disconnect_is_retried(self):
        llm = _FlakyLLM(
            [
                _Status(408, "stream error: stream disconnected before completion"),
                AIMessage(content="the finding"),
            ]
        )
        out = await run_light_subagent("x", "", tools=[], llm=llm, timeout_seconds=30)
        assert out == "the finding"
        assert llm.call_count == 2

    @pytest.mark.asyncio
    async def test_permanent_error_is_not_retried(self):
        # No wait fixes a bad model name; burning the reader's budget is worse
        # than surfacing it to spawn_subagent's handler immediately.
        llm = _FlakyLLM([Exception("model gpt-x does not exist")])
        with pytest.raises(Exception, match="does not exist"):
            await run_light_subagent("x", "", tools=[], llm=llm, timeout_seconds=30)
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_wall_clock_timeout_is_never_retried(self):
        # asyncio.TimeoutError IS the deadline. Retrying it would silently break
        # the contract that a reader self-terminates before the parent's
        # delegation batch watchdog discards the whole fan-out.
        llm = _FlakyLLM([asyncio.TimeoutError(), AIMessage(content="partial result")])
        out = await run_light_subagent("x", "", tools=[], llm=llm, timeout_seconds=30)
        # Exactly 2 calls: the timed-out turn, then the forced synthesis. A third
        # would mean the deadline got retried as if it were a transient blip.
        assert llm.call_count == 2
        assert out == "partial result"

    @pytest.mark.asyncio
    async def test_retry_budget_is_bounded_not_infinite(self):
        llm = _FlakyLLM([_Status(503), _Status(503), _Status(503)])
        with pytest.raises(_Status):
            await run_light_subagent("x", "", tools=[], llm=llm, timeout_seconds=30)
        assert llm.call_count == 2  # max_attempts=2, then it gives up


class TestFailedResultHeader:
    """A failure used to be announced as '[subagent done]' + an error body."""

    def test_failure_header_says_failed(self):
        from src.tools.delegation.spawn_subagent import _format_result

        out = _format_result("auditor", "check the thing", "Error: boom", failed=True)
        assert out.startswith("[subagent failed] — role: auditor")
        assert "[subagent done]" not in out

    def test_success_header_unchanged(self):
        from src.tools.delegation.spawn_subagent import _format_result

        out = _format_result("auditor", "check the thing", "the finding")
        assert out.startswith("[subagent done] — role: auditor")
