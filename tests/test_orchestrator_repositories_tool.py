"""Tests for persistent-session project repository tools."""

from __future__ import annotations

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


def _workspace_manager(*, supports_shell=True, exists=False):
    backend = MagicMock()
    backend.supports_shell = supports_shell
    backend.exists.return_value = exists

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
    workspace.backend.exists.assert_called_once_with("repos/hotel-erp")
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
