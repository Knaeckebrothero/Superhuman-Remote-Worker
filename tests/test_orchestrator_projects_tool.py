"""Tests for persistent-session project context tools."""

from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from agent.tools.context import ToolContext  # noqa: E402
from agent.tools.orchestrator import create_orchestrator_tools  # noqa: E402


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


@pytest.mark.asyncio
async def test_get_current_project_uses_context_project_id(monkeypatch):
    project_id = "project-123"
    url = f"http://localhost:8085/api/projects/{project_id}"
    cap = _CapturingClient(
        {
            url: {
                "id": project_id,
                "name": "Hotel ERP",
                "status": "active",
                "goal": "Build a hotel ERP system",
            }
        }
    )
    captured = {}

    def _factory(*, user_id=None):
        captured["user_id"] = user_id
        return cap

    monkeypatch.setattr("agent.tools.orchestrator.projects._get_client", _factory)

    tools = create_orchestrator_tools(
        ToolContext(user_id="user-xyz", _project_id=project_id)
    )
    get_current = _tool_by_name(tools, "get_current_project")

    result = await get_current.ainvoke({})

    assert captured["user_id"] == "user-xyz"
    assert cap.gets == [(url, {})]
    assert "Project ID: project-123" in result
    assert "Name: Hotel ERP" in result
    assert "Goal: Build a hotel ERP system" in result


@pytest.mark.asyncio
async def test_get_current_project_reports_missing_context_project():
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    get_current = _tool_by_name(tools, "get_current_project")

    result = await get_current.ainvoke({})

    assert "not scoped to a current project" in result


@pytest.mark.asyncio
async def test_list_project_jobs_defaults_to_context_project(monkeypatch):
    project_id = "project-123"
    job_id = "19707fa1-0000-4000-8000-000000000001"
    url = f"http://localhost:8085/api/projects/{project_id}/jobs"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": job_id,
                    "status": "processing",
                    "description": "Implement booking flow",
                    "config_name": "developer",
                }
            ]
        }
    )

    monkeypatch.setattr(
        "agent.tools.orchestrator.projects._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(
        ToolContext(user_id="user-xyz", _project_id=project_id)
    )
    list_jobs = _tool_by_name(tools, "list_project_jobs")

    result = await list_jobs.ainvoke({"status": "processing", "limit": 5})

    assert cap.gets == [(url, {"limit": 5, "status": "processing"})]
    assert f"Found 1 job(s) for project {project_id}" in result
    assert job_id in result
    assert "Implement booking flow" in result


@pytest.mark.asyncio
async def test_list_project_jobs_accepts_explicit_project_id(monkeypatch):
    project_id = "explicit-project"
    url = f"http://localhost:8085/api/projects/{project_id}/jobs"
    cap = _CapturingClient({url: []})
    monkeypatch.setattr(
        "agent.tools.orchestrator.projects._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    list_jobs = _tool_by_name(tools, "list_project_jobs")

    result = await list_jobs.ainvoke({"project_id": project_id})

    assert cap.gets == [(url, {"limit": 20})]
    assert f"No jobs found for project {project_id}" in result
