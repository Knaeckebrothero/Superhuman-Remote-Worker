"""Unit tests for MemoryManager (managers package).

Tests the workspace memory file management for the nested loop graph architecture.
"""

import pytest
import tempfile
import sys
import importlib.util
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
workspace_module = _import_module_directly(workspace_path, "test_memory_workspace_mgr")
WorkspaceManager = workspace_module.WorkspaceManager

# Import the memory module
memory_path = project_root / "src" / "managers" / "memory.py"
memory_module = _import_module_directly(memory_path, "test_memory_manager")
MemoryManager = memory_module.MemoryManager


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
def memory_manager(workspace_manager):
    """Create a MemoryManager for testing."""
    return MemoryManager(workspace_manager)


class TestMemoryManagerBasics:
    """Tests for basic MemoryManager operations."""

    def test_memory_file_constant(self):
        """Test that memory file constant is set correctly."""
        assert MemoryManager.MEMORY_FILE == "workspace.md"

    def test_exists_false_initially(self, memory_manager):
        """Test that memory doesn't exist initially."""
        assert memory_manager.exists() is False

    def test_exists_after_write(self, memory_manager):
        """Test that exists returns True after writing."""
        memory_manager.write("# Memory")
        assert memory_manager.exists() is True

    def test_write_and_read(self, memory_manager):
        """Test basic write and read."""
        content = "# Workspace Memory\n\n## Current State\nPhase: 1"
        memory_manager.write(content)

        result = memory_manager.read()
        assert result == content

    def test_read_nonexistent(self, memory_manager):
        """Test reading when memory doesn't exist."""
        result = memory_manager.read()
        assert result == ""


class TestMemoryManagerGetSection:
    """Tests for section extraction."""

    def test_get_section_exists(self, memory_manager):
        """Test getting an existing section."""
        content = """# Workspace Memory

## Current State
Phase: 2
Status: In progress

## Accomplishments
- Completed extraction
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Current State")
        assert section is not None
        assert "Phase: 2" in section
        assert "Status: In progress" in section

    def test_get_section_not_found(self, memory_manager):
        """Test getting a section that doesn't exist."""
        content = """# Workspace Memory

## Current State
Phase: 1
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Nonexistent")
        assert section is None

    def test_get_section_empty_file(self, memory_manager):
        """Test getting section from empty/nonexistent file."""
        section = memory_manager.get_section("Current State")
        assert section is None

    def test_get_section_case_insensitive(self, memory_manager):
        """Test that section matching is case insensitive."""
        content = """# Workspace Memory

## CURRENT STATE
Phase: 1
"""
        memory_manager.write(content)

        section = memory_manager.get_section("current state")
        assert section is not None
        assert "Phase: 1" in section

    def test_get_section_with_subsection(self, memory_manager):
        """Test getting section stops at next same-level header."""
        content = """# Workspace Memory

## Section 1
Content 1

## Section 2
Content 2

## Section 3
Content 3
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Section 1")
        assert section is not None
        assert "Content 1" in section
        assert "Content 2" not in section

    def test_get_section_three_level_header(self, memory_manager):
        """Test getting section with ### headers."""
        content = """# Workspace Memory

### Notes
Some notes here

### More Notes
More content
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Notes")
        assert section is not None
        assert "Some notes here" in section


class TestMemoryManagerUpdateSection:
    """Tests for section updating."""

    def test_update_section_existing(self, memory_manager):
        """Test updating an existing section."""
        content = """# Workspace Memory

## Current State
Phase: 1
Status: Starting

## Notes
Some notes
"""
        memory_manager.write(content)

        result = memory_manager.update_section("Current State", "Phase: 2\nStatus: In progress")

        assert result is True
        updated = memory_manager.read()
        assert "Phase: 2" in updated
        assert "Status: In progress" in updated
        assert "Phase: 1" not in updated

    def test_update_section_new(self, memory_manager):
        """Test adding a new section when it doesn't exist."""
        content = """# Workspace Memory

## Current State
Phase: 1
"""
        memory_manager.write(content)

        result = memory_manager.update_section("Accomplishments", "- Task 1 done")

        assert result is True
        updated = memory_manager.read()
        assert "## Accomplishments" in updated
        assert "- Task 1 done" in updated

    def test_update_section_empty_file(self, memory_manager):
        """Test updating section when file doesn't exist."""
        result = memory_manager.update_section("Current State", "Phase: 1")

        assert result is True
        content = memory_manager.read()
        assert "# Workspace Memory" in content
        assert "## Current State" in content
        assert "Phase: 1" in content

    def test_update_section_preserves_other_sections(self, memory_manager):
        """Test that updating one section preserves others."""
        content = """# Workspace Memory

## Section A
Content A

## Section B
Content B
"""
        memory_manager.write(content)

        memory_manager.update_section("Section A", "Updated A")

        updated = memory_manager.read()
        assert "Updated A" in updated
        assert "Content B" in updated


