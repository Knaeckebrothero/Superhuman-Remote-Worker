"""Tests for ContextManager methods not covered by test_context_safety.py.

Tests: set_current_phase, get_token_count, should_compact, should_summarize,
clear_old_tool_results, truncate_long_tool_results, prepare_messages_for_llm,
trim_messages, and sanitize_message_history.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.context import ContextManager, ContextConfig, sanitize_message_history


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def config():
    """Low thresholds for testing."""
    return ContextConfig(
        compaction_threshold_tokens=500,
        summarization_threshold_tokens=800,
        message_count_threshold=5,
        message_count_min_tokens=200,
        keep_recent_messages=3,
        keep_recent_tool_results=2,
        max_tool_result_length=100,
        placeholder_text="[cleared]",
    )


@pytest.fixture
def mgr(config):
    return ContextManager(config=config)


# =============================================================================
# sanitize_message_history (module-level function)
# =============================================================================


class TestSanitizeMessageHistory:
    """Tests for orphaned ToolMessage removal."""

    def test_empty_list(self):
        assert sanitize_message_history([]) == []

    def test_no_orphans(self):
        """Messages with matching AIMessage tool_calls should be kept."""
        messages = [
            AIMessage(
                content="", tool_calls=[{"name": "read_file", "id": "tc1", "args": {}}]
            ),
            ToolMessage(content="file content", tool_call_id="tc1"),
        ]
        result = sanitize_message_history(messages)
        assert len(result) == 2

    def test_removes_orphaned_tool_message(self):
        """ToolMessage without matching AIMessage should be removed."""
        messages = [
            HumanMessage(content="hi"),
            ToolMessage(content="orphaned result", tool_call_id="no_parent"),
        ]
        result = sanitize_message_history(messages)
        assert len(result) == 1
        assert isinstance(result[0], HumanMessage)

    def test_preserves_non_tool_messages(self):
        """Human, AI, System messages should always be kept."""
        messages = [
            SystemMessage(content="system"),
            HumanMessage(content="user"),
            AIMessage(content="assistant"),
        ]
        result = sanitize_message_history(messages)
        assert len(result) == 3

    def test_mixed_orphaned_and_valid(self):
        """Should only remove orphaned, keep valid."""
        messages = [
            AIMessage(
                content="", tool_calls=[{"name": "read", "id": "valid", "args": {}}]
            ),
            ToolMessage(content="ok", tool_call_id="valid"),
            ToolMessage(content="orphaned", tool_call_id="missing_parent"),
        ]
        result = sanitize_message_history(messages)
        assert len(result) == 2


# =============================================================================
# get_token_count, should_compact, should_summarize
# =============================================================================


class TestTokenCounting:
    """Tests for threshold-based decision methods."""

    def test_get_token_count_returns_int(self, mgr):
        messages = [HumanMessage(content="Hello world")]
        count = mgr.get_token_count(messages)
        assert isinstance(count, int)
        assert count > 0

    def test_get_token_count_updates_state(self, mgr):
        messages = [HumanMessage(content="test")]
        count = mgr.get_token_count(messages)
        assert mgr.state.current_token_count == count

    def test_should_compact_below_threshold(self, mgr):
        """Small messages should not trigger compaction."""
        messages = [HumanMessage(content="Hi")]
        assert mgr.should_compact(messages) is False

    def test_should_compact_above_threshold(self, mgr):
        """Large messages should trigger compaction."""
        # config threshold is 500 tokens (~2000 chars)
        messages = [HumanMessage(content="x" * 5000)]
        assert mgr.should_compact(messages) is True

    def test_should_summarize_below_threshold(self, mgr):
        """Small messages should not trigger summarization."""
        messages = [HumanMessage(content="Hi")]
        assert mgr.should_summarize(messages) is False

    def test_should_summarize_high_tokens(self, mgr):
        """Token count above summarization_threshold triggers summarization."""
        # threshold is 800 tokens (~3200 chars)
        messages = [HumanMessage(content="y" * 10000)]
        assert mgr.should_summarize(messages) is True

    def test_should_summarize_high_message_count(self, mgr):
        """Many messages with moderate tokens should trigger."""
        # threshold is 5 messages with 200 min tokens
        messages = [
            HumanMessage(content=f"Message {i} with some content to add tokens " * 10)
            for i in range(10)
        ]
        assert mgr.should_summarize(messages) is True


# =============================================================================
# set_current_phase
# =============================================================================


class TestSetCurrentPhase:
    """Tests for phase-switching token counter."""

    def test_set_phase_switches_counter(self, config):
        """Setting phase should use phase-specific counter if available."""
        mgr = ContextManager(config=config)
        # Default counter should work
        count = mgr.get_token_count([HumanMessage(content="test")])
        assert count > 0

        # Setting a phase that doesn't have a special counter should
        # fall back to default
        mgr.set_current_phase("strategic")
        count2 = mgr.get_token_count([HumanMessage(content="test")])
        assert count2 > 0


# =============================================================================
# clear_old_tool_results
# =============================================================================


class TestClearOldToolResults:
    """Tests for replacing old tool results with placeholder."""

    def test_no_tool_messages_unchanged(self, mgr):
        """Messages without ToolMessages should pass through."""
        messages = [HumanMessage(content="hi"), AIMessage(content="hello")]
        result = mgr.clear_old_tool_results(messages)
        assert len(result) == 2

    def test_clears_old_keeps_recent(self, mgr):
        """Should replace old tool results, keep recent ones."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "r", "id": f"tc{i}", "args": {}} for i in range(4)
                ],
            ),
            ToolMessage(content="old result 1", tool_call_id="tc0"),
            ToolMessage(content="old result 2", tool_call_id="tc1"),
            ToolMessage(content="recent 1", tool_call_id="tc2"),
            ToolMessage(content="recent 2", tool_call_id="tc3"),
        ]
        result = mgr.clear_old_tool_results(messages, keep_recent=2)

        # First two should be cleared, last two kept
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert tool_msgs[0].content == "[cleared]"
        assert tool_msgs[1].content == "[cleared]"
        assert tool_msgs[2].content == "recent 1"
        assert tool_msgs[3].content == "recent 2"

    def test_tracks_cleared_count(self, mgr):
        """Should update state tracking."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "r", "id": f"tc{i}", "args": {}} for i in range(3)
                ],
            ),
            ToolMessage(content="a", tool_call_id="tc0"),
            ToolMessage(content="b", tool_call_id="tc1"),
            ToolMessage(content="c", tool_call_id="tc2"),
        ]
        mgr.clear_old_tool_results(messages, keep_recent=1)
        assert mgr.state.total_tool_results_cleared == 2


# =============================================================================
# Evidence preservation (phase audit protocol prerequisite)
# =============================================================================


class TestEvidencePreservation:
    """Tests that write-type and error-bearing tool results survive recency
    clearing so the strategic phase audit can cite them verbatim.

    See docs/features/phase_audit_protocol.md.
    """

    def test_write_file_result_preserved_when_old(self, mgr):
        """Old write_file results must survive clearing — they prove a side effect."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "write_file", "id": "tc0", "args": {}},
                    {"name": "read_file", "id": "tc1", "args": {}},
                    {"name": "read_file", "id": "tc2", "args": {}},
                    {"name": "read_file", "id": "tc3", "args": {}},
                ],
            ),
            ToolMessage(
                content="Wrote 1234 bytes to output/transcriptions.md",
                tool_call_id="tc0",
                name="write_file",
            ),
            ToolMessage(content="read result 1", tool_call_id="tc1", name="read_file"),
            ToolMessage(content="read result 2", tool_call_id="tc2", name="read_file"),
            ToolMessage(content="read result 3", tool_call_id="tc3", name="read_file"),
        ]
        result = mgr.clear_old_tool_results(messages, keep_recent=2)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]

        # write_file result at index 0 must be preserved verbatim
        assert tool_msgs[0].content == "Wrote 1234 bytes to output/transcriptions.md"
        assert tool_msgs[0].name == "write_file"
        # Non-evidence read_file at index 1 should be cleared
        assert tool_msgs[1].content == "[cleared]"
        # Recent ones kept
        assert tool_msgs[2].content == "read result 2"
        assert tool_msgs[3].content == "read result 3"

    def test_error_content_preserved_when_old(self, mgr):
        """Old tool results containing error signals must survive clearing."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "id": f"tc{i}", "args": {}} for i in range(4)
                ],
            ),
            ToolMessage(
                content="Error: ENOENT - audio.ogg not found",
                tool_call_id="tc0",
                name="read_file",
            ),
            ToolMessage(content="plain result", tool_call_id="tc1", name="read_file"),
            ToolMessage(content="recent 1", tool_call_id="tc2", name="read_file"),
            ToolMessage(content="recent 2", tool_call_id="tc3", name="read_file"),
        ]
        result = mgr.clear_old_tool_results(messages, keep_recent=2)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]

        # Error message at index 0 preserved, non-error at index 1 cleared
        assert "ENOENT" in tool_msgs[0].content
        assert tool_msgs[1].content == "[cleared]"
        assert tool_msgs[2].content == "recent 1"
        assert tool_msgs[3].content == "recent 2"

    def test_traceback_content_preserved(self, mgr):
        """Python traceback text must survive clearing."""
        traceback_content = (
            'Traceback (most recent call last):\n  File "x.py", line 1\n'
            "ValueError: bad input"
        )
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "run_command", "id": f"tc{i}", "args": {}}
                    for i in range(3)
                ],
            ),
            ToolMessage(
                content=traceback_content, tool_call_id="tc0", name="run_command"
            ),
            ToolMessage(content="ok", tool_call_id="tc1", name="run_command"),
            ToolMessage(content="ok", tool_call_id="tc2", name="run_command"),
        ]
        result = mgr.clear_old_tool_results(messages, keep_recent=1)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert "Traceback" in tool_msgs[0].content
        assert "ValueError" in tool_msgs[0].content

    def test_non_evidence_still_cleared(self, mgr):
        """Ordinary tool results with no evidence markers still get cleared."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "kb_search", "id": f"tc{i}", "args": {}} for i in range(3)
                ],
            ),
            ToolMessage(
                content="search hit: foo", tool_call_id="tc0", name="kb_search"
            ),
            ToolMessage(content="recent 1", tool_call_id="tc1", name="kb_search"),
            ToolMessage(content="recent 2", tool_call_id="tc2", name="kb_search"),
        ]
        result = mgr.clear_old_tool_results(messages, keep_recent=2)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert tool_msgs[0].content == "[cleared]"

    def test_cleared_placeholder_preserves_tool_name(self, mgr):
        """Replaced ToolMessages must retain their original tool name so the
        audit protocol can still see which tool produced the cleared result."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "kb_search", "id": "tc0", "args": {}},
                    {"name": "kb_search", "id": "tc1", "args": {}},
                ],
            ),
            ToolMessage(content="old hit", tool_call_id="tc0", name="kb_search"),
            ToolMessage(content="recent hit", tool_call_id="tc1", name="kb_search"),
        ]
        result = mgr.clear_old_tool_results(messages, keep_recent=1)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert tool_msgs[0].content == "[cleared]"
        assert tool_msgs[0].name == "kb_search"

    def test_error_content_not_truncated_when_long(self, mgr):
        """Long error content must survive length-based truncation too."""
        long_error = "Error: " + ("x" * 500) + " Exception details"
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "run_command", "id": f"tc{i}", "args": {}}
                    for i in range(3)
                ],
            ),
            ToolMessage(content=long_error, tool_call_id="tc0", name="run_command"),
            ToolMessage(content="short", tool_call_id="tc1", name="run_command"),
            ToolMessage(content="short", tool_call_id="tc2", name="run_command"),
        ]
        result = mgr.truncate_long_tool_results(messages)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        # The full error content must be intact
        assert tool_msgs[0].content == long_error
        assert "TRUNCATED" not in tool_msgs[0].content

    def test_write_file_result_not_truncated_when_long(self, mgr):
        """write_file outputs survive length truncation."""
        long_write = "Wrote file " + ("x" * 500)
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "write_file", "id": "tc0", "args": {}},
                    {"name": "read_file", "id": "tc1", "args": {}},
                    {"name": "read_file", "id": "tc2", "args": {}},
                ],
            ),
            ToolMessage(content=long_write, tool_call_id="tc0", name="write_file"),
            ToolMessage(content="short", tool_call_id="tc1", name="read_file"),
            ToolMessage(content="short", tool_call_id="tc2", name="read_file"),
        ]
        result = mgr.truncate_long_tool_results(messages)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert tool_msgs[0].content == long_write


# =============================================================================
# truncate_long_tool_results
# =============================================================================


class TestTruncateLongToolResults:
    """Tests for truncating oversized tool results."""

    def test_short_results_unchanged(self, mgr):
        """Short tool results should not be modified."""
        messages = [
            AIMessage(content="", tool_calls=[{"name": "r", "id": "tc1", "args": {}}]),
            ToolMessage(content="short", tool_call_id="tc1"),
        ]
        result = mgr.truncate_long_tool_results(messages)
        tool_msg = [m for m in result if isinstance(m, ToolMessage)][0]
        assert tool_msg.content == "short"

    def test_long_old_results_truncated(self, mgr):
        """Long old tool results should be truncated."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "r", "id": "tc1", "args": {}},
                    {"name": "r", "id": "tc2", "args": {}},
                    {"name": "r", "id": "tc3", "args": {}},
                ],
            ),
            ToolMessage(content="x" * 500, tool_call_id="tc1"),  # Old, long
            ToolMessage(content="y" * 500, tool_call_id="tc2"),  # Recent (kept)
            ToolMessage(content="z" * 500, tool_call_id="tc3"),  # Recent (kept)
        ]
        # keep_recent=2 from config
        result = mgr.truncate_long_tool_results(messages)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]

        # First (old) should be truncated (max_length=100 from config)
        assert len(tool_msgs[0].content) < 500
        assert "TRUNCATED" in tool_msgs[0].content

        # Recent ones kept in full
        assert tool_msgs[1].content == "y" * 500
        assert tool_msgs[2].content == "z" * 500

    def test_no_tool_messages_passthrough(self, mgr):
        """Messages without ToolMessages should pass through."""
        messages = [HumanMessage(content="test")]
        result = mgr.truncate_long_tool_results(messages)
        assert len(result) == 1


