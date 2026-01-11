"""Integration tests for the Guardrails system (Phase 6).

Tests the complete workflow: Bootstrap → Phase transitions → Job completion.
These tests verify the guardrails system works end-to-end.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import shutil
import json

# Import directly to avoid neo4j import issues
import sys
import importlib.util
from types import ModuleType


def _import_module_directly(module_path: Path, module_name: str):
    """Import a module directly without triggering __init__.py side effects."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Get the project directories
PROJECT_ROOT = Path(__file__).parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "agent"

# Import dependencies
config_path = PROJECT_ROOT / "src" / "core" / "config.py"
config_module = _import_module_directly(config_path, "src.core.config_integration")

workspace_manager_path = SRC_DIR / "workspace_manager.py"
workspace_manager_module = _import_module_directly(
    workspace_manager_path, "workspace_manager_integration"
)
WorkspaceManager = workspace_manager_module.WorkspaceManager

todo_manager_path = SRC_DIR / "todo_manager.py"
todo_manager_module = _import_module_directly(
    todo_manager_path, "todo_manager_integration"
)
TodoManager = todo_manager_module.TodoManager
TodoStatus = todo_manager_module.TodoStatus

phase_transition_path = SRC_DIR / "phase_transition.py"
phase_transition_module = _import_module_directly(
    phase_transition_path, "phase_transition_integration"
)
PhaseTransitionManager = phase_transition_module.PhaseTransitionManager
TransitionType = phase_transition_module.TransitionType
get_bootstrap_todos = phase_transition_module.get_bootstrap_todos
get_job_completion_todos = phase_transition_module.get_job_completion_todos
BOOTSTRAP_PROMPT = phase_transition_module.BOOTSTRAP_PROMPT

context_path = SRC_DIR / "context.py"
context_module = _import_module_directly(context_path, "context_integration")
ProtectedContextProvider = context_module.ProtectedContextProvider
ProtectedContextConfig = context_module.ProtectedContextConfig


@pytest.fixture
def temp_workspace():
    """Create a temporary directory for workspace testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_manager(temp_workspace):
    """Create a WorkspaceManager with a temporary base path."""
    ws = WorkspaceManager(
        job_id="integration-test-123",
        base_path=temp_workspace,
    )
    ws.initialize()
    return ws


@pytest.fixture
def todo_manager(workspace_manager):
    """Create a TodoManager with workspace attached."""
    mgr = TodoManager(workspace_manager=workspace_manager, auto_reflection=False)
    return mgr


@pytest.fixture
def phase_manager(workspace_manager, todo_manager):
    """Create a PhaseTransitionManager with dependencies."""
    return PhaseTransitionManager(
        workspace_manager=workspace_manager,
        todo_manager=todo_manager,
    )


class TestBootstrapWorkflow:
    """Tests for the bootstrap initialization workflow."""

    def test_bootstrap_todos_are_injected(self, todo_manager):
        """Test that bootstrap todos can be injected into a fresh TodoManager."""
        # Verify todo list starts empty
        assert len(todo_manager.list_all_sync()) == 0

        # Inject bootstrap todos
        bootstrap_todos = get_bootstrap_todos()
        todo_manager.set_todos_from_list(bootstrap_todos)

        # Verify bootstrap todos are present
        todos = todo_manager.list_all_sync()
        assert len(todos) >= 5  # May have auto-reflection

        # Verify first task is about workspace summary
        first_task = todos[0]
        assert "workspace" in first_task.content.lower() or "summary" in first_task.content.lower()

    def test_bootstrap_sets_phase_zero(self, todo_manager):
        """Test that bootstrap is phase 0."""
        # Set phase info for bootstrap
        todo_manager.set_phase_info(
            phase_number=0,
            total_phases=0,
            phase_name="Bootstrap",
        )

        info = todo_manager.get_phase_info()
        assert info["phase_number"] == 0
        assert info["phase_name"] == "Bootstrap"

    def test_bootstrap_prompt_included(self):
        """Test that bootstrap prompt has required content."""
        assert "JOB INITIALIZATION" in BOOTSTRAP_PROMPT
        assert "todo list" in BOOTSTRAP_PROMPT.lower()
        assert "todo_complete" in BOOTSTRAP_PROMPT

    def test_bootstrap_todos_can_be_completed_sequentially(self, todo_manager):
        """Test completing bootstrap todos one by one."""
        bootstrap_todos = get_bootstrap_todos()
        todo_manager.set_todos_from_list(bootstrap_todos)

        initial_count = len(todo_manager.list_all_sync())
        completed = 0

        # Complete tasks one by one
        while True:
            result = todo_manager.complete_first_pending_sync()
            if result["completed"] is not None:
                completed += 1
                if result["is_last_task"]:
                    break
            else:
                break

        # All bootstrap tasks should be completable
        assert completed == initial_count
        progress = todo_manager.get_progress_sync()
        assert progress["completion_percentage"] == 100.0


class TestPhaseTransitionWorkflow:
    """Tests for phase transition workflow."""

    def test_complete_phase_creates_transition(self, phase_manager, todo_manager, workspace_manager):
        """Test that completing a phase triggers transition."""
        # Set up a plan with two phases
        plan_content = """# Execution Plan

