"""Unit tests for todo tools and TodoManager archive functionality.

Tests the LangGraph tool wrappers and archive/reset functionality.
"""

import pytest
import tempfile
import sys
import importlib.util
from pathlib import Path
from datetime import datetime
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

workspace_manager_path = project_root / "src" / "agents" / "shared" / "workspace_manager.py"
workspace_manager_module = _import_module_directly(workspace_manager_path, "src.agents.shared.workspace_manager")

WorkspaceManager = workspace_manager_module.WorkspaceManager
WorkspaceConfig = workspace_manager_module.WorkspaceConfig

todo_manager_path = project_root / "src" / "agents" / "shared" / "todo_manager.py"
todo_manager_module = _import_module_directly(todo_manager_path, "src.agents.shared.todo_manager")

TodoManager = todo_manager_module.TodoManager
TodoItem = todo_manager_module.TodoItem
TodoStatus = todo_manager_module.TodoStatus

# Import tools modules - need to set up fake package structure for relative imports
context_path = project_root / "src" / "agents" / "shared" / "tools" / "context.py"
context_module = _import_module_directly(context_path, "src.agents.shared.tools.context")
ToolContext = context_module.ToolContext

workspace_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "workspace_tools.py"
workspace_tools_module = _import_module_directly(workspace_tools_path, "src.agents.shared.tools.workspace_tools")

# Create a fake package for the tools module to enable relative imports
tools_package = ModuleType("src.agents.shared.tools")
tools_package.context = context_module
tools_package.workspace_tools = workspace_tools_module
tools_package.ToolContext = ToolContext
tools_package.create_workspace_tools = workspace_tools_module.create_workspace_tools
tools_package.WORKSPACE_TOOLS_METADATA = workspace_tools_module.WORKSPACE_TOOLS_METADATA
sys.modules["src.agents.shared.tools"] = tools_package

# Now we can import todo_tools
todo_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "todo_tools.py"
todo_tools_module = _import_module_directly(todo_tools_path, "src.agents.shared.tools.todo_tools")
create_todo_tools = todo_tools_module.create_todo_tools
TODO_TOOLS_METADATA = todo_tools_module.TODO_TOOLS_METADATA

# Update fake package
tools_package.todo_tools = todo_tools_module
tools_package.create_todo_tools = create_todo_tools
tools_package.TODO_TOOLS_METADATA = TODO_TOOLS_METADATA

# Import domain tools (needed by registry)
document_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "document_tools.py"
document_tools_module = _import_module_directly(document_tools_path, "src.agents.shared.tools.document_tools")
tools_package.document_tools = document_tools_module

search_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "search_tools.py"
search_tools_module = _import_module_directly(search_tools_path, "src.agents.shared.tools.search_tools")
tools_package.search_tools = search_tools_module

citation_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "citation_tools.py"
citation_tools_module = _import_module_directly(citation_tools_path, "src.agents.shared.tools.citation_tools")
tools_package.citation_tools = citation_tools_module

cache_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "cache_tools.py"
cache_tools_module = _import_module_directly(cache_tools_path, "src.agents.shared.tools.cache_tools")
tools_package.cache_tools = cache_tools_module

graph_tools_path = project_root / "src" / "agents" / "shared" / "tools" / "graph_tools.py"
graph_tools_module = _import_module_directly(graph_tools_path, "src.agents.shared.tools.graph_tools")
tools_package.graph_tools = graph_tools_module

# Now import registry
registry_path = project_root / "src" / "agents" / "shared" / "tools" / "registry.py"
registry_module = _import_module_directly(registry_path, "src.agents.shared.tools.registry")
TOOL_REGISTRY = registry_module.TOOL_REGISTRY
load_tools = registry_module.load_tools
get_tools_by_category = registry_module.get_tools_by_category


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
def todo_manager():
    """Create a TodoManager for testing."""
    return TodoManager()


@pytest.fixture
def todo_manager_with_workspace(workspace_manager):
    """Create a TodoManager with workspace manager attached."""
    mgr = TodoManager(workspace_manager=workspace_manager)
    return mgr


@pytest.fixture
def tool_context(workspace_manager, todo_manager):
    """Create a ToolContext with both workspace and todo manager."""
    todo_manager.set_workspace_manager(workspace_manager)
    return ToolContext(
        workspace_manager=workspace_manager,
        todo_manager=todo_manager,
    )


@pytest.fixture
def todo_tools(tool_context):
    """Create todo tools from context."""
    return create_todo_tools(tool_context)


