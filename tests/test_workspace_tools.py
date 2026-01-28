"""Unit tests for workspace tools read-before-write discipline.

Tests the read tracking mechanism and enforcement in workspace tools.
"""

import pytest
import tempfile
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import from src package
from src.core.workspace import WorkspaceManager
from src.tools.context import ToolContext
from src.tools.workspace_tools import create_workspace_tools


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
    """Create a ToolContext for testing."""
    return ToolContext(workspace_manager=workspace_manager)


@pytest.fixture
def workspace_tools(tool_context):
    """Create workspace tools."""
    tools = create_workspace_tools(tool_context)
    # Convert list to dict for easy access
    return {tool.name: tool for tool in tools}


class TestReadTracking:
    """Tests for ToolContext read tracking."""

    def test_record_file_read(self, tool_context):
        """Test that file reads are recorded in context."""
        tool_context.record_file_read("test.md")
        assert tool_context.was_recently_read("test.md")

    def test_read_tracking_normalizes_path(self, tool_context):
        """Test that paths are normalized for tracking."""
        tool_context.record_file_read("/test.md")
        assert tool_context.was_recently_read("test.md")
        assert tool_context.was_recently_read("/test.md")

    def test_read_tracking_deque_limit(self, tool_context):
        """Test that read tracking respects the deque limit."""
        # Record 15 files (more than the default 10)
        for i in range(15):
            tool_context.record_file_read(f"file_{i}.md")

        # First 5 should be pushed out
        for i in range(5):
            assert not tool_context.was_recently_read(f"file_{i}.md")

        # Last 10 should still be tracked
        for i in range(5, 15):
            assert tool_context.was_recently_read(f"file_{i}.md")

    def test_re_reading_moves_to_end(self, tool_context):
        """Test that re-reading a file moves it to the end of the deque."""
        # Fill deque with 10 files
        for i in range(10):
            tool_context.record_file_read(f"file_{i}.md")

        # Re-read file_0 (should move to end)
        tool_context.record_file_read("file_0.md")

        # Add 9 more files - should push out files 1-9 but not file_0
        for i in range(10, 19):
            tool_context.record_file_read(f"file_{i}.md")

        # file_0 should still be there (was moved to end)
        assert tool_context.was_recently_read("file_0.md")

        # files 1-9 should be gone
        for i in range(1, 10):
            assert not tool_context.was_recently_read(f"file_{i}.md")


class TestReadFileTracking:
    """Tests for read_file recording reads."""

    def test_read_file_records_path(self, workspace_tools, workspace_manager, tool_context):
        """Test that read_file records the path in context."""
        # Create a test file
        workspace_manager.write_file("test.md", "Hello, world!")

        # Read the file
        read_file = workspace_tools["read_file"]
        result = read_file.invoke({"path": "test.md"})

        # Check the read was recorded
        assert tool_context.was_recently_read("test.md")
        assert "Hello, world!" in result

    def test_read_file_error_does_not_record(self, workspace_tools, tool_context):
        """Test that failed reads are not recorded."""
        read_file = workspace_tools["read_file"]
        result = read_file.invoke({"path": "nonexistent.md"})

        assert "Error" in result
        assert not tool_context.was_recently_read("nonexistent.md")


class TestEditFileReadRequirement:
    """Tests for edit_file requiring recent read."""

    def test_edit_file_requires_recent_read(self, workspace_tools, workspace_manager):
        """Test that edit_file fails without recent read."""
        # Create a test file
        workspace_manager.write_file("test.md", "Hello, world!")

        # Try to edit without reading first
        edit_file = workspace_tools["edit_file"]
        result = edit_file.invoke({
            "path": "test.md",
            "old_string": "Hello",
            "new_string": "Goodbye"
        })

        assert "Error" in result
        assert "read_file" in result.lower()

    def test_edit_file_works_after_read(self, workspace_tools, workspace_manager):
        """Test that edit_file works after reading."""
        # Create a test file
        workspace_manager.write_file("test.md", "Hello, world!")

        # Read first
        read_file = workspace_tools["read_file"]
        read_file.invoke({"path": "test.md"})

        # Now edit should work
        edit_file = workspace_tools["edit_file"]
        result = edit_file.invoke({
            "path": "test.md",
            "old_string": "Hello",
            "new_string": "Goodbye"
        })

        assert "Edited" in result

        # Verify the change
        content = workspace_manager.read_file("test.md")
        assert "Goodbye, world!" in content


