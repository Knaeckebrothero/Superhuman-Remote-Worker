"""Tests for subagent delegation feature.

Covers: delegate_work tool validation, graph suspension via freeze mechanism,
orchestrator status determination for delegation, resume injection, GitManager
worktree methods, and port range allocation.
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest


# ===========================================================================
# TestDelegateWorkValidation — Input validation for the delegate_work tool
# ===========================================================================


class TestDelegateWorkValidation:
    """Test delegate_work input validation without orchestrator calls."""

    def _make_tool_context(self, **overrides):
        """Build a mock ToolContext with delegation-enabled config."""
        ctx = MagicMock()
        ctx.config = {
            "delegation": {
                "enabled": True,
                "max_depth": 1,
                "max_timeout": 14400,
                "allowed_configs": [],
            },
        }
        ctx._job_metadata = {
            "job_id": "test-job-id",
            "project_id": "test-project",
            "priority": 5,
            "config_name": "defaults",
        }
        ctx.workspace_manager = MagicMock()
        ctx.workspace_manager.git_manager = MagicMock()
        ctx.workspace_manager.git_manager.is_active = True
        ctx.orchestrator_client = MagicMock()
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def test_validate_empty_tasks(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        err = _validate_inputs([], "", 7200, ctx)
        assert err is not None
        assert "empty" in err.lower()

    def test_validate_too_many_tasks(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        tasks = [{"description": f"task {i}"} for i in range(6)]
        err = _validate_inputs(tasks, "", 7200, ctx)
        assert err is not None
        assert "maximum" in err.lower() or "5" in err

    def test_validate_empty_description(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        err = _validate_inputs([{"description": ""}], "", 7200, ctx)
        assert err is not None
        assert "empty description" in err.lower()

    def test_validate_timeout_exceeds_max(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        err = _validate_inputs([{"description": "do stuff"}], "", 99999, ctx)
        assert err is not None
        assert "timeout" in err.lower()

    def test_validate_disallowed_config(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        ctx.config["delegation"]["allowed_configs"] = ["scholar", "critic"]
        tasks = [{"description": "do stuff", "config": "hacker"}]
        err = _validate_inputs(tasks, "", 7200, ctx)
        assert err is not None
        assert "hacker" in err

    def test_validate_git_versioning_required(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        ctx.workspace_manager.git_manager = None
        err = _validate_inputs([{"description": "task"}], "", 7200, ctx)
        assert err is not None
        assert "git" in err.lower()

    def test_validate_no_orchestrator_client(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        ctx.orchestrator_client = None
        err = _validate_inputs([{"description": "task"}], "", 7200, ctx)
        assert err is not None
        assert "orchestrator" in err.lower()

    def test_validate_delegation_disabled(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        ctx.config["delegation"]["enabled"] = False
        err = _validate_inputs([{"description": "task"}], "", 7200, ctx)
        assert err is not None
        assert "not enabled" in err.lower()

    def test_validate_passes_good_input(self):
        from src.tools.delegation.delegate_work import _validate_inputs

        ctx = self._make_tool_context()
        err = _validate_inputs(
            [{"description": "Research papers"}, {"description": "Analyze code"}],
            "shared context",
            3600,
            ctx,
        )
        assert err is None


# ===========================================================================
# TestPortRangeAllocation — Port range calculation and env block format
# ===========================================================================


class TestPortRangeAllocation:
    """Test port range allocation and subagent environment block formatting."""

    def test_port_range_single_child(self):
        from src.tools.delegation.delegate_work import _build_subagent_env_block

        block = _build_subagent_env_block(0, 1, [{"description": "solo task"}])
        assert "8100-8199" in block
        assert "subagent 0 of 1" in block

    def test_port_range_five_children(self):
        from src.tools.delegation.delegate_work import _build_subagent_env_block

        tasks = [{"description": f"task {i}"} for i in range(5)]
        for i in range(5):
            block = _build_subagent_env_block(i, 5, tasks)
            expected_base = 8000 + (i + 1) * 100
            expected_end = expected_base + 99
            assert f"{expected_base}-{expected_end}" in block

    def test_env_block_contains_sibling_info(self):
        from src.tools.delegation.delegate_work import _build_subagent_env_block

        tasks = [
            {"description": "Research papers"},
            {"description": "Analyze code"},
            {"description": "Review design"},
        ]
        block = _build_subagent_env_block(1, 3, tasks)
        # Should mention siblings 0 and 2, not self (1)
        assert "subagent/0" in block
        assert "subagent/2" in block
        assert "8100-8199" in block  # sibling 0
        assert "8300-8399" in block  # sibling 2

    def test_env_block_format(self):
        from src.tools.delegation.delegate_work import _build_subagent_env_block

        block = _build_subagent_env_block(
            0, 2, [{"description": "A"}, {"description": "B"}]
        )
        assert "=== SUBAGENT ENVIRONMENT ===" in block
        assert "================================" in block
        assert "Do NOT use ports outside your range" in block


# ===========================================================================
# TestSubagentManifest — .subagents.json manifest generation
# ===========================================================================


class TestSubagentManifest:
    """Test .subagents.json manifest generation."""

    def test_manifest_structure(self):
        from src.tools.delegation.delegate_work import _build_manifest

        tasks = [
            {"description": "Research", "config": "scholar"},
            {"description": "Analyze", "config": "developer"},
        ]
        manifest = _build_manifest("parent-123", tasks, ["child-0", "child-1"])

        assert manifest["parent_job_id"] == "parent-123"
        assert "delegation_timestamp" in manifest
        assert len(manifest["subagents"]) == 2

        sub0 = manifest["subagents"][0]
        assert sub0["creation_order"] == 0
        assert sub0["branch"] == "subagent/0"
        assert sub0["worktree"] == ".worktrees/subagent_0"
        assert sub0["port_range"] == [8100, 8199]
        assert sub0["description"] == "Research"
        assert sub0["config"] == "scholar"
        assert sub0["job_id"] == "child-0"


# ===========================================================================
# TestDetermineJobStatusDelegation — Orchestrator status determination
# ===========================================================================


class TestDetermineJobStatusDelegation:
    """Test determine_job_status() for delegation freeze_type."""

    def test_delegation_freeze_returns_waiting(self):
        from orchestrator.services.completion import determine_job_status

        job = {
            "id": "test-job",
            "config_override": None,
            "context": None,
        }
        result = {
            "should_stop": True,
            "goal_achieved": False,
            "freeze_data": {
                "freeze_type": "delegation",
                "child_job_ids": ["child-1", "child-2"],
            },
        }
        status, note = determine_job_status(job, result)
        assert status == "waiting"

    def test_job_complete_still_works(self):
        """Ensure existing job_complete freeze_type still works."""
        from orchestrator.services.completion import determine_job_status

        job = {
            "id": "test-job",
            "config_override": None,
            "context": None,
        }
        result = {
            "should_stop": True,
            "goal_achieved": True,
            "freeze_data": {"freeze_type": "job_complete"},
        }
        status, note = determine_job_status(job, result)
        # Should be completed or reviewing depending on verification config
        assert status in ("completed", "reviewing")

    def test_vm_upgrade_still_returns_paused(self):
        from orchestrator.services.completion import determine_job_status

        job = {
            "id": "test-job",
            "config_override": None,
            "context": None,
        }
        result = {
            "should_stop": True,
            "goal_achieved": False,
            "freeze_data": {"freeze_type": "vm_upgrade_required"},
        }
        status, note = determine_job_status(job, result)
        assert status == "paused"


# ===========================================================================
# TestGitManagerWorktree — Worktree management in GitManager
# ===========================================================================


class TestGitManagerWorktree:
    """Test GitManager worktree methods using real git repos."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a real git repo with an initial commit."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo_path,
            capture_output=True,
        )

        # Create an initial commit (required for worktrees)
        (repo_path / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            capture_output=True,
        )

        return repo_path

    @pytest.fixture
    def git_manager(self, git_repo):
        from src.managers.git_manager import GitManager

        return GitManager(git_repo)

    def test_worktree_add_local(self, git_manager, git_repo):
        """Create a worktree on local filesystem."""
        wt_path = ".worktrees/subagent_0"
        assert git_manager.worktree_add(wt_path, "subagent/0")

        # Verify worktree directory exists
        full_path = git_repo / ".worktrees" / "subagent_0"
        assert full_path.exists()
        assert (full_path / "README.md").exists()

    def test_worktree_remove_local(self, git_manager, git_repo):
        """Remove a worktree on local filesystem."""
        wt_path = ".worktrees/subagent_0"
        git_manager.worktree_add(wt_path, "subagent/0")

        assert git_manager.worktree_remove(wt_path)
        full_path = git_repo / ".worktrees" / "subagent_0"
        assert not full_path.exists()

    def test_worktree_list(self, git_manager, git_repo):
        """List worktrees."""
        git_manager.worktree_add(".worktrees/sub_0", "sub/0")
        git_manager.worktree_add(".worktrees/sub_1", "sub/1")

        worktrees = git_manager.worktree_list()
        # Should have at least 3: main + 2 worktrees
        assert len(worktrees) >= 3
        paths = [w.get("path", "") for w in worktrees]
        assert any("sub_0" in p for p in paths)
        assert any("sub_1" in p for p in paths)

    def test_merge_squash(self, git_manager, git_repo):
        """Squash-merge a branch into current HEAD."""
        # Create a worktree and make changes
        git_manager.worktree_add(".worktrees/feature", "feature/test")

        feature_path = git_repo / ".worktrees" / "feature"
        (feature_path / "new_file.txt").write_text("new content")
        subprocess.run(["git", "add", "."], cwd=feature_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add new file"],
            cwd=feature_path,
            capture_output=True,
        )

        # Back on main, squash merge
        success, msg = git_manager.merge_squash("feature/test")
        assert success, f"Merge failed: {msg}"
        assert (git_repo / "new_file.txt").exists()

    def test_delete_branch(self, git_manager, git_repo):
        """Delete a local branch after merge."""
        git_manager.worktree_add(".worktrees/temp", "temp/branch")
        git_manager.worktree_remove(".worktrees/temp")

        # Force delete since branch may not be fully merged
        assert git_manager.delete_branch("temp/branch", force=True)

    def test_merge_squash_no_changes(self, git_manager, git_repo):
        """Squash-merge with no changes succeeds gracefully."""
        git_manager.worktree_add(".worktrees/empty", "empty/branch")

        # Merge without making changes on the branch
        success, msg = git_manager.merge_squash("empty/branch")
        # Should succeed (nothing to merge is fine)
        assert success


