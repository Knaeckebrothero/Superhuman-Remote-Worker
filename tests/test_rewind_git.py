"""GitManager.restore_tree — forward restore of a full tree, deletions included."""

import subprocess

from src.managers.git_manager import GitManager


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
