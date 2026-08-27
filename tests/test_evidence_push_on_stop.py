"""Cancelling a job must not destroy the evidence it was killed over.

Regression guard for P1-D of
knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md.

Pushes to a job's Gitea branch happen at phase-0 seed, phase boundaries,
freeze and finalize; per-todo completion commits are LOCAL-ONLY, and no push
existed anywhere in the cancel path. Cancelling a job therefore destroyed
everything since its last boundary push — workspace/VM reaping then erased it
permanently. On 2026-07-30 the supervising officer cancelled two workers and
their mid-phase work was lost forever.

The fix has two layers, tested in order:

- ``src.core.phase.push_evidence_snapshot`` — stage-all + commit + push of
  the workspace as-is (no phase tag, no archive ritual), skipping cheaply
  when there is nothing to preserve and never raising.
- ``dual_app._complete_stop`` — the cooperative-stop chokepoint shared by
  /job/cancel, /job/pause, the heartbeat preemption backstop and lifespan
  drain — invokes it bounded and best-effort before tearing the pod down.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.api.dual_app as dual_app
from src.core.phase import push_evidence_snapshot
from src.managers.git_manager import GitManager


def _workspace(*, active=True, committed=True, unpushed=False, pushed=True):
    """A workspace mock exposing just the git_manager surface the helper uses."""
    ws = MagicMock()
    git = ws.git_manager
    git.is_active = active
    git.commit.return_value = committed
    git.has_unpushed_commits.return_value = unpushed
    git.push.return_value = pushed
    return ws


class TestPushEvidenceSnapshot:
    """The phase-layer helper: what gets committed, pushed, or skipped."""

    def test_dirty_tree_commits_and_pushes(self):
        ws = _workspace()
        assert push_evidence_snapshot(ws, "cancel", "job-1") is True
        ws.git_manager.commit.assert_called_once_with(
            "Evidence snapshot: job stopped (reason=cancel)", allow_empty=False
        )
        ws.git_manager.push.assert_called_once()

    def test_commit_message_states_the_stop_reason(self):
        ws = _workspace()
        push_evidence_snapshot(ws, "pause", "job-1")
        msg = ws.git_manager.commit.call_args.args[0]
        assert "pause" in msg
        assert "Evidence snapshot" in msg

    def test_clean_tree_with_local_todo_commits_still_pushes(self):
        """Per-todo commits are local-only — a clean tree can still carry
        unpushed work that would die with the workspace."""
        ws = _workspace(committed=False, unpushed=True)
        assert push_evidence_snapshot(ws, "cancel", "job-1") is True
        ws.git_manager.push.assert_called_once()

    def test_fully_pushed_clean_tree_skips_the_push(self):
        ws = _workspace(committed=False, unpushed=False)
        assert push_evidence_snapshot(ws, "cancel", "job-1") is False
        ws.git_manager.push.assert_not_called()

    def test_inactive_git_is_a_cheap_no_op(self):
        ws = _workspace(active=False)
        assert push_evidence_snapshot(ws, "cancel", "job-1") is False
        ws.git_manager.commit.assert_not_called()

    def test_missing_workspace_or_git_manager_is_a_no_op(self):
        assert push_evidence_snapshot(None, "cancel", "job-1") is False
        ws = MagicMock()
        ws.git_manager = None
        assert push_evidence_snapshot(ws, "cancel", "job-1") is False

    def test_commit_raising_never_propagates(self):
        ws = _workspace()
        ws.git_manager.commit.side_effect = RuntimeError("ssh channel died")
        assert push_evidence_snapshot(ws, "cancel", "job-1") is False

    def test_push_raising_never_propagates(self):
        ws = _workspace()
        ws.git_manager.push.side_effect = OSError("network unreachable")
        assert push_evidence_snapshot(ws, "cancel", "job-1") is False

    def test_push_returning_false_is_reported_not_raised(self):
        ws = _workspace(pushed=False)
        assert push_evidence_snapshot(ws, "cancel", "job-1") is False


@pytest.mark.skipif(shutil.which("git") is None, reason="Git not available")
class TestPushEvidenceSnapshotRealGit:
    """The incident shape against a real repo + bare remote: mid-phase local
    todo commits and an uncommitted scratch file must land on the remote."""

    @pytest.fixture
    def workdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def bare_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote = Path(tmpdir) / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            yield remote

    @pytest.fixture
    def synced_git(self, workdir, bare_remote):
        gm = GitManager(workdir)
        gm.init_repository()
        gm.add_remote("origin", str(bare_remote))
        assert gm.push() is True
        return gm

    @staticmethod
    def _remote_subjects(bare_remote: Path) -> str:
        result = subprocess.run(
            ["git", "--git-dir", str(bare_remote), "log", "--all", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_cancel_mid_phase_preserves_all_work_on_the_remote(
        self, workdir, bare_remote, synced_git
    ):
        # Mid-phase state: one completed todo (local-only commit) + WIP file.
        (workdir / "findings.md").write_text("verified: the fix holds\n")
        synced_git.commit("Complete todo #3: verify the fix")
        (workdir / "scaffold.md").write_text("half-filled deliverable\n")

        ws = SimpleNamespace(git_manager=synced_git)
        assert push_evidence_snapshot(ws, "cancel", "job-1") is True

        subjects = self._remote_subjects(bare_remote)
        assert "Evidence snapshot: job stopped (reason=cancel)" in subjects
        assert "Complete todo #3: verify the fix" in subjects
        assert synced_git.has_unpushed_commits() is False

    def test_nothing_to_preserve_leaves_the_remote_untouched(
        self, bare_remote, synced_git
    ):
        before = self._remote_subjects(bare_remote)
        assert (
            push_evidence_snapshot(
                SimpleNamespace(git_manager=synced_git), "cancel", "job-1"
            )
            is False
        )
        assert self._remote_subjects(bare_remote) == before


class TestEvidencePushOnCooperativeStop:
    """The dual_app teardown hook: every _request_stop lineage (cancel/pause
    handlers, heartbeat preemption backstop, lifespan drain) funnels into
    _complete_stop, so the push must fire there — and never break it."""

    @pytest.fixture(autouse=True)
    def _restore_dual_app_globals(self):
        names = (
            "_pod_state",
            "_current_job_id",
            "_current_job_task",
            "_stop_reason",
            "_agent",
            "_orchestrator_client",
        )
        saved = {name: getattr(dual_app, name) for name in names}
        stop_req, stop_done = (
            dual_app._stop_requested.is_set(),
            dual_app._stop_completed.is_set(),
        )
        dual_app._pod_state = dual_app.PodState.IDLE
        dual_app._current_job_id = None
        dual_app._current_job_task = None
        dual_app._agent = None
        dual_app._orchestrator_client = None
        dual_app._clear_stop()
        yield
        for name, val in saved.items():
            setattr(dual_app, name, val)
        (dual_app._stop_requested.set if stop_req else dual_app._stop_requested.clear)()
        (
            dual_app._stop_completed.set
            if stop_done
            else dual_app._stop_completed.clear
        )()

    def _working(self, job_id="job-under-test", workspace=None):
        dual_app._pod_state = dual_app.PodState.WORKING
        dual_app._current_job_id = job_id
        agent = MagicMock()
        agent._workspace_manager = workspace
        dual_app._agent = agent

    @pytest.mark.asyncio
    async def test_cancel_teardown_pushes_evidence_once_with_reason(self):
        ws = MagicMock()
        self._working(workspace=ws)
        dual_app._request_stop("cancel")

        with patch("src.api.dual_app.push_evidence_snapshot") as snap:
            await dual_app._complete_stop("job cancel")

        # Called with the live workspace handle and the honest reason, before
        # _reset_to_idle nulled the job id (else it would say "unknown").
        snap.assert_called_once_with(ws, "cancel", "job-under-test")
        assert dual_app._stop_completed.is_set()
        assert dual_app._pod_state == dual_app.PodState.IDLE

    @pytest.mark.asyncio
    async def test_preemption_backstop_pause_lineage_pushes_too(self):
        """A cockpit pause flips the row out-of-band; the heartbeat backstop
        requests the stop — the successor pod's clone must see this work."""
        ws = MagicMock()
        self._working("steered-job", workspace=ws)
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "paused"}
        )
        assert dual_app._stop_reason == "pause"

        with patch("src.api.dual_app.push_evidence_snapshot") as snap:
            await dual_app._complete_stop("job pause")

        snap.assert_called_once_with(ws, "pause", "steered-job")
        assert dual_app._stop_completed.is_set()

    @pytest.mark.asyncio
    async def test_git_failure_never_blocks_the_teardown(self):
        self._working(workspace=MagicMock())
        dual_app._request_stop("cancel")

        with patch(
            "src.api.dual_app.push_evidence_snapshot",
            side_effect=RuntimeError("git exploded"),
        ):
            await dual_app._complete_stop("job cancel")

        assert dual_app._stop_completed.is_set()
        assert dual_app._pod_state == dual_app.PodState.IDLE
        assert dual_app._current_job_id is None

    @pytest.mark.asyncio
    async def test_wedged_push_is_cut_off_by_the_timeout(self):
        """A hung SSH backend must not eat the 120s cooperative-stop window."""
        self._working(workspace=MagicMock())
        dual_app._request_stop("cancel")

        with (
            patch.object(dual_app, "_EVIDENCE_PUSH_TIMEOUT_SECONDS", 0.05),
            patch(
                "src.api.dual_app.push_evidence_snapshot",
                side_effect=lambda *a: time.sleep(1.0),
            ),
        ):
            start = asyncio.get_running_loop().time()
            await dual_app._complete_stop("job cancel")
            elapsed = asyncio.get_running_loop().time() - start

        assert elapsed < 0.8
        assert dual_app._stop_completed.is_set()
        assert dual_app._pod_state == dual_app.PodState.IDLE

    @pytest.mark.asyncio
    async def test_uninitialized_agent_is_a_no_op(self):
        """Job died before agent init — teardown must not trip on the push."""
        dual_app._pod_state = dual_app.PodState.WORKING
        dual_app._current_job_id = "job-early-death"
        dual_app._agent = None
        dual_app._request_stop("cancel")

        with patch("src.api.dual_app.push_evidence_snapshot") as snap:
            await dual_app._complete_stop("job cancel")

        snap.assert_not_called()
        assert dual_app._stop_completed.is_set()

    @pytest.mark.asyncio
    async def test_agent_without_workspace_is_a_no_op(self):
        """Workspace never initialized (or already torn down) — skip cheaply."""
        self._working(workspace=None)
        dual_app._request_stop("cancel")

        with patch("src.api.dual_app.push_evidence_snapshot") as snap:
            await dual_app._complete_stop("job cancel")

        snap.assert_not_called()
        assert dual_app._stop_completed.is_set()
