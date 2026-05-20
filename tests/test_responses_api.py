"""Tests for Responses API integration helpers.

Tests _extract_responses_api_reasoning (reasoning_chat.py) and
_normalize_content (archiver.py) which handle the Responses API
content block format used by models like GPT-5.2-pro.
"""

from unittest.mock import MagicMock

from src.llm.reasoning_chat import (
    _extract_responses_api_reasoning,
    extract_reasoning_text_from_block,
)
from src.core.archiver import _normalize_content


# =============================================================================
# _extract_responses_api_reasoning tests
# =============================================================================


class TestExtractResponsesApiReasoning:
    """Tests for extracting reasoning from Responses API content blocks."""

    def _make_message(self, content, additional_kwargs=None):
        """Create a mock message with given content."""
        msg = MagicMock()
        msg.content = content
        msg.additional_kwargs = additional_kwargs or {}
        return msg

    def test_string_content_unchanged(self):
        """String content should pass through unmodified."""
        msg = self._make_message("Hello world")
        _extract_responses_api_reasoning(msg)
        assert msg.content == "Hello world"
        assert "reasoning_content" not in msg.additional_kwargs

    def test_extracts_reasoning_from_summary(self):
        """Should extract reasoning text from summary field."""
        msg = self._make_message(
            [
                {
                    "type": "reasoning",
                    "summary": [{"type": "text", "text": "Let me think..."}],
                },
                {"type": "output_text", "text": "The answer is 42."},
            ]
        )
        _extract_responses_api_reasoning(msg)
        assert msg.additional_kwargs["reasoning_content"] == "Let me think..."
        assert msg.content == "The answer is 42."

    def test_extracts_reasoning_from_content_field(self):
        """Should extract reasoning text from content field of reasoning block."""
        msg = self._make_message(
            [
                {
                    "type": "reasoning",
                    "content": [
                        {"type": "text", "text": "Step 1"},
                        {"type": "text", "text": "Step 2"},
                    ],
                },
                {"type": "output_text", "text": "Done."},
            ]
        )
        _extract_responses_api_reasoning(msg)
        assert msg.additional_kwargs["reasoning_content"] == "Step 1\nStep 2"
        assert msg.content == "Done."

    def test_extracts_reasoning_from_both_summary_and_content(self):
        """Should concatenate reasoning from both summary and content fields."""
        msg = self._make_message(
            [
                {
                    "type": "reasoning",
                    "summary": [{"type": "text", "text": "Summary thought"}],
                    "content": [{"type": "text", "text": "Detailed thought"}],
                },
                {"type": "output_text", "text": "Result."},
            ]
        )
        _extract_responses_api_reasoning(msg)
        assert (
            msg.additional_kwargs["reasoning_content"]
            == "Summary thought\nDetailed thought"
        )
        assert msg.content == "Result."

    def test_no_reasoning_blocks(self):
        """List content without reasoning blocks should just be flattened."""
        msg = self._make_message(
            [
                {"type": "output_text", "text": "Part 1."},
                {"type": "output_text", "text": "Part 2."},
            ]
        )
        _extract_responses_api_reasoning(msg)
        assert "reasoning_content" not in msg.additional_kwargs
        assert msg.content == "Part 1. Part 2."

    def test_mixed_string_and_dict_blocks(self):
        """Should handle a mix of plain strings and dict blocks."""
        msg = self._make_message(
            [
                "Hello",
                {"type": "output_text", "text": "world"},
            ]
        )
        _extract_responses_api_reasoning(msg)
        assert msg.content == "Hello world"

    def test_empty_list_content(self):
        """Empty list should result in empty string."""
        msg = self._make_message([])
        _extract_responses_api_reasoning(msg)
        assert msg.content == ""

    def test_multiple_reasoning_blocks(self):
        """Should concatenate reasoning from multiple reasoning blocks."""
        msg = self._make_message(
            [
                {
                    "type": "reasoning",
                    "summary": [{"type": "text", "text": "First thought"}],
                },
                {"type": "output_text", "text": "Middle text."},
                {
                    "type": "reasoning",
                    "summary": [{"type": "text", "text": "Second thought"}],
                },
                {"type": "output_text", "text": "Final text."},
            ]
        )
        _extract_responses_api_reasoning(msg)
        assert (
            msg.additional_kwargs["reasoning_content"]
            == "First thought\nSecond thought"
        )
        assert msg.content == "Middle text. Final text."

    def test_preserves_existing_additional_kwargs(self):
        """Should not overwrite other keys in additional_kwargs."""
        msg = self._make_message(
            [
                {
                    "type": "reasoning",
                    "summary": [{"type": "text", "text": "Thinking"}],
                },
                {"type": "output_text", "text": "Answer"},
            ],
            additional_kwargs={"existing_key": "existing_value"},
        )
        _extract_responses_api_reasoning(msg)
        assert msg.additional_kwargs["existing_key"] == "existing_value"
        assert msg.additional_kwargs["reasoning_content"] == "Thinking"