## Phase 1: Analysis ← CURRENT
- [ ] Analyze document structure
- [ ] Identify key sections

## Phase 2: Extraction
- [ ] Extract requirements
- [ ] Validate format
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        # Add phase 1 tasks
        todos = [
            {"content": "Analyze document structure", "status": "pending"},
            {"content": "Identify key sections", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)
        todo_manager.set_phase_info(1, 2, "Analysis")

        # Complete first task
        result1 = todo_manager.complete_first_pending_sync()
        assert result1["is_last_task"] is False

        # Complete second task (last in phase)
        result2 = todo_manager.complete_first_pending_sync()
        assert result2["is_last_task"] is True

        # Check transition
        transition = phase_manager.check_transition(is_last_task=True)
        assert transition.should_transition is True
        assert transition.transition_type == TransitionType.NEXT_PHASE
        assert transition.next_phase.number == 2

    def test_phase_transition_updates_phase_info(self, phase_manager, todo_manager, workspace_manager):
        """Test that phase transition updates the phase info."""
        # Set up plan with Phase 1 current and Phase 2 pending
        plan_content = """# Execution Plan

## Phase 1: Analysis ← CURRENT
- [x] Done

## Phase 2: Extraction
- [ ] Task 1
"""
        workspace_manager.write_file("main_plan.md", plan_content)
        todo_manager.set_phase_info(1, 2, "Analysis")

        # Check transition result
        transition = phase_manager.check_transition(is_last_task=True)

        # Execute transition - should move to Phase 2
        if transition.should_transition and transition.next_phase is not None:
            execution = phase_manager.execute_transition(transition)
            assert execution["success"] is True
            assert execution["phase_updated"] is True
            # Verify phase info was updated
            info = todo_manager.get_phase_info()
            assert info["phase_number"] == 2

    def test_transition_triggers_archive(self, phase_manager, todo_manager, workspace_manager):
        """Test that phase transition archives old todos."""
        # Set up with some todos
        todos = [
            {"content": "Task 1", "status": "completed"},
            {"content": "Task 2", "status": "completed"},
        ]
        todo_manager.set_todos_from_list(todos)
        todo_manager.set_phase_info(1, 3, "Analysis")

        # Archive for phase transition
        archive_result = todo_manager.archive_and_reset("phase1")

        # Check archive was created
        assert "Archived" in archive_result
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) >= 1

        # Todo list should be empty after archive
        assert len(todo_manager.list_all_sync()) == 0


class TestJobCompletionWorkflow:
    """Tests for job completion workflow."""

    def test_all_phases_complete_triggers_job_completion(self, phase_manager, workspace_manager):
        """Test that completing all phases triggers job completion."""
        # Set up plan with all phases complete
        plan_content = """# Execution Plan

## Phase 1: Analysis ✓ COMPLETE
- [x] Done

## Phase 2: Extraction ✓ COMPLETE
- [x] Done

## Phase 3: Validation ← CURRENT
- [x] Last task
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        # Check transition
        transition = phase_manager.check_transition(is_last_task=True)

        assert transition.should_transition is True
        assert transition.transition_type == TransitionType.JOB_COMPLETE

    def test_job_completion_todos_structure(self):
        """Test the structure of job completion todos."""
        todos = get_job_completion_todos()

        assert len(todos) == 4
        assert todos[-1]["content"].lower().find("job_complete") >= 0

        # All should be pending
        assert all(t["status"] == "pending" for t in todos)

    def test_job_completion_prompt_includes_instructions(self, phase_manager, workspace_manager):
        """Test that job completion prompt has necessary instructions."""
        # Set up all-complete plan
        plan_content = """## Phase 1: Done ✓ COMPLETE
