"""Unit tests for context management module (Phase 6).

Tests context compaction, tool result clearing, summarization,
token counting, and retry logic.
"""

import asyncio
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.agent.context_manager import (
    ContextConfig,
    ContextManager,
    ContextManagementState,
    ToolRetryManager,
    count_tokens_approximate,
    count_tokens_tiktoken,
    get_token_counter,
    write_error_to_workspace,
    TIKTOKEN_AVAILABLE,
)


class TestContextConfig:
    """Tests for ContextConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = ContextConfig()

        assert config.compaction_threshold_tokens == 80_000
        assert config.summarization_threshold_tokens == 100_000
        assert config.keep_recent_tool_results == 5
        assert config.keep_recent_messages == 20
        assert config.max_tool_result_length == 5000
        assert config.tool_retry_count == 3
        assert config.tool_retry_delay_seconds == 1.0

    def test_custom_values(self):
        """Test custom configuration values."""
        config = ContextConfig(
            compaction_threshold_tokens=50_000,
            keep_recent_tool_results=3,
            tool_retry_count=5,
        )

        assert config.compaction_threshold_tokens == 50_000
        assert config.keep_recent_tool_results == 3
        assert config.tool_retry_count == 5


class TestContextManagementState:
    """Tests for ContextManagementState dataclass."""

    def test_default_state(self):
        """Test default state initialization."""
        state = ContextManagementState()

        assert state.total_tool_results_cleared == 0
        assert state.total_messages_trimmed == 0
        assert state.total_summarizations == 0
        assert state.current_token_count == 0
        assert state.summaries == []
        assert state.last_compaction_iteration == 0

    def test_state_with_values(self):
        """Test state with custom values."""
        state = ContextManagementState(
            total_tool_results_cleared=5,
            total_summarizations=2,
            summaries=["Summary 1", "Summary 2"],
        )

        assert state.total_tool_results_cleared == 5
        assert state.total_summarizations == 2
        assert len(state.summaries) == 2


class TestTokenCounting:
    """Tests for token counting functions."""

    def test_count_tokens_approximate_simple(self):
        """Test approximate token counting with simple messages."""
        messages = [
            HumanMessage(content="Hello world"),  # 11 chars ~ 2 tokens
            AIMessage(content="Hi there!"),  # 9 chars ~ 2 tokens
        ]

        count = count_tokens_approximate(messages)
        # 20 chars / 4 = 5 tokens
        assert count == 5

    def test_count_tokens_approximate_with_tool_calls(self):
        """Test approximate counting includes tool calls."""
        messages = [
            AIMessage(
                content="Calling tool",
                tool_calls=[{"id": "call_1", "name": "read_file", "args": {"path": "test.txt"}}],
            ),
        ]

        count = count_tokens_approximate(messages)
        # Should include both content and tool calls
        assert count > 0

    def test_count_tokens_approximate_empty(self):
        """Test counting empty message list."""
        messages = []
        count = count_tokens_approximate(messages)
        assert count == 0

    @pytest.mark.skipif(not TIKTOKEN_AVAILABLE, reason="tiktoken not installed")
    def test_count_tokens_tiktoken(self):
        """Test tiktoken-based counting."""
        messages = [
            HumanMessage(content="Hello world"),
            AIMessage(content="Hi there!"),
        ]

        count = count_tokens_tiktoken(messages, model="gpt-4")
        # tiktoken should give a reasonable count
        assert count > 0
        assert count < 100  # Sanity check

    def test_get_token_counter(self):
        """Test getting appropriate token counter."""
        counter = get_token_counter("gpt-4")
        assert callable(counter)

        messages = [HumanMessage(content="Test message")]
        count = counter(messages)
        assert count > 0


class TestContextManager:
    """Tests for ContextManager class."""

    @pytest.fixture
    def context_manager(self):
        """Create a context manager for testing."""
        config = ContextConfig(
            compaction_threshold_tokens=1000,
            keep_recent_tool_results=3,
            max_tool_result_length=100,
        )
        return ContextManager(config=config)

    @pytest.fixture
    def sample_messages(self):
        """Create sample messages for testing."""
        return [
            SystemMessage(content="You are an agent."),
            HumanMessage(content="Process this document."),
            AIMessage(content="I'll read the file.", tool_calls=[
                {"id": "call_1", "name": "read_file", "args": {"path": "doc.txt"}}
            ]),
            ToolMessage(content="File contents: " + "x" * 200, tool_call_id="call_1"),
            AIMessage(content="I found some data."),
            AIMessage(content="Writing results.", tool_calls=[
                {"id": "call_2", "name": "write_file", "args": {"path": "out.txt"}}
            ]),
            ToolMessage(content="File written successfully", tool_call_id="call_2"),
        ]

    def test_get_token_count(self, context_manager, sample_messages):
        """Test token counting through context manager."""
        count = context_manager.get_token_count(sample_messages)
        assert count > 0
        assert context_manager.state.current_token_count == count

    def test_should_compact(self, context_manager):
        """Test compaction threshold check."""
        # Small message list - shouldn't trigger compaction
        small_messages = [HumanMessage(content="Hello")]
        assert not context_manager.should_compact(small_messages)

        # Large message list - should trigger compaction
        large_content = "x" * 10000  # Way more than threshold
        large_messages = [HumanMessage(content=large_content)]
        assert context_manager.should_compact(large_messages)

    def test_should_summarize(self, context_manager):
        """Test summarization threshold check."""
        # Update config for easier testing
        context_manager.config.summarization_threshold_tokens = 500

        small_messages = [HumanMessage(content="Hello")]
        assert not context_manager.should_summarize(small_messages)

        large_content = "x" * 5000
        large_messages = [HumanMessage(content=large_content)]
        assert context_manager.should_summarize(large_messages)

    def test_clear_old_tool_results(self, context_manager, sample_messages):
        """Test clearing old tool results."""
        # We have 2 tool messages, keep_recent=3, so nothing should be cleared
        result = context_manager.clear_old_tool_results(sample_messages)
        assert len(result) == len(sample_messages)

        # Now with keep_recent=1, the first tool message should be cleared
        result = context_manager.clear_old_tool_results(sample_messages, keep_recent=1)

        # Find tool messages in result
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 2

        # First one should be placeholder, second should be original
        assert "[Result processed" in tool_msgs[0].content
        assert "File written successfully" in tool_msgs[1].content

    def test_truncate_long_tool_results(self, context_manager, sample_messages):
        """Test truncating long tool results."""
        result = context_manager.truncate_long_tool_results(
            sample_messages,
            max_length=50,
            keep_recent=1,
        )

        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        # First tool message was long (200+ chars), should be truncated
        assert "[TRUNCATED" in tool_msgs[0].content
        # Second is recent and short, should be untouched
        assert "File written successfully" in tool_msgs[1].content

    def test_prepare_messages_for_llm_normal(self, context_manager, sample_messages):
        """Test message preparation in normal mode."""
        result = context_manager.prepare_messages_for_llm(
            sample_messages,
            aggressive=False,
        )
        # Should apply truncation but not aggressive clearing
        assert len(result) == len(sample_messages)

    def test_prepare_messages_for_llm_aggressive(self, context_manager, sample_messages):
        """Test message preparation in aggressive mode."""
        result = context_manager.prepare_messages_for_llm(
            sample_messages,
            aggressive=True,
        )
        # Should apply both clearing and truncation
        assert len(result) == len(sample_messages)
        # Check that old tool results were cleared
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        # With keep_recent=3 and only 2 tool messages, nothing should be cleared
        assert len(tool_msgs) == 2

    def test_trim_messages(self, context_manager):
        """Test message trimming."""
        messages = [
            SystemMessage(content="System"),
            HumanMessage(content="Original task"),
            AIMessage(content="Response 1"),
            AIMessage(content="Response 2"),
            AIMessage(content="Response 3"),
            AIMessage(content="Response 4"),
            AIMessage(content="Response 5"),
            AIMessage(content="Response 6"),
        ]

        result = context_manager.trim_messages(messages, keep_recent=3)

        # Should keep: system, first human, last 3 messages
        assert isinstance(result[0], SystemMessage)
        assert isinstance(result[1], HumanMessage)
        assert len(result) == 5  # system + first human + 3 recent

    def test_trim_messages_preserves_first_human(self, context_manager):
        """Test that trimming preserves the original task."""
        messages = [
            SystemMessage(content="System"),
            HumanMessage(content="IMPORTANT: Original task"),
            AIMessage(content="Working..."),
            HumanMessage(content="Follow up"),
            AIMessage(content="More work..."),
            HumanMessage(content="Another follow up"),
            AIMessage(content="Response"),
        ]

        result = context_manager.trim_messages(messages, keep_recent=2)

        # Check first human message is preserved
        human_msgs = [m for m in result if isinstance(m, HumanMessage)]
        assert "IMPORTANT: Original task" in human_msgs[0].content

    def test_create_pre_model_hook(self, context_manager, sample_messages):
        """Test pre-model hook creation."""
        hook = context_manager.create_pre_model_hook()
        assert callable(hook)

        state = {"messages": sample_messages}
        result = hook(state)

        assert "llm_input_messages" in result
        assert len(result["llm_input_messages"]) > 0


class TestContextManagerSummarization:
    """Tests for summarization functionality."""

    @pytest.fixture
    def context_manager(self):
        """Create a context manager for testing."""
        return ContextManager(config=ContextConfig())

    @pytest.fixture
    def mock_llm(self):
        """Create a mock LLM for testing."""
        mock = AsyncMock()
        mock.ainvoke.return_value = AIMessage(content="Summary: Tasks completed successfully.")
        return mock

    @pytest.mark.asyncio
    async def test_summarize_conversation(self, context_manager, mock_llm):
        """Test conversation summarization."""
        messages = [
            HumanMessage(content="Process document"),
            AIMessage(content="I'll analyze it"),
            ToolMessage(content="Analysis complete", tool_call_id="1"),
            AIMessage(content="Found 5 requirements"),
        ]

        summary = await context_manager.summarize_conversation(messages, mock_llm)

        assert "Summary" in summary
        assert context_manager.state.total_summarizations == 1
        assert len(context_manager.state.summaries) == 1
        mock_llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_summarize_and_compact(self, context_manager, mock_llm):
        """Test summarization with compaction."""
        # Create enough messages to trigger compaction
        messages = [SystemMessage(content="System")]
        for i in range(30):
            messages.append(AIMessage(content=f"Response {i}"))

        context_manager.config.keep_recent_messages = 5

        result = await context_manager.summarize_and_compact(messages, mock_llm)

        # Should have: system + summary + 5 recent
        assert len(result) < len(messages)
        # Check for summary message
        summary_msgs = [m for m in result if isinstance(m, SystemMessage) and "Summary" in m.content]
        assert len(summary_msgs) == 1

    @pytest.mark.asyncio
    async def test_summarize_handles_error(self, context_manager):
        """Test summarization error handling."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke.side_effect = Exception("LLM error")

        messages = [HumanMessage(content="Test")]
        summary = await context_manager.summarize_conversation(messages, mock_llm)

        assert "failed" in summary.lower()


