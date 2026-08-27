"""light_runner — the in-process ReAct harness for a single light subagent.

One throwaway subagent: a bounded `execute ↔ tools` loop with NO graph — no
strategic/tactical phase alternation, no todos, no archiver, no checkpoint. It
starts from a FRESH message list (it never sees the parent's conversation),
executes the tool calls in each of ITS turns concurrently (via `asyncio.gather`,
not a graph node — the prebuilt `ToolNode` needs an ambient langgraph runtime
that only exists inside a compiled graph run), and returns its final answer as a
plain string.

This module is deliberately pure and infra-free: everything workspace/git/shell-
shaped lives in the `tools` and `llm` the caller hands in (Phase 2 builds those,
bound to the reader's worktree on the workspace pod; Phase 3 is the tool that
calls this). The harness only drives the loop, so it is unit-testable with a
fake LLM and fake tools — no SSH, no pods, no orchestrator.

See knowledge-base/knowledge/issues/delegation_light_mode_missing.md (Phase 1).
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from ...core.context import count_tokens_approximate
from ...core.llm_retry import RetryPolicy, invoke_with_retry

logger = logging.getLogger(__name__)

# Returned when a reader finishes without emitting any text.
_EMPTY_RESULT = "[subagent returned no content]"

# Bound on the single forced-synthesis LLM call after a cap is hit, so a hung
# provider can't keep a "finished" reader alive past its own deadline.
_SYNTHESIS_TIMEOUT_SECONDS = 90

# Readers had NO retry at all until this: any provider error propagated out of
# the loop, was swallowed by spawn_subagent's blanket handler, and became the
# string "Error: subagent failed". Because a fan-out's readers run concurrently
# against the same provider, one transport blip took out the WHOLE batch — both
# readers of critic job 37c418d2 died on a single 408 stream-disconnect. The
# parent execute node's classify+retry never covered them: readers are a
# graph-less in-process harness.
#
# Budget is one extra attempt, because every retry spends the reader's own
# wall-clock deadline (`timeout_seconds`, 240 s by default) and overrunning it
# means the parent's delegation batch watchdog discards the whole fan-out.
#
# `asyncio.TimeoutError` must NEVER be retried here: it is how the wall-clock
# deadline itself surfaces, and the caller catches it to force a partial-result
# synthesis. Retrying it would silently break the deadline contract. `rate_limit`
# is excluded for the same budget reason as the auxiliary path — a reader cannot
# afford to sit out a 90 s provider window inside a 240 s life.
_READER_RETRY = RetryPolicy(
    max_attempts=2,
    base_delay=1.0,
    max_delay=5.0,
    retryable=frozenset({"transient", "auth_unavailable"}),
    never_retry=(asyncio.TimeoutError,),
    respect_retry_after=False,
)


async def _execute_tool_calls(
    tool_calls: List[Dict[str, Any]], tools_by_name: Dict[str, Any]
) -> List[Any]:
    """Run a turn's tool calls concurrently, one ToolMessage per call.

    Mirrors ToolNode's error handling (a raised tool becomes an error
    ToolMessage rather than aborting the reader) but with no langgraph runtime
    dependency, so the harness stays pure.
    """
    from langchain_core.messages import ToolMessage

    async def _run_one(tc: Dict[str, Any]) -> Any:
        name = tc.get("name")
        call_id = tc.get("id")
        tool = tools_by_name.get(name)
        if tool is None:
            content = f"Error: tool '{name}' is not available to this subagent."
        else:
            try:
                result = await tool.ainvoke(tc.get("args") or {})
                content = result if isinstance(result, str) else str(result)
            except Exception as e:  # isolate per-call failure
                content = f"Error: {name} failed — {e}"
        return ToolMessage(content=content, tool_call_id=call_id, name=name)

    return list(await asyncio.gather(*[_run_one(tc) for tc in tool_calls]))


def _message_text(msg: Any) -> str:
    """Extract plain text from an AIMessage whose content may be blocks."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content) if content else ""


def _build_reader_preamble(
    role: str,
    context: str,
    port_block: str,
    expected_return_format: str,
) -> str:
    """Build the fresh-context system preamble for a light subagent."""
    lines = [
        "You are a focused, single-use subagent. You have been given ONE "
        "self-contained task. Use your tools to complete it, then STOP.",
    ]
    if role:
        lines.append(f"Your role: {role}.")
    lines += [
        "",
        "Rules:",
        "- You cannot ask questions or request clarification — work only from "
        "the task below.",
        "- When you are done, reply with your findings as a plain text message "
        "and do NOT call any tool. That final message is your result and is the "
        "ONLY thing returned to whoever asked you.",
        "- Be concise and factual. Cite sources with your citation tools where "
        "you have them.",
    ]
    if expected_return_format:
        lines += ["", f"Return your result in this shape: {expected_return_format}"]
    if context:
        lines += ["", "Shared context from the caller:", context]
    if port_block:
        lines += ["", port_block]
    return "\n".join(lines)


async def _final_synthesis(
    llm: Any, messages: List[Any], last_text: str, *, reason: str
) -> str:
    """Force a final, tool-free answer after a cap is hit.

    Gives the reader one chance to synthesize what it gathered; falls back to the
    last text it produced, then to a bounded-stop marker.
    """
    from langchain_core.messages import HumanMessage

    prompt = (
        f"You have reached your {reason}. Do NOT call any more tools. Based only "
        "on what you have gathered so far, write your final answer now."
    )
    try:
        ai = await asyncio.wait_for(
            llm.ainvoke(messages + [HumanMessage(content=prompt)]),
            timeout=_SYNTHESIS_TIMEOUT_SECONDS,
        )
        text = _message_text(ai)
        if text:
            return text
    except Exception as e:  # never let a synthesis failure lose the reader's work
        logger.warning("light subagent final synthesis failed: %s", e)
    if last_text:
        return last_text
    return f"[subagent stopped: hit {reason} before producing a final answer]"


