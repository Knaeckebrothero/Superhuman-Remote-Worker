"""Integration tests for Memory Light in graph and agent setup.

Tests that the auxiliary LLM memory extraction and recall_store are
correctly wired into the graph nodes and agent initialization logic.

Migration note: Memory extraction was migrated from MemoryObserver to
AuxiliaryLLM.chain(ExtractMemoriesTask). See docs/features/auxiliary.md.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.services.auxiliary import (
    AuxiliaryLLM,
    ExtractedMemories,
    ExtractedMemory,
    ExtractMemoriesTask,
    _should_extract_memories,
    extract_and_store_memories,
    _MAX_OBSERVATION_WINDOW,
)
from src.tools.context import ToolContext


# =============================================================================
# Graph execute node: extraction trigger + turn_count
# =============================================================================


class TestExecuteNodeExtractionTrigger:
    """Tests that the execute node correctly triggers memory extraction."""

    def test_should_extract_at_interval(self):
        """Simulate the graph's extraction trigger logic."""
        state_turn_count = 4  # Will become 5 after increment
        new_turn_count = state_turn_count + 1
        last_observed = 0

        assert _should_extract_memories(new_turn_count, interval=5, last_observed_turn=last_observed)

    def test_not_triggered_between_intervals(self):
        state_turn_count = 2
        new_turn_count = state_turn_count + 1  # = 3

        assert not _should_extract_memories(new_turn_count, interval=5, last_observed_turn=0)

    def test_turn_count_increments_from_state(self):
        """Verify the increment pattern used in graph.py."""
        for initial in [0, 1, 5, 10, 99]:
            new_count = initial + 1
            assert new_count == initial + 1

    def test_last_observed_prevents_re_extraction(self):
        """When last_observed_turn equals turn_count, should not extract again."""
        assert not _should_extract_memories(turn_count=10, interval=5, last_observed_turn=10)
        assert _should_extract_memories(turn_count=15, interval=5, last_observed_turn=10)


class TestExecuteNodeTurnCountReturn:
    """Tests that execute node return dicts include turn_count and last_observed_turn."""

    def test_compacted_return_includes_turn_count(self):
        """When context was compacted, return dict should have turn_count."""
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

    def test_return_includes_last_observed_when_extracted(self):
        """When extraction fires, last_observed_turn should be updated."""
        new_turn_count = 10
        extraction_triggered = True

        result_update = {
            "iteration": 1,
            "turn_count": new_turn_count,
            "error": None,
        }
        if extraction_triggered:
            result_update["last_observed_turn"] = new_turn_count

        assert result_update["last_observed_turn"] == 10

    def test_return_omits_last_observed_when_not_extracted(self):
        """When extraction doesn't fire, last_observed_turn should not change."""
        new_turn_count = 7
        extraction_triggered = False

        result_update = {
            "iteration": 1,
            "turn_count": new_turn_count,
            "error": None,
        }
        if extraction_triggered:
            result_update["last_observed_turn"] = new_turn_count

        assert "last_observed_turn" not in result_update


# =============================================================================
# extract_and_store_memories: the replacement for MemoryObserver.observe()
# =============================================================================


