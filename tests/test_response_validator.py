"""Tests for LLM response degeneration validator.

Tests each detection pattern and the overall validate_response function.
"""

from src.core.response_validator import (
    ValidationResult,
    validate_response,
    _detect_tag_repetition,
    _detect_token_repetition,
    _detect_line_repetition,
    _detect_special_token_leakage,
    _detect_foreign_tool_syntax,
    _detect_unclosed_code_blocks,
    _detect_excessive_length,
)


# =============================================================================
# _detect_tag_repetition
# =============================================================================


class TestTagRepetition:
    """Tests for closing tag repetition detection."""

    def test_no_tags(self):
        assert _detect_tag_repetition("Hello world, no tags here") is None

    def test_normal_tags(self):
        content = "</invoke> " * 5
        assert _detect_tag_repetition(content) is None

    def test_excessive_tags(self):
        content = "</invoke>" * 60
        result = _detect_tag_repetition(content)
        assert result is not None
        assert "</invoke>" in result
        assert "60" in result

    def test_custom_threshold(self):
        content = "</tool_call>" * 8
        assert _detect_tag_repetition(content, max_tag_repetitions=5) is not None
        assert _detect_tag_repetition(content, max_tag_repetitions=10) is None

    def test_mixed_tags_only_flags_excessive(self):
        content = "</invoke>" * 60 + "</result>" * 3
        result = _detect_tag_repetition(content)
        assert result is not None
        assert "</invoke>" in result

    def test_namespaced_tags(self):
        content = "</ns:element>" * 60
        result = _detect_tag_repetition(content)
        assert result is not None


# =============================================================================
# _detect_token_repetition
# =============================================================================


class TestTokenRepetition:
    """Tests for consecutive token repetition detection."""

    def test_normal_text(self):
        assert (
            _detect_token_repetition("This is a normal sentence with varied words.")
            is None
        )

    def test_short_content(self):
        assert _detect_token_repetition("word word word") is None

    def test_excessive_repetition(self):
        content = " ".join(["the"] * 25)
        result = _detect_token_repetition(content)
        assert result is not None
        assert "the" in result
        assert "25" in result

    def test_custom_threshold(self):
        content = " ".join(["ok"] * 12)
        assert _detect_token_repetition(content, max_token_repetitions=10) is not None
        assert _detect_token_repetition(content, max_token_repetitions=15) is None

    def test_non_consecutive_not_flagged(self):
        """Same word many times but not consecutively should pass."""
        words = []
        for i in range(30):
            words.append("repeat")
            words.append(f"word{i}")
        content = " ".join(words)
        assert _detect_token_repetition(content) is None

    def test_long_token_preview_truncated(self):
        long_token = "a" * 100
        content = " ".join([long_token] * 25)
        result = _detect_token_repetition(content)
        assert result is not None
        assert "..." in result


# =============================================================================
# _detect_line_repetition
# =============================================================================


class TestLineRepetition:
    """Tests for line repetition detection."""

    def test_normal_content(self):
        content = "Line 1\nLine 2\nLine 3\nLine 4"
        assert _detect_line_repetition(content) is None

    def test_excessive_repetition(self):
        content = "\n".join(["This is a repeated line."] * 15)
        result = _detect_line_repetition(content)
        assert result is not None
        assert "15" in result

    def test_blank_lines_ignored(self):
        content = "\n" * 50
        assert _detect_line_repetition(content) is None

    def test_custom_threshold(self):
        content = "\n".join(["Same line"] * 6)
        assert _detect_line_repetition(content, max_line_repetitions=5) is not None
        assert _detect_line_repetition(content, max_line_repetitions=10) is None

    def test_whitespace_stripped(self):
        content = "\n".join(["  Same line  "] * 15)
        result = _detect_line_repetition(content)
        assert result is not None

    def test_long_line_preview_truncated(self):
        long_line = "x" * 100
        content = "\n".join([long_line] * 15)
        result = _detect_line_repetition(content)
        assert result is not None
        assert "..." in result


# =============================================================================
# _detect_special_token_leakage
# =============================================================================


class TestSpecialTokenLeakage:
    """Tests for special/control token leakage detection."""

    def test_no_special_tokens(self):
        assert (
            _detect_special_token_leakage("Normal text with no special tokens.") is None
        )

    def test_few_special_tokens_ok(self):
        content = "Some text <|endoftext|> and </s> more text"
        assert _detect_special_token_leakage(content) is None

    def test_excessive_special_tokens(self):
        content = "text " + "<|endoftext|> " * 5
        result = _detect_special_token_leakage(content)
        assert result is not None
        assert "endoftext" in result

    def test_mixed_special_tokens(self):
        content = "<|im_start|> " * 2 + "[INST] " * 2
        result = _detect_special_token_leakage(content)
        assert result is not None

    def test_inst_tokens(self):
        content = "[INST] do this [/INST] ok [INST] now [/INST] done"
        result = _detect_special_token_leakage(content)
        assert result is not None


# =============================================================================
# _detect_foreign_tool_syntax
# =============================================================================


