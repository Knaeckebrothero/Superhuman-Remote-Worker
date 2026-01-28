"""Unit tests for TodoManager (managers package).

Tests the stateful todo management for the nested loop graph architecture.
"""

import pytest
import tempfile
import sys
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

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


# Import workspace manager first (dependency)
workspace_path = project_root / "src" / "core" / "workspace.py"
workspace_module = _import_module_directly(workspace_path, "test_workspace_mgr")
WorkspaceManager = workspace_module.WorkspaceManager

# Import the todo module
todo_path = project_root / "src" / "managers" / "todo.py"
todo_module = _import_module_directly(todo_path, "test_todo_manager")
TodoManager = todo_module.TodoManager
TodoItem = todo_module.TodoItem
TodoStatus = todo_module.TodoStatus


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
def todo_manager(workspace_manager):
    """Create a TodoManager for testing."""
    return TodoManager(workspace_manager)


class TestTodoStatus:
    """Tests for TodoStatus enum."""

    def test_status_values(self):
        """Test that all expected status values exist."""
        assert TodoStatus.PENDING.value == "pending"
        assert TodoStatus.IN_PROGRESS.value == "in_progress"
        assert TodoStatus.COMPLETED.value == "completed"


class TestTodoItem:
    """Tests for TodoItem dataclass."""

    def test_create_basic(self):
        """Test creating a basic todo item."""
        item = TodoItem(id="todo_1", content="Test task")
        assert item.id == "todo_1"
        assert item.content == "Test task"
        assert item.status == TodoStatus.PENDING
        assert item.priority == "medium"
        assert item.notes == []
        assert isinstance(item.created_at, datetime)

    def test_create_with_options(self):
        """Test creating a todo with all options."""
        item = TodoItem(
            id="todo_2",
            content="High priority task",
            status=TodoStatus.IN_PROGRESS,
            priority="high",
            notes=["note 1", "note 2"],
        )
        assert item.status == TodoStatus.IN_PROGRESS
        assert item.priority == "high"
        assert item.notes == ["note 1", "note 2"]

    def test_to_dict(self):
        """Test serializing to dictionary."""
        item = TodoItem(
            id="todo_3",
            content="Serialize me",
            status=TodoStatus.COMPLETED,
            priority="low",
            notes=["done"],
        )
        data = item.to_dict()

        assert data["id"] == "todo_3"
        assert data["content"] == "Serialize me"
        assert data["status"] == "completed"
        assert data["priority"] == "low"
        assert data["notes"] == ["done"]
        assert "created_at" in data

    def test_from_dict(self):
        """Test deserializing from dictionary."""
        data = {
            "id": "todo_4",
            "content": "Deserialize me",
            "status": "in_progress",
            "priority": "high",
            "notes": ["started"],
            "created_at": "2024-01-15T10:30:00+00:00",
        }
        item = TodoItem.from_dict(data)

        assert item.id == "todo_4"
        assert item.content == "Deserialize me"
        assert item.status == TodoStatus.IN_PROGRESS
        assert item.priority == "high"
        assert item.notes == ["started"]

    def test_from_dict_defaults(self):
        """Test deserializing with minimal data uses defaults."""
        data = {"id": "todo_5", "content": "Minimal"}
        item = TodoItem.from_dict(data)

        assert item.status == TodoStatus.PENDING
        assert item.priority == "medium"
        assert item.notes == []


class TestTodoManagerBasics:
    """Tests for basic TodoManager operations."""

    def test_add_todo(self, todo_manager):
        """Test adding a todo item."""
        item = todo_manager.add("Test task")

        assert item.id == "todo_1"
        assert item.content == "Test task"
        assert item.priority == "medium"
        assert item.status == TodoStatus.PENDING

    def test_add_multiple_todos(self, todo_manager):
        """Test adding multiple todos generates unique IDs."""
        item1 = todo_manager.add("Task 1")
        item2 = todo_manager.add("Task 2")
        item3 = todo_manager.add("Task 3")

        assert item1.id == "todo_1"
        assert item2.id == "todo_2"
        assert item3.id == "todo_3"

    def test_add_with_priority(self, todo_manager):
        """Test adding todos with different priorities."""
        high = todo_manager.add("High priority", priority="high")
        low = todo_manager.add("Low priority", priority="low")

        assert high.priority == "high"
        assert low.priority == "low"

    def test_get_todo(self, todo_manager):
        """Test getting a todo by ID."""
        todo_manager.add("Task 1")
        item2 = todo_manager.add("Task 2")
        todo_manager.add("Task 3")

        found = todo_manager.get("todo_2")
        assert found is not None
        assert found.content == "Task 2"
        assert found == item2

    def test_get_nonexistent(self, todo_manager):
        """Test getting a todo that doesn't exist."""
        found = todo_manager.get("nonexistent")
        assert found is None


