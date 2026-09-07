"""A scripted chat model for driving ``run_persistent_loop`` in tests.

Port of the U0 spike's ``FakeChatModel`` (knowledge-base/knowledge/research/
subagents/u0_spike.py). Duck-typed to exactly what the loop touches:
``astream(messages)`` (an async iterator of ``AIMessageChunk`` / ``AIMessage``
combined with ``+``), ``ainvoke`` as a fallback, ``bind_tools`` (what the
session/child build calls at setup) and the attribute ``reasoning = None``
(so none of the Responses-API workarounds apply).

Script entries are lists of chunks (``text_turn`` / ``tool_turn``), or a
callable/exception to raise on that call (``raise_turn``), or ``HANG`` to
block forever (staleness tests).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

HANG = object()


class FakeChatModel:
    """Scripted stand-in for ``llm_with_tools``."""

    reasoning = None  # not a Responses-API/reasoning model → no ainvoke retries

    def __init__(self, script: List[Any]):
        self._script = list(script)
        self.calls: List[List[BaseMessage]] = []  # provider input per call
        self.bound_tool_names: Optional[List[str]] = None
        self.bind_calls = 0
        self.hang_started = asyncio.Event()

    def bind_tools(self, tools, **kw):  # session-side API, returns self
        self.bound_tool_names = [t.name for t in tools]
        self.bind_calls += 1
        return self

    @property
    def remaining(self) -> int:
        return len(self._script)

    async def astream(self, messages, **kw):
        self.calls.append(list(messages))
        if not self._script:
            raise RuntimeError("FakeChatModel: script exhausted")
        step = self._script.pop(0)
        if step is HANG:
            self.hang_started.set()
            await asyncio.Event().wait()  # never returns (cancelled from outside)
        if isinstance(step, BaseException):
            raise step
        if callable(step) and not isinstance(step, list):
            step = step()
            if isinstance(step, BaseException):
                raise step
        for chunk in step:
            await asyncio.sleep(0)  # yield like a network stream would
            yield chunk

    async def ainvoke(self, messages, **kw):
        chunks = []
        async for c in self.astream(messages):
            chunks.append(c)
        out = chunks[0]
        for c in chunks[1:]:
            out = out + c
        return out


def text_turn(
    text: str,
    *,
    split: int = 2,
    input_tokens: int = 120,
    output_tokens: int = 12,
    finish_reason: str = "stop",
) -> List[Any]:
    """A streamed final answer in ``split`` chunks; usage on the last chunk."""
    n = max(1, len(text) // split)
    pieces = [text[i : i + n] for i in range(0, len(text), n)] or [""]
    chunks = [AIMessageChunk(content=p) for p in pieces]
    chunks[-1] = AIMessageChunk(
        content=pieces[-1],
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        response_metadata={"finish_reason": finish_reason, "model_name": "fake-model"},
    )
    return chunks


def tool_turn(
    name: str,
    args: Dict[str, Any],
    call_id: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 8,
    extra_calls: Optional[List[Dict[str, Any]]] = None,
) -> List[Any]:
    """A single-chunk tool-calling assistant message (optionally a batch)."""
    calls = [{"id": call_id, "name": name, "args": args, "type": "tool_call"}]
    for extra in extra_calls or []:
        calls.append({"type": "tool_call", **extra})
    return [
        AIMessage(
            content="",
            tool_calls=calls,
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_metadata={
                "finish_reason": "tool_calls",
                "model_name": "fake-model",
            },
        )
    ]


def raise_turn(error: BaseException) -> BaseException:
    """Script entry: the call raises ``error`` (a transient provider failure)."""
    return error


__all__ = ["HANG", "FakeChatModel", "raise_turn", "text_turn", "tool_turn"]
