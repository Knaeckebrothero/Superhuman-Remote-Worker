"""Tests for the DescriptionManager — tool documentation generation and overrides.

Tests docstring extraction, markdown generation, workspace doc writing,
description overrides for deferred tools, and module-level convenience functions.
"""

import pytest
from unittest.mock import MagicMock

from src.tools.description_manager import (
    DescriptionManager,
    get_deferred_tools,
    get_core_tools,
)
from src.tools.registry import TOOL_REGISTRY


# =============================================================================
# Helpers
# =============================================================================


def _make_tool(name: str, description: str = "A test tool"):
    """Create a fake tool object with name and description."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    return tool


# =============================================================================
# DescriptionManager — docstring extraction
# =============================================================================


class TestExtractDocstrings:
    """Tests for extract_docstrings and get_docstring."""

    def test_extract_caches_docstring(self):
        """Should cache tool.description by tool.name."""
        mgr = DescriptionManager()
        tool = _make_tool("read_file", "Read a file from workspace")
        mgr.extract_docstrings([tool])
        assert mgr.get_docstring("read_file") == "Read a file from workspace"

    def test_extract_skips_tools_without_name(self):
        """Should skip tools missing name attribute."""
        mgr = DescriptionManager()
        tool = MagicMock(spec=[])  # No attributes
        mgr.extract_docstrings([tool])
        assert mgr._docstring_cache == {}

    def test_extract_multiple_tools(self):
        """Should extract all tools in one call."""
        mgr = DescriptionManager()
        tools = [_make_tool("a", "desc_a"), _make_tool("b", "desc_b")]
        mgr.extract_docstrings(tools)
        assert mgr.get_docstring("a") == "desc_a"
        assert mgr.get_docstring("b") == "desc_b"

    def test_extract_overwrites_cache(self):
        """Re-extraction should update cached value."""
        mgr = DescriptionManager()
        mgr.extract_docstrings([_make_tool("x", "old")])
        mgr.extract_docstrings([_make_tool("x", "new")])
        assert mgr.get_docstring("x") == "new"

    def test_get_docstring_falls_back_to_registry(self):
        """Should use TOOL_REGISTRY when not in cache."""
        mgr = DescriptionManager()
        # read_file should be in the real TOOL_REGISTRY
        result = mgr.get_docstring("read_file")
        assert result is not None
        assert isinstance(result, str)

    def test_get_docstring_returns_none_unknown(self):
        """Should return None for unknown tool not in cache or registry."""
        mgr = DescriptionManager()
        assert mgr.get_docstring("__nonexistent_tool_xyz__") is None


# =============================================================================
# Markdown generation
# =============================================================================


class TestGenerateToolDescription:
    """Tests for generate_tool_description."""

    def test_known_tool_has_header(self):
        """Should start with # tool_name header."""
        mgr = DescriptionManager()
        result = mgr.generate_tool_description("read_file")
        assert result.startswith("# read_file")

    def test_known_tool_has_category(self):
        """Should include Category field."""
        mgr = DescriptionManager()
        result = mgr.generate_tool_description("read_file")
        assert "**Category:**" in result
        assert "workspace" in result

    def test_known_tool_includes_description(self):
        """Should include description text."""
        mgr = DescriptionManager()
        # Cache a custom docstring
        mgr.extract_docstrings([_make_tool("read_file", "Custom docstring here")])
        result = mgr.generate_tool_description("read_file")
        assert "Custom docstring here" in result

    def test_unknown_tool_says_not_found(self):
        """Should indicate tool not found."""
        mgr = DescriptionManager()
        result = mgr.generate_tool_description("__nonexistent_tool__")
        assert "not found" in result.lower()


class TestGenerateToolIndex:
    """Tests for generate_tool_index."""

    def test_groups_by_category(self):
        """Should have category sections."""
        mgr = DescriptionManager()
        result = mgr.generate_tool_index(["read_file", "todo_complete"])
        assert "Workspace" in result
        assert "Core" in result

    def test_creates_markdown_links(self):
        """Should create links to individual tool docs."""
        mgr = DescriptionManager()
        result = mgr.generate_tool_index(["read_file"])
        assert "[read_file](read_file.md)" in result

    def test_unknown_tools_grouped_as_other(self):
        """Tools not in registry should be in 'other' category."""
        mgr = DescriptionManager()
        result = mgr.generate_tool_index(["__fake_tool__"])
        assert "Other" in result