class TestTodoManagerCompletion:
    """Tests for todo completion operations."""

    def test_complete_todo(self, todo_manager):
        """Test completing a todo."""
        todo_manager.add("Task to complete")

        result = todo_manager.complete("todo_1")

        assert result is not None
        assert result.status == TodoStatus.COMPLETED

    def test_complete_with_notes(self, todo_manager):
        """Test completing with notes."""
        todo_manager.add("Task with notes")

        result = todo_manager.complete("todo_1", notes=["Done successfully", "No issues"])

        assert result.notes == ["Done successfully", "No issues"]

    def test_complete_nonexistent(self, todo_manager):
        """Test completing a todo that doesn't exist."""
        result = todo_manager.complete("nonexistent")
        assert result is None

    def test_start_todo(self, todo_manager):
        """Test marking a todo as in progress."""
        todo_manager.add("Task to start")

        result = todo_manager.start("todo_1")

        assert result is not None
        assert result.status == TodoStatus.IN_PROGRESS

    def test_start_nonexistent(self, todo_manager):
        """Test starting a todo that doesn't exist."""
        result = todo_manager.start("nonexistent")
        assert result is None


class TestTodoManagerListing:
    """Tests for listing todos."""

    def test_list_all(self, todo_manager):
        """Test listing all todos."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.add("Task 3")

        all_todos = todo_manager.list_all()
        assert len(all_todos) == 3

    def test_list_all_returns_copy(self, todo_manager):
        """Test that list_all returns a copy, not the internal list."""
        todo_manager.add("Task 1")

        list1 = todo_manager.list_all()
        list1.clear()  # Modify the returned list

        list2 = todo_manager.list_all()
        assert len(list2) == 1  # Internal list unchanged

    def test_list_pending(self, todo_manager):
        """Test listing only pending todos."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.add("Task 3")
        todo_manager.complete("todo_1")

        pending = todo_manager.list_pending()
        assert len(pending) == 2
        assert all(t.status != TodoStatus.COMPLETED for t in pending)

    def test_list_pending_includes_in_progress(self, todo_manager):
        """Test that list_pending includes in_progress items."""
        todo_manager.add("Pending task")
        todo_manager.add("In progress task")
        todo_manager.start("todo_2")

        pending = todo_manager.list_pending()
        assert len(pending) == 2

    def test_list_pending_sorted_by_priority(self, todo_manager):
        """Test that list_pending sorts by priority."""
        todo_manager.add("Low priority", priority="low")
        todo_manager.add("High priority", priority="high")
        todo_manager.add("Medium priority", priority="medium")

        pending = todo_manager.list_pending()
        priorities = [t.priority for t in pending]
        assert priorities == ["high", "medium", "low"]


class TestTodoManagerStatus:
    """Tests for todo status checks."""

    def test_all_complete_empty(self, todo_manager):
        """Test all_complete with no todos."""
        assert todo_manager.all_complete() is True

    def test_all_complete_none_done(self, todo_manager):
        """Test all_complete when no todos are done."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")

        assert todo_manager.all_complete() is False

    def test_all_complete_some_done(self, todo_manager):
        """Test all_complete when some todos are done."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.complete("todo_1")

        assert todo_manager.all_complete() is False

    def test_all_complete_all_done(self, todo_manager):
        """Test all_complete when all todos are done."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.complete("todo_1")
        todo_manager.complete("todo_2")

        assert todo_manager.all_complete() is True

    def test_get_progress(self, todo_manager):
        """Test getting progress statistics."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.add("Task 3")
        todo_manager.complete("todo_1")

        progress = todo_manager.get_progress()
        assert progress["total"] == 3
        assert progress["completed"] == 1
        assert progress["pending"] == 2
        assert progress["percentage"] == pytest.approx(33.3, rel=0.1)

    def test_get_progress_empty(self, todo_manager):
        """Test progress with no todos."""
        progress = todo_manager.get_progress()
        assert progress["total"] == 0
        assert progress["completed"] == 0
        assert progress["percentage"] == 0