class TestMemoryManagerAppendToSection:
    """Tests for appending to sections."""

    def test_append_to_existing_section(self, memory_manager):
        """Test appending to an existing section."""
        content = """# Workspace Memory

## Accomplishments
- Task 1 done
"""
        memory_manager.write(content)

        result = memory_manager.append_to_section("Accomplishments", "Task 2 done")

        assert result is True
        updated = memory_manager.read()
        assert "- Task 1 done" in updated
        assert "- Task 2 done" in updated

    def test_append_to_new_section(self, memory_manager):
        """Test appending creates section if it doesn't exist."""
        memory_manager.write("# Workspace Memory")

        result = memory_manager.append_to_section("Accomplishments", "First task done")

        assert result is True
        updated = memory_manager.read()
        assert "## Accomplishments" in updated
        assert "- First task done" in updated


class TestMemoryManagerState:
    """Tests for state management."""

    def test_get_state_basic(self, memory_manager):
        """Test getting state values."""
        content = """# Workspace Memory

## Current State
Phase: 2
Status: In progress
Mode: Normal
"""
        memory_manager.write(content)

        state = memory_manager.get_state()
        assert state["phase"] == "2"
        assert state["status"] == "In progress"
        assert state["mode"] == "Normal"

    def test_get_state_empty(self, memory_manager):
        """Test getting state when section doesn't exist."""
        state = memory_manager.get_state()
        assert state == {}

    def test_get_state_ignores_list_items(self, memory_manager):
        """Test that list items are not parsed as state."""
        content = """# Workspace Memory

## Current State
Phase: 1
- Some note: value
"""
        memory_manager.write(content)

        state = memory_manager.get_state()
        assert "phase" in state
        assert "some note" not in state

    def test_set_state_existing_key(self, memory_manager):
        """Test updating an existing state key."""
        content = """# Workspace Memory

## Current State
Phase: 1
Status: Starting
"""
        memory_manager.write(content)

        result = memory_manager.set_state("Phase", "2")

        assert result is True
        state = memory_manager.get_state()
        assert state["phase"] == "2"
        assert state["status"] == "Starting"

    def test_set_state_new_key(self, memory_manager):
        """Test adding a new state key."""
        content = """# Workspace Memory

## Current State
Phase: 1
"""
        memory_manager.write(content)

        result = memory_manager.set_state("Mode", "Active")

        assert result is True
        state = memory_manager.get_state()
        assert state["mode"] == "Active"

    def test_set_state_no_current_state_section(self, memory_manager):
        """Test setting state when Current State section doesn't exist."""
        memory_manager.write("# Workspace Memory")

        result = memory_manager.set_state("Phase", "1")

        assert result is True
        state = memory_manager.get_state()
        assert state["phase"] == "1"


class TestMemoryManagerEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_section_with_special_characters(self, memory_manager):
        """Test sections with special characters in name."""
        content = """# Workspace Memory

## Key Decisions & Notes
Some content
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Key Decisions & Notes")
        assert section is not None
        assert "Some content" in section

    def test_unicode_content(self, memory_manager):
        """Test handling unicode content."""
        content = """# Workspace Memory

## Notes
日本語テキスト 🎉 émojis
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Notes")
        assert "日本語テキスト" in section
        assert "🎉" in section

    def test_multiline_section_content(self, memory_manager):
        """Test sections with multiple lines."""
        content = """# Workspace Memory

## Notes
Line 1
Line 2
Line 3

## Next Section
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Notes")
        assert "Line 1" in section
        assert "Line 2" in section
        assert "Line 3" in section

    def test_empty_section(self, memory_manager):
        """Test getting an empty section."""
        content = """# Workspace Memory

## Empty Section

## Next Section
Content
"""
        memory_manager.write(content)

        section = memory_manager.get_section("Empty Section")
        # Empty section should return empty string, not None
        assert section == "" or section is not None

    def test_state_with_colon_in_value(self, memory_manager):
        """Test state values containing colons."""
        content = """# Workspace Memory

## Current State
Time: 10:30:00
Status: Active
"""
        memory_manager.write(content)

        state = memory_manager.get_state()
        # Only first colon should split
        assert state["time"] == "10:30:00"
        assert state["status"] == "Active"