class TestToolRetryManager:
    """Tests for ToolRetryManager class."""

    @pytest.fixture
    def retry_manager(self):
        """Create a retry manager for testing."""
        return ToolRetryManager(max_retries=3, base_delay=1.0, max_delay=10.0)

    def test_default_values(self, retry_manager):
        """Test default retry manager values."""
        assert retry_manager.max_retries == 3
        assert retry_manager.base_delay == 1.0
        assert retry_manager.max_delay == 10.0

    def test_should_retry(self, retry_manager):
        """Test retry decision logic."""
        assert retry_manager.should_retry("read_file", 0)
        assert retry_manager.should_retry("read_file", 1)
        assert retry_manager.should_retry("read_file", 2)
        assert not retry_manager.should_retry("read_file", 3)

    def test_get_retry_delay(self, retry_manager):
        """Test exponential backoff calculation."""
        delay_0 = retry_manager.get_retry_delay(0)
        delay_1 = retry_manager.get_retry_delay(1)
        delay_2 = retry_manager.get_retry_delay(2)

        # Each delay should be roughly double the previous (with some jitter)
        assert delay_0 >= 1.0 and delay_0 <= 1.2  # Base delay with up to 10% jitter
        assert delay_1 >= 2.0 and delay_1 <= 2.4  # 2x with jitter
        assert delay_2 >= 4.0 and delay_2 <= 4.8  # 4x with jitter

    def test_get_retry_delay_respects_max(self, retry_manager):
        """Test that delay respects maximum."""
        delay = retry_manager.get_retry_delay(10)  # Would be 1024x base
        assert delay <= retry_manager.max_delay * 1.1  # Allow for jitter

    def test_record_failure(self, retry_manager):
        """Test failure recording."""
        count1 = retry_manager.record_failure("read_file")
        count2 = retry_manager.record_failure("read_file")
        count3 = retry_manager.record_failure("write_file")

        assert count1 == 1
        assert count2 == 2
        assert count3 == 1

    def test_record_retry(self, retry_manager):
        """Test retry recording."""
        retry_manager.record_retry()
        retry_manager.record_retry()

        assert retry_manager._total_retries == 2

    def test_get_stats(self, retry_manager):
        """Test statistics retrieval."""
        retry_manager.record_failure("read_file")
        retry_manager.record_failure("read_file")
        retry_manager.record_failure("write_file")
        retry_manager.record_retry()
        retry_manager.record_retry()

        stats = retry_manager.get_stats()

        assert stats["total_retries"] == 2
        assert stats["failure_counts"]["read_file"] == 2
        assert stats["failure_counts"]["write_file"] == 1


