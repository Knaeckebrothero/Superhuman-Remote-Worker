"""Unit tests for workspace git initialization.

Tests that WorkspaceManager correctly initializes git repositories
when git_versioning is enabled, and that GitManager writes sensible
default .gitignore patterns without relying on config.
"""

import pytest
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent.core.workspace import WorkspaceManager, WorkspaceManagerConfig  # noqa: E402
from agent.managers.git_manager import GitManager  # noqa: E402
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

    def test_isolated_job_clone_failure_raises_not_silent_init(
        self, temp_base, monkeypatch
    ):
        """F29 hardening: a failed isolated-job clone must RAISE (so the job fails
        loudly and the loop advance counts it), NOT silently `git init` a
        disconnected workspace — that path rebuilt from scratch and lost every
        push on teardown in loop runs 5 & 6.
        """
        import agent.core.workspace as ws_mod

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
                git_remote_url="http://srw-gitea:3000/x/job-test.git",
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )
        with pytest.raises(RuntimeError, match="Failed to clone job workspace repo"):
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
        import agent.core.workspace as ws_mod

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

    def test_existing_managed_auxiliary_origin_is_replaced_with_scoped_ssh(
        self, temp_base
    ):
        repo_name = "project-source"
        repo_path = temp_base / "repos" / repo_name
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
        legacy = f"http://shared-admin:shared-secret@gitea:3000/srw/{repo_name}.git"
        subprocess.run(
            ["git", "-C", str(repo_path), "remote", "add", "origin", legacy],
            check=True,
            capture_output=True,
        )
        scoped = f"ssh://srw-repo-{'a' * 32}/srw/{repo_name}.git"
        manager = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                repositories=[
                    {
                        "role": "source",
                        "name": repo_name,
                        "repo_url": scoped,
                        "is_managed": True,
                    }
                ],
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )

        manager._clone_auxiliary_repos()

        refreshed = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert refreshed == scoped
        assert "shared-admin" not in refreshed
        assert "shared-secret" not in refreshed

    def test_existing_managed_auxiliary_foreign_origin_fails_closed(self, temp_base):
        repo_name = "project-source"
        repo_path = temp_base / "repos" / repo_name
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "remote",
                "add",
                "origin",
                "http://shared-admin:shared-secret@gitea:3000/srw/foreign.git",
            ],
            check=True,
            capture_output=True,
        )
        manager = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                repositories=[
                    {
                        "role": "source",
                        "name": repo_name,
                        "repo_url": (f"ssh://srw-repo-{'b' * 32}/srw/{repo_name}.git"),
                        "is_managed": True,
                    }
                ],
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )

        with pytest.raises(RuntimeError, match="foreign origin"):
            manager._clone_auxiliary_repos()

        unchanged = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "foreign.git" in unchanged

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
JOBS_SCOPED_SSH_URL = (
    "ssh://srw-repo-2fd83ae5f72c41dbae1802e69d598aef/srw/proj-jobs.git"
)


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
                git_remote_url=JOBS_URL,
                branch_name=branch,
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
        with pytest.raises(RuntimeError, match="does not match expected job repo"):
            ws.initialize_project_workspace()
        assert ws._initialized is False

    def test_legacy_admin_origin_migrates_to_clean_scoped_ssh(
        self, temp_base, no_clone
    ):
        _seed_repo(temp_base, origin_url=JOBS_URL_ROTATED)
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["archive/"],
                git_versioning=True,
                git_remote_url=JOBS_SCOPED_SSH_URL,
            ),
            base_path=temp_base,
            backend=FilesystemTestBackend(temp_base),
        )

        ws.initialize_project_workspace()

        assert ws.git_manager is not None
        assert ws.git_manager.remote_url("origin") == JOBS_SCOPED_SSH_URL
        config = (temp_base / ".git" / "config").read_text()
        assert "token-b" not in config
        assert "://srw:" not in config

    def test_missing_origin_attaches_and_sets_origin(self, temp_base, no_clone):
        """An unset/unreadable origin is not an identity conflict — `git remote
        get-url` can fail transiently on a fresh session (seen live on k3d),
        and failing here would lose the pre-seeded work. Attach and restore
        push connectivity by setting origin to the jobs-repo URL."""
        _seed_repo(temp_base, origin_url=None)

        ws = self._make_ws(temp_base, branch="job/test-job")
        ws.initialize_project_workspace()

        assert ws._initialized is True
        assert ws.git_manager is not None
        assert (temp_base / "task_brief.md").exists()
        assert ws.git_manager.remote_url("origin") == JOBS_URL

    def test_nonempty_root_without_git_raises_before_clone(self, temp_base, no_clone):
        (temp_base / "task_brief.md").write_text("seeded content, no git")

        ws = self._make_ws(temp_base)
        with pytest.raises(RuntimeError, match="is not empty"):
            ws.initialize_project_workspace()
        assert ws._initialized is False


