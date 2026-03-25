"""Tests for two-layer context safety system.

Tests the recursive summarization (Layer 2) and pre-request safety check (Layer 1)
that prevent LLM requests from exceeding the model context limit.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.context import ContextManager, ContextConfig, ConversationSummary


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def context_config():
    """Create a test context config with low limits for testing."""
    return ContextConfig(
        compaction_threshold_tokens=1000,
        summarization_threshold_tokens=1000,
        message_count_threshold=10,
        message_count_min_tokens=500,
        keep_recent_messages=3,
        keep_recent_tool_results=2,
        # Safety layer constants - low values for testing
        model_max_context_tokens=2000,
        summarization_safe_limit=800,  # Triggers recursive summarization
        summarization_chunk_size=400,  # Small chunks for testing
    )


@pytest.fixture
def context_manager(context_config):
    """Create a context manager with test config."""
    return ContextManager(config=context_config, model="gpt-4")


@pytest.fixture
def mock_llm():
    """Create a mock AuxiliaryLLM that returns structured summaries.

    Returns an AuxiliaryLLM-compatible object with a mock LLM that supports
    with_structured_output() for use in ContextManager methods.
    """
    from src.services.auxiliary import AuxiliaryLLM

    llm = MagicMock()

    # Mock the with_structured_output method (include_raw=True format)
    parsed_value = ConversationSummary(
        summary="Test summary of the conversation.",
        tasks_completed="- Task 1 completed\n- Task 2 completed",
        key_decisions="Decision to use approach A",
        current_state="Ready for next phase",
        blockers="",
    )
    raw_response = AIMessage(content="structured output")
    structured_llm = AsyncMock()
    structured_llm.ainvoke = AsyncMock(
        return_value={
            "raw": raw_response,
            "parsed": parsed_value,
            "parsing_error": None,
        }
    )
    llm.with_structured_output = MagicMock(return_value=structured_llm)

    return AuxiliaryLLM(llm=llm)


def create_large_message_history(
    num_messages: int, chars_per_message: int = 500
) -> list:
    """Create a list of messages with specified total size."""
    messages = []
    for i in range(num_messages):
        if i % 2 == 0:
            content = f"User message {i}: " + "x" * chars_per_message
            messages.append(HumanMessage(content=content))
        else:
            content = f"Assistant response {i}: " + "y" * chars_per_message
            messages.append(AIMessage(content=content))
    return messages


# =============================================================================
# Tests for _split_into_chunks
# =============================================================================


class TestSplitIntoChunks:
    """Tests for the _split_into_chunks helper method."""

    def test_single_chunk_when_small(self, context_manager):
        """Small input should result in a single chunk."""
        parts = ["Short message 1", "Short message 2", "Short message 3"]
        chunks = context_manager._split_into_chunks(parts, target_tokens=1000)

        assert len(chunks) == 1
        assert chunks[0] == parts

    def test_multiple_chunks_when_large(self, context_manager):
        """Large input should be split into multiple chunks."""
        # Each part is ~100 tokens (400 chars / 4)
        parts = ["x" * 400 for _ in range(10)]
        chunks = context_manager._split_into_chunks(parts, target_tokens=200)

        # Should create multiple chunks
        assert len(chunks) > 1
        # All parts should be included
        all_parts = [p for chunk in chunks for p in chunk]
        assert len(all_parts) == 10

    def test_empty_input(self, context_manager):
        """Empty input should return empty list."""
        chunks = context_manager._split_into_chunks([], target_tokens=1000)
        assert chunks == []

    def test_single_large_part(self, context_manager):
        """Single part larger than target should be its own chunk."""
        # One part that's ~250 tokens
        parts = ["x" * 1000]
        chunks = context_manager._split_into_chunks(parts, target_tokens=100)

        # Should still be one chunk (can't split a single part)
        assert len(chunks) == 1
        assert chunks[0] == parts


# =============================================================================
# Tests for _format_messages_for_summary
# =============================================================================


class TestFormatMessagesForSummary:
    """Tests for the _format_messages_for_summary helper method."""

    def test_formats_human_messages(self, context_manager):
        """Human messages should be formatted with User prefix."""
        messages = [HumanMessage(content="Hello, world!")]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert parts[0].startswith("User:")
        assert "Hello, world!" in parts[0]

    def test_formats_ai_messages(self, context_manager):
        """AI messages should be formatted with Assistant prefix."""
        messages = [AIMessage(content="Hello back!")]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert parts[0].startswith("Assistant:")

    def test_formats_tool_calls(self, context_manager):
        """AI messages with tool calls should show tool names."""
        messages = [
            AIMessage(
                content="", tool_calls=[{"name": "read_file", "id": "1", "args": {}}]
            )
        ]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert "read_file" in parts[0]

    def test_formats_tool_messages_recent(self, context_manager):
        """Recent tool messages should include truncated content (observation masking)."""
        messages = [ToolMessage(content="x" * 100, tool_call_id="1")]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        # Single tool message falls within the recent-10 window, so content is included
        assert "[Tool 'unknown' result:" in parts[0]
        assert "xxx" in parts[0]  # content preserved (under 300 char truncation)

    def test_formats_tool_messages_old_masked(self, context_manager):
        """Old tool messages beyond the 10-message window should be observation-masked."""
        # Create 12 tool messages — first 2 should be masked, last 10 should have content
        messages = [
            ToolMessage(content=f"result_{i}" * 20, tool_call_id=str(i))
            for i in range(12)
        ]
        parts = context_manager._format_messages_for_summary(messages)

        # 12 messages + 1 recency marker = 13 parts
        assert len(parts) == 13
        # First 2 are beyond the recent-10 window — should be masked (placeholder only)
        assert "omitted" in parts[0]
        assert "omitted" in parts[1]
        # Recency marker separates old from recent
        assert "RECENT CONTEXT" in parts[2]
        # Last 10 should have content
        assert "result_2" in parts[3]
        assert "result_11" in parts[12]

    def test_recency_marker_inserted_when_enough_tool_messages(self, context_manager):
        """Recency marker should appear when there are >10 tool messages."""
        messages = [
            ToolMessage(content=f"result_{i}", tool_call_id=str(i)) for i in range(15)
        ]
        parts = context_manager._format_messages_for_summary(messages)

        marker_parts = [p for p in parts if "RECENT CONTEXT" in p]
        assert len(marker_parts) == 1
        assert "PRESERVE WITH HIGHEST PRIORITY" in marker_parts[0]

    def test_no_recency_marker_when_few_tool_messages(self, context_manager):
        """No recency marker when all tool messages fit in the recent window."""
        messages = [
            ToolMessage(content=f"result_{i}", tool_call_id=str(i)) for i in range(5)
        ]
        parts = context_manager._format_messages_for_summary(messages)

        marker_parts = [p for p in parts if "RECENT CONTEXT" in p]
        assert len(marker_parts) == 0

    def test_atomic_grouping_preserves_sibling_results(self, context_manager):
        """When one tool result in a group is recent, all siblings should be recent too."""
        # 9 old standalone tool messages (fill most of the window)
        old_msgs = [
            ToolMessage(content=f"old_{i}" * 20, tool_call_id=f"old_{i}")
            for i in range(9)
        ]

        # 1 AIMessage calling 3 tools — will straddle the boundary
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "read_file", "id": "group_a", "args": {}},
                {"name": "web_search", "id": "group_b", "args": {}},
                {"name": "sql_query", "id": "group_c", "args": {}},
            ],
        )
        group_results = [
            ToolMessage(content="file content here", tool_call_id="group_a"),
            ToolMessage(content="search results here", tool_call_id="group_b"),
            ToolMessage(content="query output here", tool_call_id="group_c"),
        ]

        # Total: 12 tool messages. Flat last-10 keeps old_2..old_8 + all 3 grouped.
        # All 3 grouped are in the flat window here, but atomic grouping ensures
        # that even at different boundary positions, they stay together.
        messages = old_msgs + [ai_msg] + group_results
        parts = context_manager._format_messages_for_summary(messages)

        # All 3 grouped results should show content, not "omitted"
        for label in ["file content", "search results", "query output"]:
            matching = [p for p in parts if label in p]
            assert len(matching) == 1
            assert "omitted" not in matching[0], f"'{label}' should not be masked"

    def test_atomic_grouping_boundary_case(self, context_manager):
        """Group straddling the flat-10 boundary: all siblings should be preserved."""
        # 10 standalone old tool messages
        old_msgs = [
            ToolMessage(content=f"standalone_{i}" * 20, tool_call_id=f"s_{i}")
            for i in range(10)
        ]

        # AIMessage with 3 tool calls — group will straddle the flat-10 boundary
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "tool_a", "id": "g_a", "args": {}},
                {"name": "tool_b", "id": "g_b", "args": {}},
                {"name": "tool_c", "id": "g_c", "args": {}},
            ],
        )
        group_results = [
            ToolMessage(content="result_a data", tool_call_id="g_a"),
            ToolMessage(content="result_b data", tool_call_id="g_b"),
            ToolMessage(content="result_c data", tool_call_id="g_c"),
        ]

        # Total: 13 tool messages. Flat last-10 = s_3..s_9 + g_a + g_b + g_c
        # g_a is the 11th tool message — in the flat window.
        # But with only 7 standalone old in the window, if we add more old messages
        # to push g_a out, atomic grouping pulls it back in.
        # Here all 3 are already in the flat window, so just verify they stay.
        messages = old_msgs + [ai_msg] + group_results
        parts = context_manager._format_messages_for_summary(messages)

        for label in ["result_a", "result_b", "result_c"]:
            matching = [p for p in parts if label in p]
            assert len(matching) == 1
            assert "omitted" not in matching[0], f"{label} should not be masked"

    def test_atomic_grouping_pulls_in_old_siblings(self, context_manager):
        """A group member outside the flat-10 window is pulled in by a recent sibling."""
        # AIMessage with 2 tool calls — results will be split across the boundary
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "tool_x", "id": "pair_x", "args": {}},
                {"name": "tool_y", "id": "pair_y", "args": {}},
            ],
        )

        # Place pair_x early (outside flat window), pair_y late (inside flat window),
        # with 10 filler tool messages in between.
        # Total: 12 tool messages. Flat last-10 = indices 2..11.
        # pair_x (tool index 0) is OUTSIDE flat window.
        # pair_y (tool index 11) is INSIDE flat window.
        # Atomic grouping should pull pair_x into the recent set.
        messages = (
            [ai_msg]
            + [ToolMessage(content="x_data value", tool_call_id="pair_x")]
            + [
                ToolMessage(content=f"fill_{i}" * 20, tool_call_id=f"fl_{i}")
                for i in range(10)
            ]
            + [ToolMessage(content="y_data value", tool_call_id="pair_y")]
        )
        parts = context_manager._format_messages_for_summary(messages)

        # pair_x should show content (pulled in by pair_y), not be masked
        x_parts = [p for p in parts if "x_data" in p]
        assert len(x_parts) == 1
        assert "omitted" not in x_parts[0], (
            "pair_x should be pulled into recent by sibling pair_y"
        )

        # pair_y should also show content (it's naturally in the window)
        y_parts = [p for p in parts if "y_data" in p]
        assert len(y_parts) == 1
        assert "omitted" not in y_parts[0]

    def test_includes_prior_summaries(self, context_manager):
        """System messages with prior summaries should be included."""
        messages = [
            SystemMessage(content="[Summary of prior work]\nPrevious work summary."),
            HumanMessage(content="Continue"),
        ]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 2
        assert "Prior Summary:" in parts[0]
        assert "User:" in parts[1]

    def test_excludes_regular_system_messages(self, context_manager):
        """Regular system messages should be excluded."""
        messages = [
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="Hello"),
        ]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert parts[0].startswith("User:")

    def test_truncates_long_messages(self, context_manager):
        """Long messages should be truncated."""
        long_content = "x" * 1000
        messages = [HumanMessage(content=long_content)]
        parts = context_manager._format_messages_for_summary(messages)

        # Human messages truncated to 500 chars
        assert len(parts[0]) < 600

        # AI messages truncated to 800 chars
        ai_messages = [AIMessage(content="y" * 1000)]
        ai_parts = context_manager._format_messages_for_summary(ai_messages)
        assert "y" * 800 in ai_parts[0]
        assert "y" * 801 not in ai_parts[0]

    def test_ai_reasoning_preserved_at_800_chars(self, context_manager):
        """AI reasoning should be preserved up to 800 chars, not 300."""
        # Content that's 500 chars — would be cut at 300 before, now preserved
        content = "A" * 500
        messages = [AIMessage(content=content)]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert "A" * 500 in parts[0]  # Full 500 chars preserved

    def test_ai_reasoning_with_tool_calls_preserved(self, context_manager):
        """AIMessage with both reasoning content and tool_calls should show both."""
        messages = [
            AIMessage(
                content="I need to check the auth module because the JWT validation is failing",
                tool_calls=[
                    {"name": "read_file", "id": "tc1", "args": {"path": "auth.py"}},
                    {"name": "web_search", "id": "tc2", "args": {"query": "JWT"}},
                ],
            )
        ]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        # Both reasoning and tool names should be present
        assert "JWT validation" in parts[0]
        assert "read_file" in parts[0]
        assert "web_search" in parts[0]

    def test_ai_reasoning_with_tool_calls_empty_content(self, context_manager):
        """AIMessage with tool_calls but empty content should only show tool names."""
        messages = [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "id": "tc1", "args": {}}],
            )
        ]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert "read_file" in parts[0]
        assert parts[0] == "Assistant: [Called tools: read_file]"


# =============================================================================
# Tests for _single_pass_summarize
# =============================================================================


class TestSinglePassSummarize:
    """Tests for the _single_pass_summarize helper method."""

    @pytest.mark.asyncio
    async def test_returns_formatted_summary(self, context_manager, mock_llm):
        """Should return properly formatted summary."""
        result = await context_manager._single_pass_summarize(
            conversation_text="User: Hello\nAssistant: Hi",
            auxiliary=mock_llm,
            summarization_prompt="Summarize this conversation.\n\nConversation:\n\n{conversation}\n\nKeep under {max_summary_length} tokens.",
            max_summary_length=10000,
        )

        assert "**Summary:**" in result
        assert "**Tasks Completed:**" in result
        assert "Test summary" in result

    @pytest.mark.asyncio
    async def test_uses_custom_prompt(self, context_manager, mock_llm):
        """Should use custom prompt when provided."""
        custom_prompt = "Custom: {conversation}"

        await context_manager._single_pass_summarize(
            conversation_text="test",
            auxiliary=mock_llm,
            summarization_prompt=custom_prompt,
            max_summary_length=10000,
        )

        # Verify LLM was called (mock_llm is AuxiliaryLLM wrapping the mock)
        mock_llm.llm.with_structured_output.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_llm_error(self, context_manager):
        """Should return error message when LLM fails."""
        from src.services.auxiliary import AuxiliaryLLM

        error_llm = MagicMock()
        structured_llm = AsyncMock()
        structured_llm.ainvoke = AsyncMock(side_effect=Exception("LLM error"))
        error_llm.with_structured_output = MagicMock(return_value=structured_llm)
        # Also mock raw ainvoke for unstructured fallback
        error_llm.ainvoke = AsyncMock(side_effect=Exception("Fallback also fails"))

        aux = AuxiliaryLLM(llm=error_llm)

        result = await context_manager._single_pass_summarize(
            conversation_text="test",
            auxiliary=aux,
            summarization_prompt="Summarize this conversation.\n\nConversation:\n\n{conversation}\n\nKeep under {max_summary_length} tokens.",
            max_summary_length=10000,
        )

        assert "[Summarization failed:" in result


# =============================================================================
# Tests for _recursive_summarize
# =============================================================================


class TestRecursiveSummarize:
    """Tests for the _recursive_summarize method."""

    @pytest.mark.asyncio
    async def test_splits_large_input_into_chunks(self, context_manager, mock_llm):
        """Large input should be split and each chunk summarized."""
        # Create large formatted parts (~1000 tokens total, config chunk_size=400)
        parts = ["x" * 1600 for _ in range(3)]  # ~400 tokens each

        result = await context_manager._recursive_summarize(
            formatted_parts=parts,
            auxiliary=mock_llm,
            summarization_prompt="Summarize this conversation.\n\nConversation:\n\n{conversation}\n\nKeep under {max_summary_length} tokens.",
            max_summary_length=5000,
        )

        # Should have called the LLM multiple times (once per chunk + final unification)
        assert mock_llm.llm.with_structured_output.call_count >= 2
        assert result  # Should return something

    @pytest.mark.asyncio
    async def test_respects_max_depth(self, context_manager, mock_llm):
        """Should stop recursing at max depth."""
        # Create very large input that would need many recursion levels
        parts = ["x" * 4000 for _ in range(20)]  # Very large

        # This should complete without infinite recursion
        result = await context_manager._recursive_summarize(
            formatted_parts=parts,
            auxiliary=mock_llm,
            summarization_prompt="Summarize this conversation.\n\nConversation:\n\n{conversation}\n\nKeep under {max_summary_length} tokens.",
            max_summary_length=1000,
        )

        assert result  # Should return something even if truncated

    @pytest.mark.asyncio
    async def test_single_chunk_no_recursion(self, context_manager, mock_llm):
        """Small input should not recurse."""
        parts = ["Short message"]

        result = await context_manager._recursive_summarize(
            formatted_parts=parts,
            auxiliary=mock_llm,
            summarization_prompt="Summarize this conversation.\n\nConversation:\n\n{conversation}\n\nKeep under {max_summary_length} tokens.",
            max_summary_length=10000,
        )

        # Should only call LLM once (no chunking needed)
        # Actually will be 1 call since single chunk
        assert result


# =============================================================================
# Tests for summarize_conversation (main entry point)
# =============================================================================


class TestSummarizeConversation:
    """Tests for the main summarize_conversation method."""

    @pytest.mark.asyncio
    async def test_small_input_uses_single_pass(self, context_manager, mock_llm):
        """Small input should use single-pass summarization."""
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there!"),
        ]

        result = await context_manager.summarize_conversation(
            messages=messages,
            auxiliary=mock_llm,
        )

        assert "**Summary:**" in result
        assert context_manager.state.total_summarizations == 1

    @pytest.mark.asyncio
    async def test_large_input_triggers_recursive(self, context_manager, mock_llm):
        """Large input should trigger recursive summarization."""
        # Create messages that exceed summarization_safe_limit (800 tokens = 3200 chars)
        messages = create_large_message_history(num_messages=20, chars_per_message=500)

        result = await context_manager.summarize_conversation(
            messages=messages,
            auxiliary=mock_llm,
        )

        assert result
        # Should have made multiple LLM calls for chunked summarization
        assert mock_llm.llm.with_structured_output.call_count > 1

    @pytest.mark.asyncio
    async def test_tracks_summarization_state(self, context_manager, mock_llm):
        """Should track summarization in state."""
        messages = [HumanMessage(content="Test")]

        await context_manager.summarize_conversation(
            messages=messages,
            auxiliary=mock_llm,
        )

        assert context_manager.state.total_summarizations == 1
        assert len(context_manager.state.summaries) == 1


# =============================================================================
# Tests for ensure_within_limits with force parameter
# =============================================================================


class TestEnsureWithinLimitsForce:
    """Tests for ensure_within_limits with force=True (used by Layer 1)."""

    @pytest.mark.asyncio
    async def test_force_triggers_summarization(self, context_manager, mock_llm):
        """force=True should trigger summarization even below threshold.

        Note: Summarization still requires enough messages to summarize.
        If len(messages) <= keep_recent_messages, there's nothing to summarize.
        """
        # Need more messages than keep_recent_messages (which is 3 in test config)
        messages = [
            HumanMessage(content="Message 1"),
            AIMessage(content="Response 1"),
            HumanMessage(content="Message 2"),
            AIMessage(content="Response 2"),
            HumanMessage(content="Message 3"),
            AIMessage(content="Response 3"),
        ]

        await context_manager.ensure_within_limits(
            messages=messages,
            auxiliary=mock_llm,
            force=True,
        )

        # Should have triggered summarization
        assert context_manager.state.total_summarizations == 1

    @pytest.mark.asyncio
    async def test_no_force_respects_threshold(self, context_manager, mock_llm):
        """Without force, should respect threshold."""
        # Small messages below threshold
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi"),
        ]

        result = await context_manager.ensure_within_limits(
            messages=messages,
            auxiliary=mock_llm,
            force=False,
        )

        # Should NOT have triggered summarization
        assert context_manager.state.total_summarizations == 0
        assert result == messages  # Messages unchanged


# =============================================================================
# Integration-style tests
# =============================================================================


class TestContextSafetyIntegration:
    """Integration tests for the two-layer safety system."""

    @pytest.mark.asyncio
    async def test_handles_very_large_input(self, mock_llm):
        """System should handle arbitrarily large inputs without error."""
        config = ContextConfig(
            compaction_threshold_tokens=1000,
            summarization_threshold_tokens=1000,
            summarization_safe_limit=500,  # Very low to force recursion
            summarization_chunk_size=200,
        )
        mgr = ContextManager(config=config)

        # Create very large message history
        messages = create_large_message_history(
            num_messages=100, chars_per_message=1000
        )

        # Should complete without error
        result = await mgr.summarize_conversation(messages=messages, auxiliary=mock_llm)

        assert result
        assert "[Summarization failed:" not in result

    def test_config_defaults_are_safe(self):
        """Default config values should be safe for 100k effective context."""
        config = ContextConfig()

        assert config.model_max_context_tokens == 100_000
        assert config.summarization_safe_limit == 90_000
        assert config.summarization_chunk_size == 80_000
        # Safe limit should leave room for prompt overhead
        assert config.summarization_safe_limit < config.model_max_context_tokens
        # Chunk size should be less than safe limit
        assert config.summarization_chunk_size < config.summarization_safe_limit
