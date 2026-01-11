"""Unit tests for todo tools and TodoManager.

Tests the LangGraph tool wrappers, set_todos_from_list, and archive functionality.
"""

import pytest
import tempfile
import sys
import json
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

workspace_manager_path = project_root / "src" / "agents" / "workspace_manager.py"
workspace_manager_module = _import_module_directly(workspace_manager_path, "src.agents.workspace_manager")

WorkspaceManager = workspace_manager_module.WorkspaceManager
WorkspaceConfig = workspace_manager_module.WorkspaceConfig

todo_manager_path = project_root / "src" / "agents" / "todo_manager.py"
todo_manager_module = _import_module_directly(todo_manager_path, "src.agents.todo_manager")

TodoManager = todo_manager_module.TodoManager
TodoItem = todo_manager_module.TodoItem
TodoStatus = todo_manager_module.TodoStatus

# Import tools modules - need to set up fake package structure for relative imports
context_path = project_root / "src" / "agents" / "tools" / "context.py"
context_module = _import_module_directly(context_path, "src.agents.tools.context")
ToolContext = context_module.ToolContext

workspace_tools_path = project_root / "src" / "agents" / "tools" / "workspace_tools.py"
workspace_tools_module = _import_module_directly(workspace_tools_path, "src.agents.tools.workspace_tools")

# Create a fake package for the tools module to enable relative imports
tools_package = ModuleType("src.agents.tools")
tools_package.context = context_module
tools_package.workspace_tools = workspace_tools_module
tools_package.ToolContext = ToolContext
tools_package.create_workspace_tools = workspace_tools_module.create_workspace_tools
tools_package.WORKSPACE_TOOLS_METADATA = workspace_tools_module.WORKSPACE_TOOLS_METADATA
sys.modules["src.agents.tools"] = tools_package

# Now we can import todo_tools
todo_tools_path = project_root / "src" / "agents" / "tools" / "todo_tools.py"
todo_tools_module = _import_module_directly(todo_tools_path, "src.agents.tools.todo_tools")
create_todo_tools = todo_tools_module.create_todo_tools
TODO_TOOLS_METADATA = todo_tools_module.TODO_TOOLS_METADATA

# Update fake package
tools_package.todo_tools = todo_tools_module
tools_package.create_todo_tools = create_todo_tools
tools_package.TODO_TOOLS_METADATA = TODO_TOOLS_METADATA

# Import domain tools (needed by registry)
document_tools_path = project_root / "src" / "agents" / "tools" / "document_tools.py"
document_tools_module = _import_module_directly(document_tools_path, "src.agents.tools.document_tools")
tools_package.document_tools = document_tools_module

search_tools_path = project_root / "src" / "agents" / "tools" / "search_tools.py"
search_tools_module = _import_module_directly(search_tools_path, "src.agents.tools.search_tools")
tools_package.search_tools = search_tools_module

citation_tools_path = project_root / "src" / "agents" / "tools" / "citation_tools.py"
citation_tools_module = _import_module_directly(citation_tools_path, "src.agents.tools.citation_tools")
tools_package.citation_tools = citation_tools_module

cache_tools_path = project_root / "src" / "agents" / "tools" / "cache_tools.py"
cache_tools_module = _import_module_directly(cache_tools_path, "src.agents.tools.cache_tools")
tools_package.cache_tools = cache_tools_module

graph_tools_path = project_root / "src" / "agents" / "tools" / "graph_tools.py"
graph_tools_module = _import_module_directly(graph_tools_path, "src.agents.tools.graph_tools")
tools_package.graph_tools = graph_tools_module

completion_tools_path = project_root / "src" / "agents" / "tools" / "completion_tools.py"
completion_tools_module = _import_module_directly(completion_tools_path, "src.agents.tools.completion_tools")
tools_package.completion_tools = completion_tools_module

# Now import registry
registry_path = project_root / "src" / "agents" / "tools" / "registry.py"
registry_module = _import_module_directly(registry_path, "src.agents.tools.registry")
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


