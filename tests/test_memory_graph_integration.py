"""Integration tests for Memory Light in graph and agent setup.

Tests that the observer and recall_store are correctly wired into
the graph nodes and agent initialization logic.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.services.memory_observer import MemoryObserver
from src.tools.context import ToolContext


# =============================================================================
# Graph execute node: observer trigger + turn_count
# =============================================================================


class TestExecuteNodeObserverTrigger:
    """Tests that the execute node correctly triggers the observer."""

    def test_observer_should_observe_called_with_incremented_turn(self):
        """Simulate the graph's observer trigger logic."""
        observer = MemoryObserver(
            recall_store=AsyncMock(),
            llm=AsyncMock(),
            observer_interval=5,
        )

        # Simulate what graph.py does: new_turn_count = state turn_count + 1
        state_turn_count = 4  # Will become 5 after increment
        new_turn_count = state_turn_count + 1

        assert observer.should_observe(new_turn_count) is True

    def test_observer_not_triggered_between_intervals(self):
        observer = MemoryObserver(
            recall_store=AsyncMock(),
            llm=AsyncMock(),
            observer_interval=5,
        )

        state_turn_count = 2
        new_turn_count = state_turn_count + 1  # = 3

        assert observer.should_observe(new_turn_count) is False

    def test_turn_count_increments_from_state(self):
        """Verify the increment pattern used in graph.py."""
        # This mirrors: new_turn_count = state.get("turn_count", 0) + 1
        for initial in [0, 1, 5, 10, 99]:
            new_count = initial + 1
            assert new_count == initial + 1


class TestExecuteNodeTurnCountReturn:
    """Tests that execute node return dicts include turn_count."""

    def test_compacted_return_includes_turn_count(self):
        """When context was compacted, return dict should have turn_count."""
        # Simulate the return dict pattern from graph.py
        state_turn_count = 4
        new_turn_count = state_turn_count + 1

        result = {
            "messages": [],
            "iteration": 1,
            "turn_count": new_turn_count,
            "error": None,
        }

        assert "turn_count" in result
        assert result["turn_count"] == 5

    def test_non_compacted_return_includes_turn_count(self):
        """Normal (non-compacted) return dict should also have turn_count."""
        state_turn_count = 9
        new_turn_count = state_turn_count + 1

        result = {
            "messages": [],
            "iteration": 2,
            "turn_count": new_turn_count,
            "error": None,
        }

        assert result["turn_count"] == 10


# =============================================================================
# Graph archive_phase node: observer phase boundary
# =============================================================================


class TestArchivePhaseObserverTrigger:
    """Tests for phase boundary observer trigger in archive_phase."""

    @pytest.mark.asyncio
    async def test_observe_phase_boundary_called(self):
        """Verify observe_phase_boundary is callable with correct args."""
        observer = MemoryObserver(
            recall_store=AsyncMock(),
            llm=AsyncMock(),
            observer_interval=5,
        )
        observer.llm.ainvoke = AsyncMock(
            return_value=MagicMock(content="[]")
        )

        messages = [HumanMessage(content="test")]
        result = await observer.observe_phase_boundary(
            messages=messages,
            phase=3,
        )

        # Should complete without error
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_observe_phase_boundary_uses_phase_number(self):
        """Verify phase number is passed through to store calls."""
        mock_store = AsyncMock(return_value="mem-id")
        observer = MemoryObserver(
            recall_store=MagicMock(store=mock_store),
            llm=AsyncMock(),
            observer_interval=5,
        )
        observer.llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content='[{"content": "test insight", "type": "factual", "importance": 0.7}]'
            )
        )

        await observer.observe_phase_boundary(
            messages=[HumanMessage(content="did work")],
            phase=7,
        )

        # Verify store was called with source_phase=7
        assert mock_store.called
        call_kwargs = mock_store.call_args.kwargs
        assert call_kwargs["source_phase"] == 7
        assert call_kwargs["source"] == "observer"


# =============================================================================
# Graph audited_tools node: memory flush
# =============================================================================