class TestWriteErrorToWorkspace:
    """Tests for write_error_to_workspace function."""

    @pytest.fixture
    def mock_workspace(self):
        """Create a mock workspace manager."""
        mock = AsyncMock()
        mock.write_file = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_write_error_basic(self, mock_workspace):
        """Test basic error writing."""
        error = {
            "message": "Something went wrong",
            "type": "test_error",
            "recoverable": False,
        }

        path = await write_error_to_workspace(mock_workspace, error)

        assert mock_workspace.write_file.called
        call_args = mock_workspace.write_file.call_args
        assert "output/error_" in call_args[0][0]
        content = call_args[0][1]
        assert "Something went wrong" in content
        assert "test_error" in content

    @pytest.mark.asyncio
    async def test_write_error_with_context(self, mock_workspace):
        """Test error writing with additional context."""
        error = {"message": "Error", "type": "test"}
        context = {"job_id": "abc123", "iteration": 42}

        await write_error_to_workspace(mock_workspace, error, context)

        content = mock_workspace.write_file.call_args[0][1]
        assert "abc123" in content
        assert "42" in content

    @pytest.mark.asyncio
    async def test_write_error_handles_failure(self, mock_workspace):
        """Test handling of write failures."""
        mock_workspace.write_file.side_effect = Exception("Write failed")

        error = {"message": "Error", "type": "test"}
        path = await write_error_to_workspace(mock_workspace, error)

        # Should return empty string on failure
        assert path == ""


