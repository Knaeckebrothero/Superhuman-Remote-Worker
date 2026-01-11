"""Unit tests for workspace tools and tool registry.

Tests the LangGraph tool wrappers and dynamic tool loading system.
"""

import pytest
import tempfile
import sys
import importlib.util
from pathlib import Path
from types import ModuleType

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def _import_module_directly(module_path: Path, module_name: str):
    """Import a module directly without triggering __init__.py side effects."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Import dependencies directly to avoid neo4j import issues
config_path = project_root / "src" / "core" / "config.py"
config_module = _import_module_directly(config_path, "src.core.config")

workspace_manager_path = project_root / "src" / "agent" / "workspace_manager.py"
workspace_manager_module = _import_module_directly(workspace_manager_path, "src.agent.workspace_manager")

WorkspaceManager = workspace_manager_module.WorkspaceManager
WorkspaceConfig = workspace_manager_module.WorkspaceConfig

# Import tools modules - need to set up fake package structure for relative imports
context_path = project_root / "src" / "agent" / "tools" / "context.py"
context_module = _import_module_directly(context_path, "src.agent.tools.context")
ToolContext = context_module.ToolContext

# Import pdf_utils before workspace_tools (workspace_tools has a relative import to pdf_utils)
pdf_utils_path = project_root / "src" / "agent" / "tools" / "pdf_utils.py"
pdf_utils_module = _import_module_directly(pdf_utils_path, "src.agent.tools.pdf_utils")

workspace_tools_path = project_root / "src" / "agent" / "tools" / "workspace_tools.py"
workspace_tools_module = _import_module_directly(workspace_tools_path, "src.agent.tools.workspace_tools")
create_workspace_tools = workspace_tools_module.create_workspace_tools
WORKSPACE_TOOLS_METADATA = workspace_tools_module.WORKSPACE_TOOLS_METADATA

# Create a fake package for the tools module to enable relative imports
tools_package = ModuleType("src.agent.tools")
tools_package.context = context_module
tools_package.pdf_utils = pdf_utils_module
tools_package.workspace_tools = workspace_tools_module
tools_package.ToolContext = ToolContext
tools_package.create_workspace_tools = workspace_tools_module.create_workspace_tools
tools_package.WORKSPACE_TOOLS_METADATA = workspace_tools_module.WORKSPACE_TOOLS_METADATA
sys.modules["src.agent.tools"] = tools_package

# Import todo_tools (needed by registry)
todo_tools_path = project_root / "src" / "agent" / "tools" / "todo_tools.py"
todo_tools_module = _import_module_directly(todo_tools_path, "src.agent.tools.todo_tools")

# Update fake package with todo_tools
tools_package.todo_tools = todo_tools_module
tools_package.create_todo_tools = todo_tools_module.create_todo_tools
tools_package.TODO_TOOLS_METADATA = todo_tools_module.TODO_TOOLS_METADATA

# Import domain tools (needed by registry)
document_tools_path = project_root / "src" / "agent" / "tools" / "document_tools.py"
document_tools_module = _import_module_directly(document_tools_path, "src.agent.tools.document_tools")
tools_package.document_tools = document_tools_module

search_tools_path = project_root / "src" / "agent" / "tools" / "search_tools.py"
search_tools_module = _import_module_directly(search_tools_path, "src.agent.tools.search_tools")
tools_package.search_tools = search_tools_module

citation_tools_path = project_root / "src" / "agent" / "tools" / "citation_tools.py"
citation_tools_module = _import_module_directly(citation_tools_path, "src.agent.tools.citation_tools")
tools_package.citation_tools = citation_tools_module

cache_tools_path = project_root / "src" / "agent" / "tools" / "cache_tools.py"
cache_tools_module = _import_module_directly(cache_tools_path, "src.agent.tools.cache_tools")
tools_package.cache_tools = cache_tools_module

graph_tools_path = project_root / "src" / "agent" / "tools" / "graph_tools.py"
graph_tools_module = _import_module_directly(graph_tools_path, "src.agent.tools.graph_tools")
tools_package.graph_tools = graph_tools_module

# Import vector tools (Phase 8) - optional, may not exist yet
vector_tools_path = project_root / "src" / "agent" / "tools" / "vector_tools.py"
if vector_tools_path.exists():
    vector_tools_module = _import_module_directly(vector_tools_path, "src.agent.tools.vector_tools")
    tools_package.vector_tools = vector_tools_module
    tools_package.create_vector_tools = vector_tools_module.create_vector_tools
    tools_package.VECTOR_TOOLS_METADATA = vector_tools_module.VECTOR_TOOLS_METADATA

# Import completion tools (needed by registry)
completion_tools_path = project_root / "src" / "agent" / "tools" / "completion_tools.py"
completion_tools_module = _import_module_directly(completion_tools_path, "src.agent.tools.completion_tools")
tools_package.completion_tools = completion_tools_module

# Now import registry
registry_path = project_root / "src" / "agent" / "tools" / "registry.py"
registry_module = _import_module_directly(registry_path, "src.agent.tools.registry")
TOOL_REGISTRY = registry_module.TOOL_REGISTRY
load_tools = registry_module.load_tools
get_available_tools = registry_module.get_available_tools
get_tools_by_category = registry_module.get_tools_by_category
get_categories = registry_module.get_categories
register_tool = registry_module.register_tool
unregister_tool = registry_module.unregister_tool


@pytest.fixture
def temp_workspace():
    """Create a temporary directory for workspace testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_manager(temp_workspace):
    """Create a WorkspaceManager with a temporary base path."""
    ws = WorkspaceManager(
        job_id="test-job-123",
        base_path=temp_workspace,
    )
    ws.initialize()
    return ws


