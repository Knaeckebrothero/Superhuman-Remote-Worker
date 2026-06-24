"""Tests for src/core/toolcall_recovery.py.

Covers the three helpers used to recover tool calls that a serving-layer parser
leaked into message content as text (the Gemma ``<|tool_call>...<tool_call|>``
markup):

* parse_leaked_tool_calls — strict recovery (recover vs bail)
* has_leaked_tool_call_markup — loose detector for the circuit breaker
* strip_tool_call_markup — content cleanup after recovery
"""

from src.core.toolcall_recovery import (
    has_leaked_tool_call_markup,
    parse_leaked_tool_calls,
    strip_tool_call_markup,
)

# The four real leaked strings observed in stored chat history (job 2dacba6f /
# gpt-5.5), plus the canonical-braces variant.
LEAK_GIT_LOG = "<|tool_call>call:git_log(max_count=15)<tool_call|>"
LEAK_GIT_TAGS = "<|tool_call>call:git_tags()<tool_call|>"
LEAK_TODO_PARENS = '<|tool_call>call:todo_complete(todo_id="todo_1")<tool_call|>'
LEAK_TODO_BRACES = (
    '<|tool_call>call:todo_complete{todo_id:<|"|>todo_4<|"|>}<tool_call|>'
)


# =============================================================================
# parse_leaked_tool_calls — recovery
# =============================================================================


class TestParseLeakedToolCallsRecover:
    """Cases where a clean structured tool call should be recovered."""

    def test_parens_int_arg(self):
        calls = parse_leaked_tool_calls(LEAK_GIT_LOG, allowed_names={"git_log"})
        assert len(calls) == 1
        tc = calls[0]
        assert tc["name"] == "git_log"
        assert tc["args"] == {"max_count": 15}
        assert tc["type"] == "tool_call"
        assert tc["id"].startswith("rcv_")
        assert len(tc["id"]) > len("rcv_")

    def test_parens_no_args(self):
        calls = parse_leaked_tool_calls(LEAK_GIT_TAGS, allowed_names={"git_tags"})
        assert len(calls) == 1
        assert calls[0]["name"] == "git_tags"
        assert calls[0]["args"] == {}

    def test_parens_string_arg(self):
        calls = parse_leaked_tool_calls(
            LEAK_TODO_PARENS, allowed_names={"todo_complete"}
        )
        assert calls[0]["args"] == {"todo_id": "todo_1"}

    def test_gemma_braces_string_arg(self):
        calls = parse_leaked_tool_calls(
            LEAK_TODO_BRACES, allowed_names={"todo_complete"}
        )
        assert calls[0]["name"] == "todo_complete"
        assert calls[0]["args"] == {"todo_id": "todo_4"}

    def test_gemma_braces_mixed_types_and_comma_in_string(self):
        content = (
            '<|tool_call>call:write_file{path:<|"|>a, b<|"|>,count:42,flag:true}'
            "<tool_call|>"
        )
        calls = parse_leaked_tool_calls(content, allowed_names={"write_file"})
        assert calls[0]["args"] == {"path": "a, b", "count": 42, "flag": True}

    def test_gemma_two_string_values_with_comma(self):
        content = (
            '<|tool_call>call:write_file{path:<|"|>p.md<|"|>,'
            'content:<|"|>hello, world<|"|>}<tool_call|>'
        )
        calls = parse_leaked_tool_calls(content, allowed_names={"write_file"})
        assert calls[0]["args"] == {"path": "p.md", "content": "hello, world"}

    def test_bare_bool_in_parens(self):
        # Gemma emits JS-style bare ``true`` which ast.literal_eval rejects.
        content = "<|tool_call>call:write_file(append=true)<tool_call|>"
        calls = parse_leaked_tool_calls(content, allowed_names={"write_file"})
        assert calls[0]["args"] == {"append": True}

    def test_two_consecutive_blocks_distinct_ids(self):
        content = f"{LEAK_GIT_TAGS}\n{LEAK_GIT_LOG}"
        calls = parse_leaked_tool_calls(content, allowed_names={"git_tags", "git_log"})
        assert [c["name"] for c in calls] == ["git_tags", "git_log"]
        assert calls[0]["id"] != calls[1]["id"]

    def test_surrounding_whitespace_allowed(self):
        content = f"\n   {LEAK_GIT_TAGS}  \n"
        calls = parse_leaked_tool_calls(content, allowed_names={"git_tags"})
        assert len(calls) == 1

    def test_registry_fallback_when_allowed_names_none(self):
        # read_file is a real registered tool; allowed_names=None → TOOL_REGISTRY.
        content = '<|tool_call>call:read_file{path:<|"|>plan.md<|"|>}<tool_call|>'
        calls = parse_leaked_tool_calls(content)
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"


# =============================================================================
# parse_leaked_tool_calls — bail (return [])
# =============================================================================


