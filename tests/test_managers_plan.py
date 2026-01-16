"""Unit tests for PlanManager (managers package).

Tests the plan file management for the nested loop graph architecture.
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
workspace_module = _import_module_directly(workspace_path, "test_plan_workspace_mgr")
WorkspaceManager = workspace_module.WorkspaceManager

# Import the plan module
plan_path = project_root / "src" / "managers" / "plan.py"
plan_module = _import_module_directly(plan_path, "test_plan_manager")
PlanManager = plan_module.PlanManager


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
def plan_manager(workspace_manager):
    """Create a PlanManager for testing."""
    return PlanManager(workspace_manager)


class TestPlanManagerBasics:
    """Tests for basic PlanManager operations."""

    def test_plan_file_constant(self):
        """Test that plan file constant is set correctly."""
        assert PlanManager.PLAN_FILE == "plan.md"

    def test_exists_false_initially(self, plan_manager):
        """Test that plan doesn't exist initially."""
        assert plan_manager.exists() is False

    def test_exists_after_write(self, plan_manager):
        """Test that exists returns True after writing."""
        plan_manager.write("# Test Plan")
        assert plan_manager.exists() is True

    def test_write_and_read(self, plan_manager):
        """Test basic write and read."""
        content = "# My Plan\n\n## Phase 1\n- Do something"
        plan_manager.write(content)

        result = plan_manager.read()
        assert result == content

    def test_read_nonexistent(self, plan_manager):
        """Test reading when plan doesn't exist."""
        result = plan_manager.read()
        assert result == ""


class TestPlanManagerIsComplete:
    """Tests for plan completion detection."""

    def test_is_complete_empty(self, plan_manager):
        """Test that empty plan is not complete."""
        assert plan_manager.is_complete() is False

    def test_is_complete_no_content(self, plan_manager):
        """Test that empty string is not complete."""
        assert plan_manager.is_complete("") is False

    def test_is_complete_explicit_marker_lowercase(self, plan_manager):
        """Test detection of explicit completion markers."""
        content = "# Plan\n\n# complete\n\nEverything is done."
        assert plan_manager.is_complete(content) is True

    def test_is_complete_explicit_marker_status(self, plan_manager):
        """Test detection via status marker."""
        content = "# Plan\n\nStatus: Complete\n\n## Phase 1 (done)"
        assert plan_manager.is_complete(content) is True

    def test_is_complete_goal_achieved(self, plan_manager):
        """Test detection via goal achieved phrase."""
        content = "# Plan\n\nGoal achieved! All tasks done."
        assert plan_manager.is_complete(content) is True

    def test_is_complete_all_phases_complete(self, plan_manager):
        """Test detection via all phases complete phrase."""
        content = "# Plan\n\nAll phases complete."
        assert plan_manager.is_complete(content) is True

    def test_is_complete_with_unchecked_todos(self, plan_manager):
        """Test that unchecked todos mean not complete."""
        content = """# Plan

## Phase 1 (complete)
- [x] Task 1
- [x] Task 2

## Phase 2
- [ ] Task 3
- [ ] Task 4
"""
        assert plan_manager.is_complete(content) is False

    def test_is_complete_with_pending_status(self, plan_manager):
        """Test that pending status means not complete."""
        content = """# Plan

## Phase 1
- [x] Task 1

## Phase 2
Status: pending
"""
        assert plan_manager.is_complete(content) is False

    def test_is_complete_with_in_progress_status(self, plan_manager):
        """Test that in progress status means not complete."""
        content = """# Plan

Status: in progress

## Phase 1
- [x] Task 1
"""
        assert plan_manager.is_complete(content) is False

    def test_is_complete_all_checked(self, plan_manager):
        """Test that all checked todos means complete."""
        content = """# Plan

## Phase 1
- [x] Task 1
- [x] Task 2

## Phase 2
- [x] Task 3
- [x] Task 4
"""
        assert plan_manager.is_complete(content) is True

    def test_is_complete_no_phases(self, plan_manager):
        """Test plan with no phases is not complete."""
        content = "# Plan\n\nSome random text without phases."
        assert plan_manager.is_complete(content) is False

    def test_is_complete_reads_from_file(self, plan_manager):
        """Test that is_complete reads from file when no content provided."""
        plan_manager.write("# Plan\n\n# Complete")
        assert plan_manager.is_complete() is True


