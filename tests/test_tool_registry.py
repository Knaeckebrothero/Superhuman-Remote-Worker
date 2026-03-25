"""Tests for the tool registry and phase filtering system.

Tests the centralized tool registry, phase-aware filtering, dynamic tool
registration, and instruction enforcement wrappers.
"""

import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass

from src.tools.registry import (
    TOOL_REGISTRY,
    get_available_tools,
    get_tools_by_category,
    get_categories,
    filter_tools_by_phase,
    get_tools_for_phase,
    get_phase_tool_summary,
    load_tools,
    load_tools_for_phase,
    register_tool,
    unregister_tool,
    apply_instruction_enforcement,
)
from src.tools.context import ToolContext


# =============================================================================
# Registry Metadata Integrity
# =============================================================================


class TestRegistryMetadata:
    """Tests that TOOL_REGISTRY has consistent, correct metadata."""

    def test_every_tool_has_category(self):
        """Every registered tool must have a category field."""
        for name, meta in TOOL_REGISTRY.items():
            assert "category" in meta, f"Tool '{name}' missing 'category'"

    def test_every_tool_has_phases(self):
        """Every registered tool must have a phases list."""
        for name, meta in TOOL_REGISTRY.items():
            phases = meta.get("phases", ["strategic", "tactical"])
            assert isinstance(phases, list), f"Tool '{name}' phases is not a list"
            for p in phases:
                assert p in ("strategic", "tactical"), (
                    f"Tool '{name}' has unknown phase '{p}'"
                )

    def test_every_tool_has_description(self):
        """Every registered tool must have a description."""
        for name, meta in TOOL_REGISTRY.items():
            assert "description" in meta, f"Tool '{name}' missing 'description'"
            assert len(meta["description"]) > 0, f"Tool '{name}' has empty description"

    def test_job_complete_is_strategic_only(self):
        """job_complete must be strategic-only."""
        assert "job_complete" in TOOL_REGISTRY
        assert TOOL_REGISTRY["job_complete"]["phases"] == ["strategic"]

    def test_next_phase_todos_is_strategic_only(self):
        """next_phase_todos must be strategic-only."""
        assert "next_phase_todos" in TOOL_REGISTRY
        assert TOOL_REGISTRY["next_phase_todos"]["phases"] == ["strategic"]

    def test_todo_rewind_is_tactical_only(self):
        """todo_rewind must be tactical-only."""
        assert "todo_rewind" in TOOL_REGISTRY
        assert TOOL_REGISTRY["todo_rewind"]["phases"] == ["tactical"]

    def test_mark_complete_available_both_phases(self):
        """mark_complete must be available in both phases."""
        meta = TOOL_REGISTRY["mark_complete"]
        assert "strategic" in meta["phases"]
        assert "tactical" in meta["phases"]

    def test_todo_complete_available_both_phases(self):
        """todo_complete must be available in both phases."""
        meta = TOOL_REGISTRY["todo_complete"]
        assert "strategic" in meta["phases"]
        assert "tactical" in meta["phases"]

    def test_no_duplicate_tool_names(self):
        """Registry should not contain duplicate tool names (dict guarantees this,
        but let's verify the count of metadata sources adds up)."""
        all_tools = get_available_tools()
        assert len(all_tools) == len(TOOL_REGISTRY)

    def test_expected_categories_present(self):
        """All expected tool categories must be registered."""
        categories = get_categories()
        expected = {
            "workspace",
            "core",
            "document",
            "research",
            "citation",
            "graph",
            "sql",
            "mongodb",
            "git",
            "coding",
        }
        for cat in expected:
            assert cat in categories, f"Missing category: {cat}"

    def test_registry_has_minimum_tool_count(self):
        """Registry should have a reasonable number of tools."""
        assert len(TOOL_REGISTRY) >= 40, f"Expected 40+ tools, got {len(TOOL_REGISTRY)}"


# =============================================================================
# Query Functions
# =============================================================================