class TestParseLeakedToolCallsBail:
    """Cases where recovery is unsafe and must bail entirely."""

    def test_empty_string(self):
        assert parse_leaked_tool_calls("", allowed_names={"git_log"}) == []

    def test_no_markup(self):
        assert parse_leaked_tool_calls("just some text", allowed_names={"x"}) == []

    def test_unknown_tool_not_allowed(self):
        assert parse_leaked_tool_calls(LEAK_GIT_LOG, allowed_names={"read_file"}) == []

    def test_registry_fallback_unknown_tool(self):
        content = "<|tool_call>call:definitely_not_a_real_tool_xyz()<tool_call|>"
        assert parse_leaked_tool_calls(content) == []

    def test_prose_prefixed_markup(self):
        content = f"Here is my plan. {LEAK_GIT_LOG}"
        assert parse_leaked_tool_calls(content, allowed_names={"git_log"}) == []

    def test_prose_suffixed_markup(self):
        content = f"{LEAK_GIT_LOG} and then I will continue."
        assert parse_leaked_tool_calls(content, allowed_names={"git_log"}) == []

    def test_truncated_no_closing_tag(self):
        content = "<|tool_call>call:git_log(max_count=15"
        assert parse_leaked_tool_calls(content, allowed_names={"git_log"}) == []

    def test_array_arg_gemma(self):
        content = "<|tool_call>call:next_phase_todos{tasks:[1,2]}<tool_call|>"
        assert (
            parse_leaked_tool_calls(content, allowed_names={"next_phase_todos"}) == []
        )

    def test_array_arg_parens(self):
        content = '<|tool_call>call:next_phase_todos(tasks=["a","b"])<tool_call|>'
        assert (
            parse_leaked_tool_calls(content, allowed_names={"next_phase_todos"}) == []
        )

    def test_positional_arg(self):
        content = "<|tool_call>call:git_log(15)<tool_call|>"
        assert parse_leaked_tool_calls(content, allowed_names={"git_log"}) == []

    def test_nested_object_gemma(self):
        content = "<|tool_call>call:write_file{meta:{a:1}}<tool_call|>"
        assert parse_leaked_tool_calls(content, allowed_names={"write_file"}) == []

    def test_mismatched_delimiters(self):
        content = "<|tool_call>call:git_log{max_count=15)<tool_call|>"
        assert parse_leaked_tool_calls(content, allowed_names={"git_log"}) == []

    def test_one_bad_block_bails_whole_batch(self):
        # First block is recoverable, second has an array arg → bail entirely.
        content = (
            f"{LEAK_GIT_TAGS}\n"
            "<|tool_call>call:next_phase_todos{tasks:[1]}<tool_call|>"
        )
        calls = parse_leaked_tool_calls(
            content, allowed_names={"git_tags", "next_phase_todos"}
        )
        assert calls == []


# =============================================================================
# has_leaked_tool_call_markup
# =============================================================================


class TestHasLeakedToolCallMarkup:
    def test_each_leak_string_true(self):
        for leak in (LEAK_GIT_LOG, LEAK_GIT_TAGS, LEAK_TODO_PARENS, LEAK_TODO_BRACES):
            assert has_leaked_tool_call_markup(leak) is True

    def test_block_plus_short_prose_true(self):
        assert has_leaked_tool_call_markup(f"Okay.\n{LEAK_GIT_TAGS}") is True

    def test_truncated_markup_true(self):
        # No closing tag, but markup dominates — the breaker should still trip.
        assert (
            has_leaked_tool_call_markup("<|tool_call>call:git_log(max_count=15") is True
        )

    def test_plain_prose_false(self):
        assert has_leaked_tool_call_markup("reflection turn 3: still thinking") is False

    def test_empty_false(self):
        assert has_leaked_tool_call_markup("") is False

    def test_long_prose_quoting_tag_once_false(self):
        prose = "This is a long explanation of how tool calls work. " * 12
        content = f"{prose}{LEAK_GIT_TAGS}{prose}"
        assert has_leaked_tool_call_markup(content) is False


# =============================================================================
# strip_tool_call_markup
# =============================================================================


class TestStripToolCallMarkup:
    def test_single_block_to_empty(self):
        assert strip_tool_call_markup(LEAK_GIT_LOG) == ""

    def test_two_blocks_to_empty(self):
        assert strip_tool_call_markup(f"{LEAK_GIT_TAGS}\n{LEAK_GIT_LOG}") == ""

    def test_block_plus_prose_keeps_prose(self):
        assert strip_tool_call_markup(f"Done. {LEAK_GIT_TAGS}") == "Done."

    def test_no_markup_unchanged(self):
        assert strip_tool_call_markup("plain text") == "plain text"

    def test_empty_returns_empty(self):
        assert strip_tool_call_markup("") == ""