def _timeout_tool_messages(tool_calls: List[Dict[str, Any]]) -> List[Any]:
    """Synthetic ToolMessages for calls cancelled by the wall-clock deadline.

    Every tool_call on the last AIMessage must be answered before the next LLM
    turn (the forced synthesis) — strict providers reject dangling tool_calls.
    """
    from langchain_core.messages import ToolMessage

    return [
        ToolMessage(
            content="Error: cancelled — the subagent ran out of time.",
            tool_call_id=tc.get("id"),
            name=tc.get("name"),
        )
        for tc in tool_calls
    ]


async def run_light_subagent(
    task: str,
    context: str,
    tools: List[Any],
    llm: Any,
    *,
    max_iterations: int = 10,
    max_tokens: int = 40000,
    timeout_seconds: float = 0,
    port_block: str = "",
    role: str = "",
    expected_return_format: str = "",
) -> str:
    """Run one bounded ReAct subagent and return its final answer as a string.

    Args:
        task: The complete, self-contained instruction for the subagent.
        context: Shared background from the caller (goes in the system preamble,
            not treated as the task).
        tools: The subagent's tools (already bound to its environment by the
            caller). Looked up by name to service each turn's tool calls.
        llm: An LLM already bound to `tools` (`.bind_tools(tools)`), exposing
            `await llm.ainvoke(messages) -> AIMessage`.
        max_iterations: Hard cap on execute↔tools turns.
        max_tokens: Approximate running-message token budget; once exceeded the
            reader is asked to synthesize and stop.
        timeout_seconds: Wall-clock budget for the whole reader (0 = unbounded).
            The reader must self-terminate with partial results before the
            parent graph's tool-batch watchdog fires, or the whole batch is
            discarded — keep this well under `tool_category_timeouts.delegation`.
        port_block: Optional port-range awareness block (server-start steering).
        role: Optional role hint for the preamble.
        expected_return_format: Optional natural-language shape for the result.

    Returns:
        The reader's final text (or a bounded-stop summary). Never the parent's
        history — the message list is fresh.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    preamble = _build_reader_preamble(role, context, port_block, expected_return_format)
    messages: List[Any] = [
        SystemMessage(content=preamble),
        HumanMessage(content=f"Task:\n{task}"),
    ]

    tools_by_name: Dict[str, Any] = {t.name: t for t in tools} if tools else {}

    deadline: Optional[float] = (
        time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    )

    def _remaining() -> Optional[float]:
        return None if deadline is None else deadline - time.monotonic()

    last_text = ""
    for _iteration in range(1, max_iterations + 1):
        remaining = _remaining()
        if remaining is not None and remaining <= 0:
            logger.info(
                "light subagent hit wall-clock limit (%ss); forcing synthesis",
                timeout_seconds,
            )
            return await _final_synthesis(llm, messages, last_text, reason="time limit")
        try:
            # `_remaining()` is re-read per attempt so a retry spends what is
            # actually left, not the budget the first attempt started with. Once
            # it goes non-positive, wait_for raises TimeoutError immediately and
            # (being never_retry) escapes to the synthesis path below — so the
            # deadline still wins even if a retry's backoff overshoots it.
            ai = await invoke_with_retry(
                lambda: asyncio.wait_for(llm.ainvoke(messages), timeout=_remaining()),
                policy=_READER_RETRY,
                description=f"light subagent{f' ({role})' if role else ''} turn "
                f"{_iteration}/{max_iterations}",
            )
        except asyncio.TimeoutError:
            logger.info(
                "light subagent LLM turn exceeded wall-clock limit (%ss); "
                "forcing synthesis",
                timeout_seconds,
            )
            return await _final_synthesis(llm, messages, last_text, reason="time limit")
        messages.append(ai)

        text = _message_text(ai)
        if text:
            last_text = text

        tool_calls = getattr(ai, "tool_calls", None)
        if not tool_calls:
            # Reader produced its final answer.
            return last_text or _EMPTY_RESULT

        if not tools_by_name:
            # Reader asked for tools but has none — return what it said.
            logger.warning(
                "light subagent requested tools but none are available; stopping"
            )
            return last_text or _EMPTY_RESULT

        # Execute the turn's tool calls concurrently, bounded by the deadline.
        remaining = _remaining()
        timed_out = remaining is not None and remaining <= 0
        if not timed_out:
            try:
                tool_messages = await asyncio.wait_for(
                    _execute_tool_calls(tool_calls, tools_by_name), timeout=remaining
                )
                messages.extend(tool_messages)
            except asyncio.TimeoutError:
                timed_out = True
        if timed_out:
            logger.info(
                "light subagent tool calls exceeded wall-clock limit (%ss); "
                "forcing synthesis",
                timeout_seconds,
            )
            messages.extend(_timeout_tool_messages(tool_calls))
            return await _final_synthesis(llm, messages, last_text, reason="time limit")

        if count_tokens_approximate(messages) >= max_tokens:
            logger.info(
                "light subagent hit token cap (%s); forcing synthesis", max_tokens
            )
            return await _final_synthesis(
                llm, messages, last_text, reason="token budget"
            )

    logger.info(
        "light subagent hit iteration cap (%s); forcing synthesis", max_iterations
    )
    return await _final_synthesis(llm, messages, last_text, reason="iteration limit")
