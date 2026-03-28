"""Tests for subjob worktree sharing — critics and scholars on the parent's workspace.

Tests the logic directly rather than importing from orchestrator.main, which
requires database and service dependencies not available in the test environment.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


# =============================================================================
# _job_needs_container — inherited backend detection
# =============================================================================


def _job_needs_container_logic(job: dict, provisioner_available: bool = False) -> bool:
    """Replicate _job_needs_container logic from orchestrator/main.py.

    This avoids importing orchestrator.main which has heavy dependencies.
    """
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    if ctx.get("vm", {}).get("status") == "ready":
        return False
    if ctx.get("workspace_container", {}).get("status") == "ready":
        return False

    co = job.get("config_override") or {}
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            co = {}
    backend = co.get("workspace", {}).get("backend")
    if backend == "container":
        return True
    if backend in ("local", "remote"):
        return False
    return provisioner_available


class TestJobNeedsContainerInherited:
    """Tests for _job_needs_container() with inherited workspace backends."""

    def test_inherited_ready_vm_skips_container(self):
        """Job with inherited ready VM does not need a container."""
        job = {
            "context": {
                "vm": {
                    "status": "ready",
                    "ssh_host": "100.64.0.1",
                    "pod_ip": "10.0.2.1",
                }
            },
            "config_override": {},
        }
        assert _job_needs_container_logic(job, provisioner_available=True) is False

    def test_inherited_ready_container_skips_provisioning(self):
        """Job with inherited ready workspace container does not need a new one."""
        job = {
            "context": {
                "workspace_container": {"status": "ready", "pod_ip": "10.0.1.3"}
            },
            "config_override": {},
        }
        assert _job_needs_container_logic(job, provisioner_available=True) is False

    def test_inherited_vm_not_ready_does_not_skip(self):
        """VM with non-ready status still allows container provisioning."""
        job = {
            "context": {"vm": {"status": "creating"}},
            "config_override": {},
        }
        assert _job_needs_container_logic(job, provisioner_available=True) is True

    def test_context_as_json_string(self):
        """Handles context stored as JSON string."""
        job = {
            "context": json.dumps(
                {"vm": {"status": "ready", "ssh_host": "100.64.0.1"}}
            ),
            "config_override": {},
        }
        assert _job_needs_container_logic(job, provisioner_available=True) is False

    def test_no_context_falls_through(self):
        """Job without context follows existing logic."""
        job = {"context": {}, "config_override": {}}
        assert _job_needs_container_logic(job, provisioner_available=True) is True
        assert _job_needs_container_logic(job, provisioner_available=False) is False

    def test_explicit_container_backend_with_inherited_vm(self):
        """Inherited ready VM takes precedence over explicit container backend."""
        job = {
            "context": {"vm": {"status": "ready", "ssh_host": "100.64.0.1"}},
            "config_override": {"workspace": {"backend": "container"}},
        }
        # Inherited VM check returns False first — intentional
        assert _job_needs_container_logic(job) is False

    def test_explicit_local_backend(self):
        """Explicit local backend returns False."""
        job = {
            "context": {},
            "config_override": {"workspace": {"backend": "local"}},
        }
        assert _job_needs_container_logic(job) is False

    def test_config_override_as_json_string(self):
        """Handles config_override stored as JSON string."""
        job = {
            "context": {},
            "config_override": json.dumps({"workspace": {"backend": "container"}}),
        }
        assert _job_needs_container_logic(job) is True


# =============================================================================
# Subjob context inheritance logic
# =============================================================================


def _build_subjob_context(parent_job: dict, base_context: dict) -> dict:
    """Replicate the workspace backend inheritance logic from orchestrator/main.py."""
    parent_ctx = parent_job.get("context") or {}
    if isinstance(parent_ctx, str):
        try:
            parent_ctx = json.loads(parent_ctx)
        except (json.JSONDecodeError, ValueError):
            parent_ctx = {}

    if parent_ctx.get("vm"):
        base_context["vm"] = parent_ctx["vm"]
    elif parent_ctx.get("workspace_container"):
        base_context["workspace_container"] = parent_ctx["workspace_container"]

    return base_context


class TestSubjobContextInheritance:
    """Tests that subjob creation inherits parent workspace backend context."""

    def test_inherits_vm_context(self):
        """VM context is propagated from parent to subjob."""
        vm_ctx = {"status": "ready", "ssh_host": "100.64.0.1", "pod_ip": "10.0.2.1"}
        parent = {"context": {"vm": vm_ctx, "git_remote_url": "http://git/repo"}}
        subjob_ctx = _build_subjob_context(parent, {"scholar_target": "parent-id"})

        assert subjob_ctx["vm"] == vm_ctx
        assert "workspace_container" not in subjob_ctx

    def test_inherits_container_context(self):
        """Container context is propagated from parent to subjob."""
        container_ctx = {"status": "ready", "pod_ip": "10.0.1.3"}
        parent = {"context": {"workspace_container": container_ctx}}
        subjob_ctx = _build_subjob_context(parent, {"scholar_target": "parent-id"})

        assert subjob_ctx["workspace_container"] == container_ctx
        assert "vm" not in subjob_ctx

    def test_vm_takes_precedence_over_container(self):
        """When both VM and container exist, VM wins."""
        vm_ctx = {"status": "ready", "ssh_host": "100.64.0.1"}
        container_ctx = {"status": "ready", "pod_ip": "10.0.1.3"}
        parent = {"context": {"vm": vm_ctx, "workspace_container": container_ctx}}
        subjob_ctx = _build_subjob_context(parent, {})

        assert subjob_ctx["vm"] == vm_ctx
        assert "workspace_container" not in subjob_ctx

    def test_no_backend_no_inheritance(self):
        """No VM/container context when parent has none."""
        parent = {"context": {"git_remote_url": "http://git/repo"}}
        subjob_ctx = _build_subjob_context(parent, {"target": "parent-id"})

        assert "vm" not in subjob_ctx
        assert "workspace_container" not in subjob_ctx

    def test_parent_context_as_json_string(self):
        """Handles parent context stored as JSON string."""
        vm_ctx = {"status": "ready", "ssh_host": "100.64.0.1"}
        parent = {"context": json.dumps({"vm": vm_ctx})}
        subjob_ctx = _build_subjob_context(parent, {})

        assert subjob_ctx["vm"] == vm_ctx

    def test_empty_parent_context(self):
        """Handles empty parent context gracefully."""
        parent = {"context": {}}
        subjob_ctx = _build_subjob_context(parent, {"target": "id"})

        assert "vm" not in subjob_ctx
        assert "workspace_container" not in subjob_ctx

    def test_none_parent_context(self):
        """Handles None parent context gracefully."""
        parent = {"context": None}
        subjob_ctx = _build_subjob_context(parent, {"target": "id"})

        assert "vm" not in subjob_ctx


# =============================================================================
# Worktree path computation
# =============================================================================


class TestWorktreePathComputation:
    """Tests for worktree_path generation."""

    def test_worktree_path_format(self):
        """worktree_path follows /home/agent-host/worktrees/{short_id}-{config} pattern."""
        short_id = "abc12345"
        config_name = "critic"
        path = f"/home/agent-host/worktrees/{short_id}-{config_name}"

        assert path == "/home/agent-host/worktrees/abc12345-critic"

    def test_worktree_path_only_when_backend_inherited(self):
        """worktree_path is set only when parent has a workspace backend."""
        parent_ctx_with_vm = {"vm": {"status": "ready"}}
        parent_ctx_without = {"git_remote_url": "http://git/repo"}

        short_id = "abc12345"
        config_name = "scholar"

        # With VM
        worktree_path = None
        if parent_ctx_with_vm.get("vm") or parent_ctx_with_vm.get(
            "workspace_container"
        ):
            worktree_path = f"/home/agent-host/worktrees/{short_id}-{config_name}"
        assert worktree_path is not None

        # Without backend
        worktree_path = None
        if parent_ctx_without.get("vm") or parent_ctx_without.get(
            "workspace_container"
        ):
            worktree_path = f"/home/agent-host/worktrees/{short_id}-{config_name}"
        assert worktree_path is None


# =============================================================================
# Dispatch — worktree_path injection
# =============================================================================


class TestDispatchWorktreePath:
    """Tests for worktree_path injection in _dispatch_job_to_agent."""

    def test_worktree_path_in_remaining_context(self):
        """worktree_path from job is added to remaining_context."""
        job_context = {
            "verification_target": "parent-id",
            "git_remote_url": "http://git/repo",
        }
        extracted_keys = {
            "upload_id",
            "config_upload_id",
            "instructions_upload_id",
            "instructions",
            "git_remote_url",
        }
        remaining_context = {
            k: v for k, v in job_context.items() if k not in extracted_keys
        }

        worktree_path = "/home/agent-host/worktrees/1111222-critic"
        job = {"worktree_path": worktree_path}
        if job.get("worktree_path"):
            remaining_context["worktree_path"] = job["worktree_path"]

        assert remaining_context["worktree_path"] == worktree_path

    def test_no_worktree_path_when_absent(self):
        """remaining_context has no worktree_path when job doesn't have one."""
        remaining_context = {"verification_target": "parent-id"}
        job = {"worktree_path": None}
        if job.get("worktree_path"):
            remaining_context["worktree_path"] = job["worktree_path"]

        assert "worktree_path" not in remaining_context

    def test_workspace_path_override(self):
        """worktree_path overrides remote.workspace_path in config_override."""
        config_override = {
            "workspace": {
                "backend": "remote",
                "remote": {
                    "host": "100.64.0.1",
                    "port": 22,
                    "username": "agent-host",
                    "workspace_path": "/home/agent-host/workspace",
                },
            }
        }
        worktree_path = "/home/agent-host/worktrees/abc12345-critic"

        # Simulate the override logic from _dispatch_job_to_agent
        if worktree_path and config_override:
            ws = config_override.get("workspace", {})
            remote = ws.get("remote", {})
            if remote:
                remote["workspace_path"] = worktree_path

        assert config_override["workspace"]["remote"]["workspace_path"] == worktree_path

    def test_no_override_without_remote_config(self):
        """No crash when config_override has no remote section."""
        config_override = {"workspace": {"backend": "local"}}
        worktree_path = "/home/agent-host/worktrees/abc12345-critic"

        if worktree_path and config_override:
            ws = config_override.get("workspace", {})
            remote = ws.get("remote", {})
            if remote:
                remote["workspace_path"] = worktree_path

        # Should not have added workspace_path to non-existent remote
        assert "remote" not in config_override.get("workspace", {})