class TestPlanManagerGetCurrentPhase:
    """Tests for getting the current phase."""

    def test_get_current_phase_empty(self, plan_manager):
        """Test getting current phase with no plan."""
        assert plan_manager.get_current_phase() is None

    def test_get_current_phase_first_incomplete(self, plan_manager):
        """Test getting first incomplete phase."""
        content = """# Plan

## Phase 1: Setup
- Set up environment

## Phase 2: Implementation
- Write code
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert current is not None
        assert "Phase 1" in current

    def test_get_current_phase_skips_complete(self, plan_manager):
        """Test that completed phases are skipped."""
        content = """# Plan

## Phase 1: Setup (complete)
- [x] Set up environment

## Phase 2: Implementation
- Write code
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert current is not None
        assert "Phase 2" in current

    def test_get_current_phase_status_done(self, plan_manager):
        """Test that phases with status: done are skipped."""
        content = """# Plan

## Phase 1: Setup
Status: done
- [x] Set up environment

## Phase 2: Implementation
- Write code
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert current is not None
        assert "Phase 2" in current

    def test_get_current_phase_all_complete(self, plan_manager):
        """Test when all phases are complete."""
        content = """# Plan

## Phase 1: Setup (complete)
- [x] Set up environment

## Phase 2: Implementation (complete)
- [x] Write code
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert current is None

    def test_get_current_phase_case_insensitive(self, plan_manager):
        """Test that phase matching is case insensitive."""
        content = """# Plan

## PHASE 1: Setup (COMPLETE)
- [x] Done

## PHASE 2: Implementation
- To do
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert current is not None
        assert "2" in current


class TestPlanManagerMarkPhaseComplete:
    """Tests for marking phases as complete."""

    def test_mark_phase_complete_by_number(self, plan_manager):
        """Test marking a phase complete by number."""
        content = """# Plan

## Phase 1: Setup
- Set up environment

## Phase 2: Implementation
- Write code
"""
        plan_manager.write(content)

        result = plan_manager.mark_phase_complete("1")

        assert result is True
        updated = plan_manager.read()
        assert "Phase 1: Setup (COMPLETE)" in updated

    def test_mark_phase_complete_by_name(self, plan_manager):
        """Test marking a phase complete by name."""
        content = """# Plan

## Phase 1: Setup
- Set up environment
"""
        plan_manager.write(content)

        result = plan_manager.mark_phase_complete("Phase 1")

        assert result is True
        updated = plan_manager.read()
        assert "(COMPLETE)" in updated

    def test_mark_phase_complete_already_marked(self, plan_manager):
        """Test marking a phase that's already complete."""
        content = """# Plan

## Phase 1: Setup (complete)
- [x] Set up environment
"""
        plan_manager.write(content)

        result = plan_manager.mark_phase_complete("1")

        assert result is True
        updated = plan_manager.read()
        # Should not add duplicate marker
        assert updated.count("complete") == 1

    def test_mark_phase_complete_not_found(self, plan_manager):
        """Test marking a phase that doesn't exist."""
        content = """# Plan

## Phase 1: Setup
- Set up environment
"""
        plan_manager.write(content)

        result = plan_manager.mark_phase_complete("99")

        assert result is False

    def test_mark_phase_complete_no_plan(self, plan_manager):
        """Test marking phase with no plan file."""
        result = plan_manager.mark_phase_complete("1")
        assert result is False


class TestPlanManagerEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_phase_headers_with_different_levels(self, plan_manager):
        """Test handling different header levels."""
        content = """# Plan

# Phase 1: Main Section
Some content

## Phase 2: Subsection
More content
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert current is not None

    def test_special_characters_in_phase_name(self, plan_manager):
        """Test phases with special characters."""
        content = """# Plan

## Phase 1: Setup & Configuration (v1.0)
- Configure

## Phase 2: Implementation
- Code
"""
        plan_manager.write(content)
        current = plan_manager.get_current_phase()
        assert "Setup & Configuration" in current or "Phase 1" in current

    def test_multiline_content_preservation(self, plan_manager):
        """Test that multiline content is preserved."""
        content = """# Plan

This is a multiline
plan with multiple
paragraphs.

## Phase 1
- Task 1
- Task 2
"""
        plan_manager.write(content)
        result = plan_manager.read()
        assert result == content

    def test_unicode_content(self, plan_manager):
        """Test handling unicode content."""
        content = """# Plan

## Phase 1: 日本語テスト
- Task with émojis 🎉
"""
        plan_manager.write(content)
        result = plan_manager.read()
        assert "日本語テスト" in result
        assert "🎉" in result
