"""A resumed job must continue on its own branch, not the previous occupant's.

Regression tests for the data loss in job ``6df02f64`` (dev, 2026-07-30): the
worker was returned with critic feedback, appended a ``## Sources`` section to
``output/glossary.md``, and the append vanished. It had not vanished — it was
committed *and pushed*, to ``subjob/50dee4ae/critic``. The critic subjob had run
in that workspace and left it checked out on its own branch; the parent resumed
onto the re-attached tree, kept that branch, and every commit for the rest of
the job landed there. ``main`` never advanced past round 1, so every reader
(critic, cockpit, MCP ``get_workspace_file``, and the eventual re-clone) read
``main`` and correctly reported the section missing.

See knowledge-history/done/resumed_job_inherits_subjob_git_branch.md.

These tests drive a **real** git repository through a **real** ``GitManager``.
Mocking ``current_branch``/``checkout_branch`` would assert only that the helper
calls the methods it obviously calls, and would have passed against the buggy
code — the branch behavior *is* the thing under test.
"""

import subprocess
from pathlib import Path

import pytest

from src.agent import DEFAULT_JOB_BRANCH, ensure_job_branch
from src.managers.git_manager import GitManager

SUBJOB_BRANCH = "subjob/50dee4ae/critic"

ROUND_1_GLOSSARY = """# Glossary

## Idempotency

An operation is idempotent when applying it once produces the same observable
result as applying it multiple times.
"""