class TestEditFilePositionModes:
    """Tests for edit_file position parameter (append/prepend)."""

    def test_edit_file_position_end_appends(self, workspace_tools, workspace_manager):
        """Test that position='end' appends to file."""
        # Create a test file
        workspace_manager.write_file("test.md", "Line 1")

        # Read first
        read_file = workspace_tools["read_file"]
        read_file.invoke({"path": "test.md"})

        # Append using position="end"
        edit_file = workspace_tools["edit_file"]
        result = edit_file.invoke({
            "path": "test.md",
            "new_string": "\nLine 2",
            "position": "end"
        })

        assert "Appended" in result

        # Verify the change
        content = workspace_manager.read_file("test.md")
        assert content == "Line 1\nLine 2"

    def test_edit_file_position_start_prepends(self, workspace_tools, workspace_manager):
        """Test that position='start' prepends to file."""
        # Create a test file
        workspace_manager.write_file("test.md", "Line 2")

        # Read first
        read_file = workspace_tools["read_file"]
        read_file.invoke({"path": "test.md"})

        # Prepend using position="start"
        edit_file = workspace_tools["edit_file"]
        result = edit_file.invoke({
            "path": "test.md",
            "new_string": "Line 1\n",
            "position": "start"
        })

        assert "Prepended" in result

        # Verify the change
        content = workspace_manager.read_file("test.md")
        assert content == "Line 1\nLine 2"

    def test_edit_file_position_invalid_fails(self, workspace_tools, workspace_manager):
        """Test that invalid position values fail."""
        # Create a test file
        workspace_manager.write_file("test.md", "Content")

        # Read first
        read_file = workspace_tools["read_file"]
        read_file.invoke({"path": "test.md"})

        # Try invalid position
        edit_file = workspace_tools["edit_file"]
        result = edit_file.invoke({
            "path": "test.md",
            "new_string": "New",
            "position": "middle"  # Invalid
        })

        assert "Error" in result
        assert "Invalid position" in result

    def test_edit_file_replace_requires_old_string(self, workspace_tools, workspace_manager):
        """Test that replace mode (no position) requires old_string."""
        # Create a test file
        workspace_manager.write_file("test.md", "Content")

        # Read first
        read_file = workspace_tools["read_file"]
        read_file.invoke({"path": "test.md"})

        # Try replace without old_string
        edit_file = workspace_tools["edit_file"]
        result = edit_file.invoke({
            "path": "test.md",
            "new_string": "New"
            # No old_string, no position
        })

        assert "Error" in result
        assert "old_string is required" in result


class TestWriteFileReadRequirement:
    """Tests for write_file requiring recent read for existing files."""

    def test_write_file_existing_requires_read(self, workspace_tools, workspace_manager):
        """Test that overwriting existing file fails without recent read."""
        # Create a test file
        workspace_manager.write_file("test.md", "Original content")

        # Try to overwrite without reading first
        write_file = workspace_tools["write_file"]
        result = write_file.invoke({
            "path": "test.md",
            "content": "New content"
        })

        assert "Error" in result
        assert "read_file" in result.lower()

        # Original content should be unchanged
        content = workspace_manager.read_file("test.md")
        assert content == "Original content"

    def test_write_file_new_no_read_required(self, workspace_tools, workspace_manager):
        """Test that creating a new file doesn't require read."""
        # Write a new file without reading
        write_file = workspace_tools["write_file"]
        result = write_file.invoke({
            "path": "new_file.md",
            "content": "Brand new content"
        })

        assert "Written" in result

        # Verify the file was created
        content = workspace_manager.read_file("new_file.md")
        assert content == "Brand new content"

    def test_write_file_works_after_read(self, workspace_tools, workspace_manager):
        """Test that overwriting works after reading."""
        # Create a test file
        workspace_manager.write_file("test.md", "Original content")

        # Read first
        read_file = workspace_tools["read_file"]
        read_file.invoke({"path": "test.md"})

        # Now overwrite should work
        write_file = workspace_tools["write_file"]
        result = write_file.invoke({
            "path": "test.md",
            "content": "New content"
        })

        assert "Written" in result

        # Verify the change
        content = workspace_manager.read_file("test.md")
        assert content == "New content"


class TestAppendFileRemoved:
    """Tests that append_file is no longer available."""

    def test_append_file_not_in_tools(self, workspace_tools):
        """Test that append_file is not in the returned tools."""
        assert "append_file" not in workspace_tools
