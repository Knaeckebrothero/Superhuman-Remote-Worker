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
    """Create a mock LLM that returns structured summaries."""
    llm = MagicMock()

    # Mock the with_structured_output method
    structured_llm = AsyncMock()
    structured_llm.ainvoke = AsyncMock(return_value=ConversationSummary(
        summary="Test summary of the conversation.",
        tasks_completed="- Task 1 completed\n- Task 2 completed",
        key_decisions="Decision to use approach A",
        current_state="Ready for next phase",
        blockers="",
    ))
    llm.with_structured_output = MagicMock(return_value=structured_llm)

    return llm


def create_large_message_history(num_messages: int, chars_per_message: int = 500) -> list:
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
        messages = [AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {}}]
        )]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert "read_file" in parts[0]

    def test_formats_tool_messages(self, context_manager):
        """Tool messages should show result length."""
        messages = [ToolMessage(content="x" * 100, tool_call_id="1")]
        parts = context_manager._format_messages_for_summary(messages)

        assert len(parts) == 1
        assert "[Tool result:" in parts[0]
        assert "100 chars" in parts[0]

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
            llm=mock_llm,
            summarization_prompt=None,
            oss_reasoning_level="high",
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
            llm=mock_llm,
            summarization_prompt=custom_prompt,
            oss_reasoning_level="high",
            max_summary_length=10000,
        )

        # Verify LLM was called
        mock_llm.with_structured_output.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_llm_error(self, context_manager):
        """Should return error message when LLM fails."""
        error_llm = MagicMock()
        structured_llm = AsyncMock()
        structured_llm.ainvoke = AsyncMock(side_effect=Exception("LLM error"))
        error_llm.with_structured_output = MagicMock(return_value=structured_llm)

        result = await context_manager._single_pass_summarize(
            conversation_text="test",
            llm=error_llm,
            summarization_prompt=None,
            oss_reasoning_level="high",
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
            llm=mock_llm,
            summarization_prompt=None,
            oss_reasoning_level="high",
            max_summary_length=5000,
        )

        # Should have called the LLM multiple times (once per chunk + final unification)
        assert mock_llm.with_structured_output.call_count >= 2
        assert result  # Should return something

    @pytest.mark.asyncio
    async def test_respects_max_depth(self, context_manager, mock_llm):
        """Should stop recursing at max depth."""
        # Create very large input that would need many recursion levels
        parts = ["x" * 4000 for _ in range(20)]  # Very large

        # This should complete without infinite recursion
        result = await context_manager._recursive_summarize(
            formatted_parts=parts,
            llm=mock_llm,
            summarization_prompt=None,
            oss_reasoning_level="high",
            max_summary_length=1000,
        )

        assert result  # Should return something even if truncated

    @pytest.mark.asyncio
    async def test_single_chunk_no_recursion(self, context_manager, mock_llm):
        """Small input should not recurse."""
        parts = ["Short message"]

        result = await context_manager._recursive_summarize(
            formatted_parts=parts,
            llm=mock_llm,
            summarization_prompt=None,
            oss_reasoning_level="high",
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
            llm=mock_llm,
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
            llm=mock_llm,
        )

        assert result
        # Should have made multiple LLM calls for chunked summarization
        assert mock_llm.with_structured_output.call_count > 1

    @pytest.mark.asyncio
    async def test_tracks_summarization_state(self, context_manager, mock_llm):
        """Should track summarization in state."""
        messages = [HumanMessage(content="Test")]

        await context_manager.summarize_conversation(
            messages=messages,
            llm=mock_llm,
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
            llm=mock_llm,
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
            llm=mock_llm,
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
        messages = create_large_message_history(num_messages=100, chars_per_message=1000)

        # Should complete without error
        result = await mgr.summarize_conversation(messages=messages, llm=mock_llm)

        assert result
        assert "[Summarization failed:" not in result

    def test_config_defaults_are_safe(self):
        """Default config values should be safe for 128k context models."""
        config = ContextConfig()

        assert config.model_max_context_tokens == 128_000
        assert config.summarization_safe_limit == 100_000
        assert config.summarization_chunk_size == 80_000
        # Safe limit should leave room for prompt overhead
        assert config.summarization_safe_limit < config.model_max_context_tokens
        # Chunk size should be less than safe limit
        assert config.summarization_chunk_size < config.summarization_safe_limit
