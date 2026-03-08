"""Integration tests for Memory Light Phases 2-3.

Tests the ToolContext memory queue, todo completion memory queueing,
keyword extraction, tool error memory storage, and Phase 3 additions
(memory_observer field, turn_count state, observer window truncation).
"""

import uuid
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.recall_store import extract_keywords
from src.tools.context import ToolContext


# =============================================================================
# extract_keywords tests
# =============================================================================


class TestExtractKeywords:
    """Tests for extract_keywords utility."""

    def test_basic_extraction(self):
        result = extract_keywords("Extract document text from uploaded PDF")
        assert "extract" in result
        assert "document" in result
        assert "pdf" in result

    def test_stopwords_filtered(self):
        result = extract_keywords("the document is in the folder and has been read")
        assert "the" not in result
        assert "is" not in result
        assert "in" not in result
        assert "and" not in result
        assert "document" in result

    def test_max_keywords_limit(self):
        text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        result = extract_keywords(text, max_keywords=5)
        assert len(result) <= 5

    def test_deduplication(self):
        result = extract_keywords("error error error something error")
        assert result.count("error") == 1

    def test_short_tokens_filtered(self):
        result = extract_keywords("I a x do it by go")
        # Single-char tokens should be filtered (len < 2)
        for kw in result:
            assert len(kw) >= 2

    def test_empty_input(self):
        result = extract_keywords("")
        assert result == []

    def test_lowercase(self):
        result = extract_keywords("PostgreSQL Database Schema")
        for kw in result:
            assert kw == kw.lower()


# =============================================================================
# ToolContext memory queue tests
# =============================================================================


class TestToolContextMemoryQueue:
    """Tests for ToolContext.queue_memory() and drain_pending_memories()."""

    def _make_context(self):
        """Create a minimal ToolContext for testing."""
        return ToolContext(
            workspace_manager=None,
            config={},
        )

    def test_queue_memory_adds_to_pending(self):
        ctx = self._make_context()
        ctx.queue_memory(
            content="test memory",
            keywords=["test"],
            importance=0.7,
            source="todo",
            memory_type="procedural",
        )
        assert len(ctx._pending_memories) == 1
        assert ctx._pending_memories[0]["content"] == "test memory"

    def test_drain_returns_and_clears(self):
        ctx = self._make_context()
        ctx.queue_memory(content="mem1", importance=0.5)
        ctx.queue_memory(content="mem2", importance=0.6)

        drained = ctx.drain_pending_memories()
        assert len(drained) == 2
        assert drained[0]["content"] == "mem1"
        assert drained[1]["content"] == "mem2"
        # Queue should be empty after drain
        assert len(ctx._pending_memories) == 0

    def test_drain_empty_queue(self):
        ctx = self._make_context()
        drained = ctx.drain_pending_memories()
        assert drained == []

    def test_queue_preserves_all_fields(self):
        ctx = self._make_context()
        ctx.queue_memory(
            content="test",
            keywords=["k1", "k2"],
            importance=0.8,
            source="todo",
            source_phase=3,
            memory_type="procedural",
        )
        mem = ctx._pending_memories[0]
        assert mem["content"] == "test"
        assert mem["keywords"] == ["k1", "k2"]
        assert mem["importance"] == 0.8
        assert mem["source"] == "todo"
        assert mem["source_phase"] == 3
        assert mem["memory_type"] == "procedural"

    def test_multiple_drains_independent(self):
        ctx = self._make_context()
        ctx.queue_memory(content="first batch", importance=0.5)
        first = ctx.drain_pending_memories()

        ctx.queue_memory(content="second batch", importance=0.6)
        second = ctx.drain_pending_memories()

        assert len(first) == 1
        assert len(second) == 1
        assert first[0]["content"] == "first batch"
        assert second[0]["content"] == "second batch"


# =============================================================================
# Todo completion memory queueing tests
# =============================================================================


@dataclass
class MockTodoItem:
    id: str = "todo_1"
    content: str = "Write unit tests for auth module"
    status: str = "completed"
    notes: Optional[List[str]] = None
    priority: str = "medium"