class TestTodoManagerSetTodosFromList:
    """Tests for TodoManager.set_todos_from_list() method."""

    def test_set_todos_basic(self, todo_manager):
        """Test basic todo list replacement."""
        todos = [
            {"content": "Task 1", "status": "pending"},
            {"content": "Task 2", "status": "in_progress"},
            {"content": "Task 3", "status": "completed"},
        ]
        result = todo_manager.set_todos_from_list(todos)

        assert "Task 1" in result or "Task 2" in result  # Check some content is there
        assert len(todo_manager.list_all_sync()) == 3

    def test_set_todos_replaces_existing(self, todo_manager):
        """Test that existing todos are replaced."""
        todo_manager.add_sync("Old task")
        assert len(todo_manager.list_all_sync()) == 1

        todos = [{"content": "New task", "status": "pending"}]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        assert len(items) == 1
        assert items[0].content == "New task"

    def test_set_todos_validates_required_content(self, todo_manager):
        """Test that missing content raises error."""
        with pytest.raises(ValueError, match="content"):
            todo_manager.set_todos_from_list([{"status": "pending"}])

    def test_set_todos_validates_required_status(self, todo_manager):
        """Test that missing status raises error."""
        with pytest.raises(ValueError, match="status"):
            todo_manager.set_todos_from_list([{"content": "Task"}])

    def test_set_todos_validates_status_values(self, todo_manager):
        """Test that invalid status values raise errors."""
        with pytest.raises(ValueError, match="invalid status"):
            todo_manager.set_todos_from_list([
                {"content": "Task", "status": "unknown"}
            ])

    def test_set_todos_priority_conversion(self, todo_manager):
        """Test that string priorities convert correctly."""
        todos = [
            {"content": "High", "status": "pending", "priority": "high"},
            {"content": "Medium", "status": "pending", "priority": "medium"},
            {"content": "Low", "status": "pending", "priority": "low"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        high_item = next(t for t in items if "High" in t.content)
        medium_item = next(t for t in items if "Medium" in t.content)
        low_item = next(t for t in items if "Low" in t.content)

        assert high_item.priority == 2
        assert medium_item.priority == 1
        assert low_item.priority == 0

    def test_set_todos_auto_generates_ids(self, todo_manager):
        """Test that IDs are auto-generated when not provided."""
        todos = [
            {"content": "Task 1", "status": "pending"},
            {"content": "Task 2", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        assert items[0].id.startswith("todo_")
        assert items[1].id.startswith("todo_")
        assert items[0].id != items[1].id

    def test_set_todos_preserves_provided_ids(self, todo_manager):
        """Test that provided IDs are preserved."""
        todos = [
            {"content": "Task", "status": "pending", "id": "custom_id"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        assert items[0].id == "custom_id"

    def test_set_todos_empty_list(self, todo_manager):
        """Test that empty list clears all todos."""
        todo_manager.add_sync("Existing task")
        result = todo_manager.set_todos_from_list([])

        assert len(todo_manager.list_all_sync()) == 0
        assert "No todos" in result

    def test_set_todos_status_conversion(self, todo_manager):
        """Test that status strings convert to enums."""
        todos = [
            {"content": "Pending", "status": "pending"},
            {"content": "In Progress", "status": "in_progress"},
            {"content": "Completed", "status": "completed"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        pending = next(t for t in items if "Pending" in t.content)
        in_progress = next(t for t in items if "In Progress" in t.content)
        completed = next(t for t in items if "Completed" in t.content)

        assert pending.status == TodoStatus.PENDING
        assert in_progress.status == TodoStatus.IN_PROGRESS
        assert completed.status == TodoStatus.COMPLETED

    def test_format_response_includes_progress(self, todo_manager):
        """Test response includes progress bar."""
        todos = [
            {"content": "Task 1", "status": "completed"},
            {"content": "Task 2", "status": "pending"},
        ]
        result = todo_manager.set_todos_from_list(todos)

        assert "Progress" in result
        assert "1/2" in result
        assert "50%" in result

    def test_format_response_includes_hint(self, todo_manager):
        """Test response includes next action hint."""
        todos = [
            {"content": "Current task", "status": "in_progress"},
            {"content": "Next task", "status": "pending"},
        ]
        result = todo_manager.set_todos_from_list(todos)

        assert "Currently working on" in result
        assert "Current task" in result


class TestAutoReflection:
    """Tests for auto-reflection task injection."""

    def test_reflection_task_auto_appended(self):
        """Test that reflection task is automatically added when enabled."""
        todo_manager = TodoManager(
            auto_reflection=True,
            reflection_task_content="Review and update plan",
        )
        todos = [
            {"content": "Task 1", "status": "pending"},
            {"content": "Task 2", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        # Should have 3 items: 2 original + 1 reflection
        assert len(items) == 3
        assert any("Review and update plan" in t.content for t in items)

    def test_reflection_not_duplicated(self):
        """Test that existing reflection task is not duplicated."""
        todo_manager = TodoManager(
            auto_reflection=True,
            reflection_task_content="Review and update plan",
        )
        todos = [
            {"content": "Task 1", "status": "pending"},
            {"content": "Review plan and update progress", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        # Should have 2 items, not 3 (no duplicate reflection)
        assert len(items) == 2

    def test_reflection_skipped_when_disabled(self):
        """Test that reflection task is not added when disabled."""
        todo_manager = TodoManager(
            auto_reflection=False,
            reflection_task_content="Review and update plan",
        )
        todos = [
            {"content": "Task 1", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        assert len(items) == 1
        assert not any("Review" in t.content for t in items)

    def test_reflection_skipped_when_no_pending(self):
        """Test that reflection task is not added when no pending tasks."""
        todo_manager = TodoManager(
            auto_reflection=True,
            reflection_task_content="Review and update plan",
        )
        todos = [
            {"content": "Task 1", "status": "completed"},
            {"content": "Task 2", "status": "completed"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        # Should have 2 items, no reflection added
        assert len(items) == 2
        assert not any("Review" in t.content for t in items)

    def test_reflection_has_low_priority(self):
        """Test that reflection task has lowest priority."""
        todo_manager = TodoManager(
            auto_reflection=True,
            reflection_task_content="Review and update plan",
        )
        todos = [
            {"content": "High priority", "status": "pending", "priority": "high"},
            {"content": "Medium priority", "status": "pending", "priority": "medium"},
        ]
        todo_manager.set_todos_from_list(todos)

        items = todo_manager.list_all_sync()
        reflection_item = next(t for t in items if "Review" in t.content)
        high_item = next(t for t in items if "High" in t.content)

        # Reflection should have priority 0 (low), high priority should be 2
        assert reflection_item.priority == 0
        assert high_item.priority == 2

    def test_reflection_keywords_detected(self):
        """Test that various reflection keywords prevent duplication."""
        todo_manager = TodoManager(
            auto_reflection=True,
            reflection_task_content="Review plan, update progress",
        )

        # Test "update plan" keyword
        todos1 = [{"content": "Update plan with results", "status": "pending"}]
        todo_manager.set_todos_from_list(todos1)
        assert len(todo_manager.list_all_sync()) == 1

        # Test "review progress" keyword
        todos2 = [{"content": "Review progress on extraction", "status": "pending"}]
        todo_manager.set_todos_from_list(todos2)
        assert len(todo_manager.list_all_sync()) == 1


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

        # Read archive file
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
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
        filename = archive_files[0].split("/")[-1]
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
        assert len(tools) == 2  # todo_write and archive_and_reset

    def test_requires_todo_manager(self, workspace_manager):
        """Test that todo manager is required."""
        context = ToolContext(workspace_manager=workspace_manager)
        with pytest.raises(ValueError, match="todo_manager"):
            create_todo_tools(context)

    def test_tool_names(self, todo_tools):
        """Test expected tool names are present."""
        tool_names = {t.name for t in todo_tools}
        expected = {"todo_write", "archive_and_reset"}
        assert expected == tool_names


class TestTodoWriteTool:
    """Tests for todo_write tool."""

    def test_todo_write_json_parsing(self, todo_tools):
        """Test valid JSON is parsed correctly."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")
        result = write_tool.invoke({
            "todos": '[{"content": "Test task", "status": "pending"}]'
        })
        assert "Test task" in result
        assert "Error" not in result

    def test_todo_write_invalid_json(self, todo_tools):
        """Test invalid JSON returns error."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")
        result = write_tool.invoke({"todos": "not json"})
        assert "Error" in result
        assert "JSON" in result

    def test_todo_write_not_array(self, todo_tools):
        """Test non-array JSON returns error."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")
        result = write_tool.invoke({"todos": '{"content": "task"}'})
        assert "Error" in result
        assert "array" in result

    def test_todo_write_missing_content(self, todo_tools):
        """Test missing content returns error."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")
        result = write_tool.invoke({
            "todos": '[{"status": "pending"}]'
        })
        assert "Error" in result
        assert "content" in result

    def test_todo_write_missing_status(self, todo_tools):
        """Test missing status returns error."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")
        result = write_tool.invoke({
            "todos": '[{"content": "task"}]'
        })
        assert "Error" in result
        assert "status" in result

    def test_todo_write_full_workflow(self, todo_tools, tool_context):
        """Test complete add/update/complete cycle."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")

        # Add initial tasks
        result1 = write_tool.invoke({
            "todos": '[{"content": "Task A", "status": "pending"}, {"content": "Task B", "status": "pending"}]'
        })
        assert "Pending (2)" in result1

        # Start first task
        result2 = write_tool.invoke({
            "todos": '[{"content": "Task A", "status": "in_progress"}, {"content": "Task B", "status": "pending"}]'
        })
        assert "In Progress" in result2

        # Complete first, start second
        result3 = write_tool.invoke({
            "todos": '[{"content": "Task A", "status": "completed"}, {"content": "Task B", "status": "in_progress"}]'
        })
        assert "Completed" in result3
        assert "50%" in result3

    def test_todo_write_with_priorities(self, todo_tools):
        """Test high/medium/low priorities work."""
        write_tool = next(t for t in todo_tools if t.name == "todo_write")
        result = write_tool.invoke({
            "todos": json.dumps([
                {"content": "High priority task", "status": "pending", "priority": "high"},
                {"content": "Low priority task", "status": "pending", "priority": "low"},
            ])
        })
        assert "[HIGH]" in result
        assert "High priority task" in result


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


class TestTodoToolsRegistry:
    """Tests for todo tools in registry."""

    def test_new_todo_tools_in_registry(self):
        """Test that new todo tools are registered."""
        todo_tools = get_tools_by_category("todo")
        assert "todo_write" in todo_tools
        assert "archive_and_reset" in todo_tools

    def test_old_tools_removed(self):
        """Test that old fragmented tools are removed."""
        todo_tools = get_tools_by_category("todo")
        old_tools = ["add_todo", "start_todo", "complete_todo", "list_todos", "get_progress", "get_next_todo"]
        for old_tool in old_tools:
            assert old_tool not in todo_tools, f"{old_tool} should have been removed"


class TestLoadTodoTools:
    """Tests for loading todo tools via registry."""

    def test_load_todo_tools(self, tool_context):
        """Test loading todo tools through registry."""
        tools = load_tools(["todo_write", "archive_and_reset"], tool_context)
        assert len(tools) == 2
        tool_names = {t.name for t in tools}
        assert tool_names == {"todo_write", "archive_and_reset"}

    def test_load_requires_todo_manager(self, workspace_manager):
        """Test that loading todo tools requires todo_manager."""
        context = ToolContext(workspace_manager=workspace_manager)
        with pytest.raises(ValueError, match="todo_manager"):
            load_tools(["todo_write"], context)

    def test_load_mixed_tools(self, tool_context):
        """Test loading both workspace and todo tools."""
        tools = load_tools(["read_file", "todo_write"], tool_context)
        assert len(tools) == 2
        tool_names = {t.name for t in tools}
        assert "read_file" in tool_names
        assert "todo_write" in tool_names


class TestTodoToolMetadata:
    """Tests for todo tool metadata."""

    def test_todo_tools_metadata(self):
        """Test todo tools have proper metadata."""
        expected_tools = {"todo_write", "archive_and_reset"}
        assert set(TODO_TOOLS_METADATA.keys()) == expected_tools

        for name, meta in TODO_TOOLS_METADATA.items():
            assert "module" in meta
            assert "function" in meta
            assert "description" in meta
            assert "category" in meta
            assert meta["category"] == "todo"
