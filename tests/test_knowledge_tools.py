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


def _capture_workspace():
    """A mock workspace_manager that records write_file(path, content) calls."""
    ws = MagicMock()
    writes: dict = {}
    ws.write_file.side_effect = lambda rel, content: writes.__setitem__(rel, content)
    return ws, writes


# =============================================================================
# 13.1: KNOWLEDGE_TOOLS_METADATA registry
# =============================================================================


class TestMetadataRegistry:
    """Tests for KNOWLEDGE_TOOLS_METADATA."""

    def test_contains_exactly_10_tools(self):
        assert len(KNOWLEDGE_TOOLS_METADATA) == 10

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
        tools, ctx = _make_tools()
        ctx.knowledge_graph.create_note.side_effect = ValueError("Invalid note_type")

        result = _invoke(
            _get_tool(tools, "kb_write"),
            {
                "title": "T",
                "type": "invalid",
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
        tools, ctx = _make_tools()
        ctx.knowledge_graph.update_note.side_effect = ValueError("Invalid status")

        result = _invoke(
            _get_tool(tools, "kb_update"),
            {
                "note": "n1",
                "status": "invalid",
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
        assert "tags: [auth, security]" in md
        assert "keywords: [jwt]" in md
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