# =============================================================================
# Workspace docs generation
# =============================================================================


class TestGenerateWorkspaceDocs:
    """Tests for generate_workspace_docs."""

    def test_creates_output_directory(self, tmp_path):
        """Should create output dir if it doesn't exist."""
        mgr = DescriptionManager()
        output = tmp_path / "tools"
        mgr.generate_workspace_docs(["read_file"], output)
        assert output.exists()

    def test_creates_readme(self, tmp_path):
        """Should write README.md index."""
        mgr = DescriptionManager()
        output = tmp_path / "tools"
        mgr.generate_workspace_docs(["read_file"], output)
        assert (output / "README.md").exists()

    def test_creates_tool_files(self, tmp_path):
        """Should write individual tool .md files."""
        mgr = DescriptionManager()
        output = tmp_path / "tools"
        mgr.generate_workspace_docs(["read_file", "write_file"], output)
        assert (output / "read_file.md").exists()
        assert (output / "write_file.md").exists()

    def test_returns_file_count(self, tmp_path):
        """Should return 1 (index) + N (tools)."""
        mgr = DescriptionManager()
        output = tmp_path / "tools"
        count = mgr.generate_workspace_docs(["read_file", "write_file"], output)
        assert count == 3  # README + 2 tool files


# =============================================================================
# Description overrides
# =============================================================================


class TestApplyOverrides:
    """Tests for apply_overrides."""

    def test_non_deferred_unchanged(self):
        """Tools without defer_to_workspace should pass through."""
        mgr = DescriptionManager()
        tool = _make_tool("read_file", "Full description")  # read_file is not deferred
        result = mgr.apply_overrides([tool])
        assert len(result) == 1
        # Tool should be the same object (no modification)
        assert result[0] is tool

    def test_returns_new_list(self):
        """Should return a new list."""
        mgr = DescriptionManager()
        original = [_make_tool("read_file")]
        result = mgr.apply_overrides(original)
        assert result is not original

    def test_deferred_tool_gets_short_description(self):
        """Tools with defer_to_workspace=True should get short_description."""
        # Find a deferred tool from the real registry
        deferred = get_deferred_tools()
        if not deferred:
            pytest.skip("No deferred tools in registry")

        tool_name = deferred[0]
        meta = TOOL_REGISTRY[tool_name]
        short = meta.get("short_description")
        if not short:
            pytest.skip(f"Tool '{tool_name}' has no short_description")

        mgr = DescriptionManager()
        tool = _make_tool(tool_name, "Long original description")
        # Mock model_copy for Pydantic-like behavior
        copied = _make_tool(tool_name, short)
        tool.model_copy = MagicMock(return_value=copied)

        result = mgr.apply_overrides([tool])
        assert len(result) == 1
        tool.model_copy.assert_called_once()


# =============================================================================
# Module-level functions
# =============================================================================


class TestModuleFunctions:
    """Tests for get_deferred_tools and get_core_tools."""

    def test_deferred_and_core_are_complementary(self):
        """Deferred + core should cover all tools."""
        deferred = set(get_deferred_tools())
        core = set(get_core_tools())
        all_tools = set(TOOL_REGISTRY.keys())

        assert deferred | core == all_tools
        assert deferred & core == set()  # No overlap

    def test_deferred_tools_have_flag(self):
        """Every deferred tool must have defer_to_workspace=True."""
        for name in get_deferred_tools():
            assert TOOL_REGISTRY[name].get("defer_to_workspace") is True

    def test_core_tools_not_deferred(self):
        """Core tools must not have defer_to_workspace=True."""
        for name in get_core_tools():
            assert not TOOL_REGISTRY[name].get("defer_to_workspace", False)
