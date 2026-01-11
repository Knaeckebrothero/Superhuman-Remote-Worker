"""Tests for phase transition functionality (Guardrails Phase 4).

Tests the PhaseTransitionManager and the integration with todo_complete()
for automatic phase transitions.
"""

import pytest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import tempfile
import shutil

# Import directly to avoid neo4j import issues
import sys
import importlib.util


def _import_module_directly(module_path: Path, module_name: str):
    """Import a module directly without triggering __init__.py side effects."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Get the src directory
SRC_DIR = Path(__file__).parent.parent / "src" / "agent"

# Import the modules we need for testing
phase_transition_module = _import_module_directly(
    SRC_DIR / "phase_transition.py", "phase_transition_test"
)
todo_manager_module = _import_module_directly(
    SRC_DIR / "todo_manager.py", "todo_manager_test"
)

# Get classes and functions
PhaseTransitionManager = phase_transition_module.PhaseTransitionManager
PhaseInfo = phase_transition_module.PhaseInfo
TransitionResult = phase_transition_module.TransitionResult
TransitionType = phase_transition_module.TransitionType
get_job_completion_todos = phase_transition_module.get_job_completion_todos
PHASE_TRANSITION_PROMPT = phase_transition_module.PHASE_TRANSITION_PROMPT
JOB_COMPLETE_PROMPT = phase_transition_module.JOB_COMPLETE_PROMPT
REWIND_TRANSITION_PROMPT = phase_transition_module.REWIND_TRANSITION_PROMPT

TodoManager = todo_manager_module.TodoManager
TodoStatus = todo_manager_module.TodoStatus


class TestPhaseInfo:
    """Tests for PhaseInfo dataclass."""

    def test_phase_info_properties(self):
        """Test PhaseInfo status properties."""
        current = PhaseInfo(number=1, name="Test", status="current")
        assert current.is_current is True
        assert current.is_complete is False

        complete = PhaseInfo(number=2, name="Done", status="complete")
        assert complete.is_current is False
        assert complete.is_complete is True

        pending = PhaseInfo(number=3, name="Pending", status="pending")
        assert pending.is_current is False
        assert pending.is_complete is False

    def test_phase_info_with_steps(self):
        """Test PhaseInfo with steps list."""
        phase = PhaseInfo(
            number=1,
            name="Extraction",
            status="current",
            steps=["Step 1", "Step 2", "Step 3"],
        )
        assert len(phase.steps) == 3
        assert phase.steps[0] == "Step 1"


class TestTransitionResult:
    """Tests for TransitionResult dataclass."""

    def test_default_transition_result(self):
        """Test default TransitionResult values."""
        result = TransitionResult(should_transition=False)
        assert result.should_transition is False
        assert result.transition_type is None
        assert result.next_phase is None
        assert result.transition_prompt == ""
        assert result.trigger_summarization is False
        assert result.metadata == {}

    def test_transition_result_with_all_fields(self):
        """Test TransitionResult with all fields populated."""
        phase = PhaseInfo(number=2, name="Validation", status="pending")
        result = TransitionResult(
            should_transition=True,
            transition_type=TransitionType.NEXT_PHASE,
            next_phase=phase,
            transition_prompt="Test prompt",
            trigger_summarization=True,
            metadata={"from_phase": 1, "to_phase": 2},
        )
        assert result.should_transition is True
        assert result.transition_type == TransitionType.NEXT_PHASE
        assert result.next_phase.number == 2
        assert result.trigger_summarization is True


class TestParsePlanContent:
    """Tests for parsing main_plan.md content."""

    def test_parse_simple_plan(self):
        """Test parsing a simple plan with phases."""
        content = """# Execution Plan

## Overview
This is a test plan.

## Phase 1: Document Analysis ✓ COMPLETE
- [x] Extract document structure
- [x] Identify key sections

## Phase 2: Requirement Extraction ← CURRENT
- [ ] Process section 1-3
- [ ] Process section 4-6
- [ ] Consolidate findings

