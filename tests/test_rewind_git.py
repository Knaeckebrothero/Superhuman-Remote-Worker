"""GitManager.restore_tree — forward restore of a full tree, deletions included."""

import base64
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from agent.managers.git_manager import (
    GitManager,
    WorkspaceUndoInvariantViolation,
)
from agent.services.workspace_undo import apply_workspace_undo


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


class _LinewiseShellBackend:
    """Execute real Git while emulating RemoteBackend's tmux text capture."""

    supports_shell = True

    def __init__(
        self,
        root: Path,
        *,
        temp_dir: Path | None = None,
        drop_first_encoded_line: bool = False,
    ) -> None:
        self.root = str(root)
        self.commands: list[str] = []
        self.temp_dir = temp_dir
        self.drop_first_encoded_line = drop_first_encoded_line
        self.dropped_encoded_line: str | None = None
        self.retained_encoded_payload: str | None = None

    def exists(self, path: str) -> bool:
        return (Path(self.root) / path).exists()

    def shell_run(
        self,
        command: str,
        *,
        timeout: int,
        tab_name: str,
        working_dir: str | None,
    ) -> str:
        del tab_name
        self.commands.append(command)
        cwd = Path(self.root) / working_dir if working_dir else Path(self.root)
        env = dict(os.environ)
        if self.temp_dir is not None:
            env["TMPDIR"] = str(self.temp_dir)
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        # The real tmux transport reconstructs command output line-by-line;
        # embedded NUL record separators do not reach GitManager.
        merged_lines = (
            (completed.stdout + completed.stderr).replace("\x00", "").splitlines()
        )
        if self.drop_first_encoded_line and any(
            line.startswith("SRW-Git-NUL-Integrity ") for line in merged_lines
        ):
            for index, line in enumerate(merged_lines):
                if line and not line.startswith("SRW-Git-NUL-Integrity "):
                    self.dropped_encoded_line = merged_lines.pop(index)
                    self.retained_encoded_payload = "".join(
                        value
                        for value in merged_lines
                        if value and not value.startswith("SRW-Git-NUL-Integrity ")
                    )
                    self.drop_first_encoded_line = False
                    break
        linewise = "\n".join(merged_lines)
        return (
            f"Exit code: {completed.returncode}\nCWD: {cwd}\n--- stdout ---\n{linewise}"
        )


def _commit_undo_preparation(repo, *, request_id, target_sha, change=None):
    if change is not None:
        (repo / "state.txt").write_text(change)
        _git(repo, "add", "-A")
    message = (
        f"Prepare workspace undo to {target_sha[:12]}\n\n"
        f"SRW-Undo-Prepare: {request_id}\n"
        f"SRW-Undo-Target: {target_sha}"
    )
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


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


def test_local_changed_paths_preserves_raw_git_filename_bytes(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "state.txt").write_text("A")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state A")
    target = _git(repo, "rev-parse", "HEAD")

    raw_names = [
        b"carriage\rreturn.txt",
        b"crlf\r\nname.txt",
        b"non-utf8-\xff.txt",
    ]
    for raw_name in raw_names:
        raw_path = os.fsencode(repo) + b"/" + raw_name
        with open(raw_path, "wb") as stream:
            stream.write(b"bytes")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add raw-byte paths")
    source = _git(repo, "rev-parse", "HEAD")

    assert GitManager(repo).changed_paths(target, source) == [
        os.fsdecode(raw_name) for raw_name in raw_names
    ]


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


def test_identical_duplicate_undo_preparations_converge_to_newest(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "state.txt").write_text("A")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state A")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "state.txt").write_text("B")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state B")

    manager = GitManager(repo)
    older = manager.commit_workspace_undo_preparation(
        request_id=UNDO_REQUEST_ID,
        target_sha=target,
    )
    assert older is not None
    _commit_undo_preparation(
        repo,
        request_id=UNDO_REQUEST_ID,
        target_sha=target,
    )
    newest = _commit_undo_preparation(
        repo,
        request_id=UNDO_REQUEST_ID,
        target_sha=target,
    )
    assert manager.trees_match(older, newest)

    recovered = manager.find_workspace_undo_preparation(UNDO_REQUEST_ID)
    assert recovered is not None
    assert recovered.commit_sha == newest
    assert recovered.target_sha == target


@pytest.mark.parametrize("conflict", ["target", "tree"])
def test_conflicting_duplicate_undo_preparations_fail_closed(tmp_path, conflict):
    repo = _make_repo(tmp_path)
    (repo / "state.txt").write_text("A")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state A")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "state.txt").write_text("B")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state B")
    source = _git(repo, "rev-parse", "HEAD")

    manager = GitManager(repo)
    assert manager.commit_workspace_undo_preparation(
        request_id=UNDO_REQUEST_ID,
        target_sha=target,
    )
    _commit_undo_preparation(
        repo,
        request_id=UNDO_REQUEST_ID,
        target_sha=source if conflict == "target" else target,
        change="C" if conflict == "tree" else None,
    )

    with pytest.raises(WorkspaceUndoInvariantViolation, match="conflicting"):
        manager.find_workspace_undo_preparation(UNDO_REQUEST_ID)