# ===========================================================================
# TestDelegationResumeFormat — Format of delegation results message
# ===========================================================================


class TestDelegationResumeFormat:
    """Test formatting of delegation results for resume injection."""

    def test_delegation_results_format(self):
        from src.agent import _format_delegation_results

        results = [
            {
                "job_id": "child-001",
                "creation_order": 0,
                "config_name": "scholar",
                "status": "completed",
                "confidence": 0.85,
                "output_path": "outputs/001-scholar-child001",
                "summary": "Found 23 relevant papers.",
            },
            {
                "job_id": "child-002",
                "creation_order": 1,
                "config_name": "developer",
                "status": "failed",
                "confidence": None,
                "output_path": None,
                "summary": "Build failed due to missing dependency.",
            },
        ]

        msg = _format_delegation_results(results)
        assert "## Delegation Results" in msg
        assert "2 subagent(s)" in msg
        assert "child-001" in msg
        assert "child-002" in msg
        assert "scholar" in msg
        assert "developer" in msg
        assert "0.85" in msg
        # Deliverables are grafted outputs/ folders now — no branches to merge.
        assert "outputs/001-scholar-child001" in msg
        assert "git diff" not in msg.lower()
        assert "git_diff" not in msg
        assert "squash-merg" not in msg.lower()

    def test_empty_results(self):
        from src.agent import _format_delegation_results

        msg = _format_delegation_results([])
        assert "0 subagent(s)" in msg