@pytest.fixture
def tool_context(workspace_manager):
    """Create a ToolContext with workspace manager."""
    return ToolContext(workspace_manager=workspace_manager)


@pytest.fixture
def workspace_tools(tool_context):
    """Create workspace tools from context."""
    return create_workspace_tools(tool_context)


class TestToolContext:
    """Tests for ToolContext class."""

    def test_context_with_workspace(self, workspace_manager):
        """Test creating context with workspace manager."""
        context = ToolContext(workspace_manager=workspace_manager)
        assert context.has_workspace()
        assert context.job_id == "test-job-123"

    def test_context_without_workspace(self):
        """Test creating context without workspace manager."""
        context = ToolContext()
        assert not context.has_workspace()
        assert context.job_id is None

    def test_context_requires_initialized_workspace(self, temp_workspace):
        """Test that context requires initialized workspace."""
        ws = WorkspaceManager(job_id="test", base_path=temp_workspace)
        # Not initialized
        with pytest.raises(ValueError, match="must be initialized"):
            ToolContext(workspace_manager=ws)

    def test_context_config(self, workspace_manager):
        """Test context configuration access."""
        context = ToolContext(
            workspace_manager=workspace_manager,
            config={"max_size": 1000, "debug": True}
        )
        assert context.get_config("max_size") == 1000
        assert context.get_config("debug") is True
        assert context.get_config("missing", "default") == "default"

    def test_context_connection_checks(self):
        """Test connection availability checks."""
        context = ToolContext()
        assert not context.has_workspace()
        assert not context.has_todo()
        assert not context.has_postgres()
        assert not context.has_neo4j()


class TestCreateWorkspaceTools:
    """Tests for create_workspace_tools function."""

    def test_creates_tools(self, tool_context):
        """Test that tools are created successfully."""
        tools = create_workspace_tools(tool_context)
        assert len(tools) > 0

    def test_requires_workspace_manager(self):
        """Test that workspace manager is required."""
        context = ToolContext()  # No workspace manager
        with pytest.raises(ValueError, match="workspace_manager"):
            create_workspace_tools(context)

    def test_tool_names(self, workspace_tools):
        """Test expected tool names are present."""
        tool_names = {t.name for t in workspace_tools}
        expected = {
            "read_file",
            "write_file",
            "append_file",
            "list_files",
            "delete_file",
            "search_files",
            "file_exists",
            "get_workspace_summary",
        }
        assert expected.issubset(tool_names)


