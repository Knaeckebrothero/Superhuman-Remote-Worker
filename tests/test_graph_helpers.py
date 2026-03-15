"""Tests for graph.py error handling helper functions.

Tests _extract_rate_limit_delay, _extract_tool_use_failed,
_build_tool_use_failed_feedback, _is_tool_error, _extract_markdown_content.
"""

from unittest.mock import MagicMock

from src.graph import (
    _extract_rate_limit_delay,
    _extract_tool_use_failed,
    _build_tool_use_failed_feedback,
    _is_tool_error,
    _extract_markdown_content,
)


# =============================================================================
# _extract_rate_limit_delay
# =============================================================================


class TestExtractRateLimitDelay:
    """Tests for rate limit detection and delay extraction."""

    def test_non_rate_limit_returns_none(self):
        """Non-rate-limit errors should return None."""
        err = Exception("Connection refused")
        assert _extract_rate_limit_delay(err) is None

    def test_429_in_message(self):
        """Error with '429' should be detected as rate limit."""
        err = Exception("HTTP 429 Too Many Requests")
        result = _extract_rate_limit_delay(err)
        assert result is not None
        assert result > 0

    def test_rate_limit_in_message(self):
        """Error with 'rate limit' should be detected."""
        err = Exception("Rate limit exceeded for this API key")
        result = _extract_rate_limit_delay(err)
        assert result is not None

    def test_too_many_requests_in_message(self):
        """Error with 'too many requests' should be detected."""
        err = Exception("Too many requests, please slow down")
        result = _extract_rate_limit_delay(err)
        assert result is not None

    def test_extracts_retry_after_from_response_headers(self):
        """Should extract retry-after from response.headers."""
        err = Exception("429 rate limit")
        response = MagicMock()
        response.headers = {"retry-after": "30"}
        err.response = response

        result = _extract_rate_limit_delay(err)
        assert result == 35.0  # 30 + 5 buffer

    def test_extracts_from_error_message_text(self):
        """Should extract retry-after from error message using regex."""
        err = Exception("429 Rate limit exceeded. retry-after: 60")
        result = _extract_rate_limit_delay(err)
        assert result == 65.0  # 60 + 5 buffer

    def test_fallback_default_delay(self):
        """When rate limit detected but no retry-after, should use default."""
        err = Exception("429 error with no retry info")
        result = _extract_rate_limit_delay(err)
        assert result == 90.0

    def test_walks_cause_chain(self):
        """Should walk __cause__ chain for response headers."""
        inner = Exception("inner 429")
        response = MagicMock()
        response.headers = {"retry-after": "15"}
        inner.response = response

        outer = Exception("wrapper")
        outer.__cause__ = inner
        # The outer error doesn't contain "429", but we need it to detect rate limit
        outer_with_429 = Exception("HTTP 429 happened")
        outer_with_429.__cause__ = inner

        result = _extract_rate_limit_delay(outer_with_429)
        assert result == 20.0  # 15 + 5


# =============================================================================
# _extract_tool_use_failed
# =============================================================================


class TestExtractToolUseFailed:
    """Tests for Groq tool_use_failed extraction."""

    def test_non_tool_error_returns_none(self):
        """Regular errors should return None."""
        err = Exception("Network timeout")
        assert _extract_tool_use_failed(err) is None

    def test_extracts_from_body_attribute(self):
        """Should extract from body.error.code == 'tool_use_failed'."""
        err = Exception("bad request")
        err.body = {
            "error": {
                "code": "tool_use_failed",
                "failed_generation": "truncated output...",
            }
        }
        result = _extract_tool_use_failed(err)
        assert result == "truncated output..."

    def test_extracts_from_cause_chain(self):
        """Should walk __cause__ chain."""
        inner = Exception("groq error")
        inner.body = {
            "error": {
                "code": "tool_use_failed",
                "failed_generation": "inner content",
            }
        }
        outer = Exception("langchain wrapper")
        outer.__cause__ = inner

        result = _extract_tool_use_failed(outer)
        assert result == "inner content"

    def test_fallback_regex_on_string(self):
        """Should extract via regex when body not available."""
        err = Exception(
            '{"error": {"code": "tool_use_failed", "failed_generation": "regex content"}}'
        )
        result = _extract_tool_use_failed(err)
        assert result is not None

    def test_tool_use_failed_without_generation(self):
        """Should return empty string when identified but no generation text."""
        err = Exception("tool_use_failed without details")
        result = _extract_tool_use_failed(err)
        assert result == ""