class TestGetAvailableTools:
    """Tests for get_available_tools()."""

    def test_returns_copy(self):
        """Returned dict should be a copy, not the original."""
        result = get_available_tools()
        result["__test_tool__"] = {"category": "test"}
        assert "__test_tool__" not in TOOL_REGISTRY

    def test_contains_all_tools(self):
        """Returned dict should have same keys as TOOL_REGISTRY."""
        result = get_available_tools()
        assert set(result.keys()) == set(TOOL_REGISTRY.keys())


class TestGetToolsByCategory:
    """Tests for get_tools_by_category()."""

    def test_workspace_returns_file_tools(self):
        """Workspace category should include file operation tools."""
        tools = get_tools_by_category("workspace")
        assert "read_file" in tools
        assert "write_file" in tools
        assert "list_files" in tools

    def test_core_returns_todo_tools(self):
        """Core category should include todo and job tools."""
        tools = get_tools_by_category("core")
        assert "todo_complete" in tools
        assert "job_complete" in tools
        assert "next_phase_todos" in tools

    def test_unknown_category_returns_empty(self):
        """Unknown category should return empty list."""
        tools = get_tools_by_category("nonexistent_category_xyz")
        assert tools == []

    def test_git_category(self):
        """Git category should include version control tools."""
        tools = get_tools_by_category("git")
        assert "git_log" in tools
        assert "git_diff" in tools

    def test_each_category_nonempty(self):
        """Every known category should have at least one tool."""
        for cat in get_categories():
            tools = get_tools_by_category(cat)
            assert len(tools) > 0, f"Category '{cat}' has no tools"


class TestGetCategories:
    """Tests for get_categories()."""

    def test_returns_set(self):
        """Should return a set."""
        result = get_categories()
        assert isinstance(result, set)

    def test_minimum_categories(self):
        """Should contain at least the 10 standard categories."""
        result = get_categories()
        assert len(result) >= 10


class TestFilterToolsByPhase:
    """Tests for filter_tools_by_phase()."""

    def test_strategic_includes_job_complete(self):
        """Strategic phase should include job_complete."""
        result = filter_tools_by_phase(["job_complete", "read_file"], "strategic")
        assert "job_complete" in result

    def test_tactical_excludes_job_complete(self):
        """Tactical phase should exclude job_complete."""
        result = filter_tools_by_phase(["job_complete", "read_file"], "tactical")
        assert "job_complete" not in result

    def test_strategic_excludes_todo_rewind(self):
        """Strategic phase should exclude todo_rewind."""
        result = filter_tools_by_phase(["todo_rewind", "read_file"], "strategic")
        assert "todo_rewind" not in result

    def test_tactical_includes_todo_rewind(self):
        """Tactical phase should include todo_rewind."""
        result = filter_tools_by_phase(["todo_rewind", "read_file"], "tactical")
        assert "todo_rewind" in result

    def test_both_phases_include_read_file(self):
        """read_file should be available in both phases."""
        strategic = filter_tools_by_phase(["read_file"], "strategic")
        tactical = filter_tools_by_phase(["read_file"], "tactical")
        assert "read_file" in strategic
        assert "read_file" in tactical

    def test_unknown_tool_silently_dropped(self):
        """Unknown tool names should be silently dropped."""
        result = filter_tools_by_phase(["nonexistent_tool_xyz"], "strategic")
        assert result == []

    def test_empty_input_returns_empty(self):
        """Empty tool list should return empty list."""
        result = filter_tools_by_phase([], "strategic")
        assert result == []

    def test_unknown_phase_returns_empty(self):
        """Unknown phase should return empty list (no tool has it)."""
        all_tools = list(TOOL_REGISTRY.keys())
        result = filter_tools_by_phase(all_tools, "nonexistent_phase")
        assert result == []

    def test_strategic_includes_next_phase_todos(self):
        """Strategic should include next_phase_todos."""
        result = filter_tools_by_phase(["next_phase_todos"], "strategic")
        assert "next_phase_todos" in result

    def test_tactical_excludes_next_phase_todos(self):
        """Tactical should exclude next_phase_todos."""
        result = filter_tools_by_phase(["next_phase_todos"], "tactical")
        assert "next_phase_todos" not in result


