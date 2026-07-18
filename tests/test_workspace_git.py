"""Unit tests for workspace git initialization.

Tests that WorkspaceManager correctly initializes git repositories
when git_versioning is enabled, and that GitManager writes sensible
default .gitignore patterns without relying on config.
"""

import pytest
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig  # noqa: E402
from src.managers.git_manager import GitManager  # noqa: E402
from tests._fs_backend import FilesystemTestBackend  # noqa: E402


def git_available():
    """Check if git is available on the system."""
    return shutil.which("git") is not None


# Skip all tests if git is not available
pytestmark = pytest.mark.skipif(
    not git_available(), reason="Git not available on system"
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
            backend=FilesystemTestBackend(temp_base),
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
            backend=FilesystemTestBackend(temp_base),
        )
        ws.initialize()

        assert ws.git_manager is None

    def test_project_jobs_clone_failure_raises_not_silent_init(
        self, temp_base, monkeypatch
    ):
        """F29 hardening: a failed jobs-repo clone must RAISE (so the job fails
        loudly and the loop advance counts it), NOT silently `git init` a
        disconnected workspace — that path rebuilt from scratch and lost every
        push on teardown in loop runs 5 & 6.
        """
        import src.core.workspace as ws_mod

        # No real backoff waits in the test.
        monkeypatch.setattr(ws_mod, "_CLONE_BACKOFF_SECONDS", (0, 0))
        # Simulate a persistently unreachable/broken clone (the F29 URL problem):
        # every attempt fails, so the bounded retry exhausts and F29 hard-fails.
        calls = {"n": 0}

        def _always_none(*a, **k):
            calls["n"] += 1
            return None

        monkeypatch.setattr(GitManager, "clone", _always_none)
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                repositories=[
                    {
                        "role": "jobs",
                        "name": "proj-jobs",
                        "repo_url": "http://srw-gitea:3000/x/proj-jobs.git",
                    }
                ],
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )
        with pytest.raises(RuntimeError, match="Failed to clone project jobs repo"):
            ws.initialize_project_workspace()
        # Retried before giving up (bounded), then hard-failed — did NOT silently
        # fall back to a local git init.
        assert calls["n"] == ws_mod._CLONE_ATTEMPTS
        assert ws.git_manager is None
        assert ws._initialized is False

    def test_project_jobs_clone_succeeds_after_transient_failure(
        self, temp_base, monkeypatch
    ):
        """A transient clone blip (e.g. a reachability miss during an image
        rollout) must not kill the job: the bounded retry recovers on a later
        attempt instead of hard-failing on the first miss.
        """
        import src.core.workspace as ws_mod

        monkeypatch.setattr(ws_mod, "_CLONE_BACKOFF_SECONDS", (0, 0))
        sentinel = object()
        calls = {"n": 0}

        def _fail_twice_then_ok(*a, **k):
            calls["n"] += 1
            return sentinel if calls["n"] >= 3 else None

        monkeypatch.setattr(GitManager, "clone", _fail_twice_then_ok)
        mgr = ws_mod._clone_repo_with_retry(
            "http://srw-gitea:3000/x/proj-jobs.git", temp_base, backend=None
        )
        assert mgr is sentinel
        assert calls["n"] == 3

    def test_git_directory_created(self, temp_base):
        """Test that .git directory is created."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )
        ws.initialize()

        git_dir = ws.path / ".git"
        assert git_dir.exists()

    def test_gitignore_created(self, temp_base):
        """Test that .gitignore is created with sensible defaults."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )
        ws.initialize()

        gitignore = ws.path / ".gitignore"
        assert gitignore.exists()

        content = gitignore.read_text()
        # Check GitManager's default patterns are present
        for pattern in GitManager.DEFAULT_IGNORE_PATTERNS:
            assert pattern in content

        # documents/ should NOT be ignored by default
        assert "documents/" not in content

    def test_initial_commit_created(self, temp_base):
        """Test that initial commit is created."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
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
            backend=FilesystemTestBackend(temp_base),
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

    def test_default_ignore_patterns_on_git_manager(self, temp_base):
        """Test that GitManager has sensible default ignore patterns."""
        assert "*.db" in GitManager.DEFAULT_IGNORE_PATTERNS
        assert "*.log" in GitManager.DEFAULT_IGNORE_PATTERNS
        assert "__pycache__/" in GitManager.DEFAULT_IGNORE_PATTERNS
        # documents/ should NOT be in defaults
        assert "documents/" not in GitManager.DEFAULT_IGNORE_PATTERNS

    def test_git_manager_creates_gitignore_with_defaults(self, temp_base):
        """Test that GitManager.init_repository() writes default .gitignore."""
        workspace_path = temp_base / "job_test"
        workspace_path.mkdir()

        gm = GitManager(workspace_path)
        result = gm.init_repository()
        assert result is True

        gitignore = workspace_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        for pattern in GitManager.DEFAULT_IGNORE_PATTERNS:
            assert pattern in content

    def test_from_dict_with_git_config(self, temp_base):
        """Test creating config from dict with git settings."""
        data = {
            "structure": ["archive/"],
            "git_versioning": True,
        }
        config = WorkspaceManagerConfig.from_dict(data)

        assert config.git_versioning is True

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
            backend=FilesystemTestBackend(temp_base),
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
            backend=FilesystemTestBackend(temp_base),
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
            backend=FilesystemTestBackend(temp_base),
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
        monkeypatch.setattr(
            shutil, "which", lambda x: None if x == "git" else shutil.which(x)
        )

        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
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


JOBS_URL = "http://srw:token-a@srw-gitea:3000/srw/proj-jobs.git"
JOBS_URL_ROTATED = "http://srw:token-b@srw-gitea:3000/srw/proj-jobs.git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _seed_repo(root: Path, origin_url: str | None) -> None:
    """Create a real git repo with one commit at root — simulates a workspace
    a scholar subjob already initialized on the parent's shared pod."""
    _git(root, "init")
    _git(root, "config", "user.email", "scholar@test.local")
    _git(root, "config", "user.name", "Scholar")
    (root / "task_brief.md").write_text("research notes")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "scholar research")
    if origin_url:
        _git(root, "remote", "add", "origin", origin_url)