class TestReadFileTool:
    """Tests for read_file tool."""

    def test_read_existing_file(self, workspace_tools, workspace_manager):
        """Test reading an existing file."""
        workspace_manager.write_file("test.txt", "Hello, World!")

        read_tool = next(t for t in workspace_tools if t.name == "read_file")
        result = read_tool.invoke({"path": "test.txt"})

        assert result == "Hello, World!"

    def test_read_nonexistent_file(self, workspace_tools):
        """Test reading a file that doesn't exist."""
        read_tool = next(t for t in workspace_tools if t.name == "read_file")
        result = read_tool.invoke({"path": "nonexistent.txt"})

        assert "Error" in result
        assert "not found" in result.lower()

    def test_read_truncates_large_files(self, workspace_tools, workspace_manager, tool_context):
        """Test that large files are truncated."""
        # Set a small max_read_size for testing
        tool_context.config["max_read_size"] = 100
        tools = create_workspace_tools(tool_context)

        workspace_manager.write_file("large.txt", "x" * 1000)

        read_tool = next(t for t in tools if t.name == "read_file")
        result = read_tool.invoke({"path": "large.txt"})

        assert "exceeds maximum allowed size" in result
        assert "1,000 bytes" in result

    def test_read_path_traversal_blocked(self, workspace_tools):
        """Test that path traversal is blocked."""
        read_tool = next(t for t in workspace_tools if t.name == "read_file")
        result = read_tool.invoke({"path": "../outside.txt"})

        assert "Error" in result


class TestWriteFileTool:
    """Tests for write_file tool."""

    def test_write_creates_file(self, workspace_tools, workspace_manager):
        """Test that write creates a file."""
        write_tool = next(t for t in workspace_tools if t.name == "write_file")
        result = write_tool.invoke({"path": "new.txt", "content": "New content"})

        assert "Written" in result
        assert "new.txt" in result
        assert workspace_manager.read_file("new.txt") == "New content"

    def test_write_creates_parent_dirs(self, workspace_tools, workspace_manager):
        """Test that write creates parent directories."""
        write_tool = next(t for t in workspace_tools if t.name == "write_file")
        result = write_tool.invoke({
            "path": "deep/nested/file.txt",
            "content": "Nested content"
        })

        assert "Written" in result
        assert workspace_manager.read_file("deep/nested/file.txt") == "Nested content"

    def test_write_reports_size(self, workspace_tools):
        """Test that write reports file size."""
        write_tool = next(t for t in workspace_tools if t.name == "write_file")
        result = write_tool.invoke({"path": "sized.txt", "content": "12345"})

        assert "5 bytes" in result


class TestAppendFileTool:
    """Tests for append_file tool."""

    def test_append_to_existing(self, workspace_tools, workspace_manager):
        """Test appending to existing file."""
        workspace_manager.write_file("log.txt", "Line 1\n")

        append_tool = next(t for t in workspace_tools if t.name == "append_file")
        append_tool.invoke({"path": "log.txt", "content": "Line 2\n"})

        assert workspace_manager.read_file("log.txt") == "Line 1\nLine 2\n"

    def test_append_creates_file(self, workspace_tools, workspace_manager):
        """Test appending creates file if it doesn't exist."""
        append_tool = next(t for t in workspace_tools if t.name == "append_file")
        append_tool.invoke({"path": "new_log.txt", "content": "First line"})

        assert workspace_manager.read_file("new_log.txt") == "First line"


class TestListFilesTool:
    """Tests for list_files tool."""

    def test_list_root(self, workspace_tools, workspace_manager):
        """Test listing root directory."""
        workspace_manager.write_file("plans/plan.md", "content")

        list_tool = next(t for t in workspace_tools if t.name == "list_files")
        result = list_tool.invoke({"path": ""})

        assert "plans/" in result

    def test_list_with_pattern(self, workspace_tools, workspace_manager):
        """Test listing with glob pattern."""
        workspace_manager.write_file("notes/note.md", "md")
        workspace_manager.write_file("notes/note.txt", "txt")

        list_tool = next(t for t in workspace_tools if t.name == "list_files")
        result = list_tool.invoke({"path": "notes", "pattern": "*.md"})

        assert "note.md" in result
        assert "note.txt" not in result

    def test_list_empty_directory(self, workspace_tools):
        """Test listing an empty directory."""
        list_tool = next(t for t in workspace_tools if t.name == "list_files")
        result = list_tool.invoke({"path": "archive"})

        assert "No files found" in result


class TestDeleteFileTool:
    """Tests for delete_file tool."""

    def test_delete_file(self, workspace_tools, workspace_manager):
        """Test deleting a file."""
        workspace_manager.write_file("delete_me.txt", "content")

        delete_tool = next(t for t in workspace_tools if t.name == "delete_file")
        result = delete_tool.invoke({"path": "delete_me.txt"})

        assert "Deleted" in result
        assert not workspace_manager.exists("delete_me.txt")

    def test_delete_nonexistent(self, workspace_tools):
        """Test deleting nonexistent file."""
        delete_tool = next(t for t in workspace_tools if t.name == "delete_file")
        result = delete_tool.invoke({"path": "nonexistent.txt"})

        assert "Not found" in result


