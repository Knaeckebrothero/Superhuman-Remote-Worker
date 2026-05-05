"""Tests for the guardrails matrix: resolution + tool description injection.

Covers:
  - resolve_guardrails default-only / family-override / deep-merge
  - apply_guardrails_to_tools strip-and-inject behavior
  - _replace_examples_block edge cases (no block, trailing-newline, indent)
  - format_nudge placeholder validation
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest
from langchain_core.tools import tool

from src.core.loader import resolve_guardrails
from src.services.guardrails import (
    GuardrailFormatError,
    KNOWN_NUDGES,
    _replace_examples_block,
    apply_guardrails_to_tools,
    format_nudge,
)


# =============================================================================
# resolve_guardrails — backed by the real config files
# =============================================================================


class TestResolveGuardrails:
    def test_default_family_returns_default_yaml(self):
        g = resolve_guardrails("gpt-4o")
        assert "tool_examples" in g
        assert "nudges" in g
        # parens-form sentinel from default.yaml
        assert "git_log()" in g["tool_examples"]["git_log"]

    def test_gemma_overrides_tool_examples(self):
        g = resolve_guardrails("google/gemma-4-31b")
        # Gemma file replaces git_log with brace-form
        assert "<|tool_call>call:git_log{}<tool_call|>" in g["tool_examples"]["git_log"]
        # parens-form from default must be gone
        assert "git_log()" not in g["tool_examples"]["git_log"]

    def test_gemma_overrides_only_some_nudges(self):
        g = resolve_guardrails("google/gemma-4-31b")
        # todo_action is overridden — mentions Gemma format
        assert "Gemma" in g["nudges"]["todo_action"]
        # loop_warning_suffix is NOT overridden — comes from default
        assert "[LOOP WARNING]" in g["nudges"]["loop_warning_suffix"]

    def test_unknown_family_falls_back_to_default(self):
        # An unrecognized model resolves to family "default"
        g = resolve_guardrails("some-unknown-model-xyz")
        assert "git_log()" in g["tool_examples"]["git_log"]

    def test_gpt_oss_inherits_most_from_default(self):
        g = resolve_guardrails("openai/gpt-oss-120b")
        # gpt_oss.yaml only overrides next_phase_todos
        assert (
            "harmony" in g["tool_examples"]["next_phase_todos"].lower()
            or "commentary" in g["tool_examples"]["next_phase_todos"].lower()
        )
        # Other tools come from default
        assert "git_log()" in g["tool_examples"]["git_log"]


# =============================================================================
# _replace_examples_block — pure-function tests
# =============================================================================


class TestReplaceExamplesBlock:
    REPLACEMENT = "Examples:\n    new_call(x=1)\n    new_call(x=2)"

    def test_strips_existing_block_with_trailing_newline(self):
        original = textwrap.dedent(
            """\
            Do a thing.

            Args:
                x: some arg

            Returns:
                str

            Example:
                old_call()
                old_call(x=1)
            """
        )
        out = _replace_examples_block(original, self.REPLACEMENT)
        assert "old_call" not in out
        assert "new_call(x=1)" in out
        assert "Returns:" in out

    def test_strips_block_at_end_without_trailing_newline(self):
        # Common LangChain @tool dedent shape
        original = (
            "Do a thing.\n"
            "\n"
            "Args:\n"
            "    x: arg\n"
            "\n"
            "Example:\n"
            "    old_call()\n"
            "    old_call(x=1)"
        )
        out = _replace_examples_block(original, self.REPLACEMENT)
        assert "old_call" not in out
        assert "new_call(x=1)" in out

    def test_appends_when_no_examples_block(self):
        original = "Do a thing.\n\nArgs:\n    x: arg\n\nReturns:\n    str"
        out = _replace_examples_block(original, self.REPLACEMENT)
        assert out.startswith("Do a thing.")
        assert out.rstrip().endswith("new_call(x=2)")

    def test_handles_examples_plural_heading(self):
        original = "Heading.\n\nExamples:\n    a()\n    b()\n"
        out = _replace_examples_block(original, self.REPLACEMENT)
        assert "a()" not in out
        assert "new_call" in out

    def test_handles_empty_description(self):
        out = _replace_examples_block("", self.REPLACEMENT)
        assert "new_call" in out


# =============================================================================
# apply_guardrails_to_tools — wiring against fake guardrails dicts
# =============================================================================


def _fake_guardrails(examples=None, nudges=None):
    return {
        "tool_examples": examples or {},
        "nudges": nudges or {},
    }


@tool
def _fake_git_log(max_count: int = 10) -> str:
    """View commit history.

    Args:
        max_count: max number

    Returns:
        history

    Example:
        git_log()
        git_log(max_count=5)
    """
    return ""


@tool
def _fake_unknown(x: int = 0) -> str:
    """Unknown tool with no guardrails entry.

    Args:
        x: arg

    Returns:
        str
    """
    return ""


class TestApplyGuardrailsToTools:
    def test_replaces_examples_for_known_tool(self):
        guardrails = _fake_guardrails(
            examples={"_fake_git_log": "Examples:\n    BRACE_FORM_PLACEHOLDER"}
        )
        out = apply_guardrails_to_tools([_fake_git_log], guardrails=guardrails)
        assert "BRACE_FORM_PLACEHOLDER" in out[0].description
        assert "git_log()" not in out[0].description

    def test_leaves_unknown_tool_untouched(self):
        guardrails = _fake_guardrails(examples={"_fake_git_log": "X"})
        out = apply_guardrails_to_tools([_fake_unknown], guardrails=guardrails)
        # Same description (object equality not required, but content is)
        assert out[0].description == _fake_unknown.description

    def test_does_not_mutate_source_tool(self):
        original_description = _fake_git_log.description
        guardrails = _fake_guardrails(
            examples={"_fake_git_log": "Examples:\n    NEW_CONTENT"}
        )
        apply_guardrails_to_tools([_fake_git_log], guardrails=guardrails)
        assert _fake_git_log.description == original_description

    def test_falls_back_to_default_family_when_no_model(self):
        # Bind sites pass model="" (or None) when the model name isn't yet
        # known. apply_guardrails_to_tools must tolerate this and resolve
        # the default family rather than erroring.
        out = apply_guardrails_to_tools([_fake_git_log], model=None)
        assert out[0].description == _fake_git_log.description

    def test_resolves_via_model_when_no_guardrails_passed(self):
        # Hits the real config files — sanity check, not full coverage
        out = apply_guardrails_to_tools([_fake_git_log], model="gpt-4o")
        # _fake_git_log is not in the real config, so it's untouched
        assert out[0].description == _fake_git_log.description


# =============================================================================
# format_nudge — placeholder validation
# =============================================================================


class TestFormatNudge:
    def test_resolves_default_nudge(self):
        out = format_nudge("todo_action", model="gpt-4o", todo_id="todo_3")
        assert "todo_3" in out

    def test_gemma_overrides_todo_action(self):
        out = format_nudge("todo_action", model="google/gemma-4-31b", todo_id="todo_3")
        assert "Gemma" in out
        assert "{todo_id}" not in out  # placeholder must be substituted

    def test_unknown_key_raises(self):
        with pytest.raises(GuardrailFormatError, match="Unknown nudge key"):
            format_nudge("not_a_nudge", model="gpt-4o")

    def test_unexpected_placeholder_raises(self):
        with pytest.raises(GuardrailFormatError, match="unexpected placeholders"):
            format_nudge("todo_action", model="gpt-4o", todo_id="x", surprise="y")

    def test_missing_placeholder_raises_keyerror(self):
        with pytest.raises(KeyError):
            format_nudge("todo_action", model="gpt-4o")

    def test_known_nudges_includes_all_call_sites(self):
        # Sanity — every key actually has at least an empty placeholder set
        for key, expected in KNOWN_NUDGES.items():
            assert isinstance(expected, set)


# =============================================================================
# Cross-cutting: every nudge key in KNOWN_NUDGES must exist in default.yaml
# =============================================================================


class TestNudgeCoverage:
    def test_every_known_nudge_resolves_for_default_family(self):
        g = resolve_guardrails("gpt-4o")
        nudges = g.get("nudges", {})
        missing = sorted(set(KNOWN_NUDGES) - set(nudges))
        assert not missing, (
            f"KNOWN_NUDGES has keys not present in config/guardrails/default.yaml: "
            f"{missing}"
        )