## Phase 3: Validation & Integration
- [ ] Validate against schema
- [ ] Check for duplicates
- [ ] Integrate into graph
"""
        manager = PhaseTransitionManager()
        phases, current = manager._parse_plan_content(content)

        assert len(phases) == 3
        assert phases[0].number == 1
        assert phases[0].name == "Document Analysis"
        assert phases[0].is_complete is True
        assert len(phases[0].steps) == 2

        assert phases[1].number == 2
        assert phases[1].name == "Requirement Extraction"
        assert phases[1].is_current is True
        assert len(phases[1].steps) == 3

        assert phases[2].number == 3
        assert phases[2].name == "Validation & Integration"
        assert phases[2].status == "pending"
        assert len(phases[2].steps) == 3

        assert current is not None
        assert current.number == 2

    def test_parse_empty_plan(self):
        """Test parsing an empty or invalid plan."""
        manager = PhaseTransitionManager()

        # Empty content
        phases, current = manager._parse_plan_content("")
        assert phases == []
        assert current is None

        # No phases
        content = "# Plan\n\nSome content without phases."
        phases, current = manager._parse_plan_content(content)
        assert phases == []
        assert current is None

    def test_parse_all_complete_plan(self):
        """Test parsing a plan where all phases are complete."""
        content = """# Execution Plan

## Phase 1: Analysis ✓ COMPLETE
- [x] Task 1

## Phase 2: Extraction ✓ COMPLETE
- [x] Task 2

## Phase 3: Validation ✓ COMPLETE
- [x] Task 3
"""
        manager = PhaseTransitionManager()
        phases, current = manager._parse_plan_content(content)

        assert len(phases) == 3
        assert all(p.is_complete for p in phases)
        assert current is None  # No current phase


class TestGetNextPhase:
    """Tests for determining the next phase."""

    def test_get_next_phase_normal(self):
        """Test getting next phase in normal flow."""
        phases = [
            PhaseInfo(number=1, name="Phase 1", status="complete"),
            PhaseInfo(number=2, name="Phase 2", status="current"),
            PhaseInfo(number=3, name="Phase 3", status="pending"),
        ]
        current = phases[1]

        manager = PhaseTransitionManager()
        next_phase = manager.get_next_phase(phases, current)

        assert next_phase is not None
        assert next_phase.number == 3
        assert next_phase.name == "Phase 3"

    def test_get_next_phase_all_complete(self):
        """Test when all phases are complete."""
        phases = [
            PhaseInfo(number=1, name="Phase 1", status="complete"),
            PhaseInfo(number=2, name="Phase 2", status="complete"),
        ]
        current = phases[1]

        manager = PhaseTransitionManager()
        next_phase = manager.get_next_phase(phases, current)

        assert next_phase is None

    def test_get_next_phase_no_current(self):
        """Test getting next phase when no current phase."""
        phases = [
            PhaseInfo(number=1, name="Phase 1", status="pending"),
            PhaseInfo(number=2, name="Phase 2", status="pending"),
        ]

        manager = PhaseTransitionManager()
        next_phase = manager.get_next_phase(phases, None)

        # Should return first non-complete phase
        assert next_phase is not None
        assert next_phase.number == 1


class TestCheckTransition:
    """Tests for check_transition() method."""

    def test_no_transition_when_not_last_task(self):
        """Test that no transition occurs when is_last_task=False."""
        manager = PhaseTransitionManager()
        result = manager.check_transition(is_last_task=False)

        assert result.should_transition is False

    def test_transition_to_next_phase(self):
        """Test transition to next phase."""
        # Create mock workspace that returns a plan
        mock_workspace = MagicMock()
        mock_workspace.exists.return_value = True
        mock_workspace.read_file.return_value = """
## Phase 1: Analysis ✓ COMPLETE
- [x] Task 1

## Phase 2: Extraction ← CURRENT
- [x] Task 2