class TestTodoManagerSync:
    """Tests for TodoManager synchronous methods."""

    def test_add_sync(self, todo_manager):
        """Test adding a todo synchronously."""
        item = todo_manager.add_sync("Test task")
        assert item.id == "todo_1"
        assert item.content == "Test task"
        assert item.status == TodoStatus.PENDING

    def test_add_sync_with_priority(self, todo_manager):
        """Test adding a todo with priority."""
        item = todo_manager.add_sync("High priority task", priority=2)
        assert item.priority == 2

    def test_get_sync(self, todo_manager):
        """Test getting a todo by ID."""
        todo_manager.add_sync("Test task")
        item = todo_manager.get_sync("todo_1")
        assert item is not None
        assert item.content == "Test task"

    def test_get_sync_not_found(self, todo_manager):
        """Test getting a non-existent todo."""
        item = todo_manager.get_sync("nonexistent")
        assert item is None

    def test_start_sync(self, todo_manager):
        """Test starting a todo."""
        todo_manager.add_sync("Test task")
        item = todo_manager.start_sync("todo_1")
        assert item.status == TodoStatus.IN_PROGRESS
        assert item.started_at is not None

    def test_complete_sync(self, todo_manager):
        """Test completing a todo."""
        todo_manager.add_sync("Test task")
        item = todo_manager.complete_sync("todo_1", notes="Done!")
        assert item.status == TodoStatus.COMPLETED
        assert item.completed_at is not None
        assert "Done!" in item.notes

    def test_next_sync_priority_order(self, todo_manager):
        """Test next_sync returns highest priority first."""
        todo_manager.add_sync("Low priority", priority=0)
        todo_manager.add_sync("High priority", priority=2)
        todo_manager.add_sync("Medium priority", priority=1)

        next_item = todo_manager.next_sync()
        assert next_item.content == "High priority"

    def test_list_all_sync(self, todo_manager):
        """Test listing all todos."""
        todo_manager.add_sync("Task 1")
        todo_manager.add_sync("Task 2")
        items = todo_manager.list_all_sync()
        assert len(items) == 2

    def test_list_by_status_sync(self, todo_manager):
        """Test listing by status."""
        todo_manager.add_sync("Pending")
        todo_manager.add_sync("Completed")
        todo_manager.complete_sync("todo_2")

        pending = todo_manager.list_by_status_sync(TodoStatus.PENDING)
        completed = todo_manager.list_by_status_sync(TodoStatus.COMPLETED)

        assert len(pending) == 1
        assert len(completed) == 1

    def test_get_progress_sync(self, todo_manager):
        """Test progress summary."""
        todo_manager.add_sync("Task 1")
        todo_manager.add_sync("Task 2")
        todo_manager.complete_sync("todo_1")

        progress = todo_manager.get_progress_sync()
        assert progress["total"] == 2
        assert progress["completed"] == 1
        assert progress["pending"] == 1
        assert progress["completion_percentage"] == 50.0


class TestTodoManagerArchive:
    """Tests for TodoManager archive functionality."""

    def test_archive_and_reset_creates_file(self, todo_manager, workspace_manager):
        """Test that archive_and_reset creates archive file."""
        todo_manager.set_workspace_manager(workspace_manager)
        todo_manager.add_sync("Task 1")
        todo_manager.add_sync("Task 2")
        todo_manager.complete_sync("todo_1")

        result = todo_manager.archive_and_reset("phase_1")

        assert "Archived 2 todos" in result
        assert "archive/" in result
        assert len(todo_manager.list_all_sync()) == 0

        # Verify file was created
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) == 1
        assert "phase_1" in archive_files[0]

    def test_archive_content_format(self, todo_manager, workspace_manager):
        """Test that archive content is properly formatted."""
        todo_manager.set_workspace_manager(workspace_manager)
        todo_manager.add_sync("Completed task")
        todo_manager.complete_sync("todo_1", notes="Done well!")
        todo_manager.add_sync("Pending task", priority=1)

        todo_manager.archive_and_reset("test_phase")

        # Read archive file - list_files returns paths relative to workspace root
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        # The path is already relative to workspace, like "archive/todos_xxx.md"
        content = workspace_manager.read_file(archive_files[0])

        assert "# Archived Todos: test_phase" in content
        assert "## Completed (1)" in content
        assert "## Was Pending (1)" in content
        assert "[x] Completed task" in content
        assert "Done well!" in content
        assert "[P1]" in content  # Priority marker

    def test_archive_empty_list(self, todo_manager, workspace_manager):
        """Test archiving empty todo list."""
        todo_manager.set_workspace_manager(workspace_manager)
        result = todo_manager.archive_and_reset()
        assert "empty" in result.lower()

    def test_archive_without_workspace_raises(self, todo_manager):
        """Test that archive without workspace raises error."""
        todo_manager.add_sync("Task")
        with pytest.raises(ValueError, match="workspace manager"):
            todo_manager.archive_and_reset()

    def test_archive_with_passed_workspace(self, todo_manager, workspace_manager):
        """Test passing workspace_manager to archive_and_reset."""
        todo_manager.add_sync("Task")
        result = todo_manager.archive_and_reset("test", workspace_manager=workspace_manager)
        assert "Archived" in result

    def test_archive_sanitizes_phase_name(self, todo_manager, workspace_manager):
        """Test that phase names are sanitized for filenames."""
        todo_manager.set_workspace_manager(workspace_manager)
        todo_manager.add_sync("Task")

        result = todo_manager.archive_and_reset("phase/with:special*chars")

        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) == 1
        # Extract just the filename (without path prefix)
        filename = archive_files[0].split("/")[-1]
        # Verify no special chars in filename
        assert "/" not in filename
        assert ":" not in filename
        assert "*" not in filename

    def test_archive_resets_next_id(self, todo_manager, workspace_manager):
        """Test that next ID resets after archive."""
        todo_manager.set_workspace_manager(workspace_manager)
        todo_manager.add_sync("Task 1")
        todo_manager.add_sync("Task 2")
        todo_manager.archive_and_reset("phase_1")

        # Add new task - should start from todo_1 again
        item = todo_manager.add_sync("New task")
        assert item.id == "todo_1"

    def test_clear(self, todo_manager):
        """Test clearing todos without archiving."""
        todo_manager.add_sync("Task 1")
        todo_manager.add_sync("Task 2")

        count = todo_manager.clear()

        assert count == 2
        assert len(todo_manager.list_all_sync()) == 0