class TestWorkspaceGetHeadCommit:
    """get_head_commit: the critic verdict tools' progress-detection heuristic
    (knowledge-base/knowledge/superpowers/plans/2026-07-27-verification-fail-closed.md, Task 5).

    Must work even when workspace-level git versioning (``git_manager``) is
    disabled — a target repo can be checked out at the workspace root by other
    means — and must never raise: a failure here must not break a verdict.
    """

    def test_returns_current_head_sha(self, temp_base):
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

        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ws.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert ws.get_head_commit() == expected
        assert len(expected) == 40  # full SHA-1, not an abbreviation

    def test_reflects_new_commits(self, temp_base):
        """Not cached — a fresh call after a commit sees the new HEAD."""
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

        before = ws.get_head_commit()
        ws.write_file("notes.md", "more notes")
        ws.git_manager.commit("Add notes")
        after = ws.get_head_commit()

        assert before is not None
        assert after is not None
        assert before != after

    def test_returns_none_without_git_repo(self, temp_base):
        """No `.git` at the workspace root (versioning disabled) — None, not
        an exception."""
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

        assert ws.git_manager is None  # sanity: this is the no-git_manager case
        assert ws.get_head_commit() is None

    def test_returns_none_when_git_binary_missing(self, temp_base, monkeypatch):
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

        assert ws.get_head_commit() is None

    def test_returns_none_before_workspace_initialized(self, temp_base):
        """Called on a workspace whose root doesn't exist yet — heuristic
        failure, not a crash."""
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(structure=["archive/"]),
            base_path=temp_base / "never-created",
            backend=FilesystemTestBackend(temp_base / "never-created"),
        )

        assert ws.get_head_commit() is None


