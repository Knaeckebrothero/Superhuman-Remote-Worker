"""Shared guards for degenerate / malformed LLM responses.

Both the worker graph (``src/graph.py``) and the persistent interactive loop
(``src/persistent_graph.py``) must reject the same class of bad model output:
a response with no text content and no tool calls. ``is_degenerate_response``
is the single definition both use.

``coerce_to_ai_message`` / ``finalize_streamed_response`` additionally protect
the persistent loop from a specific corruption: an interrupted or empty
*streamed* response is an ``AIMessageChunk`` (or other chunk type) that, if left
in the conversation history, makes ``langchain_openai._convert_message_to_dict``
raise ``TypeError: Got unknown type ...`` on the *next* turn — surfaced to the
user as "The assistant returned a malformed response." See
``docs/issues/persistent_session_empty_chunk_history_corruption.md``.
"""

from typing import Any, List, Optional

from langchain_core.messages import AIMessage

__all__ = [
    "is_degenerate_response",
    "coerce_to_ai_message",
    "finalize_streamed_response",
]


def is_degenerate_response(content_len: int, tool_calls_count: int) -> bool:
    """A response is degenerate when it carries no text content and no tool
    calls — nothing for the user and nothing for the loop to act on."""
    return content_len == 0 and tool_calls_count == 0


def _text_len(content: Any) -> int:
    """Length of the stripped *text* content, across str and list-of-blocks
    (Anthropic-style) shapes. Reasoning/thinking blocks do not count as an
    answer."""
    if isinstance(content, str):
        return len(content.strip())
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block.strip())
            elif isinstance(block, dict) and block.get("text"):
                total += len(str(block["text"]).strip())
        return total
    return 0


def coerce_to_ai_message(response: Any) -> AIMessage:
    """Return a concrete ``AIMessage`` for any assistant response object.

    A streamed response aggregated from chunks stays an ``AIMessageChunk``
    (or a more generic chunk type); downstream request serialization only
    recognizes the concrete message classes. Rebuilding as a plain
    ``AIMessage`` normalizes the type while preserving content, tool calls,
    and metadata.
    """
    if type(response) is AIMessage:
        return response
    return AIMessage(
        content=getattr(response, "content", "") or "",
        additional_kwargs=dict(getattr(response, "additional_kwargs", {}) or {}),
        response_metadata=dict(getattr(response, "response_metadata", {}) or {}),
        tool_calls=list(getattr(response, "tool_calls", []) or []),
        id=getattr(response, "id", None),
    )


def finalize_streamed_response(response: Any) -> Optional[AIMessage]:
    """Normalize a streamed response for safe insertion into history.

    Returns a concrete ``AIMessage``, or ``None`` if the response is
    degenerate (no text content and no tool calls) and should not be
    appended to the conversation at all.
    """
    if response is None:
        return None
    msg = coerce_to_ai_message(response)
    tool_calls: List[Any] = msg.tool_calls or []
    if is_degenerate_response(_text_len(msg.content), len(tool_calls)):
        return None
    return msg