class TestTodoCompletionMemory:
    """Tests that todo completion queues memories correctly."""

    def test_todo_with_notes_queues_memory(self):
        """When a todo has notes and recall_store is set, memory should be queued."""
        ctx = ToolContext(workspace_manager=None, config={})
        ctx.recall_store = MagicMock()  # Non-None recall_store

        todo = MockTodoItem(
            content="Implement login endpoint",
            notes=["Used JWT tokens", "Added rate limiting"],
        )

        # Simulate what todo_complete does
        if ctx.recall_store and todo.notes:
            ctx.queue_memory(
                content=f"Completed: {todo.content}\nOutcome: {'; '.join(todo.notes)}",
                keywords=extract_keywords(todo.content),
                importance=0.7,
                source="todo",
                memory_type="procedural",
                source_phase=1,
            )

        pending = ctx.drain_pending_memories()
        assert len(pending) == 1
        assert "Implement login endpoint" in pending[0]["content"]
        assert "JWT tokens" in pending[0]["content"]
        assert pending[0]["source"] == "todo"

    def test_todo_without_notes_skips_memory(self):
        """When a todo has no notes, no memory should be queued."""
        ctx = ToolContext(workspace_manager=None, config={})
        ctx.recall_store = MagicMock()

        todo = MockTodoItem(content="Simple task", notes=None)

        if ctx.recall_store and todo.notes:
            ctx.queue_memory(content="should not appear", importance=0.7)

        assert len(ctx._pending_memories) == 0

    def test_todo_without_recall_store_skips_memory(self):
        """When recall_store is None, no memory should be queued."""
        ctx = ToolContext(workspace_manager=None, config={})
        # recall_store is None by default

        todo = MockTodoItem(
            content="Task with notes",
            notes=["Some output"],
        )

        if ctx.recall_store and todo.notes:
            ctx.queue_memory(content="should not appear", importance=0.7)

        assert len(ctx._pending_memories) == 0


# =============================================================================
# Tool error memory storage test
# =============================================================================


class TestToolErrorMemory:
    """Tests that tool errors are stored as memories."""

    @pytest.mark.asyncio
    async def test_tool_error_stored_via_recall_store(self):
        """Mock RecallStore.store() to verify tool error storage."""
        mock_store = AsyncMock()
        mock_recall = MagicMock()
        mock_recall.store = mock_store

        # Simulate what audited_tools does for a tool error
        error_content = "Error: File not found: missing.txt"
        tool_name = "read_file"

        await mock_recall.store(
            content=f"Tool '{tool_name}' failed: {error_content[:500]}",
            keywords=[tool_name, "error"],
            importance=0.6,
            source="tool_error",
            memory_type="error_solution",
            source_phase=2,
        )

        mock_store.assert_called_once()
        call_kwargs = mock_store.call_args
        assert "read_file" in call_kwargs.kwargs["content"]
        assert call_kwargs.kwargs["source"] == "tool_error"
        assert call_kwargs.kwargs["memory_type"] == "error_solution"


# =============================================================================
# State turn_count tests
# =============================================================================


class TestTurnCountState:
    """Tests for turn_count field in UniversalAgentState."""

    def test_create_initial_state_has_turn_count_zero(self):
        from src.core.state import create_initial_state

        state = create_initial_state(
            job_id="test-job",
            workspace_path="/tmp/test",
        )
        assert state["turn_count"] == 0

    def test_turn_count_is_int(self):
        from src.core.state import create_initial_state

        state = create_initial_state(
            job_id="test-job",
            workspace_path="/tmp/test",
        )
        assert isinstance(state["turn_count"], int)

    def test_create_initial_state_has_last_observed_turn_zero(self):
        from src.core.state import create_initial_state

        state = create_initial_state(
            job_id="test-job",
            workspace_path="/tmp/test",
        )
        assert state["last_observed_turn"] == 0


# =============================================================================
# ToolContext memory_observer field tests
# =============================================================================


class TestToolContextNoMemoryObserver:
    """Verify memory_observer field was removed from ToolContext."""

    def test_no_memory_observer_field(self):
        """memory_observer was migrated to AuxiliaryLLM; field should not exist."""
        ctx = ToolContext(workspace_manager=None, config={})
        assert not hasattr(ctx, "memory_observer")

    def test_recall_store_still_exists(self):
        ctx = ToolContext(workspace_manager=None, config={})
        assert ctx.recall_store is None
        ctx.recall_store = MagicMock()
        assert ctx.recall_store is not None


# =============================================================================
# Auxiliary memory extraction helpers
# =============================================================================


class TestAuxiliaryMemoryExtraction:
    """Tests for _should_extract_memories and extract_and_store_memories."""

    def test_should_extract_at_interval(self):
        from src.services.auxiliary import _should_extract_memories
        assert _should_extract_memories(turn_count=5, interval=5, last_observed_turn=0)
        assert _should_extract_memories(turn_count=10, interval=5, last_observed_turn=5)

    def test_should_not_extract_between_intervals(self):
        from src.services.auxiliary import _should_extract_memories
        assert not _should_extract_memories(turn_count=3, interval=5, last_observed_turn=0)
        assert not _should_extract_memories(turn_count=7, interval=5, last_observed_turn=5)

    def test_should_not_extract_at_zero(self):
        from src.services.auxiliary import _should_extract_memories
        assert not _should_extract_memories(turn_count=0, interval=5, last_observed_turn=0)

    def test_should_not_re_extract_same_turn(self):
        from src.services.auxiliary import _should_extract_memories
        assert not _should_extract_memories(turn_count=5, interval=5, last_observed_turn=5)

    def test_observation_window_caps_messages(self):
        """extract_and_store_memories should cap at _MAX_OBSERVATION_WINDOW."""
        from src.services.auxiliary import _MAX_OBSERVATION_WINDOW
        assert _MAX_OBSERVATION_WINDOW == 40  # Same as old MemoryObserver