class TestWorkspaceGetContentTree:
    """get_content_tree: the verification gate's REAL no-progress signal.

    A commit SHA cannot serve this purpose in either direction — it moves on
    every round (every freeze commits with ``allow_empty=True``) and it
    reverts on a re-clone after a failed push. These pin the two properties
    that make a content hash usable instead.
    """

    @staticmethod
    def _make_ws(base):
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(
                structure=["output/"],
                git_versioning=True,
            ),
            base_path=base,
            backend=FilesystemTestBackend(base),
        )
        ws.initialize()
        return ws

    def test_invariant_under_an_empty_commit(self, temp_base):
        """THE defect: ``commit(..., allow_empty=True)`` runs unconditionally
        at every phase boundary and at completion, so a guard keyed on HEAD
        can never fire."""
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "deliverable")
        ws.git_manager.commit("work")

        before_tree = ws.get_content_tree()
        before_head = ws.get_head_commit()

        ws.git_manager.commit("Job frozen for review", allow_empty=True)

        assert before_tree is not None
        assert ws.get_content_tree() == before_tree
        assert ws.get_head_commit() != before_head  # the contrast

    def test_changes_when_content_changes(self, temp_base):
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "v1")
        ws.git_manager.commit("v1")
        first = ws.get_content_tree()

        ws.write_file("output/report.md", "v2")
        ws.git_manager.commit("v2")

        assert ws.get_content_tree() != first

    def test_ignores_the_completion_bookkeeping_files(self, temp_base):
        """``job_frozen.json`` / ``job_completion.json`` carry a fresh
        ``timestamp`` on every run. Counting them would make two rounds over
        identical deliverables look different — the same inertness, one layer
        down."""
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "deliverable")
        ws.write_file("output/job_frozen.json", '{"timestamp": "T1"}')
        ws.git_manager.commit("round 1")
        first = ws.get_content_tree()

        ws.write_file("output/job_frozen.json", '{"timestamp": "T2"}')
        ws.git_manager.commit("round 2")

        assert ws.get_content_tree() == first

    def test_ignores_the_todo_archive_directory_by_prefix(self, temp_base):
        """``TodoManager.archive`` writes
        ``archive/todos_phase_{N}_{type}_{TIMESTAMP}.md`` — a NEW filename
        every round, from inside ``finalize_job`` itself. An exact-path
        exclusion cannot cover a timestamped name, so ``archive/`` is matched
        by prefix. Without this the hash moved every round for any job whose
        agent used todos, i.e. essentially all of them.
        """
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "deliverable")
        ws.git_manager.commit("work")
        first = ws.get_content_tree()

        ws.write_file("archive/todos_phase_1_strategic_20260727_120000.md", "round 1")
        ws.git_manager.commit("archive round 1")
        ws.write_file("archive/todos_phase_1_strategic_20260727_130000.md", "round 2")
        ws.git_manager.commit("archive round 2")

        assert first is not None
        assert ws.get_content_tree() == first

    def test_ignores_the_resume_feedback_file(self, temp_base):
        """``feedback.md`` is written into the workspace ROOT by
        ``restore_from_feedback`` (src/graph.py) on every feedback resume —
        which is exactly what a RETURNED verification round triggers. It lands
        between one round's capture and the next, so counting it meant the
        guard could never fire on the first repeated return.

        Unlike ``archive/`` this file IS delivered to the user; it is excluded
        because it is the round's INPUT, not its output.
        """
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "deliverable")
        ws.git_manager.commit("work")
        first = ws.get_content_tree()

        ws.write_file("feedback.md", "# Human Feedback\n\n## Feedback\n\nfix F1\n")
        ws.git_manager.commit("resumed with feedback")
        assert ws.get_content_tree() == first

        # And again with DIFFERENT feedback, since a later round's findings
        # can differ.
        ws.write_file("feedback.md", "# Human Feedback\n\n## Feedback\n\nfix F2\n")
        ws.git_manager.commit("resumed again")
        assert ws.get_content_tree() == first

    def test_a_deliverable_change_still_moves_it_alongside_bookkeeping(self, temp_base):
        """Guard against over-exclusion: the exclusions must not swallow real
        content sitting outside them, however much bookkeeping churns."""
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "v1")
        ws.write_file("archive/todos_phase_1_strategic_20260727_120000.md", "a")
        ws.write_file("feedback.md", "round 1 feedback")
        ws.git_manager.commit("v1")
        first = ws.get_content_tree()

        ws.write_file("output/report.md", "v2")
        ws.write_file("archive/todos_phase_1_strategic_20260727_130000.md", "b")
        ws.write_file("feedback.md", "round 2 feedback")
        ws.git_manager.commit("v2")

        assert ws.get_content_tree() != first

    def test_exclusion_is_exact_for_non_prefix_entries(self, temp_base):
        """``feedback.md`` has no trailing slash, so it must match exactly —
        a deliverable named ``feedback.md.bak`` or ``old_feedback.md`` is real
        content."""
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "deliverable")
        ws.git_manager.commit("base")
        first = ws.get_content_tree()

        ws.write_file("old_feedback.md", "a real file")
        ws.git_manager.commit("sibling")
        assert ws.get_content_tree() != first

    def test_prefix_entries_do_not_match_a_similarly_named_sibling(self, temp_base):
        """``archive/`` must not also exclude ``archived_results.md`` — prefix
        matching on a trailing-slash entry, not a bare string prefix."""
        ws = self._make_ws(temp_base)
        ws.write_file("output/report.md", "deliverable")
        ws.git_manager.commit("base")
        first = ws.get_content_tree()

        ws.write_file("archived_results.md", "this IS a deliverable")
        ws.git_manager.commit("add sibling")

        assert ws.get_content_tree() != first

    def test_same_content_in_a_fresh_repo_hashes_the_same(self, temp_base):
        """Content-addressed, so a re-clone after a failed push — which
        reverts HEAD to an entirely different commit — does not read as 'no
        progress'."""
        a = self._make_ws(temp_base / "a")
        a.write_file("output/report.md", "deliverable")
        a.git_manager.commit("work")

        b = self._make_ws(temp_base / "b")
        b.write_file("output/report.md", "deliverable")
        b.git_manager.commit("work")
        # A different history over identical content — what a re-clone from a
        # remote that missed a push looks like from the workspace's side.
        b.git_manager.commit("an extra commit b has and a does not", allow_empty=True)

        assert a.get_content_tree() == b.get_content_tree()
        assert a.get_head_commit() != b.get_head_commit()  # the contrast

    def test_returns_none_without_git_repo(self, temp_base):
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

        assert ws.get_content_tree() is None

    def test_returns_none_before_workspace_initialized(self, temp_base):
        ws = WorkspaceManager(
            job_id="test-job",
            config=WorkspaceManagerConfig(structure=["archive/"]),
            base_path=temp_base / "never-created",
            backend=FilesystemTestBackend(temp_base / "never-created"),
        )

        assert ws.get_content_tree() is None