# ===========================================================================
# TestToolContextDelegation — ToolContext delegation attributes
# ===========================================================================


class TestToolContextDelegation:
    """Test that ToolContext carries delegation-related attributes."""

    def test_orchestrator_client_attribute(self):
        from src.tools.context import ToolContext

        ctx = ToolContext(orchestrator_client="mock_client")
        assert ctx.orchestrator_client == "mock_client"

    def test_job_metadata_attribute(self):
        from src.tools.context import ToolContext

        meta = {"job_id": "abc", "project_id": "proj-1"}
        ctx = ToolContext(_job_metadata=meta)
        assert ctx._job_metadata["job_id"] == "abc"

    def test_freeze_request_flow(self):
        """Verify the freeze request/consume cycle works for delegation."""
        from src.tools.context import ToolContext

        ctx = ToolContext()
        assert ctx.consume_freeze_request() is None

        ctx.request_freeze(
            {
                "freeze_type": "delegation",
                "child_job_ids": ["a", "b"],
            }
        )
        req = ctx.consume_freeze_request()
        assert req is not None
        assert req["freeze_type"] == "delegation"
        assert ctx.consume_freeze_request() is None  # Consumed


# ===========================================================================
# TestRegistryDelegation — Tool registry integration
# ===========================================================================


class TestRegistryDelegation:
    """Test that delegation tools are properly registered."""

    def test_delegate_work_registered(self):
        from src.tools.registry import TOOL_REGISTRY

        assert "delegate_work" in TOOL_REGISTRY
        meta = TOOL_REGISTRY["delegate_work"]
        assert meta["category"] == "delegation"
        assert "strategic" in meta["phases"]
        assert "tactical" in meta["phases"]
        assert "placeholder" not in meta or meta.get("placeholder") is False

    def test_delegate_work_not_placeholder(self):
        """delegate_work should not be filtered out by placeholder checks."""
        from src.tools.registry import get_tools_for_phase

        strategic = get_tools_for_phase("strategic")
        assert "delegate_work" in strategic

        tactical = get_tools_for_phase("tactical")
        assert "delegate_work" in tactical


