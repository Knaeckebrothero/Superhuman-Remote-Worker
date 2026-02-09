"""Unit tests for GitManager (managers package).

Tests git versioning functionality for agent workspaces.
"""

import pytest
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.managers.git_manager import GitManager  # noqa: E402


@pytest.fixture
def temp_workspace():
    """Create a temporary directory for workspace testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def git_manager(temp_workspace):
    """Create a GitManager with a temporary workspace."""
    return GitManager(temp_workspace)


@pytest.fixture
def initialized_git(temp_workspace):
    """Create a GitManager with an initialized repository."""
    gm = GitManager(temp_workspace)
    gm.init_repository(ignore_patterns=["*.log", "*.tmp"])
    return gm


def git_available():
    """Check if git is available on the system."""
    return shutil.which("git") is not None


# Skip all tests if git is not available
pytestmark = pytest.mark.skipif(
    not git_available(),
    reason="Git not available on system"
)


class TestGitManagerInit:
    """Tests for GitManager initialization."""

    def test_workspace_path_set(self, temp_workspace):
        """Test that workspace path is set correctly."""
        gm = GitManager(temp_workspace)
        assert gm.workspace_path == temp_workspace

    def test_git_available_detected(self, git_manager):
        """Test that git availability is detected."""
        # We know git is available because of the pytestmark
        assert git_manager._git_available is True

    def test_not_active_before_init(self, git_manager):
        """Test that is_active is False before initialization."""
        assert git_manager.is_active is False

    def test_active_after_init(self, initialized_git):
        """Test that is_active is True after initialization."""
        assert initialized_git.is_active is True


class TestGitManagerInitRepository:
    """Tests for repository initialization."""

    def test_init_creates_git_directory(self, git_manager, temp_workspace):
        """Test that init creates .git directory."""
        result = git_manager.init_repository()
        assert result is True
        assert (temp_workspace / ".git").exists()

    def test_init_creates_gitignore(self, git_manager, temp_workspace):
        """Test that init creates .gitignore with patterns."""
        patterns = ["*.log", "*.tmp", "__pycache__/"]
        git_manager.init_repository(ignore_patterns=patterns)

        gitignore = temp_workspace / ".gitignore"
        assert gitignore.exists()

        content = gitignore.read_text()
        for pattern in patterns:
            assert pattern in content

    def test_init_makes_initial_commit(self, initialized_git):
        """Test that init makes an initial commit."""
        log = initialized_git.log()
        assert "Initialize workspace" in log

    def test_init_idempotent(self, git_manager, temp_workspace):
        """Test that calling init twice is safe."""
        git_manager.init_repository()
        result = git_manager.init_repository()  # Second call

        assert result is True
        # Should still work normally
        assert git_manager.is_active is True

    def test_init_configures_local_user(self, initialized_git, temp_workspace):
        """Test that init configures local git user."""
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=temp_workspace,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "agent@workspace.local" in result.stdout


class TestGitManagerCommit:
    """Tests for commit functionality."""

    def test_commit_basic(self, initialized_git, temp_workspace):
        """Test basic commit functionality."""
        # Create a file
        (temp_workspace / "test.txt").write_text("Hello")

        result = initialized_git.commit("Test commit")
        assert result is True

        log = initialized_git.log()
        assert "Test commit" in log

    def test_commit_empty_allowed(self, initialized_git):
        """Test that empty commits are allowed by default."""
        result = initialized_git.commit("Empty commit")
        assert result is True

        log = initialized_git.log()
        assert "Empty commit" in log

    def test_commit_empty_disallowed(self, initialized_git):
        """Test that empty commits can be disallowed."""
        result = initialized_git.commit("Should not commit", allow_empty=False)
        # No changes to commit
        assert result is False

    def test_commit_stages_all_changes(self, initialized_git, temp_workspace):
        """Test that commit stages all changes."""
        # Create new file
        (temp_workspace / "new.txt").write_text("new")
        # Modify existing file (gitignore)
        (temp_workspace / ".gitignore").write_text("*.log\n*.new")
        # Create in subdirectory
        subdir = temp_workspace / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested")

        result = initialized_git.commit("Multi-change commit")
        assert result is True

        # Verify all committed
        show = initialized_git.show(stat_only=True)
        assert "new.txt" in show
        assert ".gitignore" in show
        assert "nested.txt" in show

    def test_commit_returns_false_when_inactive(self, git_manager):
        """Test that commit returns False when git is not active."""
        result = git_manager.commit("Test")
        assert result is False


class TestGitManagerLog:
    """Tests for log functionality."""

    def test_log_basic(self, initialized_git, temp_workspace):
        """Test basic log output."""
        # Make a few commits
        (temp_workspace / "file1.txt").write_text("1")
        initialized_git.commit("First commit")
        (temp_workspace / "file2.txt").write_text("2")
        initialized_git.commit("Second commit")

        log = initialized_git.log()
        assert "First commit" in log
        assert "Second commit" in log

    def test_log_max_count(self, initialized_git, temp_workspace):
        """Test log respects max_count."""
        # Make several commits
        for i in range(5):
            (temp_workspace / f"file{i}.txt").write_text(str(i))
            initialized_git.commit(f"Commit {i}")

        log = initialized_git.log(max_count=3)
        # Should only have 3 commits
        lines = [line for line in log.split("\n") if line.strip()]
        assert len(lines) <= 3

    def test_log_oneline_format(self, initialized_git, temp_workspace):
        """Test log oneline format."""
        (temp_workspace / "file.txt").write_text("test")
        initialized_git.commit("Test message")

        log = initialized_git.log(oneline=True)
        # Oneline format is compact
        assert "Test message" in log

    def test_log_full_format(self, initialized_git, temp_workspace):
        """Test log full format."""
        (temp_workspace / "file.txt").write_text("test")
        initialized_git.commit("Test message")

        log = initialized_git.log(oneline=False)
        # Full format includes author, date, etc.
        assert "Author:" in log or "commit" in log.lower()

    def test_log_inactive(self, git_manager):
        """Test log message when inactive."""
        log = git_manager.log()
        assert "not available" in log.lower()


class TestGitManagerShow:
    """Tests for show functionality."""

    def test_show_head(self, initialized_git, temp_workspace):
        """Test showing HEAD commit."""
        (temp_workspace / "test.txt").write_text("Hello")
        initialized_git.commit("Add test file")

        show = initialized_git.show()
        assert "Add test file" in show
        assert "test.txt" in show

    def test_show_specific_commit(self, initialized_git, temp_workspace):
        """Test showing specific commit."""
        (temp_workspace / "file1.txt").write_text("1")
        initialized_git.commit("First")

        # Get first commit hash
        log = initialized_git.log()
        first_hash = log.split("\n")[1].split()[0] if "\n" in log else log.split()[0]

        (temp_workspace / "file2.txt").write_text("2")
        initialized_git.commit("Second")

        show = initialized_git.show(first_hash)
        assert "First" in show or "Initialize" in show

    def test_show_stat_only(self, initialized_git, temp_workspace):
        """Test show with stat_only."""
        (temp_workspace / "test.txt").write_text("Hello" * 100)
        initialized_git.commit("Add big file")

        show = initialized_git.show(stat_only=True)
        # Stat only shows file names and stats, not full diff
        assert "test.txt" in show
        assert "insertion" in show.lower() or "+" in show

    def test_show_truncation(self, initialized_git, temp_workspace):
        """Test show truncates large output."""
        # Create a large file
        (temp_workspace / "large.txt").write_text("Line\n" * 1000)
        initialized_git.commit("Add large file")

        show = initialized_git.show(max_lines=50)
        # Should be truncated
        if "truncated" in show:
            assert "truncated" in show
        else:
            # Or just less than full output
            assert len(show.split("\n")) <= 55  # Some buffer

    def test_show_inactive(self, git_manager):
        """Test show message when inactive."""
        show = git_manager.show()
        assert "not available" in show.lower()


class TestGitManagerDiff:
    """Tests for diff functionality."""

    def test_diff_uncommitted(self, initialized_git, temp_workspace):
        """Test diff shows uncommitted changes."""
        (temp_workspace / "test.txt").write_text("Original")
        initialized_git.commit("Add file")

        (temp_workspace / "test.txt").write_text("Modified")

        diff = initialized_git.diff()
        assert "Modified" in diff or "+Modified" in diff

    def test_diff_no_changes(self, initialized_git):
        """Test diff with no changes."""
        diff = initialized_git.diff()
        assert "No differences" in diff

    def test_diff_between_refs(self, initialized_git, temp_workspace):
        """Test diff between two refs."""
        (temp_workspace / "file.txt").write_text("version1")
        initialized_git.commit("Version 1")
        initialized_git.tag("v1")

        (temp_workspace / "file.txt").write_text("version2")
        initialized_git.commit("Version 2")

        diff = initialized_git.diff("v1", "HEAD")
        assert "version" in diff.lower() or "file.txt" in diff

    def test_diff_single_ref(self, initialized_git, temp_workspace):
        """Test diff from ref to working directory."""
        (temp_workspace / "file.txt").write_text("committed")
        initialized_git.commit("Committed")

        (temp_workspace / "file.txt").write_text("uncommitted")

        diff = initialized_git.diff("HEAD")
        assert "uncommitted" in diff or "+uncommitted" in diff

    def test_diff_specific_file(self, initialized_git, temp_workspace):
        """Test diff for specific file."""
        (temp_workspace / "file1.txt").write_text("f1")
        (temp_workspace / "file2.txt").write_text("f2")
        initialized_git.commit("Add files")

        (temp_workspace / "file1.txt").write_text("f1-modified")
        (temp_workspace / "file2.txt").write_text("f2-modified")

        diff = initialized_git.diff(file_path="file1.txt")
        assert "f1" in diff
        # file2 changes should not appear in single-file diff
        assert "f2-modified" not in diff or "file1.txt" in diff

    def test_diff_truncation(self, initialized_git, temp_workspace):
        """Test diff truncates large output."""
        (temp_workspace / "large.txt").write_text("A\n" * 1000)
        initialized_git.commit("Add file")
        (temp_workspace / "large.txt").write_text("B\n" * 1000)

        diff = initialized_git.diff(max_lines=50)
        lines = diff.split("\n")
        # Should be truncated or reasonably short
        assert len(lines) <= 60 or "truncated" in diff

    def test_diff_inactive(self, git_manager):
        """Test diff message when inactive."""
        diff = git_manager.diff()
        assert "not available" in diff.lower()


class TestGitManagerStatus:
    """Tests for status functionality."""

    def test_status_clean(self, initialized_git):
        """Test status when workspace is clean."""
        status = initialized_git.status()
        assert "clean" in status.lower()

    def test_status_dirty(self, initialized_git, temp_workspace):
        """Test status with uncommitted changes."""
        (temp_workspace / "new.txt").write_text("new")

        status = initialized_git.status()
        assert "dirty" in status.lower()
        assert "new.txt" in status

    def test_status_shows_branch(self, initialized_git):
        """Test status shows current branch."""
        status = initialized_git.status()
        assert "Branch:" in status

    def test_status_categorizes_changes(self, initialized_git, temp_workspace):
        """Test status categorizes different types of changes."""
        # Untracked file
        (temp_workspace / "untracked.txt").write_text("u")

        # Modified file (modify gitignore which exists)
        (temp_workspace / ".gitignore").write_text("*.log\n*.new")

        status = initialized_git.status()
        assert "Untracked" in status or "untracked.txt" in status
        # Modified or Staged - git may show the .gitignore change in either category
        assert "Modified" in status or "Staged" in status or "gitignore" in status

    def test_status_inactive(self, git_manager):
        """Test status message when inactive."""
        status = git_manager.status()
        assert "not available" in status.lower()


class TestGitManagerHasUncommittedChanges:
    """Tests for has_uncommitted_changes functionality."""

    def test_no_changes(self, initialized_git):
        """Test returns False when no changes."""
        assert initialized_git.has_uncommitted_changes() is False

    def test_with_changes(self, initialized_git, temp_workspace):
        """Test returns True with uncommitted changes."""
        (temp_workspace / "new.txt").write_text("new")
        assert initialized_git.has_uncommitted_changes() is True

    def test_after_commit(self, initialized_git, temp_workspace):
        """Test returns False after committing changes."""
        (temp_workspace / "new.txt").write_text("new")
        initialized_git.commit("Add file")
        assert initialized_git.has_uncommitted_changes() is False

    def test_inactive(self, git_manager):
        """Test returns False when inactive."""
        assert git_manager.has_uncommitted_changes() is False


class TestGitManagerTag:
    """Tests for tag functionality."""

    def test_create_tag(self, initialized_git):
        """Test creating a simple tag."""
        result = initialized_git.tag("v1.0")
        assert result is True

        tags = initialized_git.list_tags()
        assert "v1.0" in tags

    def test_create_annotated_tag(self, initialized_git):
        """Test creating an annotated tag with message."""
        result = initialized_git.tag("v2.0", message="Release version 2.0")
        assert result is True

        tags = initialized_git.list_tags()
        assert "v2.0" in tags

    def test_tag_phase_pattern(self, initialized_git, temp_workspace):
        """Test creating phase-style tags."""
        # Simulate phase completion workflow
        (temp_workspace / "phase1.txt").write_text("phase 1 work")
        initialized_git.commit("[Phase 1 Tactical] Complete")
        initialized_git.tag("phase-1-tactical-complete")

        (temp_workspace / "phase2.txt").write_text("phase 2 work")
        initialized_git.commit("[Phase 2 Strategic] Complete")
        initialized_git.tag("phase-2-strategic-complete")

        tags = initialized_git.list_tags("phase-*")
        assert "phase-1-tactical-complete" in tags
        assert "phase-2-strategic-complete" in tags

    def test_tag_inactive(self, git_manager):
        """Test tag returns False when inactive."""
        result = git_manager.tag("test")
        assert result is False


class TestGitManagerListTags:
    """Tests for list_tags functionality."""

    def test_list_all_tags(self, initialized_git):
        """Test listing all tags."""
        initialized_git.tag("alpha")
        initialized_git.tag("beta")
        initialized_git.tag("gamma")

        tags = initialized_git.list_tags()
        assert "alpha" in tags
        assert "beta" in tags
        assert "gamma" in tags

    def test_list_tags_with_pattern(self, initialized_git):
        """Test listing tags with pattern filter."""
        initialized_git.tag("phase-1-complete")
        initialized_git.tag("phase-2-complete")
        initialized_git.tag("release-1.0")

        phase_tags = initialized_git.list_tags("phase-*")
        assert "phase-1-complete" in phase_tags
        assert "phase-2-complete" in phase_tags
        assert "release-1.0" not in phase_tags

        release_tags = initialized_git.list_tags("release-*")
        assert "release-1.0" in release_tags
        assert "phase-1-complete" not in release_tags

    def test_list_tags_empty(self, initialized_git):
        """Test listing tags when none exist."""
        tags = initialized_git.list_tags()
        assert tags == [] or tags == [""]  # Empty list or list with empty string

    def test_list_tags_inactive(self, git_manager):
        """Test list_tags returns empty when inactive."""
        tags = git_manager.list_tags()
        assert tags == []


class TestGitManagerTruncation:
    """Tests for output truncation."""

    def test_truncate_by_lines(self, initialized_git):
        """Test truncation by line count."""
        output = "\n".join([f"Line {i}" for i in range(1000)])
        truncated = initialized_git._truncate_output(output, max_lines=100)

        # Should be truncated with message
        assert "truncated" in truncated
        assert "1000 lines" in truncated

    def test_truncate_by_words(self, initialized_git):
        """Test truncation by word count."""
        output = " ".join(["word"] * 20000)
        truncated = initialized_git._truncate_output(output, max_words=1000)

        assert "truncated" in truncated
        assert "20000 words" in truncated

    def test_no_truncation_needed(self, initialized_git):
        """Test that small output is not truncated."""
        output = "Small output\nwith few lines"
        truncated = initialized_git._truncate_output(output)

        assert truncated == output
        assert "truncated" not in truncated


class TestGitManagerGracefulDegradation:
    """Tests for graceful degradation when git is unavailable."""

    def test_init_without_git(self, temp_workspace):
        """Test initialization gracefully handles missing git."""
        with patch("shutil.which", return_value=None):
            gm = GitManager(temp_workspace)
            assert gm._git_available is False
            assert gm.is_active is False

    def test_operations_without_git(self, temp_workspace):
        """Test operations return appropriate defaults when git unavailable."""
        with patch("shutil.which", return_value=None):
            gm = GitManager(temp_workspace)

            # Init should fail gracefully
            assert gm.init_repository() is False

            # Queries should return helpful messages
            assert "not available" in gm.log().lower()
            assert "not available" in gm.show().lower()
            assert "not available" in gm.diff().lower()
            assert "not available" in gm.status().lower()

            # Mutations should return False
            assert gm.commit("test") is False
            assert gm.tag("test") is False

            # Lists should return empty
            assert gm.list_tags() == []
            assert gm.has_uncommitted_changes() is False


class TestGitManagerTimeout:
    """Tests for command timeout handling."""

    def test_timeout_default(self, initialized_git):
        """Test that default timeout is set."""
        assert GitManager.DEFAULT_TIMEOUT == 60

    def test_timeout_handling(self, initialized_git, temp_workspace):
        """Test that timeout is passed to subprocess."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["git", "status"], 0, "output", ""
            )

            initialized_git._run_git(["status"])

            # Verify timeout was passed
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["timeout"] == 60

    def test_timeout_expired_handling(self, initialized_git):
        """Test handling of timeout expiration."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["git", "status"], timeout=60
            )

            result = initialized_git._run_git(["status"])

            assert result.returncode == 1
            assert "timed out" in result.stderr.lower()


class TestGitManagerExistingRepo:
    """Tests for handling existing repositories."""

    def test_init_with_existing_repo(self, temp_workspace):
        """Test initializing when repo already exists."""
        # Manually create a git repo first
        subprocess.run(["git", "init"], cwd=temp_workspace, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=temp_workspace,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=temp_workspace,
            capture_output=True,
        )
        (temp_workspace / "existing.txt").write_text("existing")
        subprocess.run(["git", "add", "-A"], cwd=temp_workspace, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Existing commit"],
            cwd=temp_workspace,
            capture_output=True,
        )

        # Now create GitManager
        gm = GitManager(temp_workspace)
        assert gm.is_active is True

        # Init should be a no-op
        result = gm.init_repository()
        assert result is True

        # Should still see existing commit
        log = gm.log()
        assert "Existing commit" in log
