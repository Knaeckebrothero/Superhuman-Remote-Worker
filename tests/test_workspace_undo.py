"""Crash recovery for Git/turn-ledger backed session undo."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.managers.git_manager import GitManager
from src.services.workspace_undo import (
    WorkspaceUndoRetryable,
    WorkspaceUndoUnavailable,
    apply_workspace_undo,
)


REQUEST_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
SECOND_REQUEST_ID = UUID("11111111-2222-4333-8444-555555555555")
THIRD_REQUEST_ID = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")
FOURTH_REQUEST_ID = UUID("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _turn_repo(tmp_path):
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "kept.txt").write_text("turn one")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "turn one")
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "kept.txt").write_text("turn two")
    (repo / "new.txt").write_text("created later")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "turn two")
    source = _git(repo, "rev-parse", "HEAD")
    return repo, target, source


def _three_state_repo(tmp_path, name="three-state-workspace"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    commits = []
    for state in ("A", "B", "C"):
        (repo / "state.txt").write_text(state)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"state {state}")
        commits.append(_git(repo, "rev-parse", "HEAD"))
    return repo, tuple(commits)


def _add_bare_origin(tmp_path, repo, name="remote.git"):
    remote = tmp_path / name
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return remote


class _DurableLedger:
    """One mapping per transcript seq, newest first, like the real upsert."""

    def __init__(self, commits):
        self.commits = list(commits)
        self.list_workspace_turn_commits = AsyncMock(side_effect=self._list)
        self.record_turn_commit = AsyncMock(side_effect=self._record)

    async def _list(self, _thread_id):
        return list(self.commits)

    async def _record(self, _thread_id, commit_sha):
        if self.commits:
            self.commits[0] = commit_sha
        else:
            self.commits.append(commit_sha)


@pytest.mark.asyncio
async def test_retry_recovers_marker_after_push_failure_without_second_undo(tmp_path):
    repo, target, _source = _turn_repo(tmp_path)
    manager = GitManager(repo)
    postgres = SimpleNamespace(
        list_workspace_turn_commits=AsyncMock(return_value=[_source, target]),
        record_turn_commit=AsyncMock(),
    )
    workspace = SimpleNamespace(git_manager=manager)

    # No origin: the effect commit succeeds, but acknowledgement must remain
    # pending because another workspace pod could otherwise miss it.
    with pytest.raises(WorkspaceUndoRetryable, match="pushed"):
        await apply_workspace_undo(
            thread_id="thread-1",
            request_id=REQUEST_ID,
            postgres=postgres,
            workspace_manager=workspace,
        )
    effect_head = _git(repo, "rev-parse", "HEAD")
    assert (repo / "kept.txt").read_text() == "turn one"
    assert not (repo / "new.txt").exists()
    postgres.record_turn_commit.assert_not_awaited()

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    result = await apply_workspace_undo(
        thread_id="thread-1",
        request_id=REQUEST_ID,
        postgres=postgres,
        workspace_manager=workspace,
    )

    assert _git(repo, "rev-parse", "HEAD") == effect_head
    assert result.restored_to_sha == target
    assert result.restore_commit_sha == effect_head
    assert result.paths == ("kept.txt", "new.txt")
    assert postgres.list_workspace_turn_commits.await_count == 2
    postgres.list_workspace_turn_commits.assert_awaited_with("thread-1")
    postgres.record_turn_commit.assert_awaited_once_with("thread-1", effect_head)


@pytest.mark.asyncio
async def test_restore_before_marker_crash_is_finished_without_snapshotting_target(
    tmp_path,
):
    repo, target, source = _turn_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    manager = GitManager(repo)
    preparation_sha = manager.commit_workspace_undo_preparation(
        request_id=REQUEST_ID,
        target_sha=target,
    )
    assert preparation_sha is not None
    assert manager.restore_tree(target) is True
    assert _git(repo, "rev-parse", "HEAD") == preparation_sha

    postgres = SimpleNamespace(
        list_workspace_turn_commits=AsyncMock(return_value=[source, target]),
        record_turn_commit=AsyncMock(),
    )
    result = await apply_workspace_undo(
        thread_id="thread-2",
        request_id=REQUEST_ID,
        postgres=postgres,
        workspace_manager=SimpleNamespace(git_manager=manager),
    )

    effect = manager.find_workspace_undo_commit(REQUEST_ID)
    assert effect is not None
    assert effect.source_sha == preparation_sha
    assert result.paths == ("kept.txt", "new.txt")


@pytest.mark.asyncio
async def test_retry_after_preparation_before_restore_reuses_one_snapshot(tmp_path):
    repo, target, source = _turn_repo(tmp_path)
    _add_bare_origin(tmp_path, repo, name="preparation-retry-remote.git")
    manager = GitManager(repo)
    preparation_sha = manager.commit_workspace_undo_preparation(
        request_id=REQUEST_ID,
        target_sha=target,
    )
    assert preparation_sha is not None
    assert manager.workspace_matches_tree(preparation_sha)

    postgres = _DurableLedger([source, target])
    result = await apply_workspace_undo(
        thread_id="thread-preparation",
        request_id=REQUEST_ID,
        postgres=postgres,
        workspace_manager=SimpleNamespace(git_manager=manager),
    )

    effect = manager.find_workspace_undo_commit(REQUEST_ID)
    assert effect is not None
    assert effect.source_sha == preparation_sha
    assert result.restored_to_sha == target
    preparations = _git(
        repo,
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep=SRW-Undo-Prepare: {REQUEST_ID}",
    ).splitlines()
    assert preparations == [preparation_sha]


@pytest.mark.asyncio
async def test_gitless_workspace_undo_fails_closed_before_ledger_read(tmp_path):
    postgres = SimpleNamespace(
        list_workspace_turn_commits=AsyncMock(),
        record_turn_commit=AsyncMock(),
    )
    with pytest.raises(WorkspaceUndoUnavailable) as exc:
        await apply_workspace_undo(
            thread_id="thread-3",
            request_id=REQUEST_ID,
            postgres=postgres,
            workspace_manager=SimpleNamespace(git_manager=GitManager(tmp_path)),
        )

    assert exc.value.code == "workspace_undo_unsupported"
    postgres.list_workspace_turn_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_consecutive_distinct_undo_requests_walk_b_then_a_and_replay(tmp_path):
    repo, (sha_a, sha_b, sha_c) = _three_state_repo(tmp_path)
    _add_bare_origin(tmp_path, repo)
    manager = GitManager(repo)
    ledger = _DurableLedger([sha_c, sha_b, sha_a])
    workspace = SimpleNamespace(git_manager=manager)

    first = await apply_workspace_undo(
        thread_id="thread-chain",
        request_id=REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )
    assert (repo / "state.txt").read_text() == "B"
    assert first.restored_to_sha == sha_b
    assert ledger.commits == [first.restore_commit_sha, sha_b, sha_a]

    second = await apply_workspace_undo(
        thread_id="thread-chain",
        request_id=SECOND_REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )
    assert (repo / "state.txt").read_text() == "A"
    assert second.restored_to_sha == sha_a
    assert second.paths == ("state.txt",)
    assert ledger.commits == [second.restore_commit_sha, sha_b, sha_a]
    assert ledger.record_turn_commit.await_count == 2

    head_after_second = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(WorkspaceUndoUnavailable) as exhausted:
        await apply_workspace_undo(
            thread_id="thread-chain",
            request_id=THIRD_REQUEST_ID,
            postgres=ledger,
            workspace_manager=workspace,
        )
    assert exhausted.value.code == "workspace_undo_no_checkpoint"
    assert (repo / "state.txt").read_text() == "A"
    assert _git(repo, "rev-parse", "HEAD") == head_after_second
    assert manager.find_workspace_undo_commit(THIRD_REQUEST_ID) is None
    assert ledger.record_turn_commit.await_count == 2

    # Either UUID can be replayed after the later effect. Validated trailers
    # plus ancestry make the older mapping durable without rewinding files or
    # repointing the current transcript position back to its old effect.
    first_replay = await apply_workspace_undo(
        thread_id="thread-chain",
        request_id=REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )
    second_replay = await apply_workspace_undo(
        thread_id="thread-chain",
        request_id=SECOND_REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )

    assert first_replay == first
    assert second_replay == second
    assert (repo / "state.txt").read_text() == "A"
    assert _git(repo, "rev-parse", "HEAD") == second.restore_commit_sha
    assert ledger.record_turn_commit.await_count == 2


@pytest.mark.asyncio
async def test_read_only_handoff_mapping_is_skipped_by_tree_identity(tmp_path):
    repo, (sha_a, sha_b, _sha_c) = _three_state_repo(
        tmp_path,
        name="duplicate-tree-workspace",
    )
    # Drop C so HEAD is B, then model a later attach/read-only mapping with an
    # empty commit that has a new SHA but the exact same file tree.
    _git(repo, "reset", "--hard", sha_b)
    _git(repo, "commit", "--allow-empty", "-m", "read-only handoff at B")
    duplicate_b = _git(repo, "rev-parse", "HEAD")
    assert GitManager(repo).trees_match(duplicate_b, sha_b)
    _add_bare_origin(tmp_path, repo, name="duplicate-tree-remote.git")

    ledger = _DurableLedger([duplicate_b, sha_b, sha_a])
    result = await apply_workspace_undo(
        thread_id="thread-handoff",
        request_id=REQUEST_ID,
        postgres=ledger,
        workspace_manager=SimpleNamespace(git_manager=GitManager(repo)),
    )

    assert result.restored_to_sha == sha_a
    assert (repo / "state.txt").read_text() == "A"
    assert result.paths == ("state.txt",)


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
async def test_dirty_completed_state_undoes_to_latest_mapped_tree(tmp_path, staged):
    repo, sha_a, sha_b = _turn_repo(tmp_path)
    _add_bare_origin(tmp_path, repo, name="dirty-state-remote.git")
    manager = GitManager(repo)
    ledger = _DurableLedger([sha_b, sha_a])

    # Model a completed file-changing turn C whose auto-commit failed: HEAD and
    # the durable ledger remain at B, but the visible workspace is C.
    (repo / "kept.txt").write_text("turn three, dirty")
    if staged:
        _git(repo, "add", "-A")
    result = await apply_workspace_undo(
        thread_id="thread-dirty",
        request_id=REQUEST_ID,
        postgres=ledger,
        workspace_manager=SimpleNamespace(git_manager=manager),
    )

    effect = manager.find_workspace_undo_commit(REQUEST_ID)
    assert effect is not None
    assert result.restored_to_sha == sha_b
    assert effect.target_sha == sha_b
    assert _git(repo, "show", f"{effect.source_sha}:kept.txt") == ("turn three, dirty")
    assert (repo / "kept.txt").read_text() == "turn two"
    assert (repo / "new.txt").read_text() == "created later"


@pytest.mark.asyncio
async def test_dirty_tree_equal_to_older_state_uses_current_undo_cursor(tmp_path):
    repo, (sha_a, sha_b, sha_c) = _three_state_repo(
        tmp_path,
        name="dirty-equals-old-workspace",
    )
    _add_bare_origin(tmp_path, repo, name="dirty-equals-old-remote.git")
    manager = GitManager(repo)
    ledger = _DurableLedger([sha_c, sha_b, sha_a])
    workspace = SimpleNamespace(git_manager=manager)

    await apply_workspace_undo(
        thread_id="thread-dirty-old",
        request_id=REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )
    second = await apply_workspace_undo(
        thread_id="thread-dirty-old",
        request_id=SECOND_REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )
    assert second.restored_to_sha == sha_a
    assert (repo / "state.txt").read_text() == "A"

    # A later real dirty turn recreates the bytes from old state B. Equality
    # is not restore-crash evidence without this UUID's preparation marker;
    # its predecessor is the current logical cursor A, not B itself.
    (repo / "state.txt").write_text("B")
    third = await apply_workspace_undo(
        thread_id="thread-dirty-old",
        request_id=FOURTH_REQUEST_ID,
        postgres=ledger,
        workspace_manager=workspace,
    )

    effect = manager.find_workspace_undo_commit(FOURTH_REQUEST_ID)
    assert effect is not None
    assert third.restored_to_sha == sha_a
    assert effect.target_sha == sha_a
    assert _git(repo, "show", f"{effect.source_sha}:state.txt") == "B"
    assert (repo / "state.txt").read_text() == "A"