## Phase 3: Validation
- [ ] Task 3
"""

        manager = PhaseTransitionManager(workspace_manager=mock_workspace)
        result = manager.check_transition(is_last_task=True)

        assert result.should_transition is True
        assert result.transition_type == TransitionType.NEXT_PHASE
        assert result.next_phase is not None
        assert result.next_phase.number == 3
        assert result.trigger_summarization is True
        assert "PHASE TRANSITION" in result.transition_prompt

    def test_job_complete_when_all_phases_done(self):
        """Test job completion transition when all phases are complete."""
        mock_workspace = MagicMock()
        mock_workspace.exists.return_value = True
        mock_workspace.read_file.return_value = """
## Phase 1: Analysis ✓ COMPLETE
- [x] Task 1

## Phase 2: Extraction ← CURRENT
- [x] Task 2
"""

        manager = PhaseTransitionManager(workspace_manager=mock_workspace)
        result = manager.check_transition(is_last_task=True)

        assert result.should_transition is True
        assert result.transition_type == TransitionType.JOB_COMPLETE
        assert result.next_phase is None
        assert result.trigger_summarization is True
        assert "ALL PHASES COMPLETE" in result.transition_prompt


class TestRewindTransition:
    """Tests for rewind transition."""

    def test_check_rewind_transition(self):
        """Test creating a rewind transition."""
        manager = PhaseTransitionManager()
        issue = "The API doesn't support batch operations"

        result = manager.check_rewind_transition(issue)

        assert result.should_transition is True
        assert result.transition_type == TransitionType.REWIND
        assert result.trigger_summarization is True
        assert "REWIND" in result.transition_prompt
        assert issue in result.transition_prompt
        assert result.metadata["rewind_issue"] == issue


class TestExecuteTransition:
    """Tests for execute_transition() method."""

    def test_execute_transition_no_transition(self):
        """Test execute with no transition."""
        manager = PhaseTransitionManager()
        result = TransitionResult(should_transition=False)

        execution = manager.execute_transition(result)

        assert execution["success"] is False

    def test_execute_transition_with_mocks(self):
        """Test execute transition with mocked managers."""
        # Create temp directory for workspace
        temp_dir = tempfile.mkdtemp()
        try:
            # Create mock workspace
            mock_workspace = MagicMock()
            mock_workspace.write_file = MagicMock()
            mock_workspace.exists.return_value = True
            mock_workspace.read_file.return_value = """
## Phase 1: Analysis ✓ COMPLETE
- [x] Task 1