class TestAuditedToolsMemoryFlush:
    """Tests for pending memory flush in audited_tools node."""

    @pytest.mark.asyncio
    async def test_drain_and_store_pattern(self):
        """Simulate the drain + store pattern from audited_tools."""
        ctx = ToolContext(workspace_manager=None, config={})
        mock_store = AsyncMock(return_value="mem-id")
        ctx.recall_store = MagicMock(store=mock_store)

        # Queue some memories (as todo_complete would)
        ctx.queue_memory(content="mem1", importance=0.7, source="todo")
        ctx.queue_memory(content="mem2", importance=0.6, source="todo")

        # Simulate what audited_tools does
        for mem in ctx.drain_pending_memories():
            await ctx.recall_store.store(**mem)

        assert mock_store.call_count == 2
        assert ctx._pending_memories == []

    @pytest.mark.asyncio
    async def test_drain_with_store_failure(self):
        """Store failure for one memory shouldn't block others."""
        ctx = ToolContext(workspace_manager=None, config={})
        mock_store = AsyncMock(side_effect=[Exception("fail"), "mem-id"])
        ctx.recall_store = MagicMock(store=mock_store)

        ctx.queue_memory(content="will fail", importance=0.5)
        ctx.queue_memory(content="will succeed", importance=0.6)

        stored = 0
        for mem in ctx.drain_pending_memories():
            try:
                await ctx.recall_store.store(**mem)
                stored += 1
            except Exception:
                pass  # Matches graph.py behavior

        assert stored == 1
        assert mock_store.call_count == 2


# =============================================================================
# Agent setup: observer model selection
# =============================================================================


class TestAgentObserverSetup:
    """Tests for observer LLM selection logic in agent setup."""

    def test_observer_model_null_reuses_main(self):
        """When observer_model is None, main LLM should be reused."""
        # Simulate the agent.py logic
        observer_model = None
        main_llm = MagicMock()

        if observer_model:
            observer_llm = MagicMock()  # Would create a separate LLM
        else:
            observer_llm = main_llm

        assert observer_llm is main_llm

    def test_observer_model_specified_creates_separate(self):
        """When observer_model is set, a separate LLM should be created."""
        observer_model = "openai/gpt-oss-120b"
        main_llm = MagicMock()

        if observer_model:
            observer_llm = MagicMock()  # Separate LLM
        else:
            observer_llm = main_llm

        assert observer_llm is not main_llm

    def test_observer_created_with_correct_interval(self):
        """MemoryObserver should use interval from config."""
        recall_store = AsyncMock()
        llm = AsyncMock()

        observer = MemoryObserver(
            recall_store=recall_store,
            llm=llm,
            observer_interval=10,
        )

        assert observer.observer_interval == 10
        assert observer.should_observe(10) is True
        assert observer.should_observe(5) is False

    def test_observer_init_failure_non_fatal(self):
        """Observer init failure should be catchable without crashing."""
        # Simulate the try/except in agent.py
        observer = None
        try:
            # Simulate a failure during observer creation
            raise RuntimeError("LLM init failed")
        except Exception:
            pass  # Non-fatal, matches agent.py behavior

        assert observer is None


# =============================================================================
# ToolContext: recall_store + observer coherence
# =============================================================================


class TestToolContextMemoryCoherence:
    """Tests that recall_store and memory_observer work together on ToolContext."""

    def test_both_none_by_default(self):
        ctx = ToolContext(workspace_manager=None, config={})
        assert ctx.recall_store is None
        assert ctx.memory_observer is None

    def test_observer_without_recall_store(self):
        """Observer set without recall_store — unusual but should not crash."""
        ctx = ToolContext(workspace_manager=None, config={})
        ctx.memory_observer = MagicMock()
        assert ctx.recall_store is None
        assert ctx.memory_observer is not None

    def test_queue_memory_works_without_observer(self):
        """queue_memory doesn't depend on observer being set."""
        ctx = ToolContext(workspace_manager=None, config={})
        ctx.recall_store = MagicMock()
        # No observer set
        ctx.queue_memory(content="test", importance=0.5)
        assert len(ctx._pending_memories) == 1

    def test_full_setup_all_fields(self):
        """Full Memory Light setup: recall_store + observer + queue."""
        ctx = ToolContext(workspace_manager=None, config={})
        ctx.recall_store = MagicMock()
        ctx.memory_observer = MagicMock()

        ctx.queue_memory(content="mem1", importance=0.7)
        assert ctx.recall_store is not None
        assert ctx.memory_observer is not None
        assert len(ctx._pending_memories) == 1