class TestStateIntegration:
    """Tests for state field integration."""

    def test_state_fields_exist(self):
        """Test that new state fields are defined."""
        from src.agent.state import UniversalAgentState, create_initial_state

        state = create_initial_state(
            job_id="test-job",
            workspace_path="/workspace/test",
        )

        # Check new fields exist
        assert "context_stats" in state
        assert "tool_retry_state" in state
        assert state["context_stats"] is None  # Default value
        assert state["tool_retry_state"] is None  # Default value


class TestLoaderIntegration:
    """Tests for loader function integration."""

    def test_load_summarization_prompt_exists(self):
        """Test that load_summarization_prompt function exists."""
        from src.agent.loader import load_summarization_prompt

        prompt = load_summarization_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0
        # Should contain key sections
        assert "summariz" in prompt.lower() or "conversation" in prompt.lower()

    def test_get_all_tool_names_exists(self):
        """Test that get_all_tool_names function exists."""
        from src.agent.loader import get_all_tool_names, AgentConfig
        from src.agent.loader import ToolsConfig

        config = AgentConfig(
            agent_id="test",
            display_name="Test Agent",
            tools=ToolsConfig(
                workspace=["read_file"],
                todo=["add_todo"],
                domain=["web_search"],
            ),
        )

        names = get_all_tool_names(config)
        assert "read_file" in names
        assert "add_todo" in names
        assert "web_search" in names