def test_remote_nul_integrity_rejects_dropped_leading_base64_line(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "state.txt").write_text("A")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state A")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "state.txt").write_text("B")
    changed_names = [f"changed-{index:03d}-{'x' * 24}.txt" for index in range(100)]
    for name in changed_names:
        (repo / name).write_text(name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state B")
    source = _git(repo, "rev-parse", "HEAD")

    transport_temp = tmp_path / "transport-temp"
    transport_temp.mkdir()
    backend = _LinewiseShellBackend(
        repo,
        temp_dir=transport_temp,
        drop_first_encoded_line=True,
    )
    manager = GitManager(repo, backend=backend)
    args = ["diff", "--name-only", "-z", target, source]

    assert manager._run_git_nul_records(args) is None
    assert backend.dropped_encoded_line is not None
    assert len(backend.dropped_encoded_line) == 76
    assert backend.retained_encoded_payload is not None
    retained_raw = base64.b64decode(
        backend.retained_encoded_payload,
        validate=True,
    )
    # The retained suffix is independently valid base64 and still ends at a
    # complete NUL record. Only the end-retained length/hash frame detects the
    # missing leading data line.
    assert retained_raw.endswith(b"\x00")
    assert list(transport_temp.glob("srw-git-nul.*")) == []

    # A complete retry decodes normally, and a Git failure also removes its
    # operation-scoped spool file before surfacing failure.
    complete_paths = manager._run_git_nul_records(args)
    assert complete_paths is not None
    assert complete_paths == [*changed_names, "state.txt"]
    assert (
        manager._run_git_nul_records(
            ["diff", "--name-only", "-z", "missing-ref", source]
        )
        is None
    )
    assert list(transport_temp.glob("srw-git-nul.*")) == []
    framed_commands = [
        command for command in backend.commands if "SRW-Git-NUL-Integrity" in command
    ]
    assert framed_commands
    assert all(
        command.startswith("( ") and command.endswith("; )")
        for command in framed_commands
    )


@pytest.mark.asyncio
async def test_linewise_remote_retry_reuses_preparation_and_acknowledges_same_uuid(
    tmp_path,
):
    repo = _make_repo(tmp_path)
    (repo / "state.txt").write_text("A")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state A")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "state.txt").write_text("B")
    newline_path = repo / "line\nbreak.txt"
    newline_path.write_text("created in B")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "state B")
    source = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    backend = _LinewiseShellBackend(repo)
    manager = GitManager(repo, backend=backend)
    untracked_newline = repo / "untracked\nname.txt"
    untracked_newline.write_text("not committed")
    assert manager.workspace_matches_tree("HEAD") is False
    untracked_newline.unlink()
    assert manager.workspace_matches_tree("HEAD") is True
    preparation = manager.commit_workspace_undo_preparation(
        request_id=UNDO_REQUEST_ID,
        target_sha=target,
    )
    assert preparation is not None

    ledger = [source, target]

    async def list_commits(_thread_id):
        return list(ledger)

    async def record_commit(_thread_id, commit_sha):
        ledger[0] = commit_sha

    postgres = SimpleNamespace(
        list_workspace_turn_commits=AsyncMock(side_effect=list_commits),
        record_turn_commit=AsyncMock(side_effect=record_commit),
    )
    workspace = SimpleNamespace(git_manager=manager)

    # This call models a successor retry after the preparation commit landed
    # but before restore. The remote lookup must find that one preparation.
    first = await apply_workspace_undo(
        thread_id="thread-remote",
        request_id=UNDO_REQUEST_ID,
        postgres=postgres,
        workspace_manager=workspace,
    )
    replay = await apply_workspace_undo(
        thread_id="thread-remote",
        request_id=UNDO_REQUEST_ID,
        postgres=postgres,
        workspace_manager=workspace,
    )

    preparations = _git(
        repo,
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep=SRW-Undo-Prepare: {UNDO_REQUEST_ID}",
    ).splitlines()
    effects = _git(
        repo,
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep=SRW-Control-Request: {UNDO_REQUEST_ID}",
    ).splitlines()
    assert preparations == [preparation]
    assert len(effects) == 1
    assert first == replay
    assert first.restored_to_sha == target
    assert first.paths == ("line\nbreak.txt", "state.txt")
    assert (repo / "state.txt").read_text() == "A"
    assert not newline_path.exists()
    postgres.record_turn_commit.assert_awaited_once_with(
        "thread-remote", first.restore_commit_sha
    )
    assert any("git show --no-patch --format=%B" in cmd for cmd in backend.commands)
    assert all("%x00" not in cmd for cmd in backend.commands)