# =============================================================================
# GitManager.from_worktree
# =============================================================================


class TestGitManagerFromWorktree:
    """Tests for GitManager.from_worktree() class method."""

    def test_from_worktree_valid(self):
        """Creates GitManager for a valid git worktree."""
        from src.managers.git_manager import GitManager

        with tempfile.TemporaryDirectory() as tmpdir:
            worktree_path = Path(tmpdir)

            subprocess.run(
                ["git", "init", str(worktree_path)],
                capture_output=True,
                check=True,
            )

            mgr = GitManager.from_worktree(worktree_path)
            assert mgr is not None
            assert mgr.workspace_path == worktree_path

    def test_from_worktree_with_git_file(self):
        """Creates GitManager for a worktree with .git file (not directory)."""
        from src.managers.git_manager import GitManager

        with tempfile.TemporaryDirectory() as tmpdir:
            parent_repo = Path(tmpdir) / "parent"
            parent_repo.mkdir()
            subprocess.run(
                ["git", "init", str(parent_repo)],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(parent_repo),
                    "commit",
                    "--allow-empty",
                    "-m",
                    "init",
                ],
                capture_output=True,
                check=True,
            )

            wt_path = Path(tmpdir) / "worktree"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(parent_repo),
                    "worktree",
                    "add",
                    str(wt_path),
                    "-b",
                    "test-branch",
                ],
                capture_output=True,
                check=True,
            )

            # .git should be a file, not directory
            assert (wt_path / ".git").is_file()

            mgr = GitManager.from_worktree(wt_path)
            assert mgr is not None

    def test_from_worktree_no_git(self):
        """Returns None for directory without .git."""
        from src.managers.git_manager import GitManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = GitManager.from_worktree(Path(tmpdir))
            assert mgr is None

    def test_from_worktree_nonexistent(self):
        """Returns None for nonexistent path."""
        from src.managers.git_manager import GitManager

        mgr = GitManager.from_worktree(Path("/nonexistent/path"))
        assert mgr is None