# =============================================================================
# _build_tool_use_failed_feedback
# =============================================================================


class TestBuildToolUseFailedFeedback:
    """Tests for building feedback from truncated tool output."""

    def test_short_content_included_fully(self):
        """Short content should be included in full."""
        result = _build_tool_use_failed_feedback("small output")
        assert "small output" in result
        # Short content should NOT have "words truncated" placeholder
        assert "words truncated" not in result

    def test_long_content_shows_preview(self):
        """Long content should show first/last 100 words with truncation note."""
        long_text = " ".join(f"word{i}" for i in range(500))
        result = _build_tool_use_failed_feedback(long_text)
        assert "word0" in result  # First word
        assert "word499" in result  # Last word
        assert "truncated" in result.lower()

    def test_includes_fix_instructions(self):
        """Should include guidance on splitting into smaller calls."""
        result = _build_tool_use_failed_feedback("any content")
        assert "split" in result.lower() or "smaller" in result.lower()

    def test_includes_explanation(self):
        """Should explain what happened."""
        result = _build_tool_use_failed_feedback("x")
        assert "exceeded" in result.lower() or "truncated" in result.lower()


# =============================================================================
# _is_tool_error
# =============================================================================


class TestIsToolError:
    """Tests for tool error detection in content."""

    def test_empty_content_false(self):
        assert _is_tool_error("") is False

    def test_none_content_false(self):
        assert _is_tool_error(None) is False

    def test_detects_error_prefix(self):
        assert _is_tool_error("Error: file not found") is True

    def test_detects_failed_prefix(self):
        assert _is_tool_error("Failed: could not connect") is True

    def test_detects_exception_prefix(self):
        assert _is_tool_error("Exception: ValueError occurred") is True

    def test_detects_traceback(self):
        assert _is_tool_error("Traceback (most recent call last)") is True

    def test_normal_content_false(self):
        assert _is_tool_error("The file contains 100 lines of code.") is False

    def test_case_insensitive(self):
        assert _is_tool_error("ERROR: something broke") is True
        assert _is_tool_error("error: something broke") is True


# =============================================================================
# _extract_markdown_content
# =============================================================================


class TestExtractMarkdownContent:
    """Tests for markdown extraction from LLM responses."""

    def test_empty_string_passthrough(self):
        assert _extract_markdown_content("") == ""

    def test_none_passthrough(self):
        assert _extract_markdown_content(None) is None

    def test_plain_text_passthrough(self):
        assert _extract_markdown_content("just text") == "just text"

    def test_strips_markdown_code_block(self):
        """Should strip ```markdown ... ``` wrapper."""
        content = "```markdown\n# Title\nContent here\n```"
        result = _extract_markdown_content(content)
        assert result == "# Title\nContent here"

    def test_strips_md_code_block(self):
        """Should strip ```md ... ``` wrapper."""
        content = "```md\n# Title\n```"
        result = _extract_markdown_content(content)
        assert result == "# Title"

    def test_strips_file_header(self):
        """Should strip **File: `filename.md`** header."""
        content = "**File: `plan.md`**\n\n# My Plan"
        result = _extract_markdown_content(content)
        assert result.startswith("# My Plan")
        assert "File:" not in result

    def test_strips_file_header_without_backticks(self):
        """Should strip **File: filename.md** header."""
        content = "**File: plan.md**\n# My Plan"
        result = _extract_markdown_content(content)
        assert "File:" not in result

    def test_combined_header_and_code_block(self):
        """Should strip both file header and code block."""
        content = "**File: `plan.md`**\n```markdown\n# Content\n```"
        result = _extract_markdown_content(content)
        assert result == "# Content"

    def test_whitespace_stripped(self):
        """Should strip leading/trailing whitespace."""
        content = "  \n  # Title  \n  "
        result = _extract_markdown_content(content)
        assert result == "# Title"