## Phase 2: Extraction
- [ ] Task 2
"""

            # Create real todo manager
            todo_mgr = TodoManager()
            todo_mgr.set_workspace_manager(mock_workspace)
            todo_mgr.add_sync("Test task 1")
            todo_mgr.complete_sync("todo_1")

            manager = PhaseTransitionManager(
                workspace_manager=mock_workspace,
                todo_manager=todo_mgr,
            )

            next_phase = PhaseInfo(number=2, name="Extraction", status="pending")
            result = TransitionResult(
                should_transition=True,
                transition_type=TransitionType.NEXT_PHASE,
                next_phase=next_phase,
                trigger_summarization=True,
            )

            execution = manager.execute_transition(result)

            assert execution["success"] is True
            assert execution["phase_updated"] is True
            assert todo_mgr.get_phase_info()["phase_number"] == 2

        finally:
            shutil.rmtree(temp_dir)


class TestGetJobCompletionTodos:
    """Tests for get_job_completion_todos() function."""

    def test_job_completion_todos_structure(self):
        """Test the structure of job completion todos."""
        todos = get_job_completion_todos()

        assert isinstance(todos, list)
        assert len(todos) == 4

        # Check that all have required fields
        for todo in todos:
            assert "content" in todo
            assert "status" in todo
            assert "priority" in todo
            assert todo["status"] == "pending"

    def test_job_completion_todos_include_job_complete(self):
        """Test that job completion todos include job_complete() call."""
        todos = get_job_completion_todos()

        # Check that the last todo mentions job_complete()
        last_todo = todos[-1]
        assert "job_complete" in last_todo["content"].lower()


class TestPromptTemplates:
    """Tests for transition prompt templates."""

    def test_phase_transition_prompt_format(self):
        """Test phase transition prompt has expected format."""
        assert "PHASE TRANSITION" in PHASE_TRANSITION_PROMPT
        assert "{current_phase}" in PHASE_TRANSITION_PROMPT
        assert "{next_phase}" in PHASE_TRANSITION_PROMPT
        assert "main_plan.md" in PHASE_TRANSITION_PROMPT

    def test_job_complete_prompt_format(self):
        """Test job complete prompt has expected format."""
        assert "ALL PHASES COMPLETE" in JOB_COMPLETE_PROMPT
        assert "job_complete()" in JOB_COMPLETE_PROMPT
        assert "workspace_summary.md" in JOB_COMPLETE_PROMPT

    def test_rewind_prompt_format(self):
        """Test rewind prompt has expected format."""
        assert "REWIND" in REWIND_TRANSITION_PROMPT
        assert "{issue}" in REWIND_TRANSITION_PROMPT
        assert "main_plan.md" in REWIND_TRANSITION_PROMPT


class TestIntegrationWithTodoManager:
    """Integration tests with TodoManager."""

    def test_complete_last_task_triggers_transition_check(self):
        """Test that completing the last task returns is_last_task=True."""
        todo_mgr = TodoManager()
        todo_mgr.add_sync("Task 1")
        todo_mgr.add_sync("Task 2")

        # Complete first task
        result1 = todo_mgr.complete_first_pending_sync()
        assert result1["is_last_task"] is False
        assert result1["remaining"] == 1

        # Complete second (last) task
        result2 = todo_mgr.complete_first_pending_sync()
        assert result2["is_last_task"] is True
        assert result2["remaining"] == 0

    def test_phase_info_preserved_through_transition(self):
        """Test that phase info is correctly updated through transition."""
        todo_mgr = TodoManager()
        todo_mgr.set_phase_info(phase_number=1, total_phases=3, phase_name="Analysis")

        info = todo_mgr.get_phase_info()
        assert info["phase_number"] == 1
        assert info["total_phases"] == 3
        assert info["phase_name"] == "Analysis"

        # Simulate transition to phase 2
        todo_mgr.set_phase_info(phase_number=2, total_phases=3, phase_name="Extraction")

        info = todo_mgr.get_phase_info()
        assert info["phase_number"] == 2
        assert info["phase_name"] == "Extraction"


class TestEdgeCases:
    """Tests for edge cases."""

    def test_no_workspace_manager(self):
        """Test behavior without workspace manager."""
        manager = PhaseTransitionManager()  # No workspace

        phases, current = manager.parse_main_plan()
        assert phases == []
        assert current is None

    def test_plan_file_not_found(self):
        """Test when main_plan.md doesn't exist."""
        mock_workspace = MagicMock()
        mock_workspace.exists.return_value = False

        manager = PhaseTransitionManager(workspace_manager=mock_workspace)

        phases, current = manager.parse_main_plan()
        assert phases == []
        assert current is None

    def test_malformed_phase_markers(self):
        """Test parsing with malformed phase markers."""
        content = """
## Phase 1: Analysis
- [x] Task

## Phase 2 Extraction  ← CURRENT
- [ ] Task

## Phase 3: Validation COMPLETE
- [ ] Task
"""
        manager = PhaseTransitionManager()
        phases, current = manager._parse_plan_content(content)

        # Should still parse phases even with some formatting issues
        assert len(phases) >= 1

    def test_empty_todo_list_transition(self):
        """Test transition check when plan is empty (e.g., during bootstrap)."""
        mock_workspace = MagicMock()
        mock_workspace.exists.return_value = True
        mock_workspace.read_file.return_value = ""

        manager = PhaseTransitionManager(workspace_manager=mock_workspace)
        result = manager.check_transition(is_last_task=True)

        # Should NOT trigger transition when no phases exist
        # This allows bootstrap to continue without premature job completion
        assert result.should_transition is False