class TestSearchFilesTool:
    """Tests for search_files tool."""

    def test_search_finds_matches(self, workspace_tools, workspace_manager):
        """Test searching for text."""
        workspace_manager.write_file("doc1.md", "GoBD compliance requirement")
        workspace_manager.write_file("doc2.md", "Other content")

        search_tool = next(t for t in workspace_tools if t.name == "search_files")
        result = search_tool.invoke({"query": "GoBD"})

        assert "doc1.md" in result
        assert "doc2.md" not in result

    def test_search_no_matches(self, workspace_tools, workspace_manager):
        """Test search with no matches."""
        workspace_manager.write_file("doc.md", "Some content")

        search_tool = next(t for t in workspace_tools if t.name == "search_files")
        result = search_tool.invoke({"query": "nonexistent"})

        assert "No matches" in result

    def test_search_case_insensitive(self, workspace_tools, workspace_manager):
        """Test case-insensitive search."""
        workspace_manager.write_file("doc.md", "HELLO world")

        search_tool = next(t for t in workspace_tools if t.name == "search_files")
        result = search_tool.invoke({"query": "hello", "case_sensitive": False})

        assert "doc.md" in result


class TestFileExistsTool:
    """Tests for file_exists tool."""

    def test_exists_file(self, workspace_tools, workspace_manager):
        """Test checking existing file."""
        workspace_manager.write_file("exists.txt", "content")

        exists_tool = next(t for t in workspace_tools if t.name == "file_exists")
        result = exists_tool.invoke({"path": "exists.txt"})

        assert "Exists (file)" in result
        assert "bytes" in result

    def test_exists_directory(self, workspace_tools):
        """Test checking existing directory."""
        exists_tool = next(t for t in workspace_tools if t.name == "file_exists")
        # Use a directory from the default workspace config
        result = exists_tool.invoke({"path": "archive"})

        assert "Exists (directory)" in result

    def test_not_exists(self, workspace_tools):
        """Test checking nonexistent path."""
        exists_tool = next(t for t in workspace_tools if t.name == "file_exists")
        result = exists_tool.invoke({"path": "nonexistent"})

        assert "Not found" in result


class TestGetWorkspaceSummaryTool:
    """Tests for get_workspace_summary tool."""

    def test_summary(self, workspace_tools, workspace_manager):
        """Test getting workspace summary."""
        # Use directories that exist in the default workspace config
        workspace_manager.write_file("documents/doc.pdf", "content")
        workspace_manager.write_file("archive/old.md", "content")

        summary_tool = next(t for t in workspace_tools if t.name == "get_workspace_summary")
        result = summary_tool.invoke({})

        assert "test-job-123" in result
        assert "documents/" in result or "archive/" in result


class TestToolRegistry:
    """Tests for tool registry."""

    def test_get_available_tools(self):
        """Test getting all available tools."""
        tools = get_available_tools()
        assert len(tools) > 0
        assert "read_file" in tools
        assert "write_file" in tools

    def test_get_tools_by_category(self):
        """Test getting tools by category."""
        workspace_tools = get_tools_by_category("workspace")
        assert "read_file" in workspace_tools
        assert "write_file" in workspace_tools

        todo_tools = get_tools_by_category("todo")
        assert "todo_write" in todo_tools

    def test_get_categories(self):
        """Test getting all categories."""
        categories = get_categories()
        assert "workspace" in categories
        assert "todo" in categories
        assert "domain" in categories


class TestLoadTools:
    """Tests for load_tools function."""

    def test_load_workspace_tools(self, tool_context):
        """Test loading workspace tools."""
        tools = load_tools(["read_file", "write_file"], tool_context)

        assert len(tools) == 2
        tool_names = {t.name for t in tools}
        assert tool_names == {"read_file", "write_file"}

    def test_load_unknown_tool_raises(self, tool_context):
        """Test that loading unknown tool raises error."""
        with pytest.raises(ValueError, match="Unknown tools"):
            load_tools(["read_file", "unknown_tool"], tool_context)

    def test_load_domain_tool_requires_dependencies(self, tool_context):
        """Test that domain tools load successfully when dependencies are met."""
        # Domain tools are now implemented (Phase 5 complete)
        # web_search is a domain tool that should be in the registry
        assert "web_search" in TOOL_REGISTRY
        assert TOOL_REGISTRY["web_search"]["category"] == "domain"

    def test_load_requires_workspace_for_workspace_tools(self):
        """Test that workspace tools require workspace manager."""
        context = ToolContext()
        with pytest.raises(ValueError, match="workspace_manager"):
            load_tools(["read_file"], context)