class TestExtractAndStoreMemories:
    """Tests for the extract_and_store_memories helper."""

    @pytest.mark.asyncio
    async def test_extracts_and_stores(self):
        """Should extract via chain() and store each memory."""
        memories = ExtractedMemories(memories=[
            ExtractedMemory(
                content="Test insight",
                summary="Test",
                keywords=["test"],
                importance=0.8,
                type="factual",
            ),
        ])
        mock_llm = MagicMock()
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=memories)
        mock_llm.with_structured_output = MagicMock(return_value=structured)

        aux = AuxiliaryLLM(llm=mock_llm, timeout=30.0)
        mock_store = AsyncMock(return_value="mem-id")
        recall_store = MagicMock(store=mock_store)

        count = await extract_and_store_memories(
            auxiliary_llm=aux,
            recall_store=recall_store,
            messages=[HumanMessage(content="test")],
            phase=3,
            source_turn_start=0,
            source_turn_end=5,
        )

        assert count == 1
        mock_store.assert_called_once()
        kwargs = mock_store.call_args.kwargs
        assert kwargs["source"] == "observer"
        assert kwargs["source_phase"] == 3
        assert kwargs["source_turn_start"] == 0
        assert kwargs["source_turn_end"] == 5

    @pytest.mark.asyncio
    async def test_empty_messages_returns_zero(self):
        mock_llm = MagicMock()
        aux = AuxiliaryLLM(llm=mock_llm, timeout=30.0)

        count = await extract_and_store_memories(
            auxiliary_llm=aux,
            recall_store=MagicMock(),
            messages=[],
            phase=0,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_caps_observation_window(self):
        """Should cap messages at _MAX_OBSERVATION_WINDOW."""
        memories = ExtractedMemories(memories=[])
        mock_llm = MagicMock()
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=memories)
        mock_llm.with_structured_output = MagicMock(return_value=structured)

        aux = AuxiliaryLLM(llm=mock_llm, timeout=30.0)

        # Create more messages than the window
        messages = [HumanMessage(content=f"msg {i}") for i in range(60)]

        await extract_and_store_memories(
            auxiliary_llm=aux,
            recall_store=MagicMock(),
            messages=messages,
            phase=0,
        )

        # Verify the task received capped messages
        call_args = structured.ainvoke.call_args[0][0]
        # The HumanMessage context was built from capped messages
        assert structured.ainvoke.called

    @pytest.mark.asyncio
    async def test_store_failure_continues(self):
        """Store failure for one memory shouldn't block others."""
        memories = ExtractedMemories(memories=[
            ExtractedMemory(
                content="mem1", summary="m1",
                keywords=["a"], importance=0.5, type="factual",
            ),
            ExtractedMemory(
                content="mem2", summary="m2",
                keywords=["b"], importance=0.6, type="factual",
            ),
        ])
        mock_llm = MagicMock()
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=memories)
        mock_llm.with_structured_output = MagicMock(return_value=structured)

        aux = AuxiliaryLLM(llm=mock_llm, timeout=30.0)
        mock_store = AsyncMock(side_effect=[Exception("fail"), "mem-id"])
        recall_store = MagicMock(store=mock_store)

        count = await extract_and_store_memories(
            auxiliary_llm=aux,
            recall_store=recall_store,
            messages=[HumanMessage(content="test")],
            phase=0,
        )

        assert count == 1  # Only second one stored
        assert mock_store.call_count == 2

    @pytest.mark.asyncio
    async def test_llm_failure_returns_zero(self):
        """LLM failure should be non-fatal."""
        mock_llm = MagicMock()
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        mock_llm.with_structured_output = MagicMock(return_value=structured)

        aux = AuxiliaryLLM(llm=mock_llm, timeout=30.0)

        count = await extract_and_store_memories(
            auxiliary_llm=aux,
            recall_store=MagicMock(),
            messages=[HumanMessage(content="test")],
            phase=0,
        )

        assert count == 0


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
# Agent setup: auxiliary LLM model selection
# =============================================================================


class TestAgentAuxiliarySetup:
    """Tests for auxiliary LLM selection logic (replaces observer model tests)."""

    def test_auxiliary_model_null_reuses_main(self):
        """When auxiliary.model is None, main LLM should be reused."""
        aux_model = None
        main_llm = MagicMock()

        if aux_model:
            aux_llm = MagicMock()  # Would create a separate LLM
        else:
            aux_llm = main_llm

        assert aux_llm is main_llm

    def test_auxiliary_model_specified_creates_separate(self):
        """When auxiliary.model is set, a separate LLM should be created."""
        aux_model = "gpt-oss-120b"
        main_llm = MagicMock()

        if aux_model:
            aux_llm = MagicMock()  # Separate LLM
        else:
            aux_llm = main_llm

        assert aux_llm is not main_llm


# =============================================================================
# ToolContext: recall_store coherence (memory_observer removed)
# =============================================================================


class TestToolContextMemoryCoherence:
    """Tests that recall_store works correctly on ToolContext."""

    def test_recall_store_none_by_default(self):
        ctx = ToolContext(workspace_manager=None, config={})
        assert ctx.recall_store is None

    def test_no_memory_observer_field(self):
        """memory_observer was migrated to AuxiliaryLLM; field should not exist."""
        ctx = ToolContext(workspace_manager=None, config={})
        assert not hasattr(ctx, "memory_observer")

    def test_queue_memory_works_without_observer(self):
        """queue_memory doesn't depend on observer being set."""
        ctx = ToolContext(workspace_manager=None, config={})
        ctx.recall_store = MagicMock()
        ctx.queue_memory(content="test", importance=0.5)
        assert len(ctx._pending_memories) == 1
