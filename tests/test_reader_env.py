"""Integration tests for the light-subagent reader environment (Phase 2).

Uses a real git repo + FilesystemTestBackend to verify acquire_reader_env:
worktree created/removed on the (test) workspace, reader tools exclude the
delegation category (no nesting), reader writes land in the worktree (isolated
from the parent root), and the reader context shares the parent's
orchestrator_client + job metadata (so citations stay under the parent job).
"""

import subprocess

import pytest

from src.tools.context import ToolContext
from src.tools.delegation.reader_env import (
    _reader_tool_names,
    acquire_reader_env,
    release_reader_env,
)

from tests._fs_backend import FilesystemTestBackend


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True)


@pytest.fixture
def parent_context(tmp_path):
    from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
    from src.managers.git_manager import GitManager

    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(["init"], ws)
    _git(["config", "user.email", "t@t.com"], ws)
    _git(["config", "user.name", "T"], ws)
    (ws / "README.md").write_text("# parent")
    _git(["add", "."], ws)
    _git(["commit", "-m", "init"], ws)

    parent_ws = WorkspaceManager(
        job_id="parent-job",
        config=WorkspaceManagerConfig(structure=[], base_path=str(ws)),
        backend=FilesystemTestBackend(ws),
    )
    parent_ws._initialized = True
    parent_ws._git_manager = GitManager(ws)

    return ToolContext(
        workspace_manager=parent_ws,
        orchestrator_client=object(),  # sentinel — must be shared into the reader
        _job_metadata={"job_id": "parent-job", "project_id": "proj"},
        config={"shell": {}},
    ), ws


class TestReaderToolNameFilter:
    def test_excludes_delegation_category(self):
        names = ["read_file", "delegate_work", "spawn_subagent", "cite_web"]
        out = _reader_tool_names(names, allow_writes=True)
        assert "delegate_work" not in out
        assert "spawn_subagent" not in out
        assert "read_file" in out
        assert "cite_web" in out  # citation tools survive

    def test_write_tools_gated_by_allow_writes(self):
        names = ["read_file", "write_file", "edit_file"]
        assert "write_file" not in _reader_tool_names(names, allow_writes=False)
        assert "write_file" in _reader_tool_names(names, allow_writes=True)


class TestAcquireReaderEnv:
    @pytest.mark.asyncio
    async def test_worktree_created_and_removed(self, parent_context):
        parent_ctx, ws = parent_context
        env = await acquire_reader_env(
            parent_ctx, ["read_file"], index=0, total=2, allow_writes=True
        )
        assert env.worktree_path == ".worktrees/sub_0"
        assert (ws / ".worktrees" / "sub_0").is_dir()  # created on the workspace
        assert env.branch == "sub/0"

        await release_reader_env(env)
        assert not (ws / ".worktrees" / "sub_0").exists()  # removed

    @pytest.mark.asyncio
    async def test_reader_tools_exclude_delegation(self, parent_context):
        parent_ctx, _ = parent_context
        env = await acquire_reader_env(
            parent_ctx,
            [
                "read_file",
                "write_file",
                "list_files",
                "delegate_work",
                "spawn_subagent",
            ],
            index=0,
            total=1,
            allow_writes=True,
        )
        loaded = {t.name for t in env.tools}
        assert "delegate_work" not in loaded
        assert "spawn_subagent" not in loaded
        assert "read_file" in loaded
        await release_reader_env(env)

    @pytest.mark.asyncio
    async def test_reader_writes_land_in_worktree(self, parent_context):
        parent_ctx, ws = parent_context
        env = await acquire_reader_env(
            parent_ctx, ["read_file"], index=1, total=2, allow_writes=True
        )
        # Write through the reader's workspace manager.
        env.context.workspace_manager.write_file("notes.md", "reader output")
        assert (ws / ".worktrees" / "sub_1" / "notes.md").read_text() == "reader output"
        assert not (ws / "notes.md").exists()  # NOT in the parent root
        await release_reader_env(env)

    @pytest.mark.asyncio
    async def test_reader_shares_orchestrator_and_job_metadata(self, parent_context):
        parent_ctx, _ = parent_context
        env = await acquire_reader_env(parent_ctx, ["read_file"], index=0, total=1)
        # Same objects → citations/orchestrator calls stay under the parent job.
        assert env.context.orchestrator_client is parent_ctx.orchestrator_client
        assert env.context._job_metadata is parent_ctx._job_metadata
        assert env.context._job_metadata["job_id"] == "parent-job"
        # But a distinct, worktree-rooted workspace.
        assert env.context.workspace_manager is not parent_ctx.workspace_manager
        await release_reader_env(env)

    @pytest.mark.asyncio
    async def test_reader_isolated_from_parent_undo_and_freeze(self, parent_context):
        parent_ctx, _ = parent_context
        parent_ctx._snapshot_callback = lambda p: None
        env = await acquire_reader_env(parent_ctx, ["read_file"], index=0, total=1)
        assert env.context._snapshot_callback is None
        assert env.context._freeze_request is None
        await release_reader_env(env)

    @pytest.mark.asyncio
    async def test_port_block_mentions_range_and_worktree(self, parent_context):
        parent_ctx, _ = parent_context
        env = await acquire_reader_env(
            parent_ctx, ["read_file"], index=2, total=4, allow_writes=True
        )
        assert "8300-8399" in env.port_block  # (index+1)*100 base
        assert ".worktrees/sub_2" in env.port_block
        await release_reader_env(env)

    @pytest.mark.asyncio
    async def test_no_git_falls_back_to_scratch_subdir(self, tmp_path):
        """When the parent has no active git, a scratch subdir is used instead."""
        from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig

        ws = tmp_path / "nogit"
        ws.mkdir()
        parent_ws = WorkspaceManager(
            job_id="j",
            config=WorkspaceManagerConfig(structure=[], base_path=str(ws)),
            backend=FilesystemTestBackend(ws),
        )
        parent_ws._initialized = True  # no _git_manager set
        ctx = ToolContext(
            workspace_manager=parent_ws,
            _job_metadata={"job_id": "j"},
            config={},
        )
        env = await acquire_reader_env(ctx, ["read_file"], index=0, total=1)
        assert env.worktree_path == ".subagents/reader_0"
        assert env.branch is None
        assert (ws / ".subagents" / "reader_0").is_dir()
        env.context.workspace_manager.write_file("x.md", "y")
        assert (ws / ".subagents" / "reader_0" / "x.md").read_text() == "y"
        await release_reader_env(env)
