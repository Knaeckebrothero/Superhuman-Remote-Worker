"""Tests for src/llm/response_guards.py.

Covers the message-normalization guard that prevents an interrupted/empty
streamed chunk from poisoning the persistent-thread history (issue
persistent_session_empty_chunk_history_corruption):

- is_degenerate_response  — shared "empty content + no tool calls" predicate
- coerce_to_ai_message    — any chunk/other → concrete AIMessage
- finalize_streamed_response — coerce + drop degenerate (returns None)
"""

from langchain_core.messages import AIMessage, AIMessageChunk

from src.llm.response_guards import (
    coerce_to_ai_message,
    finalize_streamed_response,
    is_degenerate_response,
)


class TestIsDegenerateResponse:
    def test_empty_content_and_no_tool_calls_is_degenerate(self):
        assert is_degenerate_response(content_len=0, tool_calls_count=0) is True

    def test_content_present_is_not_degenerate(self):
        assert is_degenerate_response(content_len=5, tool_calls_count=0) is False

    def test_tool_calls_present_is_not_degenerate(self):
        assert is_degenerate_response(content_len=0, tool_calls_count=2) is False


class TestCoerceToAiMessage:
    def test_chunk_becomes_concrete_ai_message(self):
        """An AIMessageChunk must be coerced to a concrete AIMessage so
        langchain_openai._convert_message_to_dict recognizes its type."""
        chunk = AIMessageChunk(content="hello")
        result = coerce_to_ai_message(chunk)
        assert type(result) is AIMessage  # not AIMessageChunk
        assert result.content == "hello"

    def test_concrete_ai_message_content_preserved(self):
        msg = AIMessage(content="hi there")
        result = coerce_to_ai_message(msg)
        assert isinstance(result, AIMessage)
        assert result.content == "hi there"

    def test_tool_calls_preserved_through_coercion(self):
        tc = {"name": "f", "args": {"x": 1}, "id": "call_1", "type": "tool_call"}
        chunk = AIMessageChunk(content="", tool_calls=[tc])
        result = coerce_to_ai_message(chunk)
        assert type(result) is AIMessage
        # Coercion must preserve whatever tool calls the chunk exposes.
        assert result.tool_calls == chunk.tool_calls
        assert result.tool_calls == [tc]


class TestFinalizeStreamedResponse:
    def test_empty_chunk_is_dropped(self):
        """The exact poison shape: an empty streamed chunk carrying only a
        run-id. Must be dropped (None) so it never reaches the next request."""
        chunk = AIMessageChunk(content="", id="lc_run--019e5adc-0945-7b73")
        assert finalize_streamed_response(chunk) is None

    def test_none_input_returns_none(self):
        assert finalize_streamed_response(None) is None

    def test_whitespace_only_no_tool_calls_is_dropped(self):
        assert finalize_streamed_response(AIMessage(content="   ")) is None

    def test_content_chunk_returns_concrete_ai_message(self):
        chunk = AIMessageChunk(content="here is the answer")
        result = finalize_streamed_response(chunk)
        assert type(result) is AIMessage
        assert result.content == "here is the answer"

    def test_empty_content_with_tool_calls_is_kept(self):
        """Empty text but a real tool call is a valid assistant turn — keep it."""
        tc = {"name": "f", "args": {}, "id": "c1", "type": "tool_call"}
        msg = AIMessage(content="", tool_calls=[tc])
        result = finalize_streamed_response(msg)
        assert result is not None
        assert result.tool_calls == [tc]
