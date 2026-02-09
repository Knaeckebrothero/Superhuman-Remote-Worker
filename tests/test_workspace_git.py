"""Unit tests for workspace git initialization.

Tests that WorkspaceManager correctly initializes git repositories
when git_versioning is enabled.
"""

import pytest
import shutil
import tempfile
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig  # noqa: E402


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


class TestWorkspaceGitInitialization:
    """Tests for workspace git initialization."""

    def test_git_manager_created_when_enabled(self, temp_base):
        """Test that git_manager is created when git_versioning is enabled."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        assert ws.git_manager is not None
        assert ws.git_manager.is_active is True

    def test_git_manager_none_when_disabled(self, temp_base):
        """Test that git_manager is None when git_versioning is disabled."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=False,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        assert ws.git_manager is None

    def test_git_directory_created(self, temp_base):
        """Test that .git directory is created."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        git_dir = ws.path / ".git"
        assert git_dir.exists()

    def test_gitignore_created(self, temp_base):
        """Test that .gitignore is created with configured patterns."""
        patterns = ["*.log", "*.tmp", "secret/"]
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                git_ignore_patterns=patterns,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        gitignore = ws.path / ".gitignore"
        assert gitignore.exists()

        content = gitignore.read_text()
        for pattern in patterns:
            assert pattern in content

    def test_initial_commit_created(self, temp_base):
        """Test that initial commit is created."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        log = ws.git_manager.log()
        assert "Initialize workspace" in log

    def test_reinitialize_preserves_git(self, temp_base):
        """Test that reinitializing workspace preserves git history."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        # Make a commit
        (ws.path / "test.txt").write_text("test content")
        ws.git_manager.commit("Add test file")

        # Reinitialize
        ws.initialize()

        # Git history should be preserved
        log = ws.git_manager.log()
        assert "Add test file" in log

class TestWorkspaceGitConfig:
    """Tests for workspace git configuration."""

    def test_default_git_versioning_enabled(self, temp_base):
        """Test that git_versioning is enabled by default."""
        config = WorkspaceManagerConfig()
        assert config.git_versioning is True

    def test_default_ignore_patterns(self, temp_base):
        """Test default git ignore patterns."""
        config = WorkspaceManagerConfig()
        assert "*.db" in config.git_ignore_patterns
        assert "*.log" in config.git_ignore_patterns
        assert "__pycache__/" in config.git_ignore_patterns

    def test_from_dict_with_git_config(self, temp_base):
        """Test creating config from dict with git settings."""
        data = {
            "structure": ["archive/"],
            "git_versioning": True,
            "git_ignore_patterns": ["*.custom", "build/"],
        }
        config = WorkspaceManagerConfig.from_dict(data)

        assert config.git_versioning is True
        assert "*.custom" in config.git_ignore_patterns
        assert "build/" in config.git_ignore_patterns

    def test_from_dict_git_disabled(self, temp_base):
        """Test creating config from dict with git disabled."""
        data = {
            "structure": ["archive/"],
            "git_versioning": False,
        }
        config = WorkspaceManagerConfig.from_dict(data)

        assert config.git_versioning is False


class TestWorkspaceGitOperations:
    """Tests for git operations via workspace."""

    def test_commit_via_git_manager(self, temp_base):
        """Test committing changes via git_manager."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        # Write a file via workspace
        ws.write_file("notes.md", "# Notes\n\nSome notes here.")

        # Commit via git_manager
        ws.git_manager.commit("Add notes file")

        # Verify commit
        log = ws.git_manager.log()
        assert "Add notes file" in log

    def test_tag_via_git_manager(self, temp_base):
        """Test creating tags via git_manager."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        # Create a phase tag
        ws.git_manager.tag("phase-1-strategic-complete")

        # Verify tag
        tags = ws.git_manager.list_tags("phase-*")
        assert "phase-1-strategic-complete" in tags

    def test_diff_uncommitted_changes(self, temp_base):
        """Test viewing uncommitted changes."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        # Commit initial state and add a tracked file
        ws.write_file("test_tracked.txt", "initial content")
        ws.git_manager.commit("Initial state")
        assert ws.git_manager.has_uncommitted_changes() is False

        # Make a change to an existing tracked file
        ws.write_file("test_tracked.txt", "modified content")

        # Now dirty
        assert ws.git_manager.has_uncommitted_changes() is True

        # Diff shows changes (for tracked files)
        diff = ws.git_manager.diff()
        assert "test_tracked.txt" in diff


class TestWorkspaceGitGracefulDegradation:
    """Tests for graceful degradation when git is unavailable."""

    def test_workspace_works_without_git(self, temp_base, monkeypatch):
        """Test that workspace works even if git initialization fails."""
        # Simulate git not being available
        monkeypatch.setattr(shutil, "which", lambda x: None if x == "git" else shutil.which(x))

        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
        )
        ws.initialize()

        # Workspace should still be initialized
        assert ws.is_initialized
        assert ws.path.exists()

        # Git manager should be None
        assert ws.git_manager is None

        # Can still write files
        ws.write_file("test.txt", "content")
        assert ws.read_file("test.txt") == "content"