class TestGetToolsForPhase:
    """Tests for get_tools_for_phase()."""

    def test_strategic_includes_strategic_tools(self):
        """Strategic phase should include strategic-only tools."""
        tools = get_tools_for_phase("strategic")
        assert "job_complete" in tools
        assert "next_phase_todos" in tools

    def test_tactical_includes_tactical_tools(self):
        """Tactical phase should include tactical-only tools."""
        tools = get_tools_for_phase("tactical")
        assert "todo_rewind" in tools

    def test_strategic_excludes_tactical_only(self):
        """Strategic should exclude tactical-only tools."""
        tools = get_tools_for_phase("strategic")
        assert "todo_rewind" not in tools

    def test_tactical_excludes_strategic_only(self):
        """Tactical should exclude strategic-only tools."""
        tools = get_tools_for_phase("tactical")
        assert "job_complete" not in tools
        assert "next_phase_todos" not in tools

    def test_both_include_both_phase_tools(self):
        """Both phases should include both-phase tools."""
        strategic = get_tools_for_phase("strategic")
        tactical = get_tools_for_phase("tactical")
        for tool in ["read_file", "write_file", "mark_complete", "todo_complete"]:
            assert tool in strategic, f"{tool} missing from strategic"
            assert tool in tactical, f"{tool} missing from tactical"


class TestGetPhaseToolSummary:
    """Tests for get_phase_tool_summary()."""

    def test_has_both_phase_keys(self):
        """Should have 'strategic' and 'tactical' top-level keys."""
        summary = get_phase_tool_summary()
        assert "strategic" in summary
        assert "tactical" in summary

    def test_categories_in_summary(self):
        """Each phase should have categorized tools."""
        summary = get_phase_tool_summary()
        # Both phases should have at least workspace and core
        assert "workspace" in summary["strategic"]
        assert "core" in summary["strategic"]
        assert "workspace" in summary["tactical"]

    def test_strategic_core_includes_job_complete(self):
        """Strategic core tools should include job_complete."""
        summary = get_phase_tool_summary()
        assert "job_complete" in summary["strategic"].get("core", [])

    def test_tactical_core_excludes_job_complete(self):
        """Tactical core tools should not include job_complete."""
        summary = get_phase_tool_summary()
        assert "job_complete" not in summary["tactical"].get("core", [])


# =============================================================================
# Tool Loading Validation
# =============================================================================


class TestLoadToolsValidation:
    """Tests for load_tools() validation logic (not actual tool creation)."""

    def test_unknown_tool_raises_valueerror(self):
        """Unknown tool names should raise ValueError."""
        ctx = ToolContext()
        with pytest.raises(ValueError, match="Unknown tools"):
            load_tools(["nonexistent_tool_xyz"], ctx)

    def test_valueerror_lists_available_tools(self):
        """ValueError message should list available tools."""
        ctx = ToolContext()
        with pytest.raises(ValueError) as exc_info:
            load_tools(["nonexistent_tool_xyz"], ctx)
        assert "Available tools:" in str(exc_info.value)

    def test_empty_tool_list_returns_empty(self):
        """Empty tool list should return empty list."""
        ctx = ToolContext()
        result = load_tools([], ctx)
        assert result == []

    def test_workspace_tools_require_workspace_manager(self):
        """Requesting workspace tools without workspace_manager raises ValueError."""
        ctx = ToolContext()  # no workspace_manager
        with pytest.raises(ValueError, match="workspace_manager"):
            load_tools(["read_file"], ctx)

    def test_core_tools_require_workspace_manager(self):
        """Requesting core tools without workspace_manager raises ValueError."""
        ctx = ToolContext()
        with pytest.raises(ValueError, match="workspace_manager"):
            load_tools(["todo_complete"], ctx)

    def test_core_tools_require_todo_manager(self):
        """Requesting core tools without todo_manager raises ValueError."""
        ws = MagicMock()
        ws.is_initialized = True
        ctx = ToolContext(workspace_manager=ws)  # no todo_manager
        with pytest.raises(ValueError, match="todo_manager"):
            load_tools(["todo_complete"], ctx)