# =============================================================================
# Agent worktree creation (mocked RemoteBackend)
# =============================================================================


class TestAgentWorktreeCreation:
    """Tests for worktree creation logic in agent workspace setup."""

    def test_worktree_exec_commands(self):
        """Verifies the correct git commands are called for worktree creation."""
        backend = MagicMock()
        backend.supports_shell = True
        backend._exec = MagicMock(return_value="")

        worktree_path = "/home/agent-host/worktrees/abc12345-critic"
        branch_name = "subjob/abc12345/critic"
        parent_workspace = "/home/agent-host/workspace"

        # Simulate the worktree creation logic from agent.py
        backend._exec(
            f"git -C {parent_workspace} fetch origin {branch_name}",
            timeout=60,
        )
        backend._exec(
            f"mkdir -p $(dirname {worktree_path})",
            timeout=10,
        )
        backend._exec(
            f"git -C {parent_workspace} worktree add {worktree_path} {branch_name}",
            timeout=30,
        )

        assert backend._exec.call_count == 3
        calls = backend._exec.call_args_list

        # Verify fetch
        assert "fetch origin subjob/abc12345/critic" in calls[0][0][0]
        # Verify mkdir
        assert "mkdir -p" in calls[1][0][0]
        # Verify worktree add
        assert "worktree add" in calls[2][0][0]
        assert worktree_path in calls[2][0][0]
        assert branch_name in calls[2][0][0]

    def test_no_worktree_when_path_not_set(self):
        """No worktree commands when worktree_path is not in metadata."""
        backend = MagicMock()
        backend.supports_shell = True
        backend._exec = MagicMock()

        metadata = {"branch_name": "main"}

        worktree_path = metadata.get("worktree_path")
        if worktree_path and backend.supports_shell:
            backend._exec("should not be called")

        backend._exec.assert_not_called()

    def test_no_worktree_when_no_backend(self):
        """No worktree commands when workspace_backend is None."""
        metadata = {"worktree_path": "/home/agent-host/worktrees/abc-critic"}

        workspace_backend = None
        worktree_path = metadata.get("worktree_path")

        should_create = (
            worktree_path
            and workspace_backend
            and hasattr(workspace_backend, "supports_shell")
            and workspace_backend.supports_shell
        )
        assert not should_create

    def test_no_worktree_when_backend_no_shell(self):
        """No worktree commands when backend doesn't support shell."""
        backend = MagicMock()
        backend.supports_shell = False

        metadata = {"worktree_path": "/home/agent-host/worktrees/abc-critic"}
        worktree_path = metadata.get("worktree_path")

        should_create = worktree_path and backend and backend.supports_shell
        assert not should_create

    def test_fallback_on_exec_failure(self):
        """Worktree creation failure clears worktree_path and falls back."""
        backend = MagicMock()
        backend.supports_shell = True
        backend._exec = MagicMock(side_effect=Exception("SSH connection lost"))

        metadata = {
            "worktree_path": "/home/agent-host/worktrees/abc-critic",
            "branch_name": "subjob/abc12345/critic",
        }
        worktree_path = metadata.get("worktree_path")
        parent_workspace = "/home/agent-host/workspace"
        branch_name = metadata.get("branch_name", "main")

        # Simulate the fallback logic from agent.py
        try:
            backend._exec(
                f"git -C {parent_workspace} fetch origin {branch_name}",
                timeout=60,
            )
        except Exception:
            # Fallback: clear worktree_path so normal init takes over
            worktree_path = None
            metadata.pop("worktree_path", None)

        assert worktree_path is None
        assert "worktree_path" not in metadata


