"""Tests for graph.py error handling helper functions.

Tests _extract_rate_limit_delay, _extract_tool_use_failed,
_build_tool_use_failed_feedback, _is_tool_error, _extract_markdown_content,
_check_empty_response_streak, _check_no_tool_call_streak.
"""

from unittest.mock import MagicMock

from src.graph import (
    _classify_llm_error,
    _extract_rate_limit_delay,
    _extract_tool_use_failed,
    _build_tool_use_failed_feedback,
    _is_tool_error,
    _extract_markdown_content,
    _check_empty_response_streak,
    _check_no_tool_call_streak,
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


# =============================================================================
# _check_empty_response_streak
# =============================================================================


class TestCheckEmptyResponseStreak:
    """Tests for the empty-response circuit-breaker helper."""

    def test_non_empty_content_resets_streak(self):
        """A response with any content resets the streak to zero."""
        new_streak, should_fail = _check_empty_response_streak(
            content_len=42, tool_calls_count=0, current_streak=2
        )
        assert new_streak == 0
        assert should_fail is False

    def test_tool_call_only_resets_streak(self):
        """A response with tool calls but no content resets the streak."""
        new_streak, should_fail = _check_empty_response_streak(
            content_len=0, tool_calls_count=1, current_streak=2
        )
        assert new_streak == 0
        assert should_fail is False

    def test_empty_response_increments(self):
        """An empty response increments the streak by one."""
        new_streak, should_fail = _check_empty_response_streak(
            content_len=0, tool_calls_count=0, current_streak=0
        )
        assert new_streak == 1
        assert should_fail is False

    def test_below_threshold_does_not_fail(self):
        """Streaks at or below the threshold do not trigger a fail."""
        new_streak, should_fail = _check_empty_response_streak(
            content_len=0, tool_calls_count=0, current_streak=2
        )
        assert new_streak == 3
        assert should_fail is False

    def test_above_threshold_fails(self):
        """Crossing the threshold triggers a fail."""
        new_streak, should_fail = _check_empty_response_streak(
            content_len=0, tool_calls_count=0, current_streak=3
        )
        assert new_streak == 4
        assert should_fail is True

    def test_custom_threshold(self):
        """Threshold parameter is honored."""
        new_streak, should_fail = _check_empty_response_streak(
            content_len=0, tool_calls_count=0, current_streak=1, threshold=1
        )
        assert new_streak == 2
        assert should_fail is True

    def test_recovery_then_relapse(self):
        """A successful response between empties restarts the count."""
        # Two empties
        s, _ = _check_empty_response_streak(0, 0, 0)
        s, _ = _check_empty_response_streak(0, 0, s)
        assert s == 2
        # Recovery
        s, _ = _check_empty_response_streak(
            content_len=10, tool_calls_count=0, current_streak=s
        )
        assert s == 0
        # Empty again — must start at 1, not resume from 2
        s, fail = _check_empty_response_streak(0, 0, s)
        assert s == 1
        assert fail is False


# =============================================================================
# _check_no_tool_call_streak
# =============================================================================


class TestCheckNoToolCallStreak:
    """Tests for the parser-failure circuit-breaker helper.

    Catches the case from job 3c30d72e where Gemma emitted malformed tool-call
    syntax that vLLM's gemma4 parser left as content text. tool_calls=None,
    same 21-token response repeating verbatim 1385× in a row.
    """

    LEAKED = '<|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>'

    def test_tool_call_present_resets(self):
        """Any tool call resets streak and clears the hash."""
        new_streak, should_fail, new_hash = _check_no_tool_call_streak(
            content_str=self.LEAKED,
            tool_calls_count=1,
            current_streak=2,
            last_hash="prev",
        )
        assert new_streak == 0
        assert should_fail is False
        assert new_hash == ""

    def test_empty_content_resets(self):
        """Empty content does not advance this streak (the empty-response
        guard handles that separately)."""
        new_streak, should_fail, new_hash = _check_no_tool_call_streak(
            content_str="",
            tool_calls_count=0,
            current_streak=2,
            last_hash="prev",
        )
        assert new_streak == 0
        assert should_fail is False
        assert new_hash == ""

    def test_first_no_tool_call_starts_at_one(self):
        """First no-tool-call response starts streak at 1 with new hash."""
        new_streak, should_fail, new_hash = _check_no_tool_call_streak(
            content_str=self.LEAKED,
            tool_calls_count=0,
            current_streak=0,
            last_hash="",
        )
        assert new_streak == 1
        assert should_fail is False
        assert new_hash != ""

    def test_different_content_resets_to_one(self):
        """Different content with no tool calls resets streak to 1, not 0 —
        the response *is* a no-tool-call response, just not stuck-yet."""
        # First: content A
        s1, _, h1 = _check_no_tool_call_streak(
            content_str="response A",
            tool_calls_count=0,
            current_streak=0,
            last_hash="",
        )
        assert s1 == 1
        # Second: different content B → resets streak, new hash
        s2, fail2, h2 = _check_no_tool_call_streak(
            content_str="response B",
            tool_calls_count=0,
            current_streak=s1,
            last_hash=h1,
        )
        assert s2 == 1
        assert fail2 is False
        assert h2 != h1

    def test_identical_content_increments(self):
        """Same content twice → streak 2."""
        s1, _, h1 = _check_no_tool_call_streak(self.LEAKED, 0, 0, "")
        s2, fail2, h2 = _check_no_tool_call_streak(self.LEAKED, 0, s1, h1)
        assert s2 == 2
        assert fail2 is False
        assert h2 == h1

    def test_below_threshold_does_not_fail(self):
        """At threshold (3 identical) → not yet failing (uses strict >)."""
        s, h = 0, ""
        for _ in range(3):
            s, fail, h = _check_no_tool_call_streak(self.LEAKED, 0, s, h)
            assert fail is False
        assert s == 3

    def test_above_threshold_fails(self):
        """Crossing the threshold triggers a fail at iter 4."""
        s, h = 0, ""
        for i in range(4):
            s, fail, h = _check_no_tool_call_streak(self.LEAKED, 0, s, h)
            if i < 3:
                assert fail is False
            else:
                assert fail is True
                assert s == 4

    def test_custom_threshold(self):
        """Threshold parameter is honored."""
        s1, fail1, h1 = _check_no_tool_call_streak(self.LEAKED, 0, 0, "", threshold=1)
        assert s1 == 1
        assert fail1 is False
        s2, fail2, _ = _check_no_tool_call_streak(self.LEAKED, 0, s1, h1, threshold=1)
        assert s2 == 2
        assert fail2 is True

    def test_recovery_via_tool_call_then_relapse(self):
        """Tool call mid-streak resets; subsequent identical no-tool-calls
        start fresh from 1."""
        # Two leaked-text responses
        s, h = 0, ""
        s, _, h = _check_no_tool_call_streak(self.LEAKED, 0, s, h)
        s, _, h = _check_no_tool_call_streak(self.LEAKED, 0, s, h)
        assert s == 2
        # Recovery: a tool call lands
        s, _, h = _check_no_tool_call_streak(self.LEAKED, 1, s, h)
        assert s == 0
        assert h == ""
        # Same leaked text returns — must start at 1, not resume from 2
        s, fail, _ = _check_no_tool_call_streak(self.LEAKED, 0, s, h)
        assert s == 1
        assert fail is False

    def test_varied_responses_never_fail(self):
        """A model that produces *different* no-tool-call responses across
        many iterations (legitimate reflections) never trips the detector."""
        responses = [f"reflection turn {i}" for i in range(20)]
        s, h = 0, ""
        for r in responses:
            s, fail, h = _check_no_tool_call_streak(r, 0, s, h)
            assert fail is False
            assert s == 1  # always reset to 1 because hash differs

    def test_unicode_content_hashes_stably(self):
        """Non-ASCII content (e.g. user task descriptions in German) hashes
        and matches without errors."""
        umlaut = "Küppelsmühle — über uns"
        s1, _, h1 = _check_no_tool_call_streak(umlaut, 0, 0, "")
        s2, _, h2 = _check_no_tool_call_streak(umlaut, 0, s1, h1)
        assert s2 == 2
        assert h1 == h2


# =============================================================================
# _classify_llm_error
# =============================================================================


def _make_sdk_error(class_name: str, status_code, *, body=None, message=""):
    """Build a duck-typed SDK error.

    The classifier inspects ``type(exc).__name__`` and ``exc.status_code`` /
    ``exc.body`` rather than isinstance-checking the openai/anthropic SDK
    types, so we don't need to import them here.
    """
    cls = type(class_name, (Exception,), {})
    err = cls(message)
    err.status_code = status_code
    if body is not None:
        err.body = body
    return err


class TestClassifyLlmError:
    """Tests for the permanent/rate_limit/transient classifier that gates
    the inner retry loop in create_execute_node. Regression coverage for
    the 2026-05-12 cluster outage where a 404 model-not-found looped 70+
    iterations against a guaranteed-failure endpoint."""

    def test_404_model_not_found_is_permanent(self):
        err = _make_sdk_error(
            "NotFoundError",
            404,
            body={"error": {"message": "Model 'x' not found", "type": "invalid_request_error"}},
        )
        assert _classify_llm_error(err) == "permanent"

    def test_401_auth_is_permanent(self):
        err = _make_sdk_error("AuthenticationError", 401)
        assert _classify_llm_error(err) == "permanent"

    def test_403_permission_denied_is_permanent(self):
        err = _make_sdk_error("PermissionDeniedError", 403)
        assert _classify_llm_error(err) == "permanent"

    def test_400_invalid_request_is_permanent(self):
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={"error": {"type": "invalid_request_error", "code": "schema_error"}},
        )
        assert _classify_llm_error(err) == "permanent"

    def test_400_tool_use_failed_is_transient(self):
        """Groq's tool_use_failed (400 with code=tool_use_failed) must
        retry — it's a recoverable token-budget overrun, not a config bug."""
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={"error": {"type": "invalid_request_error", "code": "tool_use_failed"}},
        )
        assert _classify_llm_error(err) == "transient"

    def test_400_rate_limit_disguised_is_rate_limit(self):
        """Some providers return rate-limit info under a 400 with a
        rate-related code — must NOT be permanent."""
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={"error": {"type": "invalid_request_error", "code": "rate_limit_exceeded"}},
        )
        assert _classify_llm_error(err) == "rate_limit"

    def test_429_is_rate_limit(self):
        err = _make_sdk_error("RateLimitError", 429)
        assert _classify_llm_error(err) == "rate_limit"

    def test_503_is_transient(self):
        err = _make_sdk_error("APIStatusError", 503)
        assert _classify_llm_error(err) == "transient"

    def test_connection_error_is_transient(self):
        """No status_code, no recognizable class — default to transient
        so we don't aggressively fail jobs on truly transient network
        flakes."""
        assert _classify_llm_error(ConnectionError("connect refused")) == "transient"

    def test_walks_cause_chain(self):
        """LangChain wraps provider exceptions — classifier must unwrap."""
        inner = _make_sdk_error("NotFoundError", 404)
        outer = Exception("LangChain wrapper")
        outer.__cause__ = inner
        assert _classify_llm_error(outer) == "permanent"

    def test_message_text_fallback_for_404_model(self):
        """When status_code/class are stripped (logs, re-raised as plain
        Exception), the stringified message is the last-resort signal —
        this is exactly the shape that surfaced in the 2026-05-12 logs."""
        err = Exception(
            "Error code: 404 - {'error': {'message': \"Model 'gpt-5.3' not found\"}}"
        )
        assert _classify_llm_error(err) == "permanent"

    def test_400_without_parseable_body_is_transient(self):
        """A bare 400 without a body we can interpret — be conservative
        and retry. Aggressive permanent-classification of all 400s would
        misfire on provider-specific edge cases."""
        err = _make_sdk_error("APIStatusError", 400)
        assert _classify_llm_error(err) == "transient"

    def test_no_status_no_class_no_keyword_is_transient(self):
        """Catch-all: unknown exception → retry as today's behaviour."""
        assert _classify_llm_error(RuntimeError("something weird")) == "transient"