# =============================================================================
# prepare_messages_for_llm
# =============================================================================


class TestPrepareMessagesForLlm:
    """Tests for the combined preparation pipeline."""

    def test_empty_list_passthrough(self, mgr):
        result = mgr.prepare_messages_for_llm([])
        assert result == []

    def test_small_messages_no_aggressive(self, mgr):
        """Small messages should only get truncation (not clearing)."""
        messages = [
            AIMessage(content="", tool_calls=[{"name": "r", "id": "tc1", "args": {}}]),
            ToolMessage(content="short", tool_call_id="tc1"),
        ]
        result = mgr.prepare_messages_for_llm(messages)
        tool_msg = [m for m in result if isinstance(m, ToolMessage)][0]
        # Not cleared since below threshold
        assert tool_msg.content == "short"

    def test_aggressive_flag_clears_old(self, mgr):
        """aggressive=True should clear old tool results even below threshold."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "r", "id": "tc1", "args": {}},
                    {"name": "r", "id": "tc2", "args": {}},
                    {"name": "r", "id": "tc3", "args": {}},
                ],
            ),
            ToolMessage(content="old", tool_call_id="tc1"),
            ToolMessage(content="recent1", tool_call_id="tc2"),
            ToolMessage(content="recent2", tool_call_id="tc3"),
        ]
        result = mgr.prepare_messages_for_llm(messages, aggressive=True)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert tool_msgs[0].content == "[cleared]"


# =============================================================================
# trim_messages
# =============================================================================


class TestTrimMessages:
    """Tests for message trimming."""

    def test_small_history_unchanged(self, mgr):
        """History shorter than keep_recent should not be trimmed."""
        messages = [
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
        ]
        result = mgr.trim_messages(messages)
        assert len(result) == 2

    def test_preserves_system_messages(self, mgr):
        """System messages should always be preserved."""
        messages = [
            SystemMessage(content="system prompt"),
            HumanMessage(content="msg 1"),
            AIMessage(content="resp 1"),
            HumanMessage(content="msg 2"),
            AIMessage(content="resp 2"),
            HumanMessage(content="msg 3"),
            AIMessage(content="resp 3"),
            HumanMessage(content="msg 4"),
            AIMessage(content="resp 4"),
        ]
        result = mgr.trim_messages(messages, keep_recent=3)
        system_msgs = [m for m in result if isinstance(m, SystemMessage)]
        assert len(system_msgs) == 1

    def test_preserves_first_human_message(self, mgr):
        """First human message (original task) should be preserved."""
        messages = [
            HumanMessage(content="original task"),
            AIMessage(content="resp 1"),
            HumanMessage(content="msg 2"),
            AIMessage(content="resp 2"),
            HumanMessage(content="msg 3"),
            AIMessage(content="resp 3"),
            HumanMessage(content="msg 4"),
            AIMessage(content="resp 4"),
        ]
        result = mgr.trim_messages(messages, keep_recent=2)
        # Original task should be there
        human_contents = [m.content for m in result if isinstance(m, HumanMessage)]
        assert "original task" in human_contents

    def test_tracks_trimmed_count(self, mgr):
        """Should update state tracking."""
        messages = [HumanMessage(content=f"msg {i}") for i in range(10)]
        mgr.trim_messages(messages, keep_recent=3)
        assert mgr.state.total_messages_trimmed > 0