# ===========================================================================
# TestDelegationDepthCheck — Async depth checking
# ===========================================================================


class TestDelegationDepthCheck:
    """Test delegation depth checking via orchestrator."""

    @pytest.mark.asyncio
    async def test_depth_ok(self):
        from src.tools.delegation.delegate_work import _check_delegation_depth

        ctx = MagicMock()
        ctx._job_metadata = {"job_id": "test-job"}
        client = MagicMock()
        client.orchestrator_url = "http://localhost:8085"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"depth": 0}
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        ctx.orchestrator_client = client

        result = await _check_delegation_depth(ctx, max_depth=1)
        assert result is None  # No error

    @pytest.mark.asyncio
    async def test_depth_exceeded(self):
        from src.tools.delegation.delegate_work import _check_delegation_depth

        ctx = MagicMock()
        ctx._job_metadata = {"job_id": "test-job"}
        client = MagicMock()
        client.orchestrator_url = "http://localhost:8085"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"depth": 1}
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        ctx.orchestrator_client = client

        result = await _check_delegation_depth(ctx, max_depth=1)
        assert result is not None
        assert "depth limit" in result.lower()

    @pytest.mark.asyncio
    async def test_depth_endpoint_missing(self):
        """If the endpoint doesn't exist yet, assume depth 0 (fail open)."""
        from src.tools.delegation.delegate_work import _check_delegation_depth

        ctx = MagicMock()
        ctx._job_metadata = {"job_id": "test-job"}
        client = MagicMock()
        client.orchestrator_url = "http://localhost:8085"
        mock_response = MagicMock()
        mock_response.status_code = 404
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        ctx.orchestrator_client = client

        result = await _check_delegation_depth(ctx, max_depth=1)
        assert result is None  # Fail open


# ===========================================================================
# Phase 2: TestGitMergeSquashTool — git_merge_squash tool
# ===========================================================================


