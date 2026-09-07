"""Tests for src/core/knowledge_injection.py.

Covers section 12 of persistent_agent_tests.md:
  12.1 KNOWLEDGE_TOOL_CALL_ID_PREFIX
  12.2 create_knowledge_injection_messages()
  12.3 is_knowledge_injection_message()
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.core.knowledge_injection import (
    KNOWLEDGE_TOOL_CALL_ID_PREFIX,
    create_knowledge_injection_messages,
    is_knowledge_injection_message,
    retrieve_bound_knowledge,
)
from agent.services.knowledge.bindings import KnowledgeBinding
from shared.runtime.services.knowledge_store import KnowledgeRecord, KnowledgeStore


# =============================================================================
# 12.1: KNOWLEDGE_TOOL_CALL_ID_PREFIX
# =============================================================================


class TestPrefix:
    """Tests for the module-level prefix constant."""

    def test_prefix_value(self):
        assert KNOWLEDGE_TOOL_CALL_ID_PREFIX == "knowledge_inject_"


# =============================================================================
# 12.2: create_knowledge_injection_messages()
# =============================================================================


class TestCreateKnowledgeInjectionMessages:
    """Tests for create_knowledge_injection_messages()."""

    def test_returns_tuple(self):
        result = create_knowledge_injection_messages("content")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_ai_and_tool_messages(self):
        ai, tool = create_knowledge_injection_messages("content")
        assert isinstance(ai, AIMessage)
        assert isinstance(tool, ToolMessage)

    def test_ai_message_has_empty_content(self):
        ai, _ = create_knowledge_injection_messages("content")
        assert ai.content == ""

    def test_ai_message_has_kb_search_tool_call(self):
        ai, _ = create_knowledge_injection_messages("content")
        assert len(ai.tool_calls) == 1
        tc = ai.tool_calls[0]
        assert tc["name"] == "kb_search"
        assert tc["args"] == {"query": "current_task_context"}

    def test_tool_call_id_starts_with_prefix(self):
        ai, _ = create_knowledge_injection_messages("content")
        tc_id = ai.tool_calls[0]["id"]
        assert tc_id.startswith(KNOWLEDGE_TOOL_CALL_ID_PREFIX)

    def test_tool_call_id_has_hex_suffix(self):
        ai, _ = create_knowledge_injection_messages("content")
        tc_id = ai.tool_calls[0]["id"]
        suffix = tc_id[len(KNOWLEDGE_TOOL_CALL_ID_PREFIX) :]
        assert len(suffix) == 8
        int(suffix, 16)  # Should not raise — valid hex

    def test_tool_message_content_is_input(self):
        _, tool = create_knowledge_injection_messages("my knowledge block")
        assert tool.content == "my knowledge block"

    def test_tool_call_ids_match(self):
        ai, tool = create_knowledge_injection_messages("content")
        assert tool.tool_call_id == ai.tool_calls[0]["id"]

    def test_each_call_generates_unique_id(self):
        _, tool1 = create_knowledge_injection_messages("a")
        _, tool2 = create_knowledge_injection_messages("b")
        assert tool1.tool_call_id != tool2.tool_call_id


# =============================================================================
# 12.3: is_knowledge_injection_message()
# =============================================================================


class TestIsKnowledgeInjectionMessage:
    """Tests for is_knowledge_injection_message()."""

    def test_true_for_matching_tool_message(self):
        msg = ToolMessage(
            content="knowledge",
            tool_call_id=f"{KNOWLEDGE_TOOL_CALL_ID_PREFIX}abcd1234",
        )
        assert is_knowledge_injection_message(msg) is True

    def test_true_for_matching_ai_message(self):
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kb_search",
                    "args": {},
                    "id": f"{KNOWLEDGE_TOOL_CALL_ID_PREFIX}abcd1234",
                }
            ],
        )
        assert is_knowledge_injection_message(msg) is True

    def test_false_for_ai_with_no_tool_calls(self):
        msg = AIMessage(content="plain response")
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_ai_with_non_matching_tool_calls(self):
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {},
                    "id": "regular_tool_call_123",
                }
            ],
        )
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_non_matching_tool_message(self):
        msg = ToolMessage(
            content="result",
            tool_call_id="regular_tool_call_123",
        )
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_human_message(self):
        msg = HumanMessage(content="hello")
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_system_message(self):
        msg = SystemMessage(content="system prompt")
        assert is_knowledge_injection_message(msg) is False

    def test_roundtrip_with_created_messages(self):
        """Messages created by create_* are detected by is_*."""
        ai, tool = create_knowledge_injection_messages("test")
        assert is_knowledge_injection_message(ai) is True
        assert is_knowledge_injection_message(tool) is True

    def test_handles_missing_tool_call_id(self):
        msg = ToolMessage(content="x", tool_call_id="")
        assert is_knowledge_injection_message(msg) is False

    def test_handles_ai_without_tool_calls_attr(self):
        msg = AIMessage(content="plain")
        # Ensure tool_calls is empty, not missing
        assert is_knowledge_injection_message(msg) is False


def _binding(alias: str, *, native: bool = False) -> KnowledgeBinding:
    return KnowledgeBinding(
        kb_id=uuid.uuid4(),
        alias=alias,
        name=alias.title(),
        kind="native" if native else "datasource",
        writable=native,
        indexed_commit=None if native else f"{alias}-commit",
    )


def _record(binding: KnowledgeBinding, note_id: str) -> KnowledgeRecord:
    return KnowledgeRecord(
        note_id=note_id,
        kb_id=binding.kb_id,
        project_id=binding.kb_id if binding.is_native else None,
        title=note_id.title(),
        note_type="learning",
        content=f"body {note_id}",
    )


def _store(results_by_kb, *, failures=()):
    store = MagicMock()
    store.embedding_service.model = "test-embed"
    store.embedding_service.expected_dimensions = 16

    async def search_chunks(**kwargs):
        kb_ids = kwargs["kb_ids"]
        if any(kb_id in failures for kb_id in kb_ids):
            raise RuntimeError("one KB unavailable")
        if len(kb_ids) > 1:
            records = []
            for kb_id in kb_ids:
                records.extend(results_by_kb.get(kb_id, []))
            return records
        return results_by_kb.get(kb_ids[0], [])

    async def get_watermark(kb_id):
        return SimpleNamespace(indexed_commit=f"wm-{str(kb_id)[:8]}")

    store.search_chunks = AsyncMock(side_effect=search_chunks)
    store.get_watermark = AsyncMock(side_effect=get_watermark)
    return store


class TestBoundKnowledgeRetrieval:
    @pytest.mark.asyncio
    async def test_protects_three_native_and_two_external_slots(self):
        native = _binding("project", native=True)
        external = _binding("docs")
        store = _store(
            {
                native.kb_id: [_record(native, f"n{i}") for i in range(5)],
                external.kb_id: [_record(external, f"e{i}") for i in range(5)],
            }
        )

        selection = await retrieve_bound_knowledge(
            store, [native, external], "current work"
        )

        assert [note.note_id for note in selection.notes] == [
            "n0",
            "n1",
            "n2",
            "e0",
            "e1",
        ]
        assert selection.counts_by_binding == {"project": 3, "docs": 2}

    @pytest.mark.asyncio
    async def test_unused_native_slots_spill_to_external(self):
        native = _binding("project", native=True)
        external = _binding("docs")
        store = _store(
            {
                native.kb_id: [_record(native, "native")],
                external.kb_id: [_record(external, f"e{i}") for i in range(5)],
            }
        )

        selection = await retrieve_bound_knowledge(store, [native, external], "q")

        assert selection.counts_by_binding == {"project": 1, "docs": 4}
        assert len(selection.notes) == 5

    @pytest.mark.asyncio
    async def test_unused_external_slots_spill_to_native(self):
        native = _binding("project", native=True)
        external = _binding("docs")
        store = _store(
            {
                native.kb_id: [_record(native, f"n{i}") for i in range(5)],
                external.kb_id: [],
            }
        )

        selection = await retrieve_bound_knowledge(store, [native, external], "q")

        assert selection.counts_by_binding == {"project": 5}
        assert len(selection.notes) == 5

    @pytest.mark.asyncio
    async def test_external_only_round_robins_up_to_five(self):
        docs = _binding("docs")
        runbooks = _binding("runbooks")
        store = _store(
            {
                docs.kb_id: [_record(docs, f"d{i}") for i in range(4)],
                runbooks.kb_id: [_record(runbooks, f"r{i}") for i in range(4)],
            }
        )

        selection = await retrieve_bound_knowledge(store, [docs, runbooks], "q")

        assert [note.note_id for note in selection.notes] == [
            "d0",
            "r0",
            "d1",
            "r1",
            "d2",
        ]
        assert selection.counts_by_binding == {"docs": 3, "runbooks": 2}

    @pytest.mark.asyncio
    async def test_external_failure_keeps_native_and_other_external_results(self):
        native = _binding("project", native=True)
        broken = _binding("broken")
        docs = _binding("docs")
        store = _store(
            {
                native.kb_id: [_record(native, f"n{i}") for i in range(3)],
                docs.kb_id: [_record(docs, "d0"), _record(docs, "d1")],
            },
            failures={broken.kb_id},
        )

        selection = await retrieve_bound_knowledge(store, [native, broken, docs], "q")

        assert selection.counts_by_binding == {"project": 3, "docs": 2}
        assert len(selection.notes) == 5

    @pytest.mark.asyncio
    async def test_deduplicates_by_kb_and_note_id_and_uses_chunk_version(self):
        docs = _binding("docs")
        duplicate = _record(docs, "same")
        store = _store({docs.kb_id: [duplicate, duplicate, _record(docs, "different")]})
        store.embedding_service.profile_fingerprint = "pf-effective-profile"

        selection = await retrieve_bound_knowledge(store, [docs], "q")

        assert [note.note_id for note in selection.notes] == ["same", "different"]
        kwargs = store.search_chunks.await_args.kwargs
        assert kwargs["embedding_version"] == ("test-embed:16:c1:pf-effective-profile")
        assert kwargs["match_count"] == 5

    @pytest.mark.asyncio
    async def test_source_labels_and_external_watermark_are_rendered(self):
        docs = _binding("docs")
        store = _store({docs.kb_id: [_record(docs, "deployments")]})

        selection = await retrieve_bound_knowledge(store, [docs], "deploy")
        block = KnowledgeStore.assemble_knowledge_block(
            selection.notes,
            bindings=selection.bindings,
            external_watermarks=selection.external_watermarks,
        )

        assert "[docs] docs:deployments" in block
        assert "External snapshots (as of): [docs] wm-" in block

    @pytest.mark.asyncio
    async def test_partial_external_injection_discloses_mixed_convergence(self):
        docs = _binding("docs")
        store = _store({docs.kb_id: [_record(docs, "deployments")]})
        store.get_watermark = AsyncMock(
            return_value=SimpleNamespace(
                indexed_commit="a" * 40,
                source_head="b" * 40,
                status="partial",
            )
        )

        selection = await retrieve_bound_knowledge(store, [docs], "deploy")
        block = KnowledgeStore.assemble_knowledge_block(
            selection.notes,
            bindings=selection.bindings,
            external_watermarks=selection.external_watermarks,
        )

        assert "partial — last clean @ " in block
        assert "source @ " in block

    @pytest.mark.asyncio
    async def test_zero_contribution_external_still_discloses_indexing(self):
        # An external KB that returned no records but is still indexing must
        # remain visible in the block — otherwise a mid-convergence KB is
        # silently dropped and reads as "nothing here".
        docs = _binding("docs")
        pending = _binding("pending")
        store = _store({docs.kb_id: [_record(docs, "deployments")]})

        async def get_watermark(kb_id):
            if kb_id == pending.kb_id:
                return SimpleNamespace(
                    indexed_commit=None,
                    source_head="b" * 40,
                    status="indexing",
                )
            return SimpleNamespace(indexed_commit=f"wm-{str(kb_id)[:8]}")

        store.get_watermark = AsyncMock(side_effect=get_watermark)

        selection = await retrieve_bound_knowledge(store, [docs, pending], "deploy")
        block = KnowledgeStore.assemble_knowledge_block(
            selection.notes,
            bindings=selection.bindings,
            external_watermarks=selection.external_watermarks,
        )

        assert "pending" in selection.external_watermarks
        assert "[pending]" in block
        assert "source @ " + "b" * 40 in block
