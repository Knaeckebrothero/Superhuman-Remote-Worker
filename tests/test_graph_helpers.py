"""Tests for graph.py error handling helper functions.

Tests _extract_rate_limit_delay, _extract_tool_use_failed,
_build_tool_use_failed_feedback, _is_tool_error, _extract_markdown_content,
_check_empty_response_streak, _check_no_tool_call_streak.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.graph import (
    _classify_llm_error,
    _cooldown_reset_seconds,
    _cooldown_detail,
    _cooldown_failfast_error,
    _cooldown_within_pause_budget,
    _COOLDOWN_MAX_PAUSE_SECONDS,
    _extract_rate_limit_delay,
    _infra_edge_status,
    _summarize_llm_error,
    _extract_tool_use_failed,
    _build_tool_use_failed_feedback,
    _is_tool_error,
    _extract_markdown_content,
    _check_empty_response_streak,
    _check_no_tool_call_streak,
    _is_output_truncated,
    initial_error_freeze_fields,
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

    # --- markup-aware hardening (job 2dacba6f: varying leaked payloads) ---

    def test_leaked_markup_varying_payloads_accumulate_and_fail(self):
        """Different leaked tool-call blocks each turn (git_log, git_tags,
        todo_complete…) must accumulate when flagged as leaked markup, even
        though their hashes differ — the 24,127-iteration regression that a
        pure hash-match could never catch."""
        payloads = [
            "<|tool_call>call:git_log(max_count=15)<tool_call|>",
            "<|tool_call>call:git_tags()<tool_call|>",
            '<|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>',
            '<|tool_call>call:read_file{path:<|"|>plan.md<|"|>}<tool_call|>',
        ]
        s, h = 0, ""
        for i, payload in enumerate(payloads):
            s, fail, h = _check_no_tool_call_streak(
                payload, 0, s, h, is_leaked_markup=True
            )
            if i < 3:
                assert fail is False
                assert s == i + 1
            else:
                assert fail is True
                assert s == 4

    def test_leaked_markup_first_response_does_not_fail(self):
        """First leaked-markup response starts the streak at 1, no fail."""
        s, fail, h = _check_no_tool_call_streak(
            self.LEAKED, 0, 0, "", is_leaked_markup=True
        )
        assert s == 1
        assert fail is False
        assert h != ""

    def test_varying_content_without_markup_flag_still_resets(self):
        """Backward-compat: with is_leaked_markup unset (default False),
        differing content still resets to 1 — unchanged legacy behavior."""
        a, fail_a, ha = _check_no_tool_call_streak("response A", 0, 5, "oldhash")
        assert a == 1
        assert fail_a is False
        b, fail_b, hb = _check_no_tool_call_streak("response B", 0, a, ha)
        assert b == 1
        assert fail_b is False
        assert hb != ha

    def test_tool_call_resets_even_with_markup_flag(self):
        """A real tool call resets the streak regardless of the markup flag."""
        s, fail, h = _check_no_tool_call_streak(
            self.LEAKED, 1, 3, "prev", is_leaked_markup=True
        )
        assert s == 0
        assert fail is False
        assert h == ""


# =============================================================================
# _classify_llm_error
# =============================================================================


def _make_sdk_error(class_name: str, status_code, *, body=None, message="", url=None):
    """Build a duck-typed SDK error.

    The classifier inspects ``type(exc).__name__`` and ``exc.status_code`` /
    ``exc.body`` rather than isinstance-checking the openai/anthropic SDK
    types, so we don't need to import them here. ``url`` populates a duck-typed
    ``.request.url`` the way ``openai.APIStatusError`` does, so URL-based
    routing checks (codex-proxy detection) can be exercised.
    """
    cls = type(class_name, (Exception,), {})
    err = cls(message)
    err.status_code = status_code
    if body is not None:
        err.body = body
    if url is not None:
        err.request = SimpleNamespace(url=url)
    return err


# The verbatim body nginx served during the 2026-07-17 MiniMax edge outage —
# the openai SDK leaves a non-JSON body as this raw string on ``exc.body`` and
# stringifies the exception to it verbatim.
NGINX_404 = (
    "<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n"
    "<center><h1>404 Not Found</h1></center>\r\n<hr><center>nginx</center>\r\n"
    "</body>\r\n</html>"
)


class TestInfraEdgeHelpers:
    """_infra_edge_status / _summarize_llm_error — edge-shaped failures
    (non-API body from a gateway/proxy in front of the provider)."""

    def test_edge_status_for_html_body(self):
        err = _make_sdk_error("NotFoundError", 404, body=NGINX_404, message=NGINX_404)
        assert _infra_edge_status(err) == 404

    def test_no_edge_status_for_api_error_body(self):
        err = _make_sdk_error("NotFoundError", 404, body={"error": {"message": "x"}})
        assert _infra_edge_status(err) is None

    def test_no_edge_status_without_status_code(self):
        assert _infra_edge_status(Exception("Connection refused")) is None

    def test_edge_status_walks_cause_chain(self):
        inner = _make_sdk_error("NotFoundError", 404, body=NGINX_404, message=NGINX_404)
        outer = Exception("wrapped by langchain")
        outer.__cause__ = inner
        assert _infra_edge_status(outer) == 404

    def test_summarize_composes_readable_message(self):
        err = _make_sdk_error("NotFoundError", 404, body=NGINX_404, message=NGINX_404)
        msg = _summarize_llm_error(err, "MiniMax-M3")
        assert "HTTP 404" in msg
        assert "MiniMax-M3" in msg
        assert "provider edge" in msg
        assert "<html>" not in msg
        assert "\r" not in msg

    def test_summarize_without_model_omits_model_clause(self):
        err = _make_sdk_error("NotFoundError", 404, body=NGINX_404, message=NGINX_404)
        assert "model" not in _summarize_llm_error(err).split("Detail:")[0]

    def test_summarize_passthrough_for_non_edge_errors(self):
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={"error": {"type": "invalid_request_error", "message": "bad"}},
            message="Error code: 400 - bad",
        )
        assert _summarize_llm_error(err, "some-model") == "Error code: 400 - bad"


class TestClassifyLlmError:
    """Tests for the permanent/rate_limit/transient classifier that gates
    the inner retry loop in create_execute_node. Regression coverage for
    the 2026-05-12 cluster outage where a 404 model-not-found looped 70+
    iterations against a guaranteed-failure endpoint."""

    def test_404_model_not_found_is_permanent(self):
        err = _make_sdk_error(
            "NotFoundError",
            404,
            body={
                "error": {
                    "message": "Model 'x' not found",
                    "type": "invalid_request_error",
                }
            },
        )
        assert _classify_llm_error(err) == "permanent"

    def test_404_html_edge_body_is_transient(self):
        """An nginx/LB default page means the request never reached the API
        application — an infra outage, not model-not-found. Regression for
        the 2026-07-17 MiniMax edge outage that hard-failed two jobs on
        attempt 1."""
        err = _make_sdk_error("NotFoundError", 404, body=NGINX_404, message=NGINX_404)
        assert _classify_llm_error(err) == "transient"

    def test_404_missing_body_is_transient(self):
        """A 404 whose body was never read (closed stream) is ambiguous —
        bias for retry; the outage ceilings bound a wrong guess."""
        err = _make_sdk_error("NotFoundError", 404, message="Error code: 404")
        assert _classify_llm_error(err) == "transient"

    def test_notfound_class_fallback_html_body_is_transient(self):
        """A wrapped exception that lost its status_code takes the class-name
        fallback — it must apply the same body-shape gate."""
        err = _make_sdk_error("NotFoundError", None, body=NGINX_404, message=NGINX_404)
        assert _classify_llm_error(err) == "transient"

    def test_notfound_class_fallback_dict_body_stays_permanent(self):
        err = _make_sdk_error(
            "NotFoundError",
            None,
            body={
                "error": {
                    "message": "Model 'x' not found",
                    "type": "invalid_request_error",
                }
            },
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
            body={
                "error": {"type": "invalid_request_error", "code": "tool_use_failed"}
            },
        )
        assert _classify_llm_error(err) == "transient"

    def test_400_rate_limit_disguised_is_rate_limit(self):
        """Some providers return rate-limit info under a 400 with a
        rate-related code — must NOT be permanent."""
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={
                "error": {
                    "type": "invalid_request_error",
                    "code": "rate_limit_exceeded",
                }
            },
        )
        assert _classify_llm_error(err) == "rate_limit"

    def test_400_minimax_bad_request_error_is_permanent(self):
        """MiniMax says type='bad_request_error' where OpenAI-compatible
        providers say 'invalid_request_error'. The 2026-07-11 wedge: a
        deterministic "invalid function arguments json string" 400 was
        classified transient and pause/backoff-looped forever — see
        knowledge-history/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md
        (Finding 3)."""
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={
                "error": {
                    "type": "bad_request_error",
                    "message": (
                        "invalid params, invalid function arguments json "
                        "string, tool_call_id: call_E7U6VHuNDwmxi6Hl8jkjkrG8"
                    ),
                }
            },
        )
        assert _classify_llm_error(err) == "permanent"

    def test_400_bad_request_rate_disguised_stays_rate_limit(self):
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={
                "error": {
                    "type": "bad_request_error",
                    "code": "rate_limit_exceeded",
                }
            },
        )
        assert _classify_llm_error(err) == "rate_limit"

    def test_stringified_bad_request_error_is_permanent(self):
        """Stringified provider errors that lost their exception class
        (observed in production audit logs) must still fail fast via the
        text fallback."""
        err = Exception(
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'bad_request_error', 'message': 'invalid params, invalid "
            "function arguments json string', 'http_code': '400'}}"
        )
        assert _classify_llm_error(err) == "permanent"

    def test_stringified_tool_use_failed_stays_transient(self):
        """The text fallback must not swallow Groq's recoverable
        tool_use_failed into 'permanent'."""
        err = Exception(
            "Error code: 400 - {'error': {'type': 'invalid_request_error', "
            "'code': 'tool_use_failed'}}"
        )
        assert _classify_llm_error(err) == "transient"

    def test_408_stream_disconnect_is_transient(self):
        """A 408 whose body mislabels a dropped response stream as
        type=invalid_request_error is a *transient* transport failure, NOT a
        deterministic input rejection. The 2026-07-14 incident: scholar subjob
        35b23256 lost 3.5h of finished research when this exact 408 was
        classified 'permanent' and hard-failed on the first attempt (0 retries).
        """
        err = _make_sdk_error(
            "APIStatusError",
            408,
            body={
                "error": {
                    "message": (
                        "stream error: stream disconnected before completion: "
                        "stream closed before response.completed"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )
        assert _classify_llm_error(err) == "transient"

    def test_stringified_408_stream_disconnect_is_transient(self):
        """The production audit shape: a stringified 408 stream-disconnect that
        lost its exception class must still reach the text fallback as
        transient, not be swallowed 'permanent' by the invalid_request_error
        heuristic."""
        err = Exception(
            "Error code: 408 - {'error': {'message': 'stream error: stream "
            "disconnected before completion: stream closed before "
            "response.completed', 'type': 'invalid_request_error'}}"
        )
        assert _classify_llm_error(err) == "transient"

    def test_stringified_novel_status_with_rejection_label_is_transient(self):
        """The generalisation that stops this being whack-a-mole: a status we
        have never seen before (499) carrying an invalid_request_error *label*
        and NO stream wording must still be transient. The stringified rule is
        written for 400s and must not claim anything else — so a future
        transport status costs no new marker and no dead job."""
        err = Exception(
            "Error code: 499 - {'error': {'type': 'invalid_request_error', "
            "'message': 'client closed request'}}"
        )
        assert _classify_llm_error(err) == "transient"

    def test_stringified_422_stays_permanent(self):
        """422 Unprocessable Entity IS a deterministic input rejection, so it
        stays permanent — the gate keys on input-rejection statuses, not on a
        blanket 'retry everything that isn't 400'."""
        err = Exception(
            "Error code: 422 - {'error': {'type': 'invalid_request_error', "
            "'message': 'schema validation failed'}}"
        )
        assert _classify_llm_error(err) == "permanent"

    def test_400_stream_disconnect_is_transient(self):
        """Defense-in-depth: a dropped stream surfaced as a 400 (rather than
        408) invalid_request_error is still transport, not input — retry it."""
        err = _make_sdk_error(
            "BadRequestError",
            400,
            body={
                "error": {
                    "type": "invalid_request_error",
                    "message": "stream closed before response.completed",
                }
            },
        )
        assert _classify_llm_error(err) == "transient"

    def test_429_is_rate_limit(self):
        err = _make_sdk_error("RateLimitError", 429)
        assert _classify_llm_error(err) == "rate_limit"

    def test_429_model_cooldown_is_cooldown(self):
        """The gpt-5.3-codex-spark incident: a 429 whose body carries a
        model_cooldown code (all credentials cooling down) classifies as
        'cooldown' so the caller fails fast instead of retry-looping."""
        err = _make_sdk_error(
            "RateLimitError",
            429,
            body={
                "error": {
                    "code": "model_cooldown",
                    "message": "All credentials for model X are cooling down",
                    "model": "gpt-5.3-codex-spark",
                    "reset_seconds": 482000,
                }
            },
        )
        assert _classify_llm_error(err) == "cooldown"

    def test_429_long_reset_without_code_is_cooldown(self):
        """A long reset window (no explicit code) is still a cooldown."""
        err = _make_sdk_error(
            "RateLimitError", 429, body={"error": {"reset_seconds": 3600}}
        )
        assert _classify_llm_error(err) == "cooldown"

    def test_429_short_reset_is_plain_rate_limit(self):
        """A short reset (per-minute throttle) stays a retriable rate limit."""
        err = _make_sdk_error(
            "RateLimitError", 429, body={"error": {"reset_seconds": 30}}
        )
        assert _classify_llm_error(err) == "rate_limit"

    def test_model_cooldown_string_fallback_is_cooldown(self):
        """The stringified provider error (no SDK class/body) still classifies
        as cooldown via the message fallback — matches the incident audit log."""
        err = Exception(
            "Error code: 429 - {'error': {'code': 'model_cooldown', "
            "'reset_seconds': 482593}}"
        )
        assert _classify_llm_error(err) == "cooldown"

    def test_429_insufficient_quota_is_quota_exhausted(self):
        """OpenAI's insufficient_quota billing wall (a 429) must fail fast, not
        pause for hours on the outage backoff path — no wait fixes a spend cap.
        See knowledge-base/knowledge/features/llm_outage_pause_and_backoff_redispatch.md."""
        err = _make_sdk_error(
            "RateLimitError",
            429,
            body={
                "error": {
                    "code": "insufficient_quota",
                    "type": "insufficient_quota",
                    "message": "You exceeded your current quota, please check "
                    "your plan and billing details.",
                }
            },
        )
        assert _classify_llm_error(err) == "quota_exhausted"

    def test_insufficient_quota_precedence_over_rate_limit(self):
        """A 429 carrying insufficient_quota is quota_exhausted, not the
        retriable rate_limit a bare 429 would otherwise be."""
        err = _make_sdk_error(
            "RateLimitError", 429, body={"error": {"type": "insufficient_quota"}}
        )
        assert _classify_llm_error(err) == "quota_exhausted"

    def test_insufficient_quota_string_fallback(self):
        """Stringified provider error (no SDK class/body) still detected."""
        err = Exception(
            "Error code: 429 - {'error': {'type': 'insufficient_quota', "
            "'message': 'You exceeded your current quota'}}"
        )
        assert _classify_llm_error(err) == "quota_exhausted"

    def test_google_resource_exhausted_stays_rate_limit(self):
        """Google's RESOURCE_EXHAUSTED doubles as a per-minute rate-limit signal,
        so it must NOT be treated as a billing wall — that would wrongly fail-fast
        a recoverable rate limit. Stays rate_limit."""
        err = _make_sdk_error(
            "RateLimitError",
            429,
            body={
                "error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}
            },
        )
        assert _classify_llm_error(err) == "rate_limit"

    def test_cooldown_reset_seconds_helper(self):
        err = _make_sdk_error(
            "RateLimitError",
            429,
            body={"error": {"code": "model_cooldown", "reset_seconds": 482000}},
        )
        assert _cooldown_reset_seconds(err) == 482000.0
        # A plain 429 with no cooldown body is not a cooldown.
        assert _cooldown_reset_seconds(_make_sdk_error("RateLimitError", 429)) is None

    def test_cooldown_detail_extracts_model_and_reset(self):
        err = _make_sdk_error(
            "RateLimitError",
            429,
            body={
                "error": {
                    "code": "model_cooldown",
                    "model": "gpt-5.3-codex-spark",
                    "reset_seconds": 482000,
                }
            },
        )
        reset, model = _cooldown_detail(err)
        assert reset == 482000.0
        assert model == "gpt-5.3-codex-spark"

    def test_503_is_transient(self):
        err = _make_sdk_error("APIStatusError", 503)
        assert _classify_llm_error(err) == "transient"

    def test_connection_error_is_transient(self):
        """No status_code, no recognizable class — default to transient
        so we don't aggressively fail jobs on truly transient network
        flakes."""
        assert _classify_llm_error(ConnectionError("connect refused")) == "transient"

    def test_walks_cause_chain(self):
        """LangChain wraps provider exceptions — classifier must unwrap.

        The inner 404 carries a JSON error body (a bodyless 404 is now an
        edge-shaped failure and deliberately transient)."""
        inner = _make_sdk_error(
            "NotFoundError",
            404,
            body={"error": {"message": "Model 'x' not found"}},
        )
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

    # --- Codex/OAuth-proxy token-unavailable: retryable, NOT permanent -----
    # A ChatGPT/Codex OAuth token (via CLIProxyAPI) can be invalidated or stuck
    # mid-refresh and return a 401 — recoverable by a proxy re-auth/refresh, so
    # it must retry (bounded) rather than hard-fail the job like a bad API key.

    def test_401_codex_auth_unavailable_code_is_retryable(self):
        """401 with code=auth_unavailable → distinct retryable class."""
        err = _make_sdk_error(
            "AuthenticationError",
            401,
            body={
                "error": {
                    "message": "Encountered invalidated oauth token for user, failing request",
                    "type": "authentication_error",
                    "code": "auth_unavailable",
                }
            },
        )
        assert _classify_llm_error(err) == "auth_unavailable"

    def test_401_invalidated_oauth_message_is_retryable(self):
        """Detected via the message text even without the code field."""
        err = _make_sdk_error(
            "AuthenticationError",
            401,
            body={"error": {"message": "invalidated oauth token for user"}},
        )
        assert _classify_llm_error(err) == "auth_unavailable"

    def test_401_genuine_bad_key_stays_permanent(self):
        """A real bad-key 401 (no auth_unavailable marker) must still fail
        fast — we did NOT make all 401s retryable."""
        err = _make_sdk_error(
            "AuthenticationError",
            401,
            body={
                "error": {
                    "message": "Incorrect API key provided",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
        assert _classify_llm_error(err) == "permanent"

    def test_401_bare_auth_error_stays_permanent(self):
        """401 with no body/markers → permanent (unchanged behaviour)."""
        err = _make_sdk_error("AuthenticationError", 401)
        assert _classify_llm_error(err) == "permanent"

    def test_auth_unavailable_message_fallback(self):
        """Stringified error (status_code/class stripped) still detected —
        matches the shape that lands in audit logs."""
        err = Exception(
            "Error code: 401 - {'error': {'message': 'Encountered invalidated "
            "oauth token for user, failing request', 'code': 'auth_unavailable'}}"
        )
        assert _classify_llm_error(err) == "auth_unavailable"

    def test_auth_unavailable_walks_cause_chain(self):
        """LangChain wraps the provider error — classifier must unwrap it."""
        inner = _make_sdk_error(
            "AuthenticationError",
            401,
            body={"error": {"code": "auth_unavailable"}},
        )
        outer = Exception("LangChain wrapper")
        outer.__cause__ = inner
        assert _classify_llm_error(outer) == "auth_unavailable"

    # --- Codex proxy GENERIC 401 (no marker) — transient blip, retryable ----
    # The 2026-06-22 "Research 01" incident: CLIProxyAPI returned a *generic*
    # 401 ("Invalid, disabled, or expired API key" / authentication_error) with
    # NO auth_unavailable marker during a token-refresh window. The same model
    # answered in a session ~50s later. The autonomous job must retry+resume,
    # not hard-fail. Detection is host-scoped to the codex proxy so a real
    # api.openai.com bad-key 401 still fails fast.

    def test_401_codex_proxy_generic_error_is_retryable(self):
        """Generic 401 (no marker) routed through the in-cluster codex proxy
        → auth_unavailable, identified by the 'codex' host."""
        err = _make_sdk_error(
            "AuthenticationError",
            401,
            body={
                "error": {
                    "message": "Invalid, disabled, or expired API key",
                    "type": "authentication_error",
                }
            },
            url="http://srw-codex-proxy:8317/v1/responses",
        )
        assert _classify_llm_error(err) == "auth_unavailable"

    def test_401_codex_proxy_localhost_default_port_is_retryable(self):
        """Local-dev codex proxy (CLIProxyAPI default localhost:8317): the host
        has no 'codex' substring, so the :8317 port marker carries it."""
        err = _make_sdk_error(
            "AuthenticationError",
            401,
            body={"error": {"message": "Invalid, disabled, or expired API key"}},
            url="http://localhost:8317/v1/chat/completions",
        )
        assert _classify_llm_error(err) == "auth_unavailable"

    def test_401_real_openai_host_stays_permanent(self):
        """A generic 401 from the REAL api.openai.com (a genuinely bad key)
        must still fail fast — the codex-proxy widening is host-scoped."""
        err = _make_sdk_error(
            "AuthenticationError",
            401,
            body={
                "error": {
                    "message": "Invalid, disabled, or expired API key",
                    "type": "authentication_error",
                }
            },
            url="https://api.openai.com/v1/chat/completions",
        )
        assert _classify_llm_error(err) == "permanent"

    def test_401_codex_proxy_error_walks_cause_chain(self):
        """LangChain wraps the provider error — URL-based codex detection must
        unwrap it like the marker-based path does."""
        inner = _make_sdk_error(
            "AuthenticationError",
            401,
            body={"error": {"message": "Invalid, disabled, or expired API key"}},
            url="http://srw-codex-proxy:8317/v1/responses",
        )
        outer = Exception("LangChain wrapper")
        outer.__cause__ = inner
        assert _classify_llm_error(outer) == "auth_unavailable"


# =============================================================================
# _is_output_truncated  (reasoning-aware output caps, §6/§7.1)
# =============================================================================


class TestIsOutputTruncated:
    """The shared finish_reason=length predicate both graphs use to tell an
    output-cap truncation apart from a generic empty response."""

    def test_plain_length(self):
        assert _is_output_truncated("length") is True

    def test_doubled_lengthlength_from_stream_merge(self):
        # OpenRouter-direct concatenates finish_reason across two chunks (§7.1).
        assert _is_output_truncated("lengthlength") is True

    def test_provider_spelling_variants(self):
        assert _is_output_truncated("MAX_TOKENS") is True
        assert _is_output_truncated("max_tokens") is True
        assert _is_output_truncated("max_output_tokens") is True

    def test_stop_is_not_truncation(self):
        # The codex-proxy empty-stop bug is finish_reason=stop — must NOT be
        # treated as a length truncation (it has its own retry path).
        assert _is_output_truncated("stop") is False

    def test_tool_calls_is_not_truncation(self):
        assert _is_output_truncated("tool_calls") is False

    def test_none_and_empty(self):
        assert _is_output_truncated(None) is False
        assert _is_output_truncated("") is False

    def test_non_string_is_safe(self):
        # Defensive: a non-string finish_reason must not raise.
        assert _is_output_truncated(0) is False
        assert _is_output_truncated(["length"]) is True  # str(list) contains it


# =============================================================================
# _cooldown_failfast_error — structured payload for the cooldown fail-fast
# (knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md)
# =============================================================================


class TestCooldownFailfastError:
    def test_carries_structured_fields(self):
        import time as _time

        before = _time.time()
        err = _cooldown_failfast_error("msg", "gpt-5.3-codex-spark", 585034.0)
        after = _time.time()
        assert err["message"] == "msg"
        assert err["type"] == "llm_error"
        assert err["recoverable"] is False
        assert err["classification"] == "cooldown"
        assert err["model"] == "gpt-5.3-codex-spark"
        assert before + 585034.0 <= err["reset_at"] <= after + 585034.0

    def test_unknown_reset_gives_none_reset_at(self):
        err = _cooldown_failfast_error("msg", "m", None)
        assert err["reset_at"] is None
        assert err["classification"] == "cooldown"


# =============================================================================
# _cooldown_within_pause_budget — the pause-vs-fail-fast cutoff for a cooldown
# (knowledge-base/knowledge/features/llm_cooldown_pause_and_resume.md)
# =============================================================================


class TestCooldownWithinPauseBudget:
    def test_none_reset_fails_fast(self):
        # An unknown reset window can't be safely bounded → fail fast, don't pause.
        assert _cooldown_within_pause_budget(None) is False

    def test_short_codex_cooldown_pauses(self):
        # The 2026-07-07 incident: a ~2.1h gpt-5.5 window is well within budget.
        assert _cooldown_within_pause_budget(2.1 * 3600) is True

    def test_five_hour_openai_window_pauses(self):
        # OpenAI's ~5h usage window still pauses out rather than failing.
        assert _cooldown_within_pause_budget(5 * 3600) is True

    def test_at_budget_boundary_pauses(self):
        # Exactly at the ceiling still pauses (<=).
        assert _cooldown_within_pause_budget(_COOLDOWN_MAX_PAUSE_SECONDS) is True

    def test_multiday_wall_fails_fast(self):
        # The original 5.5-day cooldown must still fail fast — not park for 12h.
        assert _cooldown_within_pause_budget(5.5 * 86400) is False

    def test_budget_default_is_12h(self):
        # Fused with the orchestrator's LLM_OUTAGE_CEILING_SECONDS (2026-07-15).
        assert _COOLDOWN_MAX_PAUSE_SECONDS == 43_200


class TestInitialErrorFreezeFields:
    """The FIRST error of a retry ladder is often the cause; the last, a symptom.

    Incident 2026-07-29 (job d251e513): a 408 upstream stream drop flipped the
    Codex proxy's sole auth entry to `status: error`, so retries 2-6 returned
    `503 auth_unavailable` and overwrote the only useful evidence. The freeze
    named a phantom auth failure and sent operators to re-auth a healthy token.
    """

    HEAD = "Error code: 408 - stream disconnected before completion"
    TAIL = "Error code: 503 - auth_unavailable: no auth available"

    def test_differing_head_is_carried(self):
        out = initial_error_freeze_fields(
            self.HEAD, "transient", self.TAIL, "auth_unavailable"
        )
        assert out["initial_error_summary"] == self.HEAD
        assert out["initial_classification"] == "transient"

    def test_identical_head_and_tail_adds_nothing(self):
        # The common single-cause case must not duplicate error_summary.
        assert (
            initial_error_freeze_fields(self.TAIL, "transient", self.TAIL, "transient")
            == {}
        )

    def test_same_text_different_classification_is_carried(self):
        out = initial_error_freeze_fields(
            self.TAIL, "transient", self.TAIL, "rate_limit"
        )
        assert out["initial_classification"] == "transient"

    def test_no_head_recorded_adds_nothing(self):
        assert initial_error_freeze_fields(None, None, self.TAIL, "transient") == {}

    def test_summary_is_truncated(self):
        out = initial_error_freeze_fields(
            "x" * 900, "transient", self.TAIL, "transient"
        )
        assert len(out["initial_error_summary"]) == 500
