"""Tests for ReasoningChatOpenAI — Layer 0 overflow protection and reasoning capture.

Tests count_request_tokens, ContextOverflowError raising, reasoning_content
extraction, _extract_responses_api_reasoning, and the _is_quota_error heuristic.
"""

import json
import pytest
from unittest.mock import MagicMock

from src.llm.exceptions import ContextOverflowError
from src.llm.reasoning_chat import (
    count_request_tokens,
    _extract_reasoning_from_response,
    _extract_responses_api_reasoning,
    _is_debug_stream,
    _get_debug_tail_chars,
    ReasoningCapturingClient,
)


# =============================================================================
# ContextOverflowError
# =============================================================================


class TestContextOverflowError:
    """Tests for the ContextOverflowError exception."""

    def test_stores_attributes(self):
        err = ContextOverflowError(token_count=150_000, limit=100_000, request_size_bytes=500_000)
        assert err.token_count == 150_000
        assert err.limit == 100_000
        assert err.request_size_bytes == 500_000

    def test_default_message(self):
        err = ContextOverflowError(token_count=120_000, limit=100_000)
        assert "120,000" in str(err)
        assert "100,000" in str(err)

    def test_custom_message(self):
        err = ContextOverflowError(token_count=1, limit=1, message="custom")
        assert str(err) == "custom"


# =============================================================================
# count_request_tokens
# =============================================================================


class TestCountRequestTokens:
    """Tests for count_request_tokens."""

    def test_empty_body(self):
        """Empty request should have minimal tokens (overhead only)."""
        result = count_request_tokens({})
        assert result >= 0
        assert result < 50  # Just overhead

    def test_counts_messages(self):
        """Should count tokens in message content."""
        body = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello " * 100},
            ]
        }
        result = count_request_tokens(body)
        assert result > 50  # Non-trivial content

    def test_more_content_means_more_tokens(self):
        """Larger messages should produce higher token counts."""
        small = {"messages": [{"role": "user", "content": "Hi"}]}
        large = {"messages": [{"role": "user", "content": "Hello " * 1000}]}
        assert count_request_tokens(large) > count_request_tokens(small)

    def test_counts_tool_definitions(self):
        """Should count tokens in tool definitions."""
        without_tools = {"messages": [{"role": "user", "content": "test"}]}
        with_tools = {
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}},
            ],
        }
        assert count_request_tokens(with_tools) > count_request_tokens(without_tools)

    def test_counts_tool_calls_in_messages(self):
        """Should count tokens in assistant tool_calls."""
        body = {
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ]},
            ]
        }
        result = count_request_tokens(body)
        assert result > 10

    def test_counts_multimodal_text_parts(self):
        """Should count text in multimodal content parts."""
        body = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image " * 50},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }]
        }
        result = count_request_tokens(body)
        assert result > 50

    def test_handles_responses_api_format(self):
        """Should count tokens in Responses API 'input' field."""
        body = {
            "input": [
                {"role": "user", "content": "Hello " * 100},
            ],
            "instructions": "Be helpful " * 50,
        }
        result = count_request_tokens(body)
        assert result > 50

    def test_handles_string_input_items(self):
        """Should handle plain string items in Responses API input."""
        body = {"input": ["Hello world " * 100]}
        result = count_request_tokens(body)
        assert result > 10


# =============================================================================
# _extract_responses_api_reasoning
# =============================================================================


class TestExtractResponsesApiReasoning:
    """Tests for extracting reasoning from Responses API format."""

    def test_skips_string_content(self):
        """Should not modify messages with string content."""
        msg = MagicMock()
        msg.content = "Just a string"
        msg.additional_kwargs = {}
        _extract_responses_api_reasoning(msg)
        assert msg.content == "Just a string"

    def test_extracts_reasoning_blocks(self):
        """Should extract reasoning from typed blocks."""
        msg = MagicMock()
        msg.content = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
            {"type": "output_text", "text": "Final answer"},
        ]
        msg.additional_kwargs = {}
        _extract_responses_api_reasoning(msg)

        assert msg.additional_kwargs["reasoning_content"] == "thinking..."
        assert msg.content == "Final answer"

    def test_handles_no_reasoning(self):
        """Should leave content alone when no reasoning blocks."""
        msg = MagicMock()
        msg.content = [
            {"type": "output_text", "text": "Just output"},
        ]
        msg.additional_kwargs = {}
        _extract_responses_api_reasoning(msg)
        assert "reasoning_content" not in msg.additional_kwargs

    def test_handles_empty_list(self):
        """Should handle empty content list."""
        msg = MagicMock()
        msg.content = []
        msg.additional_kwargs = {}
        _extract_responses_api_reasoning(msg)
        assert msg.content == ""


# =============================================================================
# ReasoningCapturingClient — token validation
# =============================================================================


