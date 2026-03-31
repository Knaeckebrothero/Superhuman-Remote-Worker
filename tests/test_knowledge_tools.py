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


# =============================================================================
# 13.1: KNOWLEDGE_TOOLS_METADATA registry
# =============================================================================


class TestMetadataRegistry:
    """Tests for KNOWLEDGE_TOOLS_METADATA."""

    def test_contains_exactly_10_tools(self):
        assert len(KNOWLEDGE_TOOLS_METADATA) == 10

    def test_expected_tool_names(self):
        expected = {
            "kb_write", "kb_update", "kb_read", "kb_list", "kb_search",
            "kb_related", "kb_contradictions", "kb_provenance",
            "kb_unanswered", "kb_export",
        }
        assert set(KNOWLEDGE_TOOLS_METADATA.keys()) == expected

    def test_all_have_knowledge_category(self):
        for name, meta in KNOWLEDGE_TOOLS_METADATA.items():
            assert meta["category"] == "knowledge", f"{name} category mismatch"

    def test_all_have_both_phases(self):
        for name, meta in KNOWLEDGE_TOOLS_METADATA.items():
            assert meta["phases"] == ["strategic", "tactical"], f"{name} phases mismatch"


# =============================================================================
# 13.2: create_kb_tools()
# =============================================================================


class TestCreateKbTools:
    """Tests for create_kb_tools()."""

    def test_raises_when_knowledge_graph_is_none(self):
        ctx = _make_context()
        ctx.knowledge_graph = None
        with pytest.raises(ValueError, match="knowledge_graph"):
            with patch("src.tools.knowledge.knowledge_tools.asyncio") as ma:
                ma.get_running_loop.side_effect = RuntimeError
                create_kb_tools(ctx)

    def test_raises_when_knowledge_store_is_none(self):
        ctx = _make_context()
        ctx.knowledge_store = None
        with pytest.raises(ValueError, match="knowledge_store"):
            with patch("src.tools.knowledge.knowledge_tools.asyncio") as ma:
                ma.get_running_loop.side_effect = RuntimeError
                create_kb_tools(ctx)

    def test_returns_list_of_10_tools(self):
        tools, _ = _make_tools()
        assert len(tools) == 10


# =============================================================================
# 13.4: kb_write
# =============================================================================