class TestPackageExports:
    """Tests for package-level exports."""

    def test_context_exports(self):
        """Test that context management classes are exported."""
        from src.agent import (
            ContextConfig,
            ContextManager,
            ContextManagementState,
            ToolRetryManager,
            count_tokens_tiktoken,
            count_tokens_approximate,
            get_token_counter,
            write_error_to_workspace,
        )

        assert ContextConfig is not None
        assert ContextManager is not None
        assert ContextManagementState is not None
        assert ToolRetryManager is not None

    def test_graph_exports(self):
        """Test that graph functions are exported."""
        from src.agent import (
            build_agent_graph,
            run_graph_with_streaming,
            run_graph_with_summarization,
        )

        assert callable(build_agent_graph)
        assert callable(run_graph_with_streaming)
        assert callable(run_graph_with_summarization)

    def test_loader_exports(self):
        """Test that loader functions are exported."""
        from src.agent import (
            load_summarization_prompt,
            get_all_tool_names,
        )

        assert callable(load_summarization_prompt)
        assert callable(get_all_tool_names)


class TestProtectedContextConfig:
    """Tests for ProtectedContextConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        from src.agent.context_manager import ProtectedContextConfig

        config = ProtectedContextConfig()
        assert config.enabled is True
        assert config.plan_file == "main_plan.md"
        assert config.max_plan_chars == 2000
        assert config.include_todos is True

    def test_custom_values(self):
        """Test custom configuration values."""
        from src.agent.context_manager import ProtectedContextConfig

        config = ProtectedContextConfig(
            enabled=False,
            plan_file="custom/plan.md",
            max_plan_chars=1000,
            include_todos=False,
        )
        assert config.enabled is False
        assert config.plan_file == "custom/plan.md"
        assert config.max_plan_chars == 1000
        assert config.include_todos is False


class TestProtectedContextProvider:
    """Tests for ProtectedContextProvider class."""

    def test_disabled_returns_none(self):
        """Test that disabled provider returns None."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider

        config = ProtectedContextConfig(enabled=False)
        provider = ProtectedContextProvider(config=config)

        result = provider.get_protected_context()
        assert result is None

    def test_no_managers_returns_none(self):
        """Test that provider with no managers returns None."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider

        config = ProtectedContextConfig(enabled=True)
        provider = ProtectedContextProvider(config=config)

        result = provider.get_protected_context()
        assert result is None

    def test_includes_plan_content(self):
        """Test that plan content is included."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider

        mock_workspace = MagicMock()
        mock_workspace.read_file.return_value = "# My Plan\n\nStep 1: Do something"

        config = ProtectedContextConfig(enabled=True, include_todos=False)
        provider = ProtectedContextProvider(
            workspace_manager=mock_workspace,
            config=config,
        )

        result = provider.get_protected_context()

        assert result is not None
        assert "[PROTECTED CONTEXT" in result
        assert "My Plan" in result
        assert "Step 1" in result
        mock_workspace.read_file.assert_called_once_with("main_plan.md")

    def test_plan_truncation(self):
        """Test that long plans are truncated."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider

        long_plan = "A" * 3000
        mock_workspace = MagicMock()
        mock_workspace.read_file.return_value = long_plan

        config = ProtectedContextConfig(enabled=True, max_plan_chars=100, include_todos=False)
        provider = ProtectedContextProvider(
            workspace_manager=mock_workspace,
            config=config,
        )

        result = provider.get_protected_context()

        assert result is not None
        assert "[...truncated]" in result
        # Should have max_plan_chars of content plus truncation marker
        assert len(result) < 3000

    def test_includes_todos(self):
        """Test that todos are included."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider
        from src.agent.todo_manager import TodoManager

        # Create a real todo manager with some todos
        todo_manager = TodoManager(auto_reflection=False)
        todo_manager.set_todos_from_list([
            {"content": "Task 1", "status": "pending"},
            {"content": "Task 2", "status": "in_progress"},
            {"content": "Task 3", "status": "completed"},
        ])

        config = ProtectedContextConfig(enabled=True, include_todos=True)
        provider = ProtectedContextProvider(
            todo_manager=todo_manager,
            config=config,
        )

        result = provider.get_protected_context()

        assert result is not None
        assert "Active Todos" in result
        assert "Task 1" in result
        assert "Task 2" in result
        assert "Task 3" in result
        # Check status markers
        assert "[ ]" in result  # pending
        assert "[>]" in result  # in_progress
        assert "[x]" in result  # completed

    def test_handles_missing_plan_file(self):
        """Test graceful handling of missing plan file."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider
        from src.agent.todo_manager import TodoManager

        mock_workspace = MagicMock()
        mock_workspace.read_file.side_effect = FileNotFoundError("Not found")

        todo_manager = TodoManager(auto_reflection=False)
        todo_manager.set_todos_from_list([{"content": "Task", "status": "pending"}])

        config = ProtectedContextConfig(enabled=True, include_todos=True)
        provider = ProtectedContextProvider(
            workspace_manager=mock_workspace,
            todo_manager=todo_manager,
            config=config,
        )

        # Should not raise, should still include todos
        result = provider.get_protected_context()
        assert result is not None
        assert "Task" in result

    def test_full_protected_context_format(self):
        """Test complete protected context format."""
        from src.agent.context_manager import ProtectedContextConfig, ProtectedContextProvider
        from src.agent.todo_manager import TodoManager

        mock_workspace = MagicMock()
        mock_workspace.read_file.return_value = "# Phase 2 Plan\nExtract requirements"

        todo_manager = TodoManager(auto_reflection=False)
        todo_manager.set_todos_from_list([
            {"content": "Extract chunk 5", "status": "in_progress"},
            {"content": "Extract chunk 6", "status": "pending"},
        ])

        config = ProtectedContextConfig(enabled=True, include_todos=True)
        provider = ProtectedContextProvider(
            workspace_manager=mock_workspace,
            todo_manager=todo_manager,
            config=config,
        )

        result = provider.get_protected_context()

        assert "[PROTECTED CONTEXT - Current State]" in result
        assert "## Current Plan" in result
        assert "## Active Todos" in result
        assert "[END PROTECTED CONTEXT]" in result


class TestContextManagerWithProtectedProvider:
    """Tests for ContextManager with protected context provider."""

    def test_set_protected_provider(self):
        """Test setting protected provider."""
        from src.agent.context_manager import ProtectedContextProvider

        context_manager = ContextManager()
        provider = ProtectedContextProvider()

        context_manager.set_protected_provider(provider)

        assert context_manager._protected_provider is provider

    def test_protected_provider_initially_none(self):
        """Test that protected provider is initially None."""
        context_manager = ContextManager()
        assert context_manager._protected_provider is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