class TestReasoningCapturingClientTokenValidation:
    """Tests for token validation in ReasoningCapturingClient."""

    def test_max_context_from_param(self):
        """Should use explicit max_context_tokens parameter."""
        client = ReasoningCapturingClient(max_context_tokens=50_000)
        assert client._max_context_tokens == 50_000

    def test_max_context_default(self):
        """Should default to DEFAULT_MAX_CONTEXT_TOKENS."""
        client = ReasoningCapturingClient()
        assert client._max_context_tokens == 100_000


# =============================================================================
# _is_quota_error heuristic
# =============================================================================


class TestIsQuotaError:
    """Tests for quota vs rate-limit distinction."""

    def _make_response(self, status_code=429, text="", headers=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.headers = headers or {}
        return resp

    def test_quota_keyword_in_body(self):
        """Body with 'quota' should be quota error."""
        client = ReasoningCapturingClient()
        resp = self._make_response(text='{"error": "insufficient_quota"}')
        assert client._is_quota_error(resp) is True

    def test_billing_keyword_in_body(self):
        """Body with 'billing' should be quota error."""
        client = ReasoningCapturingClient()
        resp = self._make_response(text='{"error": "billing issue"}')
        assert client._is_quota_error(resp) is True

    def test_short_retry_after_is_rate_limit(self):
        """Short retry-after without quota signal -> rate limit (not quota)."""
        client = ReasoningCapturingClient()
        resp = self._make_response(text='{"error": "too many requests"}', headers={"retry-after": "5"})
        assert client._is_quota_error(resp) is False

    def test_no_signals_defaults_to_quota(self):
        """No retry-after, no quota signal -> conservatively assume quota."""
        client = ReasoningCapturingClient()
        resp = self._make_response(text='{"error": "unknown"}')
        assert client._is_quota_error(resp) is True


# =============================================================================
# Helper functions
# =============================================================================


class TestHelperFunctions:
    """Tests for module-level helper functions."""

    def test_is_debug_stream_default(self):
        """Should return False when env not set."""
        import os
        old = os.environ.pop("DEBUG_LLM_STREAM", None)
        try:
            assert _is_debug_stream() is False
        finally:
            if old is not None:
                os.environ["DEBUG_LLM_STREAM"] = old

    def test_get_debug_tail_default(self):
        """Should return 500 by default."""
        import os
        old = os.environ.pop("DEBUG_LLM_TAIL", None)
        try:
            assert _get_debug_tail_chars() == 500
        finally:
            if old is not None:
                os.environ["DEBUG_LLM_TAIL"] = old


# =============================================================================
# _extract_reasoning_from_response
# =============================================================================


class TestExtractReasoningFromResponse:
    """Tests for multi-provider reasoning extraction from chat completion responses."""

    def _make_response(self, message_fields: dict) -> dict:
        """Build a minimal chat completion response dict."""
        return {"choices": [{"message": message_fields}]}

    def test_deepseek_format(self):
        """Should extract reasoning_content (DeepSeek R1 format)."""
        data = self._make_response({"reasoning_content": "Let me think step by step..."})
        assert _extract_reasoning_from_response(data) == "Let me think step by step..."

    def test_openrouter_string_format(self):
        """Should extract reasoning (OpenRouter plain string format)."""
        data = self._make_response({"reasoning": "I need to consider..."})
        assert _extract_reasoning_from_response(data) == "I need to consider..."

    def test_openrouter_details_format(self):
        """Should extract and join reasoning_details (OpenRouter array format)."""
        data = self._make_response({
            "reasoning_details": [
                {"type": "thinking", "text": "First step."},
                {"type": "thinking", "text": "Second step."},
            ]
        })
        assert _extract_reasoning_from_response(data) == "First step.\nSecond step."

    def test_deepseek_priority_over_openrouter(self):
        """DeepSeek format should win when both are present."""
        data = self._make_response({
            "reasoning_content": "DeepSeek thinking",
            "reasoning": "OpenRouter thinking",
        })
        assert _extract_reasoning_from_response(data) == "DeepSeek thinking"

    def test_no_reasoning_returns_none(self):
        """Should return None when no reasoning fields are present."""
        data = self._make_response({"content": "Just a normal response"})
        assert _extract_reasoning_from_response(data) is None

    def test_empty_response(self):
        """Should return None for empty/minimal response."""
        assert _extract_reasoning_from_response({}) is None
        assert _extract_reasoning_from_response({"choices": []}) is None
        assert _extract_reasoning_from_response({"choices": [{}]}) is None

    def test_empty_reasoning_details(self):
        """Should return None for empty reasoning_details array."""
        data = self._make_response({"reasoning_details": []})
        assert _extract_reasoning_from_response(data) is None

    def test_malformed_reasoning_details(self):
        """Should skip non-dict items and items without text."""
        data = self._make_response({
            "reasoning_details": [
                "not a dict",
                {"type": "thinking"},  # no text key
                {"type": "thinking", "text": ""},  # empty text (falsy)
                {"type": "thinking", "text": "Valid part"},
            ]
        })
        assert _extract_reasoning_from_response(data) == "Valid part"