class TestProjectWorkspacePrepopulatedRoot:
    """First dispatch onto a pre-populated workspace root (scholar-provisioned
    shared pod, inherited subjob workspace) must reuse a matching jobs-repo
    clone instead of failing the clone as a phantom reachability problem."""

    def _make_ws(self, temp_base, branch=None):
        return WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                branch_name=branch,
                repositories=[
                    {"role": "jobs", "name": "proj-jobs", "repo_url": JOBS_URL}
                ],
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )

    @pytest.fixture
    def no_clone(self, monkeypatch):
        """Fail the test if GitManager.clone runs — these paths must not clone."""

        def _fail_clone(*a, **k):
            pytest.fail("GitManager.clone must not run on a pre-populated root")

        monkeypatch.setattr(GitManager, "clone", _fail_clone)

    def test_reuses_matching_clone_without_recloning(self, temp_base, no_clone):
        # Same repo, different (rotated) credentials — must still match.
        _seed_repo(temp_base, origin_url=JOBS_URL_ROTATED)

        ws = self._make_ws(temp_base, branch="job/test-job")
        ws.initialize_project_workspace()

        assert ws._initialized is True
        assert ws.git_manager is not None
        # Pre-existing (scholar) content preserved
        assert (temp_base / "task_brief.md").exists()
        # Job branch created on the existing repo
        assert ws.git_manager.current_branch() == "job/test-job"
        # Origin refreshed to the current credential-bearing URL
        assert ws.git_manager.remote_url("origin") == JOBS_URL

    def test_mismatched_origin_raises_accurate_error(self, temp_base, no_clone):
        _seed_repo(temp_base, origin_url="http://srw-gitea:3000/other/elsewhere.git")

        ws = self._make_ws(temp_base)
        with pytest.raises(RuntimeError, match="does not match project jobs repo"):
            ws.initialize_project_workspace()
        assert ws._initialized is False

    def test_missing_origin_raises_accurate_error(self, temp_base, no_clone):
        _seed_repo(temp_base, origin_url=None)

        ws = self._make_ws(temp_base)
        with pytest.raises(RuntimeError, match="does not match project jobs repo"):
            ws.initialize_project_workspace()
        assert ws._initialized is False

    def test_nonempty_root_without_git_raises_before_clone(self, temp_base, no_clone):
        (temp_base / "task_brief.md").write_text("seeded content, no git")

        ws = self._make_ws(temp_base)
        with pytest.raises(RuntimeError, match="is not empty"):
            ws.initialize_project_workspace()
        assert ws._initialized is False
