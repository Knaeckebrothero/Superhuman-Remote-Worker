"""Unit tests for phase transition git operations.

Tests that phase transitions correctly update phase_state.yaml,
create git tags, and commit changes.
"""

import pytest
import shutil
import tempfile
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from src.core.phase import (
    _complete_phase_with_git,
    on_strategic_phase_complete,
    on_tactical_phase_complete,
)
from src.managers.todo import TodoManager


def git_available():
    """Check if git is available on the system."""
    return shutil.which("git") is not None


# Skip all tests if git is not available
pytestmark = pytest.mark.skipif(
    not git_available(),
    reason="Git not available on system"
)


@pytest.fixture
def temp_base():
    """Create a temporary directory for workspace base path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_with_git(temp_base):
    """Create a workspace with git enabled."""
    ws = WorkspaceManager(
        job_id="test-job",
        config=WorkspaceManagerConfig(
            structure=["archive/", "output/"],
            git_versioning=True,
        ),
        base_path=temp_base,
    )
    ws.initialize()
    # Commit initial state to have a clean baseline
    ws.git_manager.commit("Initial state")
    return ws


@pytest.fixture
def todo_manager(workspace_with_git):
    """Create a TodoManager with workspace."""
    return TodoManager(workspace=workspace_with_git)


class TestCompletePhaseWithGit:
    """Tests for the _complete_phase_with_git helper function."""

    def test_creates_tag_for_strategic_phase(self, workspace_with_git):
        """Test that git tag is created for completed strategic phase."""
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=1,
            phase_type="strategic",
            next_phase_type="tactical",
            next_phase_name="Execution",
            todos_archived=4,
        )

        tags = workspace_with_git.git_manager.list_tags("phase-*")
        assert "phase-1-strategic-complete" in tags

    def test_creates_tag_for_tactical_phase(self, workspace_with_git):
        """Test that git tag is created for completed tactical phase."""
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=1,
            phase_type="tactical",
            next_phase_type="strategic",
            next_phase_name="",
            todos_archived=10,
        )

        tags = workspace_with_git.git_manager.list_tags("phase-*")
        assert "phase-1-tactical-complete" in tags

    def test_updates_phase_state_yaml_strategic_to_tactical(self, workspace_with_git):
        """Test that phase_state.yaml is updated when going strategic -> tactical."""
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=1,
            phase_type="strategic",
            next_phase_type="tactical",
            next_phase_name="Extraction Phase",
            todos_archived=4,
        )

        content = workspace_with_git.read_file("phase_state.yaml")
        assert "phase_number: 2" in content  # Sequential: always increments
        assert "phase_type: tactical" in content
        assert "Extraction Phase" in content

    def test_updates_phase_state_yaml_tactical_to_strategic(self, workspace_with_git):
        """Test that phase_state.yaml is updated when going tactical -> strategic."""
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=1,
            phase_type="tactical",
            next_phase_type="strategic",
            next_phase_name="",
            todos_archived=10,
        )

        content = workspace_with_git.read_file("phase_state.yaml")
        assert "phase_number: 2" in content  # Incremented
        assert "phase_type: strategic" in content

    def test_creates_commit_with_phase_info(self, workspace_with_git):
        """Test that a commit is created with phase information."""
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=2,
            phase_type="tactical",
            next_phase_type="strategic",
            next_phase_name="",
            todos_archived=8,
        )

        log = workspace_with_git.git_manager.log(max_count=1, oneline=True)
        assert "[Phase 2 Tactical]" in log
        assert "Complete" in log
        assert "8 todos" in log

    def test_handles_missing_git_manager(self, workspace_with_git):
        """Test that function handles workspace without git gracefully."""
        workspace_with_git._git_manager = None

        # Should not raise an error
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=1,
            phase_type="strategic",
            next_phase_type="tactical",
            next_phase_name="",
            todos_archived=4,
        )

    def test_handles_inactive_git_manager(self, workspace_with_git):
        """Test that function handles inactive git manager gracefully."""
        mock_git = MagicMock()
        mock_git.is_active = False
        workspace_with_git._git_manager = mock_git

        # Should not call any git operations
        _complete_phase_with_git(
            workspace=workspace_with_git,
            phase_number=1,
            phase_type="strategic",
            next_phase_type="tactical",
            next_phase_name="",
            todos_archived=4,
        )

        mock_git.tag.assert_not_called()
        mock_git.commit.assert_not_called()


class TestOnStrategicPhaseComplete:
    """Tests for on_strategic_phase_complete with git integration."""

    def test_creates_git_tag_on_transition(self, workspace_with_git, todo_manager):
        """Test that strategic phase completion creates git tag."""
        # Stage some todos (minimum 5 required)
        todo_manager.stage_tactical_todos(
            [
                "First task to complete with enough content",
                "Second task to complete with enough content",
                "Third task to complete with enough content",
                "Fourth task to complete with enough content",
                "Fifth task to complete with enough content",
            ],
            phase_name="Test Phase",
        )

        state = {
            "job_id": "test-job",
            "phase_number": 1,
            "is_strategic_phase": True,
        }

        result = on_strategic_phase_complete(
            state=state,
            workspace=workspace_with_git,
            todo_manager=todo_manager,
            min_todos=5,
            max_todos=20,
        )

        assert result.success is True
        tags = workspace_with_git.git_manager.list_tags("phase-*")
        assert "phase-1-strategic-complete" in tags

    def test_updates_phase_state_on_transition(self, workspace_with_git, todo_manager):
        """Test that phase_state.yaml is updated on transition."""
        # Stage some todos (minimum 5 required)
        todo_manager.stage_tactical_todos(
            [
                "First task to complete with enough content",
                "Second task to complete with enough content",
                "Third task to complete with enough content",
                "Fourth task to complete with enough content",
                "Fifth task to complete with enough content",
            ],
            phase_name="Execution Phase",
        )

        state = {
            "job_id": "test-job",
            "phase_number": 1,
            "is_strategic_phase": True,
        }

        on_strategic_phase_complete(
            state=state,
            workspace=workspace_with_git,
            todo_manager=todo_manager,
        )

        content = workspace_with_git.read_file("phase_state.yaml")
        assert "phase_type: tactical" in content
        assert "Execution Phase" in content


class TestOnTacticalPhaseComplete:
    """Tests for on_tactical_phase_complete with git integration."""

    def test_creates_git_tag_on_transition(self, workspace_with_git, todo_manager):
        """Test that tactical phase completion creates git tag."""
        # Set up some completed todos
        todo_manager.add("First task completed")
        todo_manager.complete("todo_1")

        state = {
            "job_id": "test-job",
            "phase_number": 1,
            "is_strategic_phase": False,
        }

        result = on_tactical_phase_complete(
            state=state,
            workspace=workspace_with_git,
            todo_manager=todo_manager,
        )

        assert result.success is True
        tags = workspace_with_git.git_manager.list_tags("phase-*")
        assert "phase-1-tactical-complete" in tags

    def test_updates_phase_state_on_transition(self, workspace_with_git, todo_manager):
        """Test that phase_state.yaml is updated on transition."""
        state = {
            "job_id": "test-job",
            "phase_number": 1,
            "is_strategic_phase": False,
        }

        on_tactical_phase_complete(
            state=state,
            workspace=workspace_with_git,
            todo_manager=todo_manager,
        )

        content = workspace_with_git.read_file("phase_state.yaml")
        assert "phase_number: 2" in content  # Incremented
        assert "phase_type: strategic" in content

    def test_commits_phase_completion(self, workspace_with_git, todo_manager):
        """Test that phase completion creates a commit."""
        # Set up completed todos to get an accurate count
        todo_manager.add("Task one")
        todo_manager.add("Task two")
        todo_manager.complete("todo_1")
        todo_manager.complete("todo_2")

        state = {
            "job_id": "test-job",
            "phase_number": 1,
            "is_strategic_phase": False,
        }

        on_tactical_phase_complete(
            state=state,
            workspace=workspace_with_git,
            todo_manager=todo_manager,
        )

        log = workspace_with_git.git_manager.log(max_count=1, oneline=True)
        assert "[Phase 1 Tactical]" in log
        assert "Complete" in log


class TestPhaseStateYamlUpdate:
    """Tests for WorkspaceManager.update_phase_state()."""

    def test_update_phase_state_basic(self, workspace_with_git):
        """Test basic phase state update."""
        workspace_with_git.update_phase_state(
            phase_number=3,
            phase_type="tactical",
            phase_name="Data Extraction",
        )

        content = workspace_with_git.read_file("phase_state.yaml")
        assert "phase_number: 3" in content
        assert "phase_type: tactical" in content
        assert "Data Extraction" in content
        assert "started_at:" in content

    def test_update_phase_state_empty_name(self, workspace_with_git):
        """Test phase state update with empty name."""
        workspace_with_git.update_phase_state(
            phase_number=2,
            phase_type="strategic",
            phase_name="",
        )

        content = workspace_with_git.read_file("phase_state.yaml")
        assert "phase_number: 2" in content
        assert "phase_type: strategic" in content
        assert 'phase_name: ""' in content