class TestCreateTodoTools:
    """Tests for create_todo_tools function."""

    def test_creates_tools(self, tool_context):
        """Test that tools are created successfully."""
        tools = create_todo_tools(tool_context)
        assert len(tools) > 0

    def test_requires_todo_manager(self, workspace_manager):
        """Test that todo manager is required."""
        context = ToolContext(workspace_manager=workspace_manager)
        with pytest.raises(ValueError, match="todo_manager"):
            create_todo_tools(context)

    def test_tool_names(self, todo_tools):
        """Test expected tool names are present."""
        tool_names = {t.name for t in todo_tools}
        expected = {
            "add_todo",
            "complete_todo",
            "start_todo",
            "list_todos",
            "get_progress",
            "archive_and_reset",
            "get_next_todo",
        }
        assert expected.issubset(tool_names)


class TestAddTodoTool:
    """Tests for add_todo tool."""

    def test_add_todo(self, todo_tools):
        """Test adding a todo."""
        add_tool = next(t for t in todo_tools if t.name == "add_todo")
        result = add_tool.invoke({"content": "Test task"})
        assert "Added" in result
        assert "todo_1" in result

    def test_add_todo_with_priority(self, todo_tools):
        """Test adding with priority."""
        add_tool = next(t for t in todo_tools if t.name == "add_todo")
        result = add_tool.invoke({"content": "High priority", "priority": 2})
        assert "Added" in result


class TestCompleteTodoTool:
    """Tests for complete_todo tool."""

    def test_complete_todo(self, todo_tools, tool_context):
        """Test completing a todo."""
        # Add a todo first
        tool_context.todo_manager.add_sync("Test task")

        complete_tool = next(t for t in todo_tools if t.name == "complete_todo")
        result = complete_tool.invoke({"todo_id": "todo_1"})
        assert "Completed" in result

    def test_complete_todo_with_notes(self, todo_tools, tool_context):
        """Test completing with notes."""
        tool_context.todo_manager.add_sync("Test task")

        complete_tool = next(t for t in todo_tools if t.name == "complete_todo")
        result = complete_tool.invoke({"todo_id": "todo_1", "notes": "Done!"})
        assert "Completed" in result

    def test_complete_nonexistent_todo(self, todo_tools):
        """Test completing non-existent todo."""
        complete_tool = next(t for t in todo_tools if t.name == "complete_todo")
        result = complete_tool.invoke({"todo_id": "nonexistent"})
        assert "not found" in result


class TestStartTodoTool:
    """Tests for start_todo tool."""

    def test_start_todo(self, todo_tools, tool_context):
        """Test starting a todo."""
        tool_context.todo_manager.add_sync("Test task")

        start_tool = next(t for t in todo_tools if t.name == "start_todo")
        result = start_tool.invoke({"todo_id": "todo_1"})
        assert "Started" in result


