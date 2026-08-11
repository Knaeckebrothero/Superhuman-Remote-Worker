"""GitManager.restore_tree — forward restore of a full tree, deletions included."""

import subprocess
from uuid import UUID

import pytest

from src.managers.git_manager import (
    GitManager,
    WorkspaceUndoInvariantViolation,
)


UNDO_REQUEST_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_repo(tmp_path):
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def test_restore_tree_restores_content_and_deletes_new_files(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "kept.txt").write_text("v1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "A")
    sha_a = _git(repo, "rev-parse", "HEAD")

    (repo / "kept.txt").write_text("v2")
    (repo / "new.txt").write_text("added later")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "B")

    mgr = GitManager(repo)
    assert mgr.is_active
    assert mgr.restore_tree(sha_a) is True

    assert (repo / "kept.txt").read_text() == "v1"
    assert not (repo / "new.txt").exists()  # the checkout -- . trap: must delete
    # HEAD did not move — this is a worktree/index restore, not a reset.
    assert _git(repo, "rev-parse", "HEAD") != sha_a
    # Committing the restored state keeps history linear (fast-forward safe).
    assert mgr.commit("Rewind: restore workspace") is True
    log = _git(repo, "log", "--oneline")
    assert len(log.splitlines()) == 3


def test_restore_tree_bad_sha_returns_false(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "A")
    mgr = GitManager(repo)
    assert mgr.restore_tree("0000000000000000000000000000000000000000") is False


def test_restore_tree_inactive_repo_returns_false(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = GitManager(plain)
    assert mgr.restore_tree("HEAD") is False


def test_changed_paths_reports_files_across_turn_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "keep.txt").write_text("one")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "keep.txt").write_text("two")
    (repo / "new.txt").write_text("new")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD").strip()

    assert GitManager(repo).changed_paths(first, second) == ["keep.txt", "new.txt"]


def test_workspace_undo_commit_is_idempotently_recoverable(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "keep.txt").write_text("before")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "turn one")
    target = _git(repo, "rev-parse", "HEAD")

    (repo / "keep.txt").write_text("after")
    (repo / "new.txt").write_text("created")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "turn two")
    source = _git(repo, "rev-parse", "HEAD")

    mgr = GitManager(repo)
    assert mgr.restore_tree(target) is True
    assert mgr.workspace_matches_tree(target) is True
    effect_sha = mgr.commit_workspace_undo(
        request_id=UNDO_REQUEST_ID,
        source_sha=source,
        target_sha=target,
    )
    assert effect_sha is not None

    recovered = mgr.find_workspace_undo_commit(UNDO_REQUEST_ID)
    assert recovered is not None
    assert recovered.commit_sha == effect_sha
    assert recovered.source_sha == source
    assert recovered.target_sha == target
    assert mgr.trees_match(effect_sha, target) is True
    assert mgr.changed_paths(recovered.source_sha, recovered.commit_sha) == [
        "keep.txt",
        "new.txt",
    ]

    # Later empty bookkeeping commits do not hide the effect marker.
    assert mgr.commit("later bookkeeping", allow_empty=True) is True
    assert mgr.find_workspace_undo_commit(UNDO_REQUEST_ID) == recovered


def test_workspace_undo_recovery_rejects_marker_on_wrong_tree(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "value.txt").write_text("target")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "target")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "value.txt").write_text("source")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "source")
    source = _git(repo, "rev-parse", "HEAD")
    marker = (
        "forged undo marker\n\n"
        f"SRW-Control-Request: {UNDO_REQUEST_ID}\n"
        f"SRW-Undo-Source: {source}\n"
        f"SRW-Undo-Target: {target}"
    )
    _git(repo, "commit", "--allow-empty", "-m", marker)

    with pytest.raises(WorkspaceUndoInvariantViolation, match="malformed"):
        GitManager(repo).find_workspace_undo_commit(UNDO_REQUEST_ID)