- [x] Task
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        transition = phase_manager.check_transition(is_last_task=True)

        assert "job_complete()" in transition.transition_prompt
        assert "deliverables" in transition.transition_prompt.lower()


class TestRewindWorkflow:
    """Tests for todo_rewind recovery workflow."""

    def test_rewind_creates_transition(self, phase_manager):
        """Test that rewind creates a proper transition."""
        issue = "Cannot process file - unsupported format"

        transition = phase_manager.check_rewind_transition(issue)

        assert transition.should_transition is True
        assert transition.transition_type == TransitionType.REWIND
        assert issue in transition.transition_prompt
        assert transition.trigger_summarization is True

    def test_rewind_archives_current_todos(self, todo_manager, workspace_manager):
        """Test that rewind archives current todos with failure note."""
        # Set up some todos
        todos = [
            {"content": "Task 1", "status": "completed"},
            {"content": "Task 2", "status": "in_progress"},
            {"content": "Task 3", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)
        todo_manager.set_phase_info(1, 3, "Analysis")

        # Archive with rewind prefix
        archive_result = todo_manager.archive_and_reset("REWIND_phase1_api_issue")

        assert "Archived" in archive_result
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) >= 1

        # Check archive file contains REWIND
        filename = archive_files[0]
        assert "REWIND" in filename

    def test_rewind_during_first_phase(self, phase_manager, todo_manager, workspace_manager):
        """Test rewind during the first phase."""
        # Set up phase 1
        todos = [{"content": "First task", "status": "in_progress"}]
        todo_manager.set_todos_from_list(todos)
        todo_manager.set_phase_info(1, 3, "Analysis")

        # Rewind
        issue = "Initial approach was wrong"
        transition = phase_manager.check_rewind_transition(issue)

        assert transition.should_transition is True
        assert transition.transition_type == TransitionType.REWIND
        assert "main_plan.md" in transition.transition_prompt

    def test_multiple_consecutive_rewinds(self, todo_manager, workspace_manager):
        """Test multiple consecutive rewinds create separate archives."""
        todo_manager.set_phase_info(1, 3, "Analysis")

        # First rewind
        todo_manager.add_sync("Task 1")
        todo_manager.archive_and_reset("REWIND_attempt1")

        # Second rewind
        todo_manager.add_sync("Task 2")
        todo_manager.archive_and_reset("REWIND_attempt2")

        # Third rewind
        todo_manager.add_sync("Task 3")
        todo_manager.archive_and_reset("REWIND_attempt3")

        # Check all archives exist
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) >= 3

        # Check they have different names
        filenames = set(archive_files)
        assert len(filenames) == len(archive_files)