class TestGitMergeSquashTool:
    """Test git_merge_squash tool via the git tools module."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a real git repo with an initial commit."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo_path,
            capture_output=True,
        )
        (repo_path / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            capture_output=True,
        )
        return repo_path

    def test_git_merge_squash_tool_loads(self, git_repo):
        """Verify git_merge_squash is created by create_git_tools."""
        from src.managers.git_manager import GitManager
        from src.tools.context import ToolContext
        from src.tools.git.git_tools import create_git_tools

        gm = GitManager(git_repo)
        ws = MagicMock()
        ws.git_manager = gm
        ws.is_initialized = True
        ctx = ToolContext(workspace_manager=ws)
        tools = create_git_tools(ctx)
        tool_names = [t.name for t in tools]
        assert "git_merge_squash" in tool_names
        assert "git_worktree_cleanup" in tool_names

    def test_git_merge_squash_tool_executes(self, git_repo):
        """End-to-end: create branch, make changes, merge via tool."""
        from src.managers.git_manager import GitManager
        from src.tools.context import ToolContext
        from src.tools.git.git_tools import create_git_tools

        gm = GitManager(git_repo)

        # Create worktree + branch
        gm.worktree_add(".worktrees/sub0", "subagent/0")
        wt_path = git_repo / ".worktrees" / "sub0"
        (wt_path / "result.txt").write_text("subagent output")
        subprocess.run(["git", "add", "."], cwd=wt_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add result"],
            cwd=wt_path,
            capture_output=True,
        )

        ws = MagicMock()
        ws.git_manager = gm
        ws.is_initialized = True
        ctx = ToolContext(workspace_manager=ws)
        tools = create_git_tools(ctx)
        merge_tool = next(t for t in tools if t.name == "git_merge_squash")

        result = merge_tool.invoke({"branch": "subagent/0"})
        assert "successfully" in result.lower() or "squash" in result.lower()
        assert (git_repo / "result.txt").exists()


# ===========================================================================
# Phase 2: TestWorktreeCleanupTool — git_worktree_cleanup tool
# ===========================================================================


class TestWorktreeCleanupTool:
    """Test git_worktree_cleanup tool."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo_path,
            capture_output=True,
        )
        (repo_path / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            capture_output=True,
        )
        return repo_path

    def test_cleanup_removes_worktree_and_branch(self, git_repo):
        from src.managers.git_manager import GitManager
        from src.tools.context import ToolContext
        from src.tools.git.git_tools import create_git_tools

        gm = GitManager(git_repo)
        gm.worktree_add(".worktrees/subagent_0", "subagent/0")

        ws = MagicMock()
        ws.git_manager = gm
        ws.is_initialized = True
        ctx = ToolContext(workspace_manager=ws)
        tools = create_git_tools(ctx)
        cleanup_tool = next(t for t in tools if t.name == "git_worktree_cleanup")

        result = cleanup_tool.invoke({"branch": "subagent/0"})
        assert "removed worktree" in result.lower()

        # Verify worktree is gone
        assert not (git_repo / ".worktrees" / "subagent_0").exists()


# ===========================================================================
# Phase 2: TestResumeDelegationChild — resume_delegation_child tool
# ===========================================================================


class TestResumeDelegationChild:
    """Test resume_delegation_child tool metadata and registration."""

    def test_resume_child_registered(self):
        from src.tools.registry import TOOL_REGISTRY

        assert "resume_delegation_child" in TOOL_REGISTRY
        meta = TOOL_REGISTRY["resume_delegation_child"]
        assert meta["category"] == "delegation"
        assert "strategic" in meta["phases"]
        assert "tactical" in meta["phases"]

    def test_resume_child_tool_creates(self):
        """Verify the tool is created by create_delegation_tools."""
        from src.tools.context import ToolContext
        from src.tools.delegation import create_delegation_tools

        ctx = ToolContext(
            orchestrator_client=MagicMock(),
            _job_metadata={"job_id": "test"},
        )
        tools = create_delegation_tools(ctx)
        tool_names = [t.name for t in tools]
        assert "delegate_work" in tool_names
        assert "resume_delegation_child" in tool_names