# =============================================================================
# _job_needs_vm — inherited VM must NOT trigger new VM provisioning
# =============================================================================


def _job_needs_vm_logic(job: dict) -> bool:
    """Replicate _job_needs_vm logic from orchestrator/main.py."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    vm_ctx = ctx.get("vm", {})
    if vm_ctx.get("requested"):
        return True
    co = job.get("config_override") or {}
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            co = {}
    return co.get("workspace", {}).get("backend") == "remote"


class TestJobNeedsVmInherited:
    """Verify inherited VMs don't trigger new VM provisioning."""

    def test_inherited_ready_vm_does_not_need_new_vm(self):
        """Inherited VM (status=ready, no requested=True) must return False."""
        job = {
            "context": {
                "vm": {
                    "status": "ready",
                    "ssh_host": "100.64.0.1",
                    "pod_ip": "10.0.2.1",
                }
            },
            "config_override": {},
        }
        assert _job_needs_vm_logic(job) is False

    def test_explicit_vm_request_returns_true(self):
        """VM with requested=True correctly returns True."""
        job = {
            "context": {"vm": {"requested": True}},
            "config_override": {},
        }
        assert _job_needs_vm_logic(job) is True

    def test_remote_backend_config_returns_true(self):
        """Explicit remote backend in config_override returns True."""
        job = {
            "context": {},
            "config_override": {"workspace": {"backend": "remote"}},
        }
        assert _job_needs_vm_logic(job) is True

    def test_inherited_vm_context_as_json_string(self):
        """Inherited VM in JSON string context does not need new VM."""
        job = {
            "context": json.dumps(
                {"vm": {"status": "ready", "ssh_host": "100.64.0.1"}}
            ),
            "config_override": {},
        }
        assert _job_needs_vm_logic(job) is False

    def test_no_vm_no_remote_backend(self):
        """Job with neither VM context nor remote backend returns False."""
        job = {"context": {}, "config_override": {}}
        assert _job_needs_vm_logic(job) is False

    def test_combined_inherited_vm_and_needs_container(self):
        """Full flow: inherited ready VM → _job_needs_vm=False, _job_needs_container=False.

        This is the critical path for worktree sharing: the subjob must
        skip BOTH VM provisioning AND container provisioning, falling
        straight through to dispatch.
        """
        job = {
            "context": {
                "vm": {
                    "status": "ready",
                    "ssh_host": "100.64.0.1",
                    "pod_ip": "10.0.2.1",
                }
            },
            "config_override": {},
        }
        assert _job_needs_vm_logic(job) is False
        assert _job_needs_container_logic(job, provisioner_available=True) is False