class TestListTodosTool:
    """Tests for list_todos tool."""

    def test_list_todos_empty(self, todo_tools):
        """Test listing empty todos."""
        list_tool = next(t for t in todo_tools if t.name == "list_todos")
        result = list_tool.invoke({})
        assert "No todos" in result

    def test_list_todos_with_items(self, todo_tools, tool_context):
        """Test listing todos."""
        tool_context.todo_manager.add_sync("Task 1")
        tool_context.todo_manager.add_sync("Task 2")

        list_tool = next(t for t in todo_tools if t.name == "list_todos")
        result = list_tool.invoke({})
        assert "Task 1" in result
        assert "Task 2" in result


class TestGetProgressTool:
    """Tests for get_progress tool."""

    def test_get_progress_empty(self, todo_tools):
        """Test progress with no todos."""
        progress_tool = next(t for t in todo_tools if t.name == "get_progress")
        result = progress_tool.invoke({})
        assert "0/0" in result

    def test_get_progress_with_items(self, todo_tools, tool_context):
        """Test progress with todos."""
        tool_context.todo_manager.add_sync("Task 1")
        tool_context.todo_manager.add_sync("Task 2")
        tool_context.todo_manager.complete_sync("todo_1")

        progress_tool = next(t for t in todo_tools if t.name == "get_progress")
        result = progress_tool.invoke({})
        assert "1/2" in result
        assert "50" in result


class TestArchiveAndResetTool:
    """Tests for archive_and_reset tool."""

    def test_archive_and_reset(self, todo_tools, tool_context):
        """Test archiving todos."""
        tool_context.todo_manager.add_sync("Task 1")

        archive_tool = next(t for t in todo_tools if t.name == "archive_and_reset")
        result = archive_tool.invoke({"phase_name": "test_phase"})
        assert "Archived" in result
        assert "test_phase" in result

    def test_archive_empty(self, todo_tools):
        """Test archiving empty list."""
        archive_tool = next(t for t in todo_tools if t.name == "archive_and_reset")
        result = archive_tool.invoke({})
        assert "empty" in result.lower()


class TestGetNextTodoTool:
    """Tests for get_next_todo tool."""

    def test_get_next_todo(self, todo_tools, tool_context):
        """Test getting next todo."""
        tool_context.todo_manager.add_sync("Task 1")

        next_tool = next(t for t in todo_tools if t.name == "get_next_todo")
        result = next_tool.invoke({})
        assert "Next" in result
        assert "Task 1" in result

    def test_get_next_todo_empty(self, todo_tools):
        """Test getting next todo when empty."""
        next_tool = next(t for t in todo_tools if t.name == "get_next_todo")
        result = next_tool.invoke({})
        assert "No pending" in result


class TestTodoToolsRegistry:
    """Tests for todo tools in registry."""

    def test_todo_tools_in_registry(self):
        """Test that todo tools are registered."""
        todo_tools = get_tools_by_category("todo")
        assert "add_todo" in todo_tools
        assert "complete_todo" in todo_tools
        assert "archive_and_reset" in todo_tools

    def test_todo_tools_not_placeholder(self):
        """Test that todo tools are not placeholders."""
        for name in ["add_todo", "complete_todo", "list_todos", "get_progress", "archive_and_reset"]:
            assert name in TOOL_REGISTRY
            assert not TOOL_REGISTRY[name].get("placeholder", False)


class TestLoadTodoTools:
    """Tests for loading todo tools via registry."""

    def test_load_todo_tools(self, tool_context):
        """Test loading todo tools through registry."""
        tools = load_tools(["add_todo", "complete_todo"], tool_context)
        assert len(tools) == 2
        tool_names = {t.name for t in tools}
        assert tool_names == {"add_todo", "complete_todo"}

    def test_load_requires_todo_manager(self, workspace_manager):
        """Test that loading todo tools requires todo_manager."""
        context = ToolContext(workspace_manager=workspace_manager)
        with pytest.raises(ValueError, match="todo_manager"):
            load_tools(["add_todo"], context)

    def test_load_mixed_tools(self, tool_context):
        """Test loading both workspace and todo tools."""
        tools = load_tools(["read_file", "add_todo", "list_todos"], tool_context)
        assert len(tools) == 3
        tool_names = {t.name for t in tools}
        assert "read_file" in tool_names
        assert "add_todo" in tool_names
        assert "list_todos" in tool_names


class TestTodoToolMetadata:
    """Tests for todo tool metadata."""

    def test_todo_tools_metadata(self):
        """Test todo tools have proper metadata."""
        for name, meta in TODO_TOOLS_METADATA.items():
            assert "module" in meta
            assert "function" in meta
            assert "description" in meta
            assert "category" in meta
            assert meta["category"] == "todo"