class TestLoadToolsForPhase:
    """Tests for load_tools_for_phase()."""

    def test_empty_after_filtering_logs_warning(self):
        """Should log warning when no tools survive phase filtering."""
        ctx = ToolContext()
        # All strategic-only tools in tactical = empty
        result = load_tools_for_phase(["job_complete"], "tactical", ctx)
        assert result == []

    def test_filters_before_loading(self):
        """Should filter tools by phase before loading."""
        # If we try to load job_complete in tactical, it should be filtered out
        # and not cause a workspace_manager error
        ctx = ToolContext()
        result = load_tools_for_phase(["job_complete"], "tactical", ctx)
        assert result == []


# =============================================================================
# Dynamic Tool Registration
# =============================================================================


class TestRegisterTool:
    """Tests for register_tool() and unregister_tool()."""

    def setup_method(self):
        """Clean up test tools before each test."""
        # Remove any leftover test tools
        TOOL_REGISTRY.pop("__test_custom_tool__", None)

    def teardown_method(self):
        """Clean up after each test."""
        TOOL_REGISTRY.pop("__test_custom_tool__", None)

    def test_register_adds_to_registry(self):
        """register_tool should add tool to TOOL_REGISTRY."""
        register_tool(
            name="__test_custom_tool__",
            module="test_module",
            function="test_func",
            description="A test tool",
            category="custom",
        )
        assert "__test_custom_tool__" in TOOL_REGISTRY

    def test_register_stores_metadata(self):
        """Registered tool should have correct metadata."""
        register_tool(
            name="__test_custom_tool__",
            module="test_module",
            function="test_func",
            description="A test tool",
            category="custom",
        )
        meta = TOOL_REGISTRY["__test_custom_tool__"]
        assert meta["module"] == "test_module"
        assert meta["function"] == "test_func"
        assert meta["description"] == "A test tool"
        assert meta["category"] == "custom"

    def test_register_stores_extra_kwargs(self):
        """Extra kwargs should be stored as metadata."""
        register_tool(
            name="__test_custom_tool__",
            module="m",
            function="f",
            description="d",
            phases=["tactical"],
            placeholder=True,
        )
        meta = TOOL_REGISTRY["__test_custom_tool__"]
        assert meta["phases"] == ["tactical"]
        assert meta["placeholder"] is True

    def test_register_overwrites_existing(self):
        """Registering same name should overwrite."""
        register_tool(
            name="__test_custom_tool__", module="m1", function="f1", description="d1"
        )
        register_tool(
            name="__test_custom_tool__", module="m2", function="f2", description="d2"
        )
        assert TOOL_REGISTRY["__test_custom_tool__"]["module"] == "m2"

    def test_unregister_removes_tool(self):
        """unregister_tool should remove tool from registry."""
        register_tool(
            name="__test_custom_tool__", module="m", function="f", description="d"
        )
        result = unregister_tool("__test_custom_tool__")
        assert result is True
        assert "__test_custom_tool__" not in TOOL_REGISTRY

    def test_unregister_nonexistent_returns_false(self):
        """unregister_tool should return False for unknown tools."""
        result = unregister_tool("__nonexistent_tool_xyz__")
        assert result is False

    def test_unregistered_tool_not_in_available(self):
        """After unregistering, tool should not appear in get_available_tools."""
        register_tool(
            name="__test_custom_tool__", module="m", function="f", description="d"
        )
        unregister_tool("__test_custom_tool__")
        assert "__test_custom_tool__" not in get_available_tools()


# =============================================================================
# Instruction Enforcement
# =============================================================================


@dataclass
class FakeInstructionEntry:
    """Fake InstructionFileEntry for testing."""

    file: str
    trigger_type: str
    trigger_target: str
    enforce: bool


