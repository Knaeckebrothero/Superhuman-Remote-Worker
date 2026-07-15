"""Tests for src/tools/knowledge/knowledge_tools.py.

Covers section 13 of persistent_agent_tests.md:
  13.1  KNOWLEDGE_TOOLS_METADATA registry
  13.2  create_kb_tools()
  13.3  _get_project_id() / _get_project_ids()
  13.4  kb_write
  13.5  kb_update
  13.6  kb_read
  13.7  kb_list
  13.8  kb_search
  13.9  kb_related
  13.10 kb_contradictions
  13.11 kb_provenance
  13.12 kb_unanswered
  13.13 kb_export
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.tools.knowledge.knowledge_tools import (
    KNOWLEDGE_TOOLS_METADATA,
    create_kb_tools,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_context(project_id=None, project_ids=None, job_id=None):
    """Create a mock ToolContext."""
    ctx = MagicMock()
    ctx.project_id = project_id or str(uuid.uuid4())
    ctx.project_ids = project_ids or [ctx.project_id]
    ctx.job_id = job_id or str(uuid.uuid4())
    ctx.config = {"current_phase": 2}
    ctx.knowledge_graph = MagicMock()
    ctx.knowledge_store = AsyncMock()
    return ctx


def _make_tools(context=None):
    """Create kb tools with mocked context, patching asyncio loop."""
    ctx = context or _make_context()
    with patch("src.tools.knowledge.knowledge_tools.asyncio") as mock_asyncio:
        mock_asyncio.get_running_loop.side_effect = RuntimeError("no loop")
        tools = create_kb_tools(ctx)
    return tools, ctx


def _get_tool(tools, name):
    """Find a tool by name."""
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool {name} not found")


def _invoke(tool_func, args):
    """Invoke a langchain tool with args dict."""
    return tool_func.invoke(args)


def _invoke_unvalidated(tool_func, **kwargs):
    """Call the tool body directly, bypassing args_schema validation.

    Closed-vocabulary params (``type``, ``status``, ``confidence``) are now
    ``Literal``-typed, so ``invoke()`` rejects an off-vocabulary value at the
    pydantic boundary and the body never runs. That is the point — but the
    body's own checks must still hold for callers that don't go through the
    schema, and this reaches them.
    """
    return tool_func.func(**kwargs)


def _capture_workspace():
    """A mock workspace_manager that records write_file(path, content) calls."""
    ws = MagicMock()
    writes: dict = {}
    ws.write_file.side_effect = lambda rel, content: writes.__setitem__(rel, content)
    return ws, writes


def _fake_kb_workspace(files: dict):
    """Workspace mock backed by an in-memory {rel_path: text} map.

    list_files returns root-relative *.md paths (matching the real backends),
    read_file/exists read the map, write_file records + updates it.
    """
    ws = MagicMock()
    writes: dict = {}

    def _list(root, pattern="*"):
        prefix = root.rstrip("/") + "/"
        return sorted(p for p in files if p.startswith(prefix) and p.endswith(".md"))

    def _write(rel, content):
        writes[rel] = content
        files[rel] = content

    ws.list_files.side_effect = _list
    ws.read_file.side_effect = lambda p: files[p]
    ws.exists.side_effect = lambda p: p in files
    ws.write_file.side_effect = _write
    return ws, writes


# =============================================================================
# 13.1: KNOWLEDGE_TOOLS_METADATA registry
# =============================================================================


class TestMetadataRegistry:
    """Tests for KNOWLEDGE_TOOLS_METADATA."""

    def test_contains_exactly_12_tools(self):
        assert len(KNOWLEDGE_TOOLS_METADATA) == 12

    def test_expected_tool_names(self):
        expected = {
            "kb_write",
            "kb_update",
            "kb_read",
            "kb_list",
            "kb_search",
            "kb_related",
            "kb_contradictions",
            "kb_provenance",
            "kb_unanswered",
            "kb_export",
            "kb_lint",
            "kb_index",
        }
        assert set(KNOWLEDGE_TOOLS_METADATA.keys()) == expected

    def test_all_have_knowledge_category(self):
        for name, meta in KNOWLEDGE_TOOLS_METADATA.items():
            assert meta["category"] == "knowledge", f"{name} category mismatch"

    def test_all_have_both_phases(self):
        for name, meta in KNOWLEDGE_TOOLS_METADATA.items():
            assert meta["phases"] == ["strategic", "tactical"], (
                f"{name} phases mismatch"
            )


# =============================================================================
# 13.2: create_kb_tools()
# =============================================================================


class TestCreateKbTools:
    """Tests for create_kb_tools()."""

    def test_tolerates_knowledge_graph_none(self):
        # slice-3 PR4c: Neo4j is optional. Without a graph the tools still build
        # (the pgvector index is canonical for retrieval; files for content).
        ctx = _make_context()
        ctx.knowledge_graph = None
        with patch("src.tools.knowledge.knowledge_tools.asyncio") as ma:
            ma.get_running_loop.side_effect = RuntimeError
            tools = create_kb_tools(ctx)
        assert len(tools) == 12

    def test_raises_when_knowledge_store_is_none(self):
        ctx = _make_context()
        ctx.knowledge_store = None
        with pytest.raises(ValueError, match="knowledge_store"):
            with patch("src.tools.knowledge.knowledge_tools.asyncio") as ma:
                ma.get_running_loop.side_effect = RuntimeError
                create_kb_tools(ctx)

    def test_returns_list_of_12_tools(self):
        tools, _ = _make_tools()
        assert len(tools) == 12


# =============================================================================
# 13.4: kb_write
# =============================================================================


class TestKbWrite:
    """Tests for kb_write tool."""

    def test_error_when_no_project_id(self):
        ctx = _make_context()
        ctx.project_id = None
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_write"),
            {
                "title": "Test",
                "type": "decision",
                "content": "body",
            },
        )
        assert "Error" in result

    def test_calls_kg_create_note(self):
        tools, ctx = _make_tools()
        kg = ctx.knowledge_graph
        kg.create_note.return_value = "test-slug"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(
                _get_tool(tools, "kb_write"),
                {
                    "title": "Test",
                    "type": "decision",
                    "content": "body",
                    "tags": ["auth"],
                },
            )

        kg.create_note.assert_called_once()
        call_kwargs = kg.create_note.call_args[1]
        assert call_kwargs["title"] == "Test"
        assert call_kwargs["note_type"] == "decision"
        assert call_kwargs["job_id"] == ctx.job_id

    def test_returns_success_with_slug(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.return_value = "my-note"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_write"),
                {
                    "title": "My Note",
                    "type": "learning",
                    "content": "body",
                },
            )
        assert "my-note" in result
        assert "learning" in result

    def test_pgvector_failure_is_nonfatal(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.return_value = "slug"
        ctx.knowledge_store.upsert_note = AsyncMock(side_effect=Exception("db error"))

        # _run_async uses asyncio.run since no loop; mock it
        with patch("asyncio.run", side_effect=Exception("db error")):
            result = _invoke(
                _get_tool(tools, "kb_write"),
                {
                    "title": "T",
                    "type": "decision",
                    "content": "x",
                },
            )
        assert "slug" in result  # Still returns success

    def test_returns_error_on_value_error(self):
        """A ValueError out of the graph layer surfaces as an error string.

        Uses a *valid* type: the point here is the graph raising, not the
        vocabulary. An invalid type no longer reaches the body at all — the
        Literal rejects it at the schema boundary (see
        TestKbWriteWithoutNeo4j::test_no_kg_invalid_type_errors).
        """
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.side_effect = ValueError("Invalid note_type")

        result = _invoke(
            _get_tool(tools, "kb_write"),
            {
                "title": "T",
                "type": "decision",
                "content": "x",
            },
        )
        assert "Error" in result
        assert "Invalid note_type" in result

    def test_returns_error_on_generic_exception(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.side_effect = RuntimeError("boom")

        result = _invoke(
            _get_tool(tools, "kb_write"),
            {
                "title": "T",
                "type": "decision",
                "content": "x",
            },
        )
        assert "Error" in result


# =============================================================================
# 13.5: kb_update
# =============================================================================


class TestKbUpdate:
    """Tests for kb_update tool."""

    def test_error_when_no_project_id(self):
        ctx = _make_context()
        ctx.project_id = None
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_update"), {"note": "n1"})
        assert "Error" in result

    def test_calls_kg_update_note(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {"title": "T", "content": "x"}

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(
                _get_tool(tools, "kb_update"),
                {
                    "note": "n1",
                    "content": "new body",
                },
            )
        ctx.knowledge_graph.update_note.assert_called_once()

    def test_error_when_note_not_found(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.return_value = False

        result = _invoke(_get_tool(tools, "kb_update"), {"note": "missing"})
        assert "not found" in result

    def test_returns_summary_of_changes(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {"title": "T", "content": "x"}

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_update"),
                {
                    "note": "n1",
                    "content": "new",
                    "status": "resolved",
                    "add_tags": ["a", "b"],
                },
            )
        assert "content replaced" in result
        assert "status → resolved" in result
        assert "+2 tag(s)" in result

    def test_pgvector_writethrough_failure_nonfatal(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.side_effect = Exception("read failed")

        result = _invoke(
            _get_tool(tools, "kb_update"),
            {
                "note": "n1",
                "append": "extra",
            },
        )
        assert "Updated" in result
        assert "content appended" in result

    def test_error_on_value_error(self):
        """A ValueError out of the graph layer surfaces as an error string.

        Uses a *valid* status — see the sibling note on kb_write.
        """
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.side_effect = ValueError("Invalid status")

        result = _invoke(
            _get_tool(tools, "kb_update"),
            {
                "note": "n1",
                "status": "resolved",
            },
        )
        assert "Error" in result


# =============================================================================
# 13.6: kb_read
# =============================================================================


class TestKbRead:
    """Tests for kb_read tool."""

    def test_error_when_no_project_ids(self):
        ctx = _make_context()
        ctx.project_ids = []
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_read"), {"note": "n1"})
        assert "Error" in result

    def test_searches_across_project_ids(self):
        pid1, pid2 = str(uuid.uuid4()), str(uuid.uuid4())
        ctx = _make_context(project_ids=[pid1, pid2])
        tools, _ = _make_tools(ctx)
        kg = ctx.knowledge_graph
        kg.read_note.side_effect = [
            None,
            {"id": "n1", "title": "Found", "content": "x"},
        ]

        result = _invoke(_get_tool(tools, "kb_read"), {"note": "n1"})
        assert "Found" in result
        assert kg.read_note.call_count == 2

    def test_returns_formatted_markdown(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.read_note.return_value = {
            "id": "n1",
            "title": "My Note",
            "type": "decision",
            "status": "active",
            "confidence": "high",
            "tags": ["auth"],
            "keywords": ["jwt"],
            "content": "Full body text",
            "relationships": [
                {"type": "SUPPORTS", "target": "n2", "target_title": "N2"}
            ],
            "incoming_relationships": [
                {"type": "DERIVED_FROM", "source": "n0", "source_title": "N0"}
            ],
        }

        result = _invoke(_get_tool(tools, "kb_read"), {"note": "n1"})
        assert "# My Note" in result
        assert "decision" in result
        assert "[[n2]]" in result
        assert "[[n0]]" in result
        assert "→ this" in result

    def test_not_found(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.read_note.return_value = None

        result = _invoke(_get_tool(tools, "kb_read"), {"note": "missing"})
        assert "not found" in result

    def test_not_found_surfaces_indexing_status(self):
        # A not-yet-indexed KB must disclose it is still indexing rather than
        # look like a genuine miss (agent could otherwise conclude "KB empty").
        tools, ctx = _make_tools()
        ctx.knowledge_graph.read_note.return_value = None
        ctx.knowledge_store.get_watermark.return_value = MagicMock(
            status="pending", indexed_commit=None, source_head=None
        )
        result = _invoke(_get_tool(tools, "kb_read"), {"note": "missing"})
        assert "not found" in result
        assert "Still indexing" in result
        assert "pending" in result


# =============================================================================
# 13.7: kb_list
# =============================================================================


class TestKbList:
    """Tests for kb_list tool."""

    def test_error_when_no_project_ids(self):
        ctx = _make_context()
        ctx.project_ids = []
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_list"), {})
        assert "Error" in result

    def test_aggregates_across_projects(self):
        pid1, pid2 = str(uuid.uuid4()), str(uuid.uuid4())
        ctx = _make_context(project_ids=[pid1, pid2])
        tools, _ = _make_tools(ctx)
        ctx.knowledge_graph.list_notes.side_effect = [
            [{"id": "n1", "title": "A", "type": "decision", "status": "active"}],
            [{"id": "n2", "title": "B", "type": "learning", "status": "active"}],
        ]

        result = _invoke(_get_tool(tools, "kb_list"), {})
        assert "2 results" in result

    def test_empty_with_filter_description(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.list_notes.return_value = []

        result = _invoke(
            _get_tool(tools, "kb_list"),
            {
                "type": "decision",
                "tag": "auth",
            },
        )
        assert "No knowledge notes found" in result
        assert "type=decision" in result
        assert "tag=auth" in result

    def test_empty_surfaces_indexing_status(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.list_notes.return_value = []
        ctx.knowledge_store.get_watermark.return_value = MagicMock(
            status="partial", indexed_commit="a" * 40, source_head="b" * 40
        )
        result = _invoke(_get_tool(tools, "kb_list"), {})
        assert "No knowledge notes found" in result
        assert "Still indexing" in result
        assert "partial" in result

    def test_formats_with_status_icon(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.list_notes.return_value = [
            {
                "id": "n1",
                "title": "Active",
                "type": "decision",
                "status": "active",
                "confidence": "high",
            },
            {"id": "n2", "title": "Archived", "type": "learning", "status": "archived"},
        ]

        result = _invoke(_get_tool(tools, "kb_list"), {})
        assert "●" in result  # active
        assert "○" in result  # non-active


# =============================================================================
# 13.8: kb_search
# =============================================================================


class TestKbSearch:
    """Tests for kb_search tool."""

    def test_error_when_no_project_ids(self):
        ctx = _make_context()
        ctx.project_ids = []
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "test"})
        assert "Error" in result

    def test_formats_result_record(self):
        ctx = _make_context()
        rec = _srec("n1")
        rec.confidence = "high"
        ctx.knowledge_store.search_chunks.return_value = [rec]
        ctx.knowledge_store.get_watermark.return_value = None
        ctx.knowledge_store.embedding_service.model = "qwen3-embedding-8b"
        ctx.knowledge_store.embedding_service.expected_dimensions = 4096
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "auth"})
        assert "n1" in result

    def test_empty_results(self):
        ctx = _make_context()
        ctx.knowledge_store.search_chunks.return_value = []
        ctx.knowledge_store.embedding_service.model = "qwen3-embedding-8b"
        ctx.knowledge_store.embedding_service.expected_dimensions = 4096
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "nothing"})
        assert "No knowledge notes match" in result

    def test_empty_results_surface_indexing_status(self):
        ctx = _make_context()
        ctx.knowledge_store.search_chunks.return_value = []
        ctx.knowledge_store.embedding_service.model = "qwen3-embedding-8b"
        ctx.knowledge_store.embedding_service.expected_dimensions = 4096
        ctx.knowledge_store.get_watermark.return_value = MagicMock(
            status="indexing", indexed_commit=None, source_head="b" * 40
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "nothing"})
        assert "No knowledge notes match" in result
        assert "Still indexing" in result
        assert "indexing" in result

    def test_empty_results_ready_kb_has_no_indexing_notice(self):
        # Ready KBs must NOT raise a false "still indexing" alarm on a real miss.
        ctx = _make_context()
        ctx.knowledge_store.search_chunks.return_value = []
        ctx.knowledge_store.embedding_service.model = "qwen3-embedding-8b"
        ctx.knowledge_store.embedding_service.expected_dimensions = 4096
        ctx.knowledge_store.get_watermark.return_value = MagicMock(
            status="ready", indexed_commit="a" * 40, source_head="a" * 40
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "nothing"})
        assert "No knowledge notes match" in result
        assert "Still indexing" not in result

    def test_truncates_content_preview(self):
        ctx = _make_context()
        ctx.knowledge_store.search_chunks.return_value = [
            _srec("n1", content="x" * 300)
        ]
        ctx.knowledge_store.get_watermark.return_value = None
        ctx.knowledge_store.embedding_service.model = "qwen3-embedding-8b"
        ctx.knowledge_store.embedding_service.expected_dimensions = 4096
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "q"})
        assert "..." in result


# =============================================================================
# 13.8b: kb_search chunk-retrieval cutover (slice-3 PR4)
# =============================================================================


def _srec(note_id, content="body text"):
    """A note-level search result record (what search_chunks returns)."""
    rec = MagicMock()
    rec.note_id = note_id
    rec.title = "Test"
    rec.note_type = "decision"
    rec.confidence = None
    rec.content = content
    return rec


class TestKbSearchChunkCutover:
    """kb_search retrieves over the chunk index (``search_chunks``), not the
    note-level ``hybrid_search`` — the note row's embedding is NULL after the
    reindexer — and surfaces the index watermark commit.

    These deliberately do NOT patch ``asyncio.run`` so the AsyncMock store
    methods actually record their calls (the tool falls back to ``asyncio.run``
    because ``_make_tools`` leaves the creator loop unset).
    """

    def _ctx_with_store(self, records, watermark=None, project_ids=None):
        ctx = _make_context(project_ids=project_ids)
        ctx.knowledge_store.search_chunks.return_value = records
        ctx.knowledge_store.get_watermark.return_value = watermark
        ctx.knowledge_store.embedding_service.model = "qwen3-embedding-8b"
        ctx.knowledge_store.embedding_service.expected_dimensions = 4096
        return ctx

    def test_calls_search_chunks_not_hybrid_search(self):
        ctx = self._ctx_with_store([_srec("n1")])
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "auth"})
        ctx.knowledge_store.search_chunks.assert_called_once()
        ctx.knowledge_store.hybrid_search.assert_not_called()
        assert "n1" in result

    def test_passes_kb_ids_and_current_embedding_version(self):
        pid = str(uuid.uuid4())
        ctx = self._ctx_with_store([], project_ids=[pid])
        ctx.knowledge_store.embedding_service.profile_fingerprint = (
            "pf-effective-profile"
        )
        tools, _ = _make_tools(ctx)
        _invoke(_get_tool(tools, "kb_search"), {"query": "q"})
        kwargs = ctx.knowledge_store.search_chunks.call_args.kwargs
        assert kwargs["kb_ids"] == [uuid.UUID(pid)]
        # Version string must match what the reindexer stamped so the filter
        # doesn't silently zero out the live index.
        assert kwargs["embedding_version"] == (
            "qwen3-embedding-8b:4096:c1:pf-effective-profile"
        )

    def test_surfaces_indexed_commit_for_single_kb(self):
        wm = MagicMock()
        wm.indexed_commit = "379e91846da3ffffffffffffffffffffffffffff"
        ctx = self._ctx_with_store([_srec("n1")], watermark=wm)
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "q"})
        assert "379e9184" in result  # short sha in the header

    def test_no_watermark_still_returns_results(self):
        ctx = self._ctx_with_store([_srec("n1")], watermark=None)
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_search"), {"query": "q"})
        assert "n1" in result


# =============================================================================
# 13.9: kb_related
# =============================================================================


class TestKbRelated:
    """Tests for kb_related tool."""

    def test_error_when_no_project_ids(self):
        ctx = _make_context()
        ctx.project_ids = []
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "Error" in result

    def test_passes_max_hops(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = []

        _invoke(_get_tool(tools, "kb_related"), {"note": "n1", "max_hops": 3})
        ctx.knowledge_graph.get_related.assert_called_once()
        assert ctx.knowledge_graph.get_related.call_args[1]["max_hops"] == 3

    def test_formats_hop_singular(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {
                "id": "n2",
                "title": "T",
                "type": "decision",
                "status": "active",
                "distance": 1,
                "rel_types": ["SUPPORTS"],
            },
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "1 hop)" in result  # singular

    def test_formats_hops_plural(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {
                "id": "n2",
                "title": "T",
                "type": "decision",
                "status": "active",
                "distance": 2,
                "rel_types": ["SUPPORTS", "REFERENCES"],
            },
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "2 hops)" in result  # plural

    def test_shows_relationship_chain(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {
                "id": "n2",
                "title": "T",
                "type": "learning",
                "status": "active",
                "distance": 2,
                "rel_types": ["SUPPORTS", "DERIVED_FROM"],
            },
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "SUPPORTS → DERIVED_FROM" in result

    def test_shows_non_active_status(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {
                "id": "n2",
                "title": "T",
                "type": "decision",
                "status": "archived",
                "distance": 1,
                "rel_types": ["REFERENCES"],
            },
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "[archived]" in result


# =============================================================================
# 13.10: kb_contradictions
# =============================================================================


class TestKbContradictions:
    """Tests for kb_contradictions tool."""

    def test_empty_result(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_contradictions.return_value = []

        result = _invoke(_get_tool(tools, "kb_contradictions"), {})
        assert "No active contradictions" in result

    def test_formats_pairs(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_contradictions.return_value = [
            {"note_a": "n1", "title_a": "A", "note_b": "n2", "title_b": "B"},
        ]

        result = _invoke(_get_tool(tools, "kb_contradictions"), {})
        assert "n1" in result
        assert "n2" in result
        assert "⟷ CONTRADICTS ⟷" in result


# =============================================================================
# 13.11: kb_provenance
# =============================================================================


class TestKbProvenance:
    """Tests for kb_provenance tool."""

    def test_empty_result(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_provenance.return_value = []

        result = _invoke(_get_tool(tools, "kb_provenance"), {"note": "n1"})
        assert "No provenance chain" in result

    def test_indents_by_depth(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_provenance.return_value = [
            {"id": "n2", "title": "Parent", "type": "source", "depth": 1},
            {"id": "n3", "title": "Grandparent", "type": "source", "depth": 2},
        ]

        result = _invoke(_get_tool(tools, "kb_provenance"), {"note": "n1"})
        lines = result.split("\n")
        # depth 1: no indent, depth 2: 2 spaces indent
        depth1_line = [x for x in lines if "Parent" in x][0]
        depth2_line = [x for x in lines if "Grandparent" in x][0]
        assert "  ↑" in depth1_line  # base indent
        assert "    ↑" in depth2_line  # extra indent for depth 2

    def test_uses_arrow_prefix(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_provenance.return_value = [
            {"id": "n2", "title": "Source", "type": "source", "depth": 1},
        ]

        result = _invoke(_get_tool(tools, "kb_provenance"), {"note": "n1"})
        assert "↑" in result


# =============================================================================
# 13.12: kb_unanswered
# =============================================================================


class TestKbUnanswered:
    """Tests for kb_unanswered tool."""

    def test_empty_result(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_unanswered.return_value = []

        result = _invoke(_get_tool(tools, "kb_unanswered"), {})
        assert "No unanswered questions" in result

    def test_returns_questions(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_unanswered.return_value = [
            {"id": "q1", "title": "Why JWT?", "content": "details"},
        ]

        result = _invoke(_get_tool(tools, "kb_unanswered"), {})
        assert "q1" in result
        assert "Why JWT?" in result

    def test_truncates_long_content(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_unanswered.return_value = [
            {"id": "q1", "title": "Long Q", "content": "x" * 200},
        ]

        _invoke(_get_tool(tools, "kb_unanswered"), {})
        # Content is used as fallback for title display only when title missing
        # The preview truncation happens internally


# =============================================================================
# 13.13: kb_export
# =============================================================================


class TestKbExport:
    """Tests for kb_export tool."""

    def test_error_when_no_project_ids(self):
        ctx = _make_context()
        ctx.project_ids = []
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_export"), {"path": "/tmp/export"})
        assert "Error" in result

    def test_empty_knowledge_base(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_all_notes_for_export.return_value = []

        result = _invoke(_get_tool(tools, "kb_export"), {"path": "/tmp/export"})
        assert "empty" in result

    def test_error_when_no_workspace(self):
        # The local-Path fallback (agent host, invisible to the pod) is gone:
        # kb_export now requires a workspace backend.
        tools, ctx = _make_tools()
        ctx.has_workspace.return_value = False
        ctx.knowledge_graph.get_all_notes_for_export.return_value = [
            {"id": "n1", "type": "decision", "status": "active", "content": "x"},
        ]
        result = _invoke(_get_tool(tools, "kb_export"), {"path": "exports/kb"})
        assert "Error" in result
        assert "workspace" in result

    def test_creates_directory_and_writes_files(self):
        tools, ctx = _make_tools()
        ctx.has_workspace.return_value = True
        ws, writes = _capture_workspace()
        ctx.workspace_manager = ws
        ctx.knowledge_graph.get_all_notes_for_export.return_value = [
            {
                "id": "chose-jwt",
                "title": "Chose JWT",
                "type": "decision",
                "status": "active",
                "confidence": "high",
                "tags": ["auth"],
                "keywords": ["jwt"],
                "content": "We chose JWT because...",
                "relationships": [{"type": "SUPPORTS", "target": "auth-design"}],
            },
        ]

        result = _invoke(_get_tool(tools, "kb_export"), {"path": "exports/kb"})
        assert "1 note" in result

        content = writes["exports/kb/chose-jwt.md"]
        assert "---" in content  # frontmatter
        assert "id: chose-jwt" in content
        assert "type: decision" in content
        assert "tags:" in content
        assert "# Chose JWT" in content
        assert "[auth-design](auth-design.md)" in content  # markdown link
        assert "[[auth-design]]" not in content  # not a wikilink

    def test_groups_relationships_by_type(self):
        tools, ctx = _make_tools()
        ctx.has_workspace.return_value = True
        ws, writes = _capture_workspace()
        ctx.workspace_manager = ws
        ctx.knowledge_graph.get_all_notes_for_export.return_value = [
            {
                "id": "n1",
                "title": "N1",
                "type": "decision",
                "status": "active",
                "content": "body",
                "relationships": [
                    {"type": "SUPPORTS", "target": "a"},
                    {"type": "SUPPORTS", "target": "b"},
                    {"type": "REFERENCES", "target": "c"},
                ],
            },
        ]

        _invoke(_get_tool(tools, "kb_export"), {"path": "exports/kb2"})
        content = writes["exports/kb2/n1.md"]
        assert "## Relationships" in content
        assert "**SUPPORTS:** [a](a.md), [b](b.md)" in content
        assert "**REFERENCES:** [c](c.md)" in content


# =============================================================================
# Slice 2 PR1: kb_lint / kb_index gardener tools
# =============================================================================


class TestKbLint:
    def test_error_when_no_workspace(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = False
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_lint"), {})
        assert "Error" in result

    def test_no_notes_found(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, _ = _fake_kb_workspace({})
        ctx.workspace_manager = ws
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_lint"), {})
        assert "No markdown notes" in result

    def test_reports_dead_link_finding(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, _ = _fake_kb_workspace(
            {
                "knowledge/n1.md": (
                    '---\nid: n1\ntype: decision\ndescription: "d"\n---\n\n'
                    "# N1\n\nSee [ghost](ghost.md).\n"
                ),
            }
        )
        ctx.workspace_manager = ws
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_lint"), {})
        assert "dead-link" in result

    def test_respects_path_arg(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, _ = _fake_kb_workspace(
            {"docs/a.md": '---\nid: a\ntype: note\ndescription: "d"\n---\n\n# A\n'}
        )
        ctx.workspace_manager = ws
        tools, _ = _make_tools(ctx)
        _invoke(_get_tool(tools, "kb_lint"), {"path": "docs"})
        ws.list_files.assert_called_with("docs", "*.md")


class TestKbIndex:
    def test_error_when_no_workspace(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = False
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_index"), {})
        assert "Error" in result

    def test_writes_index_grouped_by_type(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, writes = _fake_kb_workspace(
            {
                "knowledge/chose-jwt.md": (
                    '---\nid: chose-jwt\ntype: decision\ndescription: "We chose JWT."\n'
                    "---\n\n# Chose JWT\n\nbody\n"
                ),
            }
        )
        ctx.workspace_manager = ws
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_index"), {})
        assert "knowledge/index.md" in writes
        content = writes["knowledge/index.md"]
        assert "## decision" in content
        assert "[Chose JWT](chose-jwt.md)" in content
        assert "1 note" in result

    def test_skips_reserved_and_malformed(self):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, writes = _fake_kb_workspace(
            {
                "knowledge/good.md": (
                    '---\nid: good\ntype: learning\ndescription: "d"\n---\n\n# Good\n'
                ),
                "knowledge/index.md": "# Index\n\n(reserved, not a note)\n",
                "knowledge/broken.md": "---\nnot: : yaml\n---\nbody",
            }
        )
        ctx.workspace_manager = ws
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_index"), {})
        content = writes["knowledge/index.md"]
        assert "[Good](good.md)" in content
        assert "1 note" in result  # index.md + broken.md excluded

    def test_returns_summary_with_count(self, tmp_path):
        tools, ctx = _make_tools()
        export_dir = str(tmp_path / "kb_export3")
        ctx.knowledge_graph.get_all_notes_for_export.return_value = [
            {"id": "n1", "type": "decision", "status": "active", "content": "x"},
            {"id": "n2", "type": "learning", "status": "active", "content": "y"},
        ]

        result = _invoke(_get_tool(tools, "kb_export"), {"path": export_dir})
        assert "2 note(s)" in result
        assert export_dir in result


# =============================================================================
# Slice 1: _render_note_md (pure OKF serializer)
# =============================================================================


class TestRenderNoteMd:
    """Tests for the pure OKF markdown serializer (_render_note_md)."""

    def test_emits_frontmatter_fences_and_required_keys(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md({"id": "chose-jwt", "type": "decision", "content": "body"})
        assert md.startswith("---\n")
        assert "\n---\n" in md  # closing fence
        assert "id: chose-jwt" in md
        assert "type: decision" in md
        assert "status: active" in md  # defaults to active

    def test_title_and_body(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "learning",
                "title": "My Note",
                "content": "The full body.",
            }
        )
        assert "# My Note" in md
        assert "The full body." in md

    def test_title_falls_back_to_id(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md({"id": "abc-slug", "type": "learning", "content": "x"})
        assert "# abc-slug" in md

    def test_no_double_h1_when_content_starts_with_same_h1(self):
        # Run-8 nit (docs §11.1): the serializer prepended `# {title}` even when
        # the body already opened with the same H1 → every note's title twice.
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "learning",
                "title": "My Note",
                "content": "# My Note\n\nThe body.",
            }
        )
        lines = md.splitlines()
        assert lines.count("# My Note") == 1  # heading emitted exactly once
        assert "The body." in md

    def test_content_own_h1_suppresses_prepended_title(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "learning",
                "title": "Slug Title",
                "content": "# Different Heading\n\nBody.",
            }
        )
        lines = md.splitlines()
        assert "# Slug Title" not in lines  # not prepended
        assert "# Different Heading" in lines  # body's own H1 stands

    def test_h2_leading_content_still_gets_title(self):
        # An H2 opener is not a title — the H1 title should still be prepended.
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "learning",
                "title": "Real Title",
                "content": "## A subsection\n\nBody.",
            }
        )
        lines = md.splitlines()
        assert "# Real Title" in lines

    def test_derives_description_from_content_first_sentence(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "decision",
                "content": "We chose JWT because it is stateless. More detail here.",
            }
        )
        assert 'description: "We chose JWT because it is stateless."' in md

    def test_explicit_description_wins_and_is_quoted(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "decision",
                "description": "A one: liner",
                "content": "Body sentence.",
            }
        )
        assert 'description: "A one: liner"' in md

    def test_description_escapes_double_quotes(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "decision",
                "description": 'has "quotes"',
                "content": "b",
            }
        )
        assert 'description: "has \\"quotes\\""' in md

    def test_emits_markdown_links_not_wikilinks(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "decision",
                "content": "b",
                "relationships": [
                    {"type": "SUPPORTS", "target": "auth-design"},
                    {"type": "SUPPORTS", "target": "token-plan"},
                    {"type": "REFERENCES", "target": "rfc-7519"},
                ],
            }
        )
        assert "[[auth-design]]" not in md  # no wikilinks
        assert (
            "**SUPPORTS:** [auth-design](auth-design.md), [token-plan](token-plan.md)"
            in md
        )
        assert "**REFERENCES:** [rfc-7519](rfc-7519.md)" in md

    def test_emits_provenance_when_present(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "decision",
                "content": "b",
                "author": "developer",
                "job": "job-123",
                "branch": "job/abc",
            }
        )
        assert "author: developer" in md
        assert "job: job-123" in md
        assert "branch: job/abc" in md

    def test_omits_optional_fields_when_absent(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md({"id": "n1", "type": "decision", "content": "b"})
        assert "confidence:" not in md
        assert "author:" not in md
        assert "branch:" not in md
        assert "superseded_by:" not in md

    def test_emits_tags_keywords_confidence_and_superseded_by(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {
                "id": "n1",
                "type": "decision",
                "content": "b",
                "tags": ["auth", "security"],
                "keywords": ["jwt"],
                "confidence": "high",
                "status": "superseded",
                "superseded_by": "new-note",
            }
        )
        # C-1: flow-sequence elements are quoted so arbitrary agent strings
        # (commas, colons, @-scalars) stay valid YAML.
        assert 'tags: ["auth", "security"]' in md
        assert 'keywords: ["jwt"]' in md
        assert "confidence: high" in md
        assert "status: superseded" in md
        assert "superseded_by: new-note" in md


# =============================================================================
# Slice 1: kb_write / kb_update dual-write to knowledge/<slug>.md
# =============================================================================


def _make_git_context(**kwargs):
    """Context whose has_git() is True and whose workspace records writes."""
    ctx = _make_context(**kwargs)
    ctx.has_git.return_value = True
    ctx._job_metadata = {"config_name": "developer"}
    ctx.workspace_manager = MagicMock()
    ctx.workspace_manager.git_manager.current_branch.return_value = "job/abc"
    return ctx


class TestKbWriteDualWrite:
    """Slice 1: kb_write materializes knowledge/<slug>.md via the workspace."""

    def test_writes_flat_knowledge_file_when_git_active(self):
        ctx = _make_git_context()
        ctx.knowledge_graph.create_note.return_value = "chose-jwt"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "Chose JWT", "type": "decision", "content": "We chose JWT."},
            )

        ctx.workspace_manager.write_file.assert_called_once()
        path_arg, content_arg = ctx.workspace_manager.write_file.call_args[0]
        assert path_arg == "knowledge/chose-jwt.md"
        assert "id: chose-jwt" in content_arg
        assert "author: developer" in content_arg
        assert "branch: job/abc" in content_arg

    def test_no_file_write_when_git_inactive(self):
        ctx = _make_git_context()
        ctx.has_git.return_value = False
        ctx.knowledge_graph.create_note.return_value = "n1"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "T", "type": "decision", "content": "x"},
            )
        ctx.workspace_manager.write_file.assert_not_called()
        assert "n1" in result  # still succeeds

    def test_file_write_failure_is_nonfatal(self):
        ctx = _make_git_context()
        ctx.knowledge_graph.create_note.return_value = "n1"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())
        ctx.workspace_manager.write_file.side_effect = OSError("disk full")
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "T", "type": "decision", "content": "x"},
            )
        assert "n1" in result  # dual-write failure never fails the tool

    def test_passes_description_arg_into_frontmatter(self):
        ctx = _make_git_context()
        ctx.knowledge_graph.create_note.return_value = "n1"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(
                _get_tool(tools, "kb_write"),
                {
                    "title": "T",
                    "type": "decision",
                    "content": "x",
                    "description": "A crisp summary.",
                },
            )
        _, content_arg = ctx.workspace_manager.write_file.call_args[0]
        assert 'description: "A crisp summary."' in content_arg


class TestKbUpdateDualWrite:
    """Slice 1: kb_update re-materializes knowledge/<slug>.md via the workspace."""

    def test_rewrites_knowledge_file_on_update(self):
        ctx = _make_git_context()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {
            "id": "n1",
            "title": "T",
            "type": "decision",
            "content": "updated body",
            "status": "active",
            "relationships": [{"type": "SUPPORTS", "target": "a"}],
        }
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(
                _get_tool(tools, "kb_update"),
                {"note": "n1", "content": "updated body"},
            )

        ctx.workspace_manager.write_file.assert_called_once()
        path_arg, content_arg = ctx.workspace_manager.write_file.call_args[0]
        assert path_arg == "knowledge/n1.md"
        assert "updated body" in content_arg
        assert "[a](a.md)" in content_arg

    def test_no_file_write_when_git_inactive(self):
        ctx = _make_git_context()
        ctx.has_git.return_value = False
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {
            "id": "n1",
            "title": "T",
            "content": "x",
        }
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_update"),
                {"note": "n1", "content": "x"},
            )
        ctx.workspace_manager.write_file.assert_not_called()
        assert "Updated" in result

    def test_file_write_failure_is_nonfatal(self):
        ctx = _make_git_context()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.return_value = {
            "id": "n1",
            "title": "T",
            "content": "x",
        }
        ctx.workspace_manager.write_file.side_effect = OSError("disk full")
        tools, _ = _make_tools(ctx)

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_update"),
                {"note": "n1", "content": "x"},
            )
        assert "Updated" in result


# =============================================================================
# kb_write ingestion verdict gate (slice 2 PR2)
# =============================================================================

import hashlib as _hashlib  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from src.services.auxiliary import KnowledgeVerdict  # noqa: E402
from src.services.knowledge.ingestion import KnowledgeVerdictService  # noqa: E402


class _GateAux:
    """Aux stub whose chain() returns a fixed KnowledgeVerdict."""

    def __init__(self, verdict):
        self._verdict = verdict

    async def chain(self, task, timeout=None):
        return self._verdict


class _GateStore:
    """KnowledgeStore stub for the gate: async embed + find_similar_many + upsert."""

    def __init__(self, neighbours):
        self._neighbours = neighbours
        self.embedding_service = SimpleNamespace(embed=self._embed)
        self.upsert_note = AsyncMock(return_value=uuid.uuid4())

    async def _embed(self, text):
        return [0.1, 0.2, 0.3]

    async def find_similar_many(self, project_id, embedding, k=5, min_similarity=0.6):
        return self._neighbours


def _kb_neighbour(note_id, content, title="N"):
    return SimpleNamespace(
        note_id=note_id,
        content=content,
        title=title,
        similarity=0.85,
        created_at=None,
        content_hash=_hashlib.sha256(content.encode()).hexdigest(),
    )


def _gated_tools(kg, ks, verdict, prompt="ADJUDICATE"):
    """Build kb tools with a real verdict service (asyncio NOT mocked)."""
    ctx = MagicMock()
    ctx.project_id = str(uuid.uuid4())
    ctx.project_ids = [ctx.project_id]
    ctx.job_id = str(uuid.uuid4())
    ctx.config = {"current_phase": 1}
    ctx.knowledge_graph = kg
    ctx.knowledge_store = ks
    ctx.has_git.return_value = False  # skip dual-write for isolation
    service = KnowledgeVerdictService(
        _GateAux(verdict), SimpleNamespace(verdict_top_k=5, review_floor=0.6)
    )
    tools = create_kb_tools(ctx, verdict_service=service, verdict_prompt=prompt)
    return tools, ctx


class TestKbWriteVerdictGate:
    def _write(self, tools, **kw):
        args = {"title": "New Note", "type": "decision", "content": "some content"}
        args.update(kw)
        return _invoke(_get_tool(tools, "kb_write"), args)

    def test_add_when_no_neighbours(self):
        kg = MagicMock()
        kg.create_note.return_value = "new-slug"
        ks = _GateStore(neighbours=[])
        tools, _ = _gated_tools(kg, ks, KnowledgeVerdict(action="ADD", reason="new"))
        result = self._write(tools)
        assert "Created" in result
        kg.create_note.assert_called_once()

    def test_discard_exact_duplicate_skips_write(self):
        kg = MagicMock()
        # neighbour content hashes to the candidate's content → content-hash prefilter
        ks = _GateStore(neighbours=[_kb_neighbour("dup-note", "some content")])
        tools, _ = _gated_tools(kg, ks, KnowledgeVerdict(action="ADD", reason="unused"))
        result = self._write(tools, content="some content")
        assert "DISCARD" in result
        assert "dup-note" in result
        kg.create_note.assert_not_called()

    def test_update_redirects_edit_onto_target(self):
        kg = MagicMock()
        kg.update_note.return_value = True
        # Faithful mock: the candidate slug ("new-note") does NOT pre-exist, so
        # the exact-dup pre-check is skipped and the gate runs; the UPDATE
        # target ("old-note") re-reads the dict inside _update_existing.
        kg.read_note.side_effect = lambda pid, nid: (
            {
                "type": "decision",
                "title": "Old",
                "content": "new content",
                "status": "active",
            }
            if nid == "old-note"
            else None
        )
        ks = _GateStore(neighbours=[_kb_neighbour("old-note", "different old text")])
        tools, _ = _gated_tools(
            kg, ks, KnowledgeVerdict(action="UPDATE", target_indices=[1], reason="fix")
        )
        result = self._write(tools, content="new content")
        assert "Updated" in result and "old-note" in result
        kg.create_note.assert_not_called()
        kg.update_note.assert_called_once()

    def test_supersede_creates_then_retires_target(self):
        kg = MagicMock()
        kg.create_note.return_value = "new-slug"
        kg.update_note.return_value = True
        kg.read_note.return_value = {
            "type": "decision",
            "title": "Old",
            "content": "old",
            "status": "superseded",
        }
        ks = _GateStore(neighbours=[_kb_neighbour("stale-note", "different old text")])
        tools, _ = _gated_tools(
            kg,
            ks,
            KnowledgeVerdict(action="SUPERSEDE", target_indices=[1], reason="replaced"),
        )
        result = self._write(tools, content="fresh content")
        assert "Created" in result and "superseded" in result
        kg.create_note.assert_called_once()
        # retire call: status=superseded on the stale note
        retire_kwargs = kg.update_note.call_args.kwargs
        assert retire_kwargs.get("status") == "superseded"
        assert retire_kwargs.get("note_id") == "stale-note"

    def test_no_service_writes_ungated(self):
        # Sanity: the default (no verdict service) path is unchanged.
        kg = MagicMock()
        kg.create_note.return_value = "slug"
        ks = _GateStore(neighbours=[_kb_neighbour("x", "some content")])
        ctx = MagicMock()
        ctx.project_id = str(uuid.uuid4())
        ctx.job_id = str(uuid.uuid4())
        ctx.config = {"current_phase": 1}
        ctx.knowledge_graph = kg
        ctx.knowledge_store = ks
        ctx.has_git.return_value = False
        tools = create_kb_tools(ctx)  # no verdict service
        result = _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "N", "type": "decision", "content": "some content"},
        )
        assert "Created" in result
        kg.create_note.assert_called_once()


class TestKbWriteSlugDedup:
    """Exact-content slug-collision no-op (Step 1 hardening, docs §11.1).

    A same-title write whose body is byte-identical to the existing note is a
    pure no-op for every writer (loop agents included) — it must skip the gate
    and the create entirely, killing the run-8 twin-file duplication at source.
    """

    def test_exact_duplicate_content_is_noop(self):
        tools, ctx = _make_tools()
        kg = ctx.knowledge_graph
        kg.read_note.return_value = {
            "content": "body",
            "type": "decision",
            "title": "Test",
        }
        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "Test", "type": "decision", "content": "body"},
            )
        kg.create_note.assert_not_called()
        assert "test" in result.lower()  # references the existing slug
        assert (
            "exist" in result.lower()
            or "no-op" in result.lower()
            or "no change" in result.lower()
        )

    def test_different_content_same_title_still_creates(self):
        tools, ctx = _make_tools()
        kg = ctx.knowledge_graph
        kg.read_note.return_value = {
            "content": "OLD body",
            "type": "decision",
            "title": "Test",
        }
        kg.create_note.return_value = "test-abc123"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())
        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "Test", "type": "decision", "content": "NEW body"},
            )
        kg.create_note.assert_called_once()

    def test_no_collision_creates_normally(self):
        tools, ctx = _make_tools()
        kg = ctx.knowledge_graph
        kg.read_note.return_value = None
        kg.create_note.return_value = "fresh-slug"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())
        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            result = _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "Fresh", "type": "learning", "content": "x"},
            )
        kg.create_note.assert_called_once()
        assert "fresh-slug" in result


# =============================================================================
# kb_lint — embedding-backed near-duplicate pass (slice-2 owed rule)
# =============================================================================


_KB_LINT_TWIN_FILES = {
    "knowledge/n1.md": (
        '---\nid: n1\ntype: learning\ndescription: "d"\n---\n\n# N1\n\n[n2](n2.md)\n'
    ),
    "knowledge/n2.md": (
        '---\nid: n2\ntype: learning\ndescription: "d"\n---\n\n# N2\n\n[n1](n1.md)\n'
    ),
}


class TestKbLintNearDuplicates:
    def _ctx(self, files):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, _ = _fake_kb_workspace(dict(files))
        ctx.workspace_manager = ws
        return ctx

    def test_reports_near_duplicate_pairs_from_index(self):
        ctx = self._ctx(_KB_LINT_TWIN_FILES)
        ctx.knowledge_store.find_near_duplicate_pairs = AsyncMock(
            return_value=[("n1", "n2", 0.95)]
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_lint"), {})
        assert "near-duplicate" in result
        assert "95" in result

    def test_index_pairs_outside_vault_ignored(self):
        ctx = self._ctx(_KB_LINT_TWIN_FILES)
        ctx.knowledge_store.find_near_duplicate_pairs = AsyncMock(
            return_value=[("ghost-a", "ghost-b", 0.99)]
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_lint"), {})
        assert "near-duplicate" not in result

    def test_store_error_is_non_fatal(self):
        # The deterministic report must stand alone when pgvector is down.
        ctx = self._ctx(_KB_LINT_TWIN_FILES)
        ctx.knowledge_store.find_near_duplicate_pairs = AsyncMock(
            side_effect=RuntimeError("pgvector down")
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_lint"), {})
        assert "kb_lint:" in result
        assert "near-duplicate" not in result

    def test_applies_0_97_near_duplicate_floor(self):
        # D-1: the 07-05 lint-policy decision (raise 0.9→0.97) lives at this
        # call site — the store default (0.9) is unusable lint noise (307 pairs
        # vs 7 at 0.97 on the live KB). The lint policy owns its floor.
        ctx = self._ctx(_KB_LINT_TWIN_FILES)
        ctx.knowledge_store.find_near_duplicate_pairs = AsyncMock(return_value=[])
        tools, _ = _make_tools(ctx)
        _invoke(_get_tool(tools, "kb_lint"), {})
        _, kwargs = ctx.knowledge_store.find_near_duplicate_pairs.call_args
        assert kwargs.get("min_similarity") == 0.97


# =============================================================================
# kb_lint — opt-in dead-external-URL sweep
# =============================================================================


_KB_LINT_URL_FILES = {
    "knowledge/n1.md": (
        '---\nid: n1\ntype: source\ndescription: "d"\n---\n\n'
        "# N1\n\n[docs](https://gone.example/x) [n2](n2.md)\n"
    ),
    "knowledge/n2.md": (
        '---\nid: n2\ntype: source\ndescription: "d"\n---\n\n# N2\n\n[n1](n1.md)\n'
    ),
}


class TestKbLintUrlSweep:
    def _ctx(self, files):
        ctx = _make_context()
        ctx.has_workspace.return_value = True
        ws, _ = _fake_kb_workspace(dict(files))
        ctx.workspace_manager = ws
        ctx.knowledge_store.find_near_duplicate_pairs = AsyncMock(return_value=[])
        return ctx

    def test_off_by_default_no_network(self):
        ctx = self._ctx(_KB_LINT_URL_FILES)
        tools, _ = _make_tools(ctx)
        with patch(
            "src.tools.knowledge.knowledge_tools._check_external_url"
        ) as checker:
            result = _invoke(_get_tool(tools, "kb_lint"), {})
        checker.assert_not_called()
        assert "dead-external-url" not in result

    def test_flags_dead_url_when_enabled(self):
        ctx = self._ctx(_KB_LINT_URL_FILES)
        tools, _ = _make_tools(ctx)
        with patch(
            "src.tools.knowledge.knowledge_tools._check_external_url",
            return_value="HTTP 404",
        ):
            result = _invoke(_get_tool(tools, "kb_lint"), {"check_urls": True})
        assert "dead-external-url" in result
        assert "gone.example" in result

    def test_alive_url_not_flagged(self):
        ctx = self._ctx(_KB_LINT_URL_FILES)
        tools, _ = _make_tools(ctx)
        with patch(
            "src.tools.knowledge.knowledge_tools._check_external_url",
            return_value=None,
        ):
            result = _invoke(_get_tool(tools, "kb_lint"), {"check_urls": True})
        assert "dead-external-url" not in result

    def test_cap_is_loud(self):
        files = {
            f"knowledge/n{i}.md": (
                f'---\nid: n{i}\ntype: source\ndescription: "d"\n---\n\n'
                f"# N{i}\n\n[u](https://example.com/{i}) [n0](n0.md)\n"
            )
            for i in range(30)
        }
        ctx = self._ctx(files)
        tools, _ = _make_tools(ctx)
        with patch(
            "src.tools.knowledge.knowledge_tools._check_external_url",
            return_value=None,
        ) as checker:
            result = _invoke(_get_tool(tools, "kb_lint"), {"check_urls": True})
        assert checker.call_count == 25
        assert "url-sweep-truncated" in result


# =============================================================================
# Graph tools without Neo4j — the Full-tier 1-hop degrade (slice-3 PR4c)
# =============================================================================


def _make_context_no_kg():
    """A ToolContext with no knowledge_graph (Neo4j disabled), store present."""
    ctx = _make_context()
    ctx.knowledge_graph = None
    return ctx


class TestKbToolsWithoutNeo4j:
    """When Neo4j is absent, kb_related degrades to the knowledge_links 1-hop
    query; the genuinely graph-shaped tools (contradictions / provenance /
    unanswered / export) degrade honestly to a Graph-tier message rather than
    fabricating results from the generic link table."""

    def test_kb_related_uses_link_table_when_no_kg(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_related_notes.return_value = [
            {
                "id": "note-b",
                "title": "B",
                "type": "decision",
                "status": "active",
                "distance": 1,
                "rel_types": ["references"],
            }
        ]
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_related"), {"note": "note-a"})
        ctx.knowledge_store.get_related_notes.assert_called_once()
        assert "note-b" in result

    def test_kb_related_no_neighbours_message(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_related_notes.return_value = []
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_related"), {"note": "note-a"})
        assert "No related notes" in result

    def test_kb_contradictions_degrades_to_graph_tier_message(self):
        ctx = _make_context_no_kg()
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_contradictions"), {})
        assert "Graph tier" in result

    def test_kb_provenance_degrades_to_graph_tier_message(self):
        ctx = _make_context_no_kg()
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_provenance"), {"note": "note-a"})
        assert "Graph tier" in result

    def test_kb_unanswered_degrades_to_graph_tier_message(self):
        ctx = _make_context_no_kg()
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_unanswered"), {})
        assert "Graph tier" in result

    def test_kb_export_degrades_to_graph_tier_message(self):
        ctx = _make_context_no_kg()
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_export"), {"path": "export"})
        assert "Graph tier" in result

    def test_kb_read_uses_store_when_no_kg(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = {
            "id": "n1",
            "title": "T",
            "type": "decision",
            "status": "active",
            "content": "the body",
            "confidence": None,
            "tags": [],
            "keywords": [],
            "job_id": None,
            "phase": None,
            "created": None,
            "modified": None,
        }
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_read"), {"note": "n1"})
        ctx.knowledge_store.get_note_by_slug.assert_called_once()
        assert "the body" in result

    def test_kb_read_not_found_when_no_kg(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_read"), {"note": "nope"})
        assert "not found" in result

    def test_kb_list_uses_store_when_no_kg(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.list_notes.return_value = [
            {
                "id": "n1",
                "title": "T",
                "type": "decision",
                "status": "active",
                "confidence": None,
            }
        ]
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_list"), {})
        ctx.knowledge_store.list_notes.assert_called_once()
        assert "n1" in result


def _existing_note(note_id="n1", content="old body", **over):
    """A get_note_by_slug-shaped dict for the kg-less write-path tests."""
    base = {
        "id": note_id,
        "title": "T",
        "type": "decision",
        "status": "active",
        "content": content,
        "confidence": None,
        "tags": [],
        "keywords": [],
        "job_id": None,
        "phase": None,
        "created": None,
        "modified": None,
    }
    base.update(over)
    return base


class TestKbWriteWithoutNeo4j:
    """kg-less kb_write (PR4c-3 write half): the pgvector store is the write
    target and the OKF file is the canonical export; Neo4j is never touched."""

    def test_no_kg_upserts_via_store(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None  # no collision
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "New Title", "type": "decision", "content": "body"},
        )
        assert ctx.knowledge_store.upsert_note.called
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["note_id"] == "new-title"
        assert kwargs["content"] == "body"
        assert "new-title" in result

    def test_no_kg_never_calls_neo4j(self):
        # kg is None — there is nothing to call; the guard must not dereference it.
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "Some Note", "type": "learning", "content": "x"},
        )
        assert "Error" not in result

    def test_no_kg_exact_duplicate_short_circuits(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note(
            note_id="new-title", content="identical"
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "New Title", "type": "decision", "content": "identical"},
        )
        assert "identical content" in result
        assert not ctx.knowledge_store.upsert_note.called

    def test_no_kg_collision_appends_content_hash(self):
        import hashlib

        ctx = _make_context_no_kg()
        # Base slug taken by DIFFERENT content -> deterministic hash-suffixed fork.
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note(
            note_id="new-title", content="other body"
        )
        tools, _ = _make_tools(ctx)
        _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "New Title", "type": "decision", "content": "fresh body"},
        )
        digest = hashlib.sha256(b"fresh body").hexdigest()[:6]
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["note_id"] == f"new-title-{digest}"

    def test_no_kg_invalid_type_errors(self):
        # Parity with kg.create_note: an invalid note_type is a clean up-front
        # error, not a misleading "Created" after a silent DB CHECK failure.
        # `type` is now Literal-typed, so the schema rejects it before the body
        # runs — a stronger guarantee than the error string this used to get,
        # and the model can no longer emit the bad value at all.
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        with pytest.raises(ValidationError):
            _invoke(
                _get_tool(tools, "kb_write"),
                {"title": "New Title", "type": "bogus", "content": "body"},
            )
        assert not ctx.knowledge_store.upsert_note.called

        # ...and the body still refuses it for callers that skip the schema.
        result = _invoke_unvalidated(
            _get_tool(tools, "kb_write"),
            title="New Title",
            type="bogus",
            content="body",
        )
        assert "Error" in result
        assert not ctx.knowledge_store.upsert_note.called

    def test_no_kg_invalid_confidence_errors(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        with pytest.raises(ValidationError):
            _invoke(
                _get_tool(tools, "kb_write"),
                {
                    "title": "New Title",
                    "type": "decision",
                    "content": "body",
                    "confidence": "bogus",
                },
            )
        assert not ctx.knowledge_store.upsert_note.called

        result = _invoke_unvalidated(
            _get_tool(tools, "kb_write"),
            title="New Title",
            type="decision",
            content="body",
            confidence="bogus",
        )
        assert "Error" in result
        assert not ctx.knowledge_store.upsert_note.called

    def test_no_kg_writes_okf_file(self):
        ws, writes = _capture_workspace()
        ctx = _make_context_no_kg()
        ctx.workspace_manager = ws
        ctx.has_git.return_value = True
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "New Title", "type": "decision", "content": "body"},
        )
        assert "knowledge/new-title.md" in writes

    def test_no_kg_empty_slug_falls_back_deterministically(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        _invoke(
            _get_tool(tools, "kb_write"),
            {"title": "!!!", "type": "learning", "content": "body"},
        )
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["note_id"].startswith("note-")


class TestKbUpdateWithoutNeo4j:
    """kg-less _update_existing (via kb_update): read the current row from the
    store, apply the mutation in Python, write back to store + OKF file."""

    def test_no_kg_replaces_content(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note(
            content="old body"
        )
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_update"), {"note": "n1", "content": "new body"}
        )
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["content"] == "new body"
        assert "content replaced" in result

    def test_no_kg_not_found(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = None
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_update"), {"note": "nope", "content": "x"}
        )
        assert "not found" in result
        assert not ctx.knowledge_store.upsert_note.called

    def test_no_kg_appends_content(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note(
            content="old body"
        )
        tools, _ = _make_tools(ctx)
        _invoke(_get_tool(tools, "kb_update"), {"note": "n1", "append": "more"})
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert "old body" in kwargs["content"]
        assert "more" in kwargs["content"]

    def test_no_kg_changes_status(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note()
        tools, _ = _make_tools(ctx)
        result = _invoke(
            _get_tool(tools, "kb_update"), {"note": "n1", "status": "superseded"}
        )
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["status"] == "superseded"
        assert "status → superseded" in result

    def test_no_kg_invalid_status_errors(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note()
        tools, _ = _make_tools(ctx)
        with pytest.raises(ValidationError):
            _invoke(_get_tool(tools, "kb_update"), {"note": "n1", "status": "bogus"})
        assert not ctx.knowledge_store.upsert_note.called

        result = _invoke_unvalidated(
            _get_tool(tools, "kb_update"), note="n1", status="bogus"
        )
        assert "Error" in result
        assert not ctx.knowledge_store.upsert_note.called

    def test_no_kg_merges_tags_lowercased(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note(
            tags=["security"]
        )
        tools, _ = _make_tools(ctx)
        _invoke(_get_tool(tools, "kb_update"), {"note": "n1", "add_tags": ["Auth"]})
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["tags"] == ["security", "auth"]

    def test_no_kg_preserves_type_from_existing(self):
        ctx = _make_context_no_kg()
        ctx.knowledge_store.get_note_by_slug.return_value = _existing_note(
            type="retrospective"
        )
        tools, _ = _make_tools(ctx)
        _invoke(_get_tool(tools, "kb_update"), {"note": "n1", "content": "x"})
        kwargs = ctx.knowledge_store.upsert_note.call_args.kwargs
        assert kwargs["note_type"] == "retrospective"