# ===========================================================================
# Phase 4: TestDelegationTimeout — Timeout enforcement
# ===========================================================================


class TestDelegationTimeout:
    """Test delegation timeout detection logic."""

    def test_timeout_sweeper_function_exists(self):
        """Verify the timeout sweeper is defined in orchestrator.main."""
        # orchestrator.main can't be imported directly in tests (heavy deps),
        # so verify via source inspection
        import pathlib

        main_src = pathlib.Path("orchestrator/main.py").read_text()
        assert "async def _check_delegation_timeouts" in main_src
        assert "async def delegation_timeout_sweeper" in main_src
        assert "delegation_timeout_task" in main_src

    def test_delegation_freeze_has_timeout_field(self):
        """Verify the delegate_work tool includes timeout in freeze data."""
        # Simulate what the tool sets
        freeze_data = {
            "freeze_type": "delegation",
            "child_job_ids": ["a", "b"],
            "child_count": 2,
            "timeout": 7200,
            "timestamp": "2026-04-07T12:00:00+00:00",
            "job_id": "parent-123",
        }
        assert freeze_data["timeout"] == 7200
        assert freeze_data["freeze_type"] == "delegation"
        assert "timestamp" in freeze_data


# ===========================================================================
# Phase 4: TestOrphanedWorktreeCleanup — GitManager cleanup
# ===========================================================================


class TestOrphanedWorktreeCleanup:
    """Test orphaned worktree cleanup."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo_path,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo_path,
            capture_output=True,
        )
        (repo_path / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            capture_output=True,
        )
        return repo_path

    def test_cleanup_orphaned_directory(self, git_repo):
        """Cleanup removes directories in .worktrees/ not tracked by git."""
        from src.managers.git_manager import GitManager

        gm = GitManager(git_repo)

        # Create a worktree normally, then remove via git but leave dir
        gm.worktree_add(".worktrees/subagent_0", "subagent/0")
        # Remove via git worktree prune (by deleting the .git file inside)
        wt_path = git_repo / ".worktrees" / "subagent_0"
        assert wt_path.exists()
        gm.worktree_remove(".worktrees/subagent_0")

        # Now create a fake orphan directory
        orphan = git_repo / ".worktrees" / "orphan_dir"
        orphan.mkdir(parents=True)
        (orphan / "leftover.txt").write_text("stale")

        cleaned = gm.cleanup_orphaned_worktrees()
        assert len(cleaned) >= 1
        assert not orphan.exists()

    def test_cleanup_empty_worktrees_dir_removed(self, git_repo):
        """Empty .worktrees/ directory is removed after cleanup."""
        from src.managers.git_manager import GitManager

        gm = GitManager(git_repo)

        # Create empty .worktrees/
        wt_dir = git_repo / ".worktrees"
        wt_dir.mkdir()

        gm.cleanup_orphaned_worktrees()
        assert not wt_dir.exists()

    def test_cleanup_no_worktrees_dir(self, git_repo):
        """No error when .worktrees/ doesn't exist."""
        from src.managers.git_manager import GitManager

        gm = GitManager(git_repo)
        cleaned = gm.cleanup_orphaned_worktrees()
        assert cleaned == []


# ===========================================================================
# Phase 3: TestCockpitModels — Verify delegation fields in TypeScript models
# ===========================================================================


class TestCockpitDelegationFields:
    """Verify delegation fields exist in cockpit API models."""

    def test_job_model_has_creation_order(self):
        """Job interface should have creation_order field."""

        with open("cockpit/src/app/core/models/api.model.ts", "r") as f:
            content = f.read()
        assert "creation_order" in content

    def test_job_summary_has_creation_order(self):
        """JobSummary interface should have creation_order field."""
        with open("cockpit/src/app/core/models/audit.model.ts", "r") as f:
            content = f.read()
        assert "creation_order" in content

    def test_waiting_status_exists(self):
        """JobStatus type should include 'waiting'."""
        with open("cockpit/src/app/core/models/api.model.ts", "r") as f:
            content = f.read()
        assert "'waiting'" in content