class TestRegisterUnregisterTools:
    """Tests for registering and unregistering custom tools."""

    def test_register_tool(self):
        """Test registering a custom tool."""
        register_tool(
            name="custom_tool",
            module="custom_module",
            function="custom_function",
            description="A custom tool",
            category="custom",
        )

        assert "custom_tool" in TOOL_REGISTRY
        assert TOOL_REGISTRY["custom_tool"]["category"] == "custom"

        # Clean up
        unregister_tool("custom_tool")

    def test_unregister_tool(self):
        """Test unregistering a tool."""
        register_tool(
            name="temp_tool",
            module="temp",
            function="temp",
            description="Temp",
        )

        assert "temp_tool" in TOOL_REGISTRY

        result = unregister_tool("temp_tool")
        assert result is True
        assert "temp_tool" not in TOOL_REGISTRY

    def test_unregister_nonexistent(self):
        """Test unregistering nonexistent tool."""
        result = unregister_tool("nonexistent_tool")
        assert result is False


class TestToolMetadata:
    """Tests for tool metadata."""

    def test_workspace_tools_metadata(self):
        """Test workspace tools have proper metadata."""
        for name, meta in WORKSPACE_TOOLS_METADATA.items():
            assert "module" in meta
            assert "function" in meta
            assert "description" in meta
            assert "category" in meta
            assert meta["category"] == "workspace"


class TestAddAccomplishmentTool:
    """Tests for add_accomplishment tool."""

    def test_add_accomplishment_creates_file(self, workspace_tools, workspace_manager):
        """Test that add_accomplishment creates accomplishments.md."""
        tool = next(t for t in workspace_tools if t.name == "add_accomplishment")
        result = tool.invoke({"accomplishment": "Completed initial analysis"})

        assert "Recorded" in result
        assert workspace_manager.exists("accomplishments.md")

    def test_add_accomplishment_appends(self, workspace_tools, workspace_manager):
        """Test that add_accomplishment appends to existing file."""
        tool = next(t for t in workspace_tools if t.name == "add_accomplishment")

        tool.invoke({"accomplishment": "First accomplishment"})
        tool.invoke({"accomplishment": "Second accomplishment"})

        content = workspace_manager.read_file("accomplishments.md")
        assert "First accomplishment" in content
        assert "Second accomplishment" in content

    def test_add_accomplishment_strips_bullet(self, workspace_tools, workspace_manager):
        """Test that leading bullets are stripped."""
        tool = next(t for t in workspace_tools if t.name == "add_accomplishment")
        tool.invoke({"accomplishment": "- Already has bullet"})

        content = workspace_manager.read_file("accomplishments.md")
        # Should not have double bullet
        assert "- - Already" not in content
        assert "- Already has bullet" in content

    def test_add_accomplishment_empty_error(self, workspace_tools):
        """Test that empty accomplishment returns error."""
        tool = next(t for t in workspace_tools if t.name == "add_accomplishment")
        result = tool.invoke({"accomplishment": ""})

        assert "Error" in result


class TestAddNoteTool:
    """Tests for add_note tool."""

    def test_add_note_creates_file(self, workspace_tools, workspace_manager):
        """Test that add_note creates notes.md."""
        tool = next(t for t in workspace_tools if t.name == "add_note")
        result = tool.invoke({"note": "Found unexpected formatting"})

        assert "Noted" in result
        assert workspace_manager.exists("notes.md")

    def test_add_note_includes_timestamp(self, workspace_tools, workspace_manager):
        """Test that notes include timestamps."""
        tool = next(t for t in workspace_tools if t.name == "add_note")
        tool.invoke({"note": "Important observation"})

        content = workspace_manager.read_file("notes.md")
        # Should have timestamp like [HH:MM]
        assert "[" in content
        assert "]" in content
        assert "Important observation" in content

    def test_add_note_empty_error(self, workspace_tools):
        """Test that empty note returns error."""
        tool = next(t for t in workspace_tools if t.name == "add_note")
        result = tool.invoke({"note": "  "})

        assert "Error" in result