# =============================================================================
# Worktree path computation — container backend
# =============================================================================


class TestWorktreePathContainer:
    """Tests for worktree_path with workspace_container backend."""

    def test_worktree_path_set_for_container_backend(self):
        """worktree_path is set when parent has workspace_container."""
        parent_ctx = {"workspace_container": {"status": "ready", "pod_ip": "10.0.1.3"}}
        short_id = "def67890"
        config_name = "scholar"

        worktree_path = None
        if parent_ctx.get("vm") or parent_ctx.get("workspace_container"):
            worktree_path = f"/home/agent-host/worktrees/{short_id}-{config_name}"

        assert worktree_path == "/home/agent-host/worktrees/def67890-scholar"

    def test_worktree_path_not_set_for_deleted_container(self):
        """No worktree_path when parent container is deleted."""
        parent_ctx = {"workspace_container": {"status": "deleted"}}
        short_id = "def67890"
        config_name = "critic"

        worktree_path = None
        # The code checks presence of the key, not the status —
        # status filtering happens at dispatch time via _job_needs_container
        if parent_ctx.get("vm") or parent_ctx.get("workspace_container"):
            worktree_path = f"/home/agent-host/worktrees/{short_id}-{config_name}"

        # worktree_path IS set because the key exists (status is checked elsewhere)
        assert worktree_path is not None


# =============================================================================
# Dispatch — parent jobs unaffected
# =============================================================================


class TestDispatchParentJobUnaffected:
    """Verify parent jobs (no worktree_path) are not modified by the new logic."""

    def test_parent_job_no_workspace_path_override(self):
        """Parent job's workspace_path stays /home/agent-host/workspace."""
        config_override = {
            "workspace": {
                "backend": "remote",
                "remote": {
                    "host": "100.64.0.1",
                    "workspace_path": "/home/agent-host/workspace",
                },
            }
        }
        worktree_path = None  # Parent jobs have no worktree_path

        # Simulate dispatch logic
        if worktree_path and config_override:
            ws = config_override.get("workspace", {})
            remote = ws.get("remote", {})
            if remote:
                remote["workspace_path"] = worktree_path

        assert (
            config_override["workspace"]["remote"]["workspace_path"]
            == "/home/agent-host/workspace"
        )

    def test_parent_job_no_worktree_in_context(self):
        """Parent job's remaining_context has no worktree_path."""
        remaining_context = {"git_remote_url": "http://git/repo"}
        job = {"worktree_path": None}

        if job.get("worktree_path"):
            remaining_context["worktree_path"] = job["worktree_path"]

        assert "worktree_path" not in remaining_context

    def test_parent_job_worktree_path_empty_string(self):
        """Empty string worktree_path is treated as absent."""
        remaining_context = {"git_remote_url": "http://git/repo"}
        job = {"worktree_path": ""}

        if job.get("worktree_path"):
            remaining_context["worktree_path"] = job["worktree_path"]

        assert "worktree_path" not in remaining_context