class TestApplyInstructionEnforcement:
    """Tests for apply_instruction_enforcement()."""

    def _make_tool(self, name: str):
        """Create a fake tool with name and func."""
        tool = MagicMock()
        tool.name = name
        tool.func = lambda *a, **kw: f"{name} called"
        return tool

    def test_no_instruction_files_returns_unchanged(self):
        """When context has no instruction_files, tools are returned as-is."""
        ctx = ToolContext()
        tools = [self._make_tool("read_file"), self._make_tool("write_file")]
        result = apply_instruction_enforcement(tools, ctx)
        assert result is tools

    def test_no_enforcement_entries_returns_unchanged(self):
        """When no entries have enforce=True + before_tool, tools unchanged."""
        ctx = ToolContext()
        # Phase trigger, not before_tool
        ctx._instruction_files = [
            FakeInstructionEntry(
                file="guide.md",
                trigger_type="phase",
                trigger_target="strategic",
                enforce=False,
            )
        ]
        tools = [self._make_tool("read_file")]
        result = apply_instruction_enforcement(tools, ctx)
        # func should still work normally
        assert result[0].func() == "read_file called"

    def test_wraps_matching_tool(self):
        """Tool matching enforcement entry should be wrapped."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry(
                file="todo_guide.md",
                trigger_type="before_tool",
                trigger_target="next_phase_todos",
                enforce=True,
            )
        ]
        tool = self._make_tool("next_phase_todos")
        tools = [tool]
        apply_instruction_enforcement(tools, ctx)

        # Without reading the file, should return error
        result = tool.func()
        assert "Error" in result
        assert "todo_guide.md" in result

    def test_enforcement_passes_after_read(self):
        """Wrapped tool should work after reading the required file."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry(
                file="todo_guide.md",
                trigger_type="before_tool",
                trigger_target="next_phase_todos",
                enforce=True,
            )
        ]
        tool = self._make_tool("next_phase_todos")
        tools = [tool]
        apply_instruction_enforcement(tools, ctx)

        # Record the file as read
        ctx.record_file_read("todo_guide.md")

        # Now the wrapped tool should call through to original
        result = tool.func()
        assert result == "next_phase_todos called"

    def test_non_matching_tools_unaffected(self):
        """Tools not matching enforcement should not be wrapped."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry(
                file="todo_guide.md",
                trigger_type="before_tool",
                trigger_target="next_phase_todos",
                enforce=True,
            )
        ]
        read_tool = self._make_tool("read_file")
        tools = [read_tool]
        apply_instruction_enforcement(tools, ctx)

        # read_file should work normally
        result = read_tool.func()
        assert result == "read_file called"

    def test_multiple_enforcement_files(self):
        """Multiple enforcement files on same tool — all must be read."""
        ctx = ToolContext()
        ctx._instruction_files = [
            FakeInstructionEntry(
                file="guide_a.md",
                trigger_type="before_tool",
                trigger_target="my_tool",
                enforce=True,
            ),
            FakeInstructionEntry(
                file="guide_b.md",
                trigger_type="before_tool",
                trigger_target="my_tool",
                enforce=True,
            ),
        ]
        tool = self._make_tool("my_tool")
        tools = [tool]
        apply_instruction_enforcement(tools, ctx)

        # Only reading one file shouldn't be enough
        ctx.record_file_read("guide_a.md")
        result = tool.func()
        assert "Error" in result
        assert "guide_b.md" in result

        # Reading both should pass
        ctx.record_file_read("guide_b.md")
        result = tool.func()
        assert result == "my_tool called"


# =============================================================================
# Datasource warning paths
# =============================================================================


class TestLoadToolsDatasourceWarnings:
    """Tests for load_tools() datasource warning paths."""

    def test_graph_tools_warn_without_neo4j(self):
        """Graph tools should warn (not raise) when no neo4j datasource."""
        ctx = ToolContext()  # no datasources
        # This should not raise — just warn and skip
        result = load_tools(["execute_cypher_query"], ctx)
        assert result == []

    def test_sql_tools_warn_without_postgresql(self):
        """SQL tools should warn when no postgresql datasource."""
        ctx = ToolContext()
        result = load_tools(["sql_query"], ctx)
        assert result == []

    def test_mongodb_tools_warn_without_mongodb(self):
        """MongoDB tools should warn when no mongodb datasource."""
        ctx = ToolContext()
        result = load_tools(["mongo_query"], ctx)
        assert result == []

    def test_git_tools_warn_without_git_manager(self):
        """Git tools should warn when workspace_manager has no git_manager."""
        ws = MagicMock()
        ws.is_initialized = True
        ws.git_manager = None
        ctx = ToolContext(workspace_manager=ws)
        result = load_tools(["git_log"], ctx)
        assert result == []