# =============================================================================
# extract_reasoning_text_from_block tests
# =============================================================================


class TestExtractReasoningTextFromBlock:
    """Tests for the streaming-tolerant reasoning text extractor."""

    def test_empty_block(self):
        assert extract_reasoning_text_from_block({}) == ""

    def test_direct_text_field(self):
        """Some streaming chunks ship text directly on the block."""
        assert extract_reasoning_text_from_block({"text": "hi"}) == "hi"

    def test_summary_items(self):
        block = {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "First."},
                {"type": "summary_text", "text": "Second."},
            ],
        }
        # Streaming concatenation: no separator between deltas.
        assert extract_reasoning_text_from_block(block) == "First.Second."

    def test_content_items(self):
        block = {
            "type": "reasoning",
            "content": [
                {"type": "reasoning_text", "text": "A"},
                {"type": "reasoning_text", "text": "B"},
            ],
        }
        assert extract_reasoning_text_from_block(block) == "AB"

    def test_direct_and_summary_combined(self):
        """Direct text + summary items concatenated in order."""
        block = {
            "text": "direct ",
            "summary": [{"text": "summary"}],
        }
        assert extract_reasoning_text_from_block(block) == "direct summary"

    def test_skips_non_string_text(self):
        """Items with non-string text fields are skipped."""
        block = {
            "summary": [
                {"text": "ok"},
                {"text": None},
                {"text": 42},
                {"other": "ignored"},
            ],
        }
        assert extract_reasoning_text_from_block(block) == "ok"

    def test_skips_non_dict_items(self):
        block = {"summary": ["raw string", {"text": "kept"}, None]}
        assert extract_reasoning_text_from_block(block) == "kept"

    def test_none_summary_handled(self):
        """A None summary (explicit absence) shouldn't crash."""
        assert extract_reasoning_text_from_block({"summary": None}) == ""


# =============================================================================
# _normalize_content tests
# =============================================================================


class TestNormalizeContent:
    """Tests for normalizing message content to string."""

    def test_string_passthrough(self):
        assert _normalize_content("hello") == "hello"

    def test_empty_string(self):
        assert _normalize_content("") == ""

    def test_none_returns_empty(self):
        assert _normalize_content(None) == ""

    def test_list_of_strings(self):
        assert _normalize_content(["hello", "world"]) == "hello world"

    def test_list_of_text_dicts(self):
        result = _normalize_content(
            [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": "Part 2"},
            ]
        )
        assert result == "Part 1 Part 2"

    def test_mixed_strings_and_dicts(self):
        result = _normalize_content(
            [
                "Hello",
                {"type": "text", "text": "world"},
            ]
        )
        assert result == "Hello world"

    def test_empty_list(self):
        assert _normalize_content([]) == ""

    def test_list_with_non_text_dicts(self):
        """Dicts without 'text' key should be skipped."""
        result = _normalize_content(
            [
                {"type": "text", "text": "Keep this"},
                {"type": "image", "url": "http://example.com/img.png"},
            ]
        )
        assert result == "Keep this"

    def test_integer_content(self):
        """Non-string non-list content should be stringified."""
        assert _normalize_content(42) == "42"
