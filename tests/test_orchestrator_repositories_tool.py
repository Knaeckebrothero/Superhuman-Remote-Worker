"""Tests for persistent-session project repository tools."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tools.context import ToolContext  # noqa: E402
from src.tools.orchestrator import create_orchestrator_tools  # noqa: E402


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


class _CapturingClient:
    def __init__(self, responses=None):
        self.gets: list[tuple[str, dict]] = []
        self.responses = responses or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=self.responses.get(url, {}))
        return resp


def _workspace_manager(
    *,
    supports_shell=True,
    exists=False,
    gitignore_exists=False,
    gitignore_content="",
):
    """Mock workspace manager for repository-tool tests.

    `exists` controls the pre-clone `backend.exists(checkout_path)` guard.
    `gitignore_exists`/`gitignore_content` independently control the
    `.gitignore` probe inside `_ensure_checkout_path_ignored` (F1), since a
    successful checkout calls `backend.exists()` for both paths through the
    same mock.
    """
    backend = MagicMock()
    backend.supports_shell = supports_shell

    def _exists(path):
        if path == ".gitignore":
            return gitignore_exists
        return exists

    backend.exists.side_effect = _exists
    backend.read_file.return_value = gitignore_content

    workspace = MagicMock()
    workspace.is_initialized = True
    workspace.path = Path("/workspace")
    workspace.backend = backend
    return workspace


@pytest.mark.asyncio
async def test_list_project_repositories_defaults_to_context_project(monkeypatch):
    project_id = "project-123"
    url = f"http://localhost:8085/api/projects/{project_id}/repositories"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": "repo-1",
                    "project_id": project_id,
                    "name": "hotel-erp",
                    "role": "jobs",
                    "repo_url": "https://git.example/hotel-erp.git",
                    "branch": "main",
                    "read_only": False,
                }
            ]
        }
    )

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(user_id="user-xyz", _project_id=project_id)
    )
    list_repos = _tool_by_name(tools, "list_project_repositories")

    result = await list_repos.ainvoke({})

    assert cap.gets == [(url, {})]
    assert f"Found 1 repository row(s) for project {project_id}" in result
    assert "--- repo-1 ---" in result
    assert "Name: hotel-erp" in result
    assert "Display URL: https://git.example/hotel-erp.git" in result


@pytest.mark.asyncio
async def test_get_default_project_repository_prefers_source_role(monkeypatch):
    project_id = "project-123"
    url = f"http://localhost:8085/api/projects/{project_id}/repositories"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": "legacy-repo",
                    "project_id": project_id,
                    "name": "legacy-jobs",
                    "role": "jobs",
                    "repo_url": "https://git.example/legacy.git",
                    "branch": "main",
                    "read_only": False,
                },
                {
                    "id": "repo-1",
                    "project_id": project_id,
                    "name": "hotel-erp",
                    "role": "source",
                    "repo_url": "https://git.example/hotel-erp.git",
                    "branch": "develop",
                    "read_only": False,
                },
            ]
        }
    )

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(user_id="user-xyz", _project_id=project_id)
    )
    get_default = _tool_by_name(tools, "get_default_project_repository")

    result = await get_default.ainvoke({})

    assert cap.gets == [(url, {})]
    assert "--- repo-1 ---" in result
    assert "Name: hotel-erp" in result
    assert "Branch: develop" in result


@pytest.mark.asyncio
async def test_managed_knowledge_repo_is_not_mistaken_for_default_source(monkeypatch):
    project_id = "project-123"
    url = f"http://localhost:8085/api/projects/{project_id}/repositories"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": "knowledge-repo",
                    "project_id": project_id,
                    "name": "project-123-knowledge",
                    "role": "knowledge",
                    "repo_url": "https://git.example/project-123-knowledge.git",
                    "branch": "main",
                    "read_only": False,
                }
            ]
        }
    )
    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(user_id="user-xyz", _project_id=project_id)
    )
    get_default = _tool_by_name(tools, "get_default_project_repository")

    result = await get_default.ainvoke({})

    assert "No writable source repository is linked" in result
    assert "project cloud folder" in result
    assert "project-123-knowledge" not in result


@pytest.mark.asyncio
async def test_legacy_jobs_repo_is_not_a_default_or_checkout_fallback(monkeypatch):
    project_id = "project-123"
    thread_id = "thread-1"
    public_url = f"http://localhost:8085/api/projects/{project_id}/repositories"
    internal_url = f"http://localhost:8085/api/agents/threads/{thread_id}/workspace"
    legacy = {
        "id": "legacy-repo",
        "project_id": project_id,
        "name": "project-123-jobs",
        "role": "jobs",
        "repo_url": "https://git.example/project-123-jobs.git",
        "branch": "main",
        "read_only": False,
    }
    cap = _CapturingClient(
        {public_url: [legacy], internal_url: {"repositories": [legacy]}}
    )
    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=_workspace_manager(),
        )
    )

    get_default = _tool_by_name(tools, "get_default_project_repository")
    checkout = _tool_by_name(tools, "checkout_project_repository")
    assert "No writable source repository is linked" in await get_default.ainvoke({})
    assert "No matching repository found" in await checkout.ainvoke({})


@pytest.mark.asyncio
async def test_managed_repository_cannot_be_checked_out_by_explicit_id(monkeypatch):
    project_id = "project-123"
    thread_id = "thread-1"
    internal_url = f"http://localhost:8085/api/agents/threads/{thread_id}/workspace"
    cap = _CapturingClient(
        {
            internal_url: {
                "repositories": [
                    {
                        "id": "knowledge-repo",
                        "project_id": project_id,
                        "name": "project-123-knowledge",
                        "role": "knowledge",
                        "repo_url": "https://git.example/project-123-knowledge.git",
                        "branch": "main",
                        "read_only": False,
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )
    workspace = _workspace_manager()
    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone") as mock_clone:
        result = await checkout.ainvoke({"repo_id": "knowledge-repo"})

    mock_clone.assert_not_called()
    assert "managed role 'knowledge'" in result
    assert "not checkout-eligible" in result


@pytest.mark.asyncio
async def test_checkout_project_repository_requires_shell_workspace():
    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id="project-123",
            workspace_manager=_workspace_manager(supports_shell=False),
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    result = await checkout.ainvoke({})

    assert "requires a shell-capable sandbox or VM workspace" in result
    assert "request_workspace_upgrade" in result


@pytest.mark.asyncio
async def test_checkout_project_repository_clones_internal_repo_without_printing_url(
    monkeypatch,
):
    project_id = "project-123"
    thread_id = "thread-1"
    internal_url = f"http://localhost:8085/api/agents/threads/{thread_id}/workspace"
    repo_url = "http://admin:secret@srw-gitea:3000/org/hotel-erp.git"
    cap = _CapturingClient(
        {
            internal_url: {
                "repositories": [
                    {
                        "id": "repo-1",
                        "project_id": project_id,
                        "name": "hotel-erp",
                        "role": "source",
                        "repo_url": repo_url,
                        "branch": "main",
                        "read_only": False,
                    }
                ]
            }
        }
    )
    workspace = _workspace_manager()
    git_mgr = MagicMock()

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch(
        "src.managers.git_manager.GitManager.clone", return_value=git_mgr
    ) as mock_clone:
        result = await checkout.ainvoke({})

    assert cap.gets == [(internal_url, {})]
    # F1's _ensure_checkout_path_ignored also probes `backend.exists(".gitignore")`
    # after a successful clone, so `exists` is no longer called exactly once;
    # this still confirms the pre-clone existence guard fired on the right path.
    workspace.backend.exists.assert_any_call("repos/hotel-erp")
    mock_clone.assert_called_once_with(
        repo_url,
        Path("/workspace/repos/hotel-erp"),
        backend=workspace.backend,
        remote_cwd="repos/hotel-erp",
    )
    git_mgr.checkout_branch.assert_not_called()
    assert "Repository checked out." in result
    assert "Path: repos/hotel-erp" in result
    assert "Repository ID: repo-1" in result
    assert "secret" not in result
    assert repo_url not in result


@pytest.mark.asyncio
async def test_checkout_project_repository_refuses_workspace_root(monkeypatch):
    project_id = "project-123"
    thread_id = "thread-1"
    internal_url = f"http://localhost:8085/api/agents/threads/{thread_id}/workspace"
    cap = _CapturingClient(
        {
            internal_url: {
                "repositories": [
                    {
                        "id": "repo-1",
                        "project_id": project_id,
                        "name": "hotel-erp",
                        "role": "source",
                        "repo_url": "http://admin:secret@srw-gitea:3000/org/hotel-erp.git",
                        "branch": "main",
                        "read_only": False,
                    }
                ]
            }
        }
    )

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=_workspace_manager(),
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone") as mock_clone:
        result = await checkout.ainvoke({"target_path": "."})

    mock_clone.assert_not_called()
    assert "Refusing to clone into workspace root" in result


# =============================================================================
# F1 -- gitignore the nested checkout so the per-turn `git add -A` doesn't
# commit it as a contentless gitlink (bug b1758f38).
# =============================================================================


def _source_repo_cap(project_id, thread_id, *, repo_url, name="hotel-erp"):
    internal_url = f"http://localhost:8085/api/agents/threads/{thread_id}/workspace"
    return _CapturingClient(
        {
            internal_url: {
                "repositories": [
                    {
                        "id": "repo-1",
                        "project_id": project_id,
                        "name": name,
                        "role": "source",
                        "repo_url": repo_url,
                        "branch": "main",
                        "read_only": False,
                    }
                ]
            }
        }
    )


@pytest.mark.asyncio
async def test_checkout_project_repository_creates_gitignore_when_absent(monkeypatch):
    project_id = "project-123"
    thread_id = "thread-1"
    repo_url = "http://admin:secret@srw-gitea:3000/org/hotel-erp.git"
    cap = _source_repo_cap(project_id, thread_id, repo_url=repo_url)
    workspace = _workspace_manager()

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone", return_value=MagicMock()):
        result = await checkout.ainvoke({})

    workspace.backend.write_file.assert_called_once()
    written_path, written_content = workspace.backend.write_file.call_args.args
    assert written_path == ".gitignore"
    assert "/repos/hotel-erp/" in written_content
    workspace.backend.append_file.assert_not_called()
    assert "Repository checked out." in result


@pytest.mark.asyncio
async def test_checkout_project_repository_appends_gitignore_entry_when_file_exists(
    monkeypatch,
):
    project_id = "project-123"
    thread_id = "thread-1"
    repo_url = "http://admin:secret@srw-gitea:3000/org/hotel-erp.git"
    cap = _source_repo_cap(project_id, thread_id, repo_url=repo_url)
    workspace = _workspace_manager(gitignore_exists=True, gitignore_content="*.log\n")

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone", return_value=MagicMock()):
        result = await checkout.ainvoke({})

    workspace.backend.append_file.assert_called_once()
    appended_path, appended_content = workspace.backend.append_file.call_args.args
    assert appended_path == ".gitignore"
    assert "/repos/hotel-erp/" in appended_content
    workspace.backend.write_file.assert_not_called()
    assert "Repository checked out." in result


@pytest.mark.asyncio
async def test_checkout_project_repository_gitignore_entry_is_idempotent(monkeypatch):
    project_id = "project-123"
    thread_id = "thread-1"
    repo_url = "http://admin:secret@srw-gitea:3000/org/hotel-erp.git"
    cap = _source_repo_cap(project_id, thread_id, repo_url=repo_url)
    workspace = _workspace_manager(
        gitignore_exists=True, gitignore_content="/repos/hotel-erp/\n"
    )

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone", return_value=MagicMock()):
        result = await checkout.ainvoke({})

    workspace.backend.write_file.assert_not_called()
    workspace.backend.append_file.assert_not_called()
    assert "Repository checked out." in result


@pytest.mark.asyncio
async def test_checkout_project_repository_gitignore_uses_custom_target_path(
    monkeypatch,
):
    project_id = "project-123"
    thread_id = "thread-1"
    repo_url = "http://admin:secret@srw-gitea:3000/org/hotel-erp.git"
    cap = _source_repo_cap(project_id, thread_id, repo_url=repo_url)
    workspace = _workspace_manager()

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone", return_value=MagicMock()):
        result = await checkout.ainvoke({"target_path": "vendor/lib"})

    workspace.backend.write_file.assert_called_once()
    written_path, written_content = workspace.backend.write_file.call_args.args
    assert written_path == ".gitignore"
    assert "/vendor/lib/" in written_content
    assert "repos/" not in written_content
    assert "Repository checked out." in result


@pytest.mark.asyncio
async def test_checkout_project_repository_gitignore_failure_does_not_fail_checkout(
    monkeypatch,
):
    project_id = "project-123"
    thread_id = "thread-1"
    repo_url = "http://admin:secret@srw-gitea:3000/org/hotel-erp.git"
    cap = _source_repo_cap(project_id, thread_id, repo_url=repo_url)
    workspace = _workspace_manager(gitignore_exists=True, gitignore_content="*.log\n")
    workspace.backend.append_file.side_effect = OSError("disk full")

    monkeypatch.setattr(
        "src.tools.orchestrator.repositories._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(
            user_id="user-xyz",
            _project_id=project_id,
            _thread_id=thread_id,
            workspace_manager=workspace,
        )
    )
    checkout = _tool_by_name(tools, "checkout_project_repository")

    with patch("src.managers.git_manager.GitManager.clone", return_value=MagicMock()):
        result = await checkout.ainvoke({})

    assert "Repository checked out." in result


def _run_git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_nested_checkout(root: Path) -> None:
    """git init `root` with a seed commit, plus a real nested repo at
    root/repos/foo with its own seed commit. Mirrors a session workspace
    immediately after checkout_project_repository clones a project repo,
    before the per-turn auto-commit's `git add -A` runs.
    """
    root.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], cwd=root)
    _run_git(["config", "user.email", "test@example.com"], cwd=root)
    _run_git(["config", "user.name", "Test"], cwd=root)
    (root / "README.md").write_text("root\n")
    _run_git(["add", "README.md"], cwd=root)
    _run_git(["commit", "-m", "seed"], cwd=root)

    nested = root / "repos" / "foo"
    nested.mkdir(parents=True)
    _run_git(["init"], cwd=nested)
    _run_git(["config", "user.email", "test@example.com"], cwd=nested)
    _run_git(["config", "user.name", "Test"], cwd=nested)
    (nested / "file.txt").write_text("hello\n")
    _run_git(["add", "file.txt"], cwd=nested)
    _run_git(["commit", "-m", "nested seed"], cwd=nested)


def test_gitignoring_checkout_path_prevents_nested_repo_gitlink_commit(tmp_path):
    """Git-level proof for bug b1758f38.

    An uncured nested checkout is committed by `git add -A` as a
    contentless gitlink (mode 160000) -- the baseline below reproduces
    that. Once a path is tracked, gitignore has no retroactive effect on
    it (verified empirically: writing .gitignore after the fact does not
    remove an already-committed gitlink, and `check-ignore` reports
    already-tracked paths as not ignored). The real fix is therefore a
    prevention, not a cleanup: `_ensure_checkout_path_ignored` runs *before*
    the first `git add -A` ever sees the checkout path (see the ordering
    guarantee in checkout_project_repository). This test models that by
    writing .gitignore before the first add -A on an independent, otherwise
    identically-constructed repo.
    """
    if not shutil.which("git"):
        pytest.skip("git is not available in this environment")

    # Baseline: no .gitignore -> the nested checkout is staged and committed
    # as a contentless gitlink.
    baseline_root = tmp_path / "baseline"
    _init_repo_with_nested_checkout(baseline_root)
    _run_git(["add", "-A"], cwd=baseline_root)
    _run_git(["commit", "-m", "baseline auto-commit"], cwd=baseline_root)
    baseline_tree = _run_git(["ls-tree", "-r", "HEAD"], cwd=baseline_root).stdout
    assert "160000" in baseline_tree
    assert "repos/foo" in baseline_tree

    # Fix: .gitignore is written *before* the first `add -A`, matching the
    # tool's ordering guarantee, so the gitlink never enters the tree.
    fixed_root = tmp_path / "fixed"
    _init_repo_with_nested_checkout(fixed_root)
    (fixed_root / ".gitignore").write_text("/repos/foo/\n")
    _run_git(["add", "-A"], cwd=fixed_root)
    _run_git(["commit", "-m", "fixed auto-commit"], cwd=fixed_root)
    fixed_tree = _run_git(["ls-tree", "-r", "HEAD"], cwd=fixed_root).stdout
    assert "160000" not in fixed_tree
    assert "repos/foo" not in fixed_tree

    check_ignore = subprocess.run(
        ["git", "check-ignore", "repos/foo"],
        cwd=fixed_root,
        capture_output=True,
        text=True,
    )
    assert check_ignore.returncode == 0