class TestTodoManagerDisplay:
    """Tests for display formatting."""

    def test_format_empty(self, todo_manager):
        """Test formatting empty todo list."""
        result = todo_manager.format_for_display()
        assert result == "No active todos."

    def test_format_with_pending(self, todo_manager):
        """Test formatting with pending todos."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")

        result = todo_manager.format_for_display()
        assert "## Current Todos" in result
        assert "**Pending:**" in result
        assert "Task 1" in result
        assert "Task 2" in result

    def test_format_with_in_progress(self, todo_manager):
        """Test formatting with in progress todos."""
        todo_manager.add("Working on this")
        todo_manager.start("todo_1")

        result = todo_manager.format_for_display()
        assert "**In Progress:**" in result
        assert "Working on this" in result

    def test_format_with_completed(self, todo_manager):
        """Test formatting shows completed count."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.complete("todo_1")

        result = todo_manager.format_for_display()
        assert "**Completed:** 1/2" in result

    def test_format_high_priority_marker(self, todo_manager):
        """Test that high priority items get marked."""
        todo_manager.add("Urgent task", priority="high")

        result = todo_manager.format_for_display()
        assert "[!]Urgent task" in result


class TestTodoManagerArchive:
    """Tests for archiving todos."""

    def test_archive_empty(self, todo_manager):
        """Test archiving with no todos returns empty string."""
        result = todo_manager.archive()
        assert result == ""

    def test_archive_creates_file(self, todo_manager, workspace_manager):
        """Test that archive creates a file in workspace."""
        todo_manager.add("Task 1")
        todo_manager.complete("todo_1")

        archive_path = todo_manager.archive()

        assert archive_path.startswith("archive/")
        assert workspace_manager.exists(archive_path)

    def test_archive_with_phase_name(self, todo_manager, workspace_manager):
        """Test archiving with a phase name."""
        todo_manager.add("Task 1")
        todo_manager.complete("todo_1")

        archive_path = todo_manager.archive(phase_name="extraction")

        assert "extraction" in archive_path
        assert workspace_manager.exists(archive_path)

    def test_archive_content(self, todo_manager, workspace_manager):
        """Test archive file content."""
        todo_manager.add("Completed task")
        todo_manager.add("Not completed task")
        todo_manager.complete("todo_1", notes=["Successfully completed"])

        archive_path = todo_manager.archive(phase_name="test_phase")
        content = workspace_manager.read_file(archive_path)

        assert "# Archived Todos: test_phase" in content
        assert "## Completed (1)" in content
        assert "[x] Completed task" in content
        assert "Successfully completed" in content
        assert "## Not Completed (1)" in content
        assert "Not completed task" in content
        assert "## Summary" in content

    def test_archive_clears_list(self, todo_manager):
        """Test that archive clears the todo list."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")

        todo_manager.archive()

        assert len(todo_manager.list_all()) == 0

    def test_archive_resets_id_counter(self, todo_manager):
        """Test that archive resets the ID counter."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.archive()

        # New todos should start from todo_1 again
        new_item = todo_manager.add("New task")
        assert new_item.id == "todo_1"


class TestTodoManagerClear:
    """Tests for clearing todos without archiving."""

    def test_clear(self, todo_manager):
        """Test clearing todos without archiving."""
        todo_manager.add("Task 1")
        todo_manager.add("Task 2")

        todo_manager.clear()

        assert len(todo_manager.list_all()) == 0

    def test_clear_resets_id(self, todo_manager):
        """Test that clear resets the ID counter."""
        todo_manager.add("Task 1")
        todo_manager.clear()

        new_item = todo_manager.add("New task")
        assert new_item.id == "todo_1"


class TestTodoManagerLogging:
    """Tests for logging functionality."""

    def test_log_state(self, todo_manager, caplog):
        """Test that log_state logs todo statistics."""
        import logging
        caplog.set_level(logging.INFO)

        todo_manager.add("Task 1")
        todo_manager.add("Task 2")
        todo_manager.start("todo_1")
        todo_manager.complete("todo_2")

        todo_manager.log_state()

        assert "total=2" in caplog.text
        assert "completed=1" in caplog.text
        assert "in_progress=1" in caplog.text
        assert "pending=0" in caplog.text
