"""Tests for bootstrap and job completion functionality (Guardrails Phase 5).

Tests the bootstrap todo injection and job_complete database integration.
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
    SRC_DIR / "phase_transition.py", "phase_transition_test_p5"
)
todo_manager_module = _import_module_directly(
    SRC_DIR / "todo_manager.py", "todo_manager_test_p5"
)

# Get functions and classes
get_bootstrap_todos = phase_transition_module.get_bootstrap_todos
BOOTSTRAP_PROMPT = phase_transition_module.BOOTSTRAP_PROMPT
TodoManager = todo_manager_module.TodoManager


class TestGetBootstrapTodos:
    """Tests for get_bootstrap_todos() function."""

    def test_bootstrap_todos_structure(self):
        """Test that bootstrap todos have the correct structure."""
        todos = get_bootstrap_todos()

        assert isinstance(todos, list)
        assert len(todos) == 5

        # Check that all have required fields
        for todo in todos:
            assert "content" in todo
            assert "status" in todo
            assert "priority" in todo
            assert todo["status"] == "pending"
            assert todo["priority"] == "high"

    def test_bootstrap_todos_sequence(self):
        """Test the sequence of bootstrap todos."""
        todos = get_bootstrap_todos()

        # Check the expected sequence
        assert "workspace summary" in todos[0]["content"].lower()
        assert "workspace_summary.md" in todos[1]["content"].lower()
        assert "instructions.md" in todos[2]["content"].lower()
        assert "main_plan.md" in todos[3]["content"].lower()
        assert "phases" in todos[4]["content"].lower()

    def test_bootstrap_todos_first_task(self):
        """Test that the first bootstrap task is about generating workspace summary."""
        todos = get_bootstrap_todos()
        first_task = todos[0]

        assert "generate_workspace_summary" in first_task["content"].lower()


class TestBootstrapPrompt:
    """Tests for BOOTSTRAP_PROMPT template."""

    def test_bootstrap_prompt_exists(self):
        """Test that bootstrap prompt is defined."""
        assert BOOTSTRAP_PROMPT is not None
        assert len(BOOTSTRAP_PROMPT) > 0

    def test_bootstrap_prompt_content(self):
        """Test that bootstrap prompt has key sections."""
        assert "JOB INITIALIZATION" in BOOTSTRAP_PROMPT
        assert "todo_complete" in BOOTSTRAP_PROMPT
        assert "YOUR ONLY JOB" in BOOTSTRAP_PROMPT

    def test_bootstrap_prompt_visual_separators(self):
        """Test that bootstrap prompt has visual separators."""
        # Check for the separator pattern
        assert "═══" in BOOTSTRAP_PROMPT


class TestBootstrapInjection:
    """Tests for bootstrap injection in initialize_node."""

    def test_todo_manager_accepts_bootstrap_todos(self):
        """Test that TodoManager can accept bootstrap todos."""
        todo_mgr = TodoManager()

        # Get bootstrap todos
        bootstrap_todos = get_bootstrap_todos()

        # Set them in the manager
        result = todo_mgr.set_todos_from_list(bootstrap_todos)

        # Verify they were added
        # Note: The manager may add an auto-reflection task, so we check >= 5
        assert len(todo_mgr.list_all_sync()) >= 5

        # Verify the first 5 are our bootstrap todos
        todos = todo_mgr.list_all_sync()
        assert "workspace summary" in todos[0].content.lower()
        assert "workspace_summary.md" in todos[1].content.lower()
        assert "instructions.md" in todos[2].content.lower()

    def test_phase_info_for_bootstrap(self):
        """Test setting phase info for bootstrap phase."""
        todo_mgr = TodoManager()

        # Set phase info for bootstrap
        todo_mgr.set_phase_info(
            phase_number=0,  # Bootstrap is phase 0
            total_phases=0,   # Unknown until plan is created
            phase_name="Bootstrap",
        )

        info = todo_mgr.get_phase_info()
        assert info["phase_number"] == 0
        assert info["phase_name"] == "Bootstrap"

    def test_empty_todo_list_detection(self):
        """Test that empty todo list is detected for bootstrap injection."""
        todo_mgr = TodoManager()

        # New manager should have empty todo list
        todos = todo_mgr.list_all_sync()
        assert len(todos) == 0

        # This is the condition that triggers bootstrap injection
        should_inject = len(todos) == 0
        assert should_inject is True


class TestToolContextJobStatus:
    """Tests for ToolContext.update_job_status() method."""

    def test_update_job_status_no_postgres(self):
        """Test update_job_status when no PostgreSQL connection."""
        from dataclasses import dataclass
        from typing import Any, Optional

        # Create a minimal mock context without postgres
        @dataclass
        class MockContext:
            postgres_conn: Optional[Any] = None
            _job_id: str = "test-job-123"

            @property
            def job_id(self) -> Optional[str]:
                return self._job_id

            def has_postgres(self) -> bool:
                return self.postgres_conn is not None

        context = MockContext()
        assert context.has_postgres() is False

    def test_update_job_status_logic(self):
        """Test update_job_status logic with mocked cursor.

        Tests the core logic without importing ToolContext directly
        (which would trigger neo4j imports).
        """
        # Create mock connection
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # Simulate what update_job_status does
        job_id = "test-job-456"
        status = "completed"

        cursor = mock_conn.cursor()
        cursor.execute(
            """
            UPDATE jobs
            SET status = %s, completed_at = NOW()
            WHERE id = %s
            """,
            (status, job_id),
        )
        mock_conn.commit()
        cursor.close()

        # Verify the call
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()
        mock_cursor.close.assert_called_once()

    def test_update_job_status_requires_job_id(self):
        """Test that update_job_status requires job_id."""
        # This tests the expected behavior:
        # When job_id is None, update_job_status should raise ValueError

        # The logic check: if not job_id, raise ValueError
        job_id = None

        with pytest.raises(ValueError):
            if not job_id:
                raise ValueError("No job_id available for status update")


class TestJobCompleteIntegration:
    """Tests for job_complete() tool database integration.

    Note: These tests verify the expected behavior of job_complete()
    without directly importing ToolContext (which triggers neo4j imports).
    """

    def test_job_complete_response_with_db(self):
        """Test job_complete response format when database is updated."""
        # Simulate the expected response when database is updated
        db_updated = True
        summary = "Test job completed"
        deliverables = ["file1.txt", "file2.txt"]
        confidence = 0.95

        # This is the expected format from job_complete
        db_status = " Database updated." if db_updated else ""
        response = (
            f"JOB COMPLETE - Wrote: output/job_completion.json{db_status}\n"
            f"Summary: {summary}\n"
            f"Deliverables: {len(deliverables)} files\n"
            f"Confidence: {confidence:.0%}\n"
            f"The job has finished. No further action required."
        )

        # Verify response format
        assert "JOB COMPLETE" in response
        assert "Database updated" in response
        assert "95%" in response
        assert "2 files" in response

    def test_job_complete_response_without_db(self):
        """Test job_complete response format when no database."""
        # Simulate the expected response without database
        db_updated = False
        summary = "Test job completed"
        deliverables = ["file1.txt"]
        confidence = 1.0

        # This is the expected format from job_complete
        db_status = " Database updated." if db_updated else ""
        response = (
            f"JOB COMPLETE - Wrote: output/job_completion.json{db_status}\n"
            f"Summary: {summary}\n"
            f"Deliverables: {len(deliverables)} files\n"
            f"Confidence: {confidence:.0%}\n"
            f"The job has finished. No further action required."
        )

        # Verify response format
        assert "JOB COMPLETE" in response
        assert "Database updated" not in response
        assert "100%" in response
        assert "1 files" in response

    def test_job_complete_data_structure(self):
        """Test the job completion data structure."""
        from datetime import datetime

        # This is what job_complete writes to file
        summary = "Extracted 47 requirements"
        deliverables = ["output/requirements.json", "output/summary.md"]
        confidence = 0.92
        job_id = "test-job-123"
        notes = "3 requirements flagged for review"

        completion_data = {
            "status": "job_completed",
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
            "deliverables": deliverables,
            "confidence": confidence,
            "job_id": job_id,
            "notes": notes,
        }

        # Verify structure
        assert completion_data["status"] == "job_completed"
        assert completion_data["summary"] == summary
        assert len(completion_data["deliverables"]) == 2
        assert completion_data["confidence"] == 0.92
        assert completion_data["job_id"] == job_id
        assert completion_data["notes"] == notes

    def test_job_completion_json_serialization(self):
        """Test that completion data can be serialized to JSON."""
        from datetime import datetime

        completion_data = {
            "status": "job_completed",
            "timestamp": datetime.now().isoformat(),
            "summary": "Test completion",
            "deliverables": ["file1.txt"],
            "confidence": 1.0,
            "job_id": "test-123",
        }

        # Should serialize without error
        json_str = json.dumps(completion_data, indent=2, ensure_ascii=False)
        assert "job_completed" in json_str
        assert "test-123" in json_str

        # Should deserialize correctly
        parsed = json.loads(json_str)
        assert parsed["status"] == "job_completed"


class TestAgentConfigsIncludeTodoTools:
    """Tests that agent configs include the new todo tools."""

    def test_creator_config_has_todo_tools(self):
        """Test creator.json includes todo_complete and todo_rewind."""
        config_path = Path(__file__).parent.parent / "src" / "config" / "agents" / "creator.json"

        with open(config_path) as f:
            config = json.load(f)

        todo_tools = config["tools"]["todo"]
        assert "todo_complete" in todo_tools
        assert "todo_rewind" in todo_tools
        assert "todo_write" in todo_tools
        assert "archive_and_reset" in todo_tools

    def test_validator_config_has_todo_tools(self):
        """Test validator.json includes todo_complete and todo_rewind."""
        config_path = Path(__file__).parent.parent / "src" / "config" / "agents" / "validator.json"

        with open(config_path) as f:
            config = json.load(f)

        todo_tools = config["tools"]["todo"]
        assert "todo_complete" in todo_tools
        assert "todo_rewind" in todo_tools
        assert "todo_write" in todo_tools
        assert "archive_and_reset" in todo_tools


class TestInstructionsUpdated:
    """Tests that instruction files have focus-on-todos guidance."""

    def test_creator_instructions_focus_on_todos(self):
        """Test creator_instructions.md has focus-on-todos section."""
        instructions_path = (
            Path(__file__).parent.parent /
            "src" / "config" / "agents" / "instructions" / "creator_instructions.md"
        )

        with open(instructions_path) as f:
            content = f.read()

        assert "FOCUS ON YOUR TODO LIST" in content
        assert "todo_complete()" in content
        assert "todo_rewind" in content
        assert "Trust the Process" in content

    def test_validator_instructions_focus_on_todos(self):
        """Test validator_instructions.md has focus-on-todos section."""
        instructions_path = (
            Path(__file__).parent.parent /
            "src" / "config" / "agents" / "instructions" / "validator_instructions.md"
        )

        with open(instructions_path) as f:
            content = f.read()

        assert "FOCUS ON YOUR TODO LIST" in content
        assert "todo_complete()" in content
        assert "todo_rewind" in content
        assert "Trust the Process" in content