class TestGenerateWorkspaceSummaryTool:
    """Tests for generate_workspace_summary tool."""

    def test_generate_creates_file(self, workspace_tools, workspace_manager):
        """Test that generate_workspace_summary creates workspace_summary.md."""
        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        result = tool.invoke({})

        assert "Generated" in result
        assert workspace_manager.exists("workspace_summary.md")

    def test_generate_includes_header(self, workspace_tools, workspace_manager):
        """Test that summary includes header with timestamp."""
        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "# Workspace Summary" in content
        assert "Generated:" in content

    def test_generate_lists_files(self, workspace_tools, workspace_manager):
        """Test that summary lists workspace files."""
        workspace_manager.write_file("main_plan.md", "# Plan")
        workspace_manager.write_file("research.md", "# Research")

        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Files" in content
        assert "main_plan.md" in content
        assert "Execution plan" in content  # Purpose from heuristics

    def test_generate_includes_accomplishments(self, workspace_tools, workspace_manager):
        """Test that summary includes accomplishments."""
        # Add some accomplishments first
        workspace_manager.write_file("accomplishments.md", "- First milestone\n- Second milestone\n")

        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Accomplishments" in content
        assert "First milestone" in content

    def test_generate_includes_notes(self, workspace_tools, workspace_manager):
        """Test that summary includes notes."""
        workspace_manager.write_file("notes.md", "- Important note\n- Another note\n")

        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Notes" in content
        assert "Important note" in content

    def test_generate_confirmation_message(self, workspace_tools, workspace_manager):
        """Test that tool returns confirmation with stats."""
        workspace_manager.write_file("file1.md", "content")
        workspace_manager.write_file("file2.md", "content")

        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        result = tool.invoke({})

        assert "Generated workspace_summary.md" in result
        assert "files documented" in result

    def test_generate_handles_empty_workspace(self, workspace_tools, workspace_manager):
        """Test that summary handles empty workspace gracefully."""
        tool = next(t for t in workspace_tools if t.name == "generate_workspace_summary")
        result = tool.invoke({})

        # Should not error
        assert "Generated" in result
        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Accomplishments" in content
        assert "(No accomplishments recorded yet)" in content


class TestGenerateWorkspaceSummaryWithTodoContext:
    """Tests for generate_workspace_summary with todo manager context."""

    @pytest.fixture
    def todo_manager(self):
        """Create a TodoManager for testing."""
        # Import TodoManager
        todo_manager_path = project_root / "src" / "agent" / "todo_manager.py"
        todo_manager_module = _import_module_directly(todo_manager_path, "src.agent.todo_manager")
        TodoManager = todo_manager_module.TodoManager
        return TodoManager()

    @pytest.fixture
    def full_tool_context(self, workspace_manager, todo_manager):
        """Create a ToolContext with both workspace and todo manager."""
        return ToolContext(workspace_manager=workspace_manager, todo_manager=todo_manager)

    @pytest.fixture
    def full_workspace_tools(self, full_tool_context):
        """Create workspace tools with full context."""
        return create_workspace_tools(full_tool_context)

    def test_generate_includes_phase_info(self, full_workspace_tools, workspace_manager, todo_manager):
        """Test that summary includes phase information."""
        todo_manager.set_phase_info(2, 5, "Document Analysis")

        tool = next(t for t in full_workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Current State" in content
        assert "Phase 2 of 5" in content
        assert "Document Analysis" in content

    def test_generate_includes_todo_progress(self, full_workspace_tools, workspace_manager, todo_manager):
        """Test that summary includes todo progress."""
        todo_manager.add_sync("Task 1", priority=1)
        todo_manager.add_sync("Task 2", priority=1)
        todo_manager.complete_sync("todo_1")

        tool = next(t for t in full_workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Current State" in content
        assert "1 of 2" in content or "50.0%" in content

    def test_generate_includes_completed_todos_as_accomplishments(self, full_workspace_tools, workspace_manager, todo_manager):
        """Test that completed todos appear as accomplishments."""
        todo_manager.add_sync("Extract requirements", priority=1)
        todo_manager.complete_sync("todo_1")

        tool = next(t for t in full_workspace_tools if t.name == "generate_workspace_summary")
        tool.invoke({})

        content = workspace_manager.read_file("workspace_summary.md")
        assert "## Accomplishments" in content
        assert "Extract requirements" in content