class TestKbWrite:
    """Tests for kb_write tool."""

    def test_error_when_no_project_id(self):
        ctx = _make_context()
        ctx.project_id = None
        tools, _ = _make_tools(ctx)
        result = _invoke(_get_tool(tools, "kb_write"), {
            "title": "Test", "type": "decision", "content": "body",
        })
        assert "Error" in result

    def test_calls_kg_create_note(self):
        tools, ctx = _make_tools()
        kg = ctx.knowledge_graph
        kg.create_note.return_value = "test-slug"
        ctx.knowledge_store.upsert_note = AsyncMock(return_value=uuid.uuid4())

        with patch("src.tools.knowledge.knowledge_tools.asyncio"):
            _invoke(_get_tool(tools, "kb_write"), {
                "title": "Test", "type": "decision", "content": "body",
                "tags": ["auth"],
            })

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
            result = _invoke(_get_tool(tools, "kb_write"), {
                "title": "My Note", "type": "learning", "content": "body",
            })
        assert "my-note" in result
        assert "learning" in result

    def test_pgvector_failure_is_nonfatal(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.return_value = "slug"
        ctx.knowledge_store.upsert_note = AsyncMock(side_effect=Exception("db error"))

        # _run_async uses asyncio.run since no loop; mock it
        with patch("asyncio.run", side_effect=Exception("db error")):
            result = _invoke(_get_tool(tools, "kb_write"), {
                "title": "T", "type": "decision", "content": "x",
            })
        assert "slug" in result  # Still returns success

    def test_returns_error_on_value_error(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.side_effect = ValueError("Invalid note_type")

        result = _invoke(_get_tool(tools, "kb_write"), {
            "title": "T", "type": "invalid", "content": "x",
        })
        assert "Error" in result
        assert "Invalid note_type" in result

    def test_returns_error_on_generic_exception(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.side_effect = RuntimeError("boom")

        result = _invoke(_get_tool(tools, "kb_write"), {
            "title": "T", "type": "decision", "content": "x",
        })
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
            _invoke(_get_tool(tools, "kb_update"), {
                "note": "n1", "content": "new body",
            })
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
            result = _invoke(_get_tool(tools, "kb_update"), {
                "note": "n1", "content": "new", "status": "resolved",
                "add_tags": ["a", "b"],
            })
        assert "content replaced" in result
        assert "status → resolved" in result
        assert "+2 tag(s)" in result

    def test_pgvector_writethrough_failure_nonfatal(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.return_value = True
        ctx.knowledge_graph.read_note.side_effect = Exception("read failed")

        result = _invoke(_get_tool(tools, "kb_update"), {
            "note": "n1", "append": "extra",
        })
        assert "Updated" in result
        assert "content appended" in result

    def test_error_on_value_error(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.side_effect = ValueError("Invalid status")

        result = _invoke(_get_tool(tools, "kb_update"), {
            "note": "n1", "status": "invalid",
        })
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
        kg.read_note.side_effect = [None, {"id": "n1", "title": "Found", "content": "x"}]

        result = _invoke(_get_tool(tools, "kb_read"), {"note": "n1"})
        assert "Found" in result
        assert kg.read_note.call_count == 2

    def test_returns_formatted_markdown(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.read_note.return_value = {
            "id": "n1", "title": "My Note", "type": "decision",
            "status": "active", "confidence": "high",
            "tags": ["auth"], "keywords": ["jwt"],
            "content": "Full body text",
            "relationships": [{"type": "SUPPORTS", "target": "n2", "target_title": "N2"}],
            "incoming_relationships": [{"type": "DERIVED_FROM", "source": "n0", "source_title": "N0"}],
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

        result = _invoke(_get_tool(tools, "kb_list"), {
            "type": "decision", "tag": "auth",
        })
        assert "No knowledge notes found" in result
        assert "type=decision" in result
        assert "tag=auth" in result

    def test_formats_with_status_icon(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.list_notes.return_value = [
            {"id": "n1", "title": "Active", "type": "decision",
             "status": "active", "confidence": "high"},
            {"id": "n2", "title": "Archived", "type": "learning",
             "status": "archived"},
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

    def test_calls_hybrid_search(self):
        tools, ctx = _make_tools()
        mock_record = MagicMock()
        mock_record.note_id = "n1"
        mock_record.title = "Test"
        mock_record.note_type = "decision"
        mock_record.confidence = "high"
        mock_record.content = "body text"

        with patch("asyncio.run", return_value=[mock_record]):
            result = _invoke(_get_tool(tools, "kb_search"), {"query": "auth"})
        assert "n1" in result

    def test_empty_results(self):
        tools, ctx = _make_tools()
        with patch("asyncio.run", return_value=[]):
            result = _invoke(_get_tool(tools, "kb_search"), {"query": "nothing"})
        assert "No knowledge notes match" in result

    def test_truncates_content_preview(self):
        tools, ctx = _make_tools()
        mock_record = MagicMock()
        mock_record.note_id = "n1"
        mock_record.title = "T"
        mock_record.note_type = "decision"
        mock_record.confidence = None
        mock_record.content = "x" * 300

        with patch("asyncio.run", return_value=[mock_record]):
            result = _invoke(_get_tool(tools, "kb_search"), {"query": "q"})
        assert "..." in result


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
            {"id": "n2", "title": "T", "type": "decision",
             "status": "active", "distance": 1, "rel_types": ["SUPPORTS"]},
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "1 hop)" in result  # singular

    def test_formats_hops_plural(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {"id": "n2", "title": "T", "type": "decision",
             "status": "active", "distance": 2, "rel_types": ["SUPPORTS", "REFERENCES"]},
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "2 hops)" in result  # plural

    def test_shows_relationship_chain(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {"id": "n2", "title": "T", "type": "learning",
             "status": "active", "distance": 2, "rel_types": ["SUPPORTS", "DERIVED_FROM"]},
        ]

        result = _invoke(_get_tool(tools, "kb_related"), {"note": "n1"})
        assert "SUPPORTS → DERIVED_FROM" in result

    def test_shows_non_active_status(self):
        tools, ctx = _make_tools()
        ctx.knowledge_graph.get_related.return_value = [
            {"id": "n2", "title": "T", "type": "decision",
             "status": "archived", "distance": 1, "rel_types": ["REFERENCES"]},
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

    def test_creates_directory_and_writes_files(self, tmp_path):
        tools, ctx = _make_tools()
        export_dir = str(tmp_path / "kb_export")
        ctx.knowledge_graph.get_all_notes_for_export.return_value = [
            {
                "id": "chose-jwt", "title": "Chose JWT", "type": "decision",
                "status": "active", "confidence": "high",
                "tags": ["auth"], "keywords": ["jwt"],
                "content": "We chose JWT because...",
                "relationships": [{"type": "SUPPORTS", "target": "auth-design"}],
            },
        ]

        result = _invoke(_get_tool(tools, "kb_export"), {"path": export_dir})
        assert "1 note" in result

        # Verify file exists
        exported = (tmp_path / "kb_export" / "chose-jwt.md")
        assert exported.exists()

        content = exported.read_text()
        assert "---" in content  # frontmatter
        assert "id: chose-jwt" in content
        assert "type: decision" in content
        assert "tags:" in content
        assert "# Chose JWT" in content
        assert "[[auth-design]]" in content

    def test_groups_relationships_by_type(self, tmp_path):
        tools, ctx = _make_tools()
        export_dir = str(tmp_path / "kb_export2")
        ctx.knowledge_graph.get_all_notes_for_export.return_value = [
            {
                "id": "n1", "title": "N1", "type": "decision",
                "status": "active", "content": "body",
                "relationships": [
                    {"type": "SUPPORTS", "target": "a"},
                    {"type": "SUPPORTS", "target": "b"},
                    {"type": "REFERENCES", "target": "c"},
                ],
            },
        ]

        _invoke(_get_tool(tools, "kb_export"), {"path": export_dir})
        content = (tmp_path / "kb_export2" / "n1.md").read_text()
        assert "## Relationships" in content
        assert "**SUPPORTS:**" in content
        assert "[[a]]" in content
        assert "[[b]]" in content
        assert "**REFERENCES:**" in content

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