SOURCES_SECTION = """
## Sources

- Lamport, L. "Paxos Made Simple." Microsoft Research, 2001.
- "Reactive Streams Specification," Version 1.0.4, reactive-streams.org.
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A job workspace a critic subjob left checked out on its own branch.

    Mirrors the incident: round 1 completed on ``main``; the critic subjob then
    ran in the same workspace, committed its verdict artifacts on
    ``subjob/50dee4ae/critic``, and left the tree there.
    """
    repo = tmp_path / "workspace"
    (repo / "output").mkdir(parents=True)

    _git(repo, "init", "-b", DEFAULT_JOB_BRANCH)
    _git(repo, "config", "user.email", "agent@workspace.local")
    _git(repo, "config", "user.name", "Agent")

    (repo / "output" / "glossary.md").write_text(ROUND_1_GLOSSARY, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Job completed (autonomy=full)")

    # The critic subjob runs on its own branch and leaves the tree on it.
    _git(repo, "checkout", "-b", SUBJOB_BRANCH)
    (repo / "output" / "critic_verdict.json").write_text('{"verdict":"returned"}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Critic verdict: returned")

    return repo


def _content_on(repo: Path, branch: str, path: str) -> str:
    """What a reader resolving this job to ``branch`` actually sees."""
    return _git(repo, "show", f"{branch}:{path}")


class TestResumeBranchRestore:
    def test_standalone_job_returns_to_main(self, workspace: Path):
        """A job row with no ``branch_name`` belongs on main, not on the subjob."""
        git_mgr = GitManager(workspace)
        assert git_mgr.current_branch() == SUBJOB_BRANCH, "fixture precondition"

        # Standalone (non-project) jobs carry branch_name = NULL.
        ensure_job_branch(
            git_mgr, {"git_remote_url": "http://gitea/job.git"}, "6df02f64"
        )

        assert git_mgr.current_branch() == DEFAULT_JOB_BRANCH

    def test_append_after_resume_is_visible_on_main(self, workspace: Path):
        """The actual loss: work committed after a resume must reach ``main``.

        This is the failing assertion that reproduced the incident — the append
        was committed and pushed, but to the subjob branch, so the section was
        missing from every ref anyone reads.
        """
        git_mgr = GitManager(workspace)
        ensure_job_branch(
            git_mgr, {"git_remote_url": "http://gitea/job.git"}, "6df02f64"
        )

        # Round 2: the agent appends the Sources section (edit_file position=end
        # is read + concat + write_file), and todo_complete auto-commits.
        glossary = workspace / "output" / "glossary.md"
        glossary.write_text(
            glossary.read_text(encoding="utf-8") + SOURCES_SECTION, encoding="utf-8"
        )
        assert git_mgr.commit("[Phase 3 Tactical] todo_2: Append a ## Sources section")

        assert "## Sources" in _content_on(
            workspace, DEFAULT_JOB_BRANCH, "output/glossary.md"
        )

    def test_project_job_keeps_its_own_branch(self, workspace: Path):
        """An explicit ``branch_name`` still wins — no regression for project jobs."""
        git_mgr = GitManager(workspace)
        _git(workspace, "branch", "job/abc12345")

        ensure_job_branch(git_mgr, {"branch_name": "job/abc12345"}, "abc12345")

        assert git_mgr.current_branch() == "job/abc12345"

    def test_subjob_stays_on_its_own_branch(self, workspace: Path):
        """The critic subjob itself must not be dragged onto main."""
        git_mgr = GitManager(workspace)

        ensure_job_branch(git_mgr, {"branch_name": SUBJOB_BRANCH}, "50dee4ae")

        assert git_mgr.current_branch() == SUBJOB_BRANCH

    def test_missing_target_branch_is_reported_not_silent(
        self, workspace: Path, caplog
    ):
        """Failing to re-point must be loud — this bug class is defined by silence."""
        git_mgr = GitManager(workspace)

        with caplog.at_level("WARNING"):
            ensure_job_branch(git_mgr, {"branch_name": "does/not/exist"}, "6df02f64")

        assert any(
            "does/not/exist" in r.message and "will not advance" in r.message
            for r in caplog.records
        ), (
            f"expected a warning naming the branch, got: {[r.message for r in caplog.records]}"
        )

    def test_no_git_manager_is_tolerated(self):
        assert ensure_job_branch(None, {"branch_name": "main"}, "x") is None


class TestPodHandoffCreatesMissingBranch:
    """The DB can name a branch Gitea does not have.

    Provisioning logs a failed `create_branch` and still writes `branch_name`
    to the jobs row (orchestrator/services/job_provisioning.py:167-188), so a
    freshly cloned pod-handoff workspace can be told to check out a branch that
    exists nowhere. A plain checkout returns False *silently* there, leaving the
    tree on the clone default — every commit lands on `main` while every reader
    resolves `job/<short_id>` and sees nothing. This is the incident inverted.
    """

    def test_missing_branch_is_created_not_silently_ignored(self, workspace: Path):
        git_mgr = GitManager(workspace)
        _git(workspace, "checkout", DEFAULT_JOB_BRANCH)
        metadata = {"branch_name": "job/abc12345"}  # never created in the repo

        ensure_job_branch(git_mgr, metadata, "abc12345", create=True)

        assert git_mgr.current_branch() == "job/abc12345"

    def test_work_lands_on_the_branch_readers_resolve(self, workspace: Path):
        git_mgr = GitManager(workspace)
        _git(workspace, "checkout", DEFAULT_JOB_BRANCH)

        ensure_job_branch(git_mgr, {"branch_name": "job/abc12345"}, "abc", create=True)
        (workspace / "output" / "glossary.md").write_text("recovered", encoding="utf-8")
        assert git_mgr.commit("[Phase 1 Tactical] todo_1: write deliverable")

        assert (
            _content_on(workspace, "job/abc12345", "output/glossary.md") == "recovered"
        )

    def test_create_false_still_reports_a_missing_branch(self, workspace: Path, caplog):
        """Without create, the failure must at least be loud."""
        git_mgr = GitManager(workspace)
        with caplog.at_level("WARNING"):
            ensure_job_branch(git_mgr, {"branch_name": "job/nope"}, "x", create=False)
        assert any("job/nope" in r.message for r in caplog.records)