class TestForeignToolSyntax:
    """Tests for foreign/namespaced XML tool-call detection."""

    def test_normal_content(self):
        assert _detect_foreign_tool_syntax("Regular text here") is None

    def test_normal_xml(self):
        assert _detect_foreign_tool_syntax("<tag>content</tag>") is None

    def test_minimax_tool_call(self):
        content = 'Some text <minimax:tool_call name="search">'
        result = _detect_foreign_tool_syntax(content)
        assert result is not None
        assert "minimax" in result

    def test_anthropic_tool_call(self):
        content = '<anthropic:invoke name="tool">'
        result = _detect_foreign_tool_syntax(content)
        assert result is not None

    def test_google_tool_call(self):
        content = '<google:function_call name="search">'
        result = _detect_foreign_tool_syntax(content)
        assert result is not None

    def test_multiple_providers(self):
        content = "<minimax:call> <anthropic:invoke>"
        result = _detect_foreign_tool_syntax(content)
        assert result is not None


# =============================================================================
# _detect_unclosed_code_blocks
# =============================================================================


class TestUnclosedCodeBlocks:
    """Tests for unclosed code fence detection."""

    def test_short_content_ignored(self):
        content = "```python\ncode\n"
        assert _detect_unclosed_code_blocks(content) is None

    def test_balanced_fences(self):
        content = "x" * 5000 + "\n```python\ncode here\n```\n"
        assert _detect_unclosed_code_blocks(content) is None

    def test_unclosed_fence_long_content(self):
        content = "x" * 5000 + "\n```python\ncode here\n"
        result = _detect_unclosed_code_blocks(content)
        assert result is not None
        assert "Unclosed" in result

    def test_even_fence_count(self):
        content = "x" * 5000 + "\n```\nblock1\n```\n```\nblock2\n```\n"
        assert _detect_unclosed_code_blocks(content) is None

    def test_triple_fence_odd(self):
        content = "x" * 5000 + "\n```\nblock1\n```\n```\nblock2\n"
        result = _detect_unclosed_code_blocks(content)
        assert result is not None


# =============================================================================
# _detect_excessive_length
# =============================================================================


class TestExcessiveLength:
    """Tests for excessive content length detection."""

    def test_normal_length(self):
        content = "x" * 1000
        assert _detect_excessive_length(content) is None

    def test_long_but_has_tool_calls(self):
        content = "x" * 60000
        assert (
            _detect_excessive_length(content, tool_calls=[{"name": "search"}]) is None
        )

    def test_excessive_no_tool_calls(self):
        content = "x" * 60000
        result = _detect_excessive_length(content)
        assert result is not None
        assert "60000" in result

    def test_custom_threshold(self):
        content = "x" * 10000
        assert _detect_excessive_length(content, max_content_length=5000) is not None
        assert _detect_excessive_length(content, max_content_length=20000) is None

    def test_empty_tool_calls(self):
        content = "x" * 60000
        assert _detect_excessive_length(content, tool_calls=[]) is not None

    def test_none_tool_calls(self):
        content = "x" * 60000
        assert _detect_excessive_length(content, tool_calls=None) is not None


# =============================================================================
# validate_response (integration)
# =============================================================================


class TestValidateResponse:
    """Integration tests for the main validation function."""

    def test_clean_response(self):
        result = validate_response("This is a normal, clean LLM response.")
        assert not result.is_degenerate
        assert not result.has_warnings
        assert len(result.matched_patterns) == 0
        assert result.truncated_content is None

    def test_empty_content(self):
        result = validate_response("")
        assert not result.is_degenerate
        assert not result.has_warnings

    def test_none_like_empty(self):
        result = validate_response("")
        assert isinstance(result, ValidationResult)

    def test_critical_pattern_sets_degenerate(self):
        content = "</invoke>" * 60
        result = validate_response(content)
        assert result.is_degenerate
        assert any(p.severity == "critical" for p in result.matched_patterns)

    def test_warning_pattern_sets_warnings(self):
        content = "x" * 60000
        result = validate_response(content)
        assert not result.is_degenerate
        assert result.has_warnings
        assert any(p.name == "excessive_length" for p in result.matched_patterns)

    def test_tool_calls_suppress_length_warning(self):
        content = "x" * 60000
        result = validate_response(content, tool_calls=[{"name": "write_file"}])
        assert not result.has_warnings or not any(
            p.name == "excessive_length" for p in result.matched_patterns
        )

    def test_multiple_patterns_detected(self):
        # Content with both tag repetition AND token repetition
        content = "</invoke>" * 60 + " " + " ".join(["spam"] * 30)
        result = validate_response(content)
        assert result.is_degenerate
        assert len(result.matched_patterns) >= 2

    def test_truncated_content_on_critical(self):
        content = "start " + "</invoke>" * 100 + " end"
        result = validate_response(content)
        assert result.is_degenerate
        assert result.truncated_content is not None
        assert "truncated" in result.truncated_content

    def test_no_truncation_for_short_degenerate(self):
        # Short but degenerate (foreign tool syntax)
        content = '<minimax:tool_call name="search">'
        result = validate_response(content)
        assert result.is_degenerate
        # Short content should not be truncated
        assert result.truncated_content == content

    def test_custom_thresholds(self):
        content = "</invoke>" * 10
        # Default threshold (50) should pass
        result = validate_response(content)
        assert not result.is_degenerate

        # Custom lower threshold should catch it
        result = validate_response(content, max_tag_repetitions=5)
        assert result.is_degenerate

    def test_real_world_minimax_degeneration(self):
        """Simulate the actual Minimax degeneration case that motivated this feature."""
        content = (
            "I'll search for the document.\n\n"
            '<tool_call>\n<invoke name="search_files">\n'
            '<parameter name="query">requirements</parameter>\n'
            "</invoke>\n" + "</invoke>\n" * 500
        )
        result = validate_response(content)
        assert result.is_degenerate
        assert any(p.name == "tag_repetition_loop" for p in result.matched_patterns)