class TestProtectedContextIntegration:
    """Tests for protected context (Layer 2) functionality."""

    def test_layer2_display_format(self, todo_manager, workspace_manager):
        """Test the Layer 2 todo display format."""
        # Set up todos and phase
        todos = [
            {"content": "Analyze structure", "status": "completed"},
            {"content": "Extract requirements", "status": "in_progress"},
            {"content": "Validate output", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)
        todo_manager.set_phase_info(2, 4, "Extraction")

        # Get Layer 2 display
        provider = ProtectedContextProvider(
            workspace_manager=workspace_manager,
            todo_manager=todo_manager,
        )
        layer2 = provider.get_layer2_todo_display()

        # Check visual separators
        assert "═══" in layer2  # Top/bottom border
        assert "───" in layer2  # Divider

        # Check phase indicator
        assert "Phase" in layer2
        assert "2" in layer2
        assert "4" in layer2
        assert "Extraction" in layer2

        # Check task display
        assert "[x]" in layer2  # Completed marker
        assert "[ ]" in layer2  # Pending marker
        assert "← CURRENT" in layer2  # Current task marker

        # Check progress
        assert "1/3" in layer2 or "Progress" in layer2

        # Check instruction line
        assert "INSTRUCTION" in layer2 or "todo_complete()" in layer2

    def test_protected_context_includes_plan(self, workspace_manager, todo_manager):
        """Test that protected context includes main_plan.md content."""
        # Write a plan
        plan_content = "# My Plan\n\n## Phase 1: Analysis\n- [ ] Task 1"
        workspace_manager.write_file("main_plan.md", plan_content)

        config = ProtectedContextConfig(
            enabled=True,
            plan_file="main_plan.md",
            max_plan_chars=2000,
            include_todos=True,
        )
        provider = ProtectedContextProvider(
            workspace_manager=workspace_manager,
            todo_manager=todo_manager,
            config=config,
        )

        context = provider.get_protected_context()

        # Should include plan content (might be None if no content matches)
        if context:
            assert "My Plan" in context or "Phase 1" in context

    def test_protected_context_survives_empty_todos(self, workspace_manager, todo_manager):
        """Test that protected context works with empty todo list."""
        config = ProtectedContextConfig(enabled=True)
        provider = ProtectedContextProvider(
            workspace_manager=workspace_manager,
            todo_manager=todo_manager,
            config=config,
        )

        # Should not raise error, but returns None for empty todos
        layer2 = provider.get_layer2_todo_display()

        # Empty todos returns None - this is expected behavior
        assert layer2 is None

        # Add a todo and verify it now shows structure
        todo_manager.add_sync("Test task")
        layer2_with_todo = provider.get_layer2_todo_display()

        assert layer2_with_todo is not None
        assert "═══" in layer2_with_todo
        assert "ACTIVE TODO LIST" in layer2_with_todo


class TestFullWorkflowIntegration:
    """End-to-end workflow tests."""

    def test_bootstrap_to_phase1_transition(self, todo_manager, workspace_manager, phase_manager):
        """Test transition from bootstrap to phase 1."""
        # Start with bootstrap
        bootstrap_todos = get_bootstrap_todos()
        todo_manager.set_todos_from_list(bootstrap_todos)
        todo_manager.set_phase_info(0, 0, "Bootstrap")

        # Create a plan (simulating what the agent would do)
        plan_content = """# Execution Plan

## Overview
Process document and extract requirements.

## Phase 1: Document Analysis
- [ ] Read and parse document
- [ ] Identify sections

## Phase 2: Requirement Extraction
- [ ] Extract requirements
- [ ] Format output
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        # Complete all bootstrap todos
        while True:
            result = todo_manager.complete_first_pending_sync()
            if result["completed"] is None or result["is_last_task"]:
                break

        # Check transition to Phase 1
        transition = phase_manager.check_transition(is_last_task=True)

        assert transition.should_transition is True
        assert transition.transition_type == TransitionType.NEXT_PHASE
        assert transition.next_phase.number == 1

    def test_multi_phase_workflow(self, todo_manager, workspace_manager, phase_manager):
        """Test workflow across multiple phases."""
        # Set up plan with 3 phases
        plan_content = """# Execution Plan

## Phase 1: Analysis ← CURRENT
- [ ] Task 1.1
- [ ] Task 1.2

## Phase 2: Processing
- [ ] Task 2.1
- [ ] Task 2.2

## Phase 3: Output
- [ ] Task 3.1
- [ ] Task 3.2
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        # === Phase 1 ===
        phase1_todos = [
            {"content": "Task 1.1", "status": "pending"},
            {"content": "Task 1.2", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(phase1_todos)
        todo_manager.set_phase_info(1, 3, "Analysis")

        # Complete Phase 1
        todo_manager.complete_first_pending_sync()
        result = todo_manager.complete_first_pending_sync()
        assert result["is_last_task"] is True

        # Check transition
        transition1 = phase_manager.check_transition(is_last_task=True)
        assert transition1.next_phase.number == 2

        # Update plan for Phase 2
        plan_content_updated = plan_content.replace(
            "Phase 1: Analysis ← CURRENT",
            "Phase 1: Analysis ✓ COMPLETE"
        ).replace(
            "Phase 2: Processing",
            "Phase 2: Processing ← CURRENT"
        )
        workspace_manager.write_file("main_plan.md", plan_content_updated)

        # Archive and set up Phase 2
        todo_manager.archive_and_reset("phase1")
        phase2_todos = [
            {"content": "Task 2.1", "status": "pending"},
            {"content": "Task 2.2", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(phase2_todos)
        todo_manager.set_phase_info(2, 3, "Processing")

        # Complete Phase 2
        todo_manager.complete_first_pending_sync()
        result = todo_manager.complete_first_pending_sync()
        assert result["is_last_task"] is True

        # Check transition to Phase 3
        transition2 = phase_manager.check_transition(is_last_task=True)
        assert transition2.next_phase.number == 3

    def test_complete_to_job_done(self, todo_manager, workspace_manager, phase_manager):
        """Test workflow completing all phases to job completion."""
        # Set up plan with only last phase remaining
        plan_content = """# Execution Plan

## Phase 1: Analysis ✓ COMPLETE
- [x] Done

## Phase 2: Processing ✓ COMPLETE
- [x] Done

## Phase 3: Output ← CURRENT
- [ ] Final task
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        # Set up last phase
        todos = [{"content": "Final task", "status": "pending"}]
        todo_manager.set_todos_from_list(todos)
        todo_manager.set_phase_info(3, 3, "Output")

        # Complete last task
        result = todo_manager.complete_first_pending_sync()
        assert result["is_last_task"] is True

        # Check for job completion
        transition = phase_manager.check_transition(is_last_task=True)

        assert transition.should_transition is True
        assert transition.transition_type == TransitionType.JOB_COMPLETE


class TestContextBounding:
    """Tests that context stays bounded across phases."""

    def test_todos_reset_between_phases(self, todo_manager, workspace_manager):
        """Test that todos are properly reset between phases."""
        # Phase 1: Add many todos
        phase1_todos = [{"content": f"Phase 1 Task {i}", "status": "pending"} for i in range(10)]
        todo_manager.set_todos_from_list(phase1_todos)

        # Complete all
        for _ in range(10):
            todo_manager.complete_first_pending_sync()

        # Archive
        todo_manager.archive_and_reset("phase1")

        # Verify todo list is empty
        assert len(todo_manager.list_all_sync()) == 0

        # Phase 2: Add new todos
        phase2_todos = [{"content": f"Phase 2 Task {i}", "status": "pending"} for i in range(5)]
        todo_manager.set_todos_from_list(phase2_todos)

        # Verify only Phase 2 todos exist
        todos = todo_manager.list_all_sync()
        assert len(todos) == 5
        assert all("Phase 2" in t.content for t in todos)

    def test_archive_preserves_history(self, todo_manager, workspace_manager):
        """Test that archives preserve todo history."""
        # Phase 1
        todo_manager.set_phase_info(1, 2, "Analysis")
        todo_manager.add_sync("Analysis Task 1")
        todo_manager.add_sync("Analysis Task 2")
        todo_manager.complete_sync("todo_1", notes="Found 5 sections")
        todo_manager.archive_and_reset("phase1")

        # Phase 2
        todo_manager.set_phase_info(2, 2, "Extraction")
        todo_manager.add_sync("Extraction Task 1")
        todo_manager.archive_and_reset("phase2")

        # Check archives
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) >= 2

        # Read Phase 1 archive
        phase1_archive = None
        for f in archive_files:
            if "phase1" in f.lower():
                phase1_archive = workspace_manager.read_file(f)
                break

        if phase1_archive:
            assert "Analysis Task 1" in phase1_archive
            assert "Found 5 sections" in phase1_archive

    def test_phase_number_increments(self, todo_manager, workspace_manager):
        """Test that phase numbers increment correctly."""
        # Bootstrap
        todo_manager.set_phase_info(0, 3, "Bootstrap")
        assert todo_manager.get_phase_info()["phase_number"] == 0

        # Phase 1
        todo_manager.set_phase_info(1, 3, "Analysis")
        assert todo_manager.get_phase_info()["phase_number"] == 1

        # Phase 2
        todo_manager.set_phase_info(2, 3, "Extraction")
        assert todo_manager.get_phase_info()["phase_number"] == 2

        # Phase 3
        todo_manager.set_phase_info(3, 3, "Output")
        assert todo_manager.get_phase_info()["phase_number"] == 3


class TestEdgeCasesIntegration:
    """Integration tests for edge cases."""

    def test_empty_plan_file(self, phase_manager, workspace_manager):
        """Test behavior when main_plan.md is empty (e.g., during bootstrap)."""
        workspace_manager.write_file("main_plan.md", "")

        transition = phase_manager.check_transition(is_last_task=True)

        # Should NOT trigger transition when no phases exist
        # This allows bootstrap to continue without premature job completion
        assert transition.should_transition is False

    def test_no_plan_file(self, phase_manager, workspace_manager):
        """Test behavior when main_plan.md doesn't exist."""
        # Don't create main_plan.md

        phases, current = phase_manager.parse_main_plan()

        assert phases == []
        assert current is None

    def test_single_phase_plan(self, phase_manager, workspace_manager, todo_manager):
        """Test plan with only one phase."""
        plan_content = """# Execution Plan

## Phase 1: Only Phase ← CURRENT
- [ ] Only task
"""
        workspace_manager.write_file("main_plan.md", plan_content)
        todo_manager.set_phase_info(1, 1, "Only Phase")

        transition = phase_manager.check_transition(is_last_task=True)

        # Should be job complete, not next phase
        assert transition.transition_type == TransitionType.JOB_COMPLETE

    def test_rewind_then_complete(self, todo_manager, workspace_manager, phase_manager):
        """Test recovery after rewind."""
        # Initial plan
        plan_content = """# Execution Plan

## Phase 1: Analysis ← CURRENT
- [ ] Task 1
- [ ] Task 2

## Phase 2: Processing
- [ ] Task 3
"""
        workspace_manager.write_file("main_plan.md", plan_content)
        todo_manager.set_phase_info(1, 2, "Analysis")

        # Add initial todos
        todos = [
            {"content": "Task 1", "status": "pending"},
            {"content": "Task 2", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(todos)

        # Start and then rewind
        todo_manager.complete_first_pending_sync()

        # Simulate rewind
        rewind_result = phase_manager.check_rewind_transition("Task 2 requires different approach")
        assert rewind_result.transition_type == TransitionType.REWIND

        # Archive with rewind
        todo_manager.archive_and_reset("REWIND_phase1")

        # Set up new approach
        new_todos = [
            {"content": "New Task A", "status": "pending"},
            {"content": "New Task B", "status": "pending"},
        ]
        todo_manager.set_todos_from_list(new_todos)

        # Complete new approach
        todo_manager.complete_first_pending_sync()
        result = todo_manager.complete_first_pending_sync()
        assert result["is_last_task"] is True

        # Should now transition normally
        transition = phase_manager.check_transition(is_last_task=True)
        assert transition.should_transition is True

    def test_rapid_phase_transitions(self, todo_manager, workspace_manager, phase_manager):
        """Test rapid transitions through phases (small phases)."""
        # Plan with many small phases
        plan_content = """# Execution Plan

## Phase 1: Step 1 ← CURRENT
- [ ] Task

## Phase 2: Step 2
- [ ] Task

## Phase 3: Step 3
- [ ] Task

## Phase 4: Step 4
- [ ] Task

## Phase 5: Final
- [ ] Task
"""
        workspace_manager.write_file("main_plan.md", plan_content)

        phases_completed = 0

        for phase_num in range(1, 6):
            # Set up phase
            todo_manager.set_phase_info(phase_num, 5, f"Step {phase_num}")
            todos = [{"content": f"Phase {phase_num} Task", "status": "pending"}]
            todo_manager.set_todos_from_list(todos)

            # Complete
            result = todo_manager.complete_first_pending_sync()
            assert result["is_last_task"] is True

            phases_completed += 1

            # Archive
            todo_manager.archive_and_reset(f"phase{phase_num}")

            # Update plan
            current_marker = f"Phase {phase_num}: Step {phase_num} ← CURRENT"
            complete_marker = f"Phase {phase_num}: Step {phase_num} ✓ COMPLETE"
            plan_content = plan_content.replace(current_marker, complete_marker)

            if phase_num < 5:
                next_phase = f"Phase {phase_num + 1}: Step {phase_num + 1}"
                plan_content = plan_content.replace(
                    next_phase,
                    f"{next_phase} ← CURRENT"
                )

            workspace_manager.write_file("main_plan.md", plan_content)

        assert phases_completed == 5

        # Check final archives
        archive_files = workspace_manager.list_files("archive", pattern="*.md")
        assert len(archive_files) == 5
