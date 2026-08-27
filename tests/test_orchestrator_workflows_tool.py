"""Tests for persistent-session automation and project-loop workflow tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tools.context import ToolContext  # noqa: E402
from src.tools.orchestrator.workflows import create_workflow_tools  # noqa: E402
from src.tools.registry import get_tools_by_category  # noqa: E402


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


class _CapturingClient:
    def __init__(self, responses=None):
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self.responses = responses or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        return self._response(url)

    async def post(self, url, json=None, **kwargs):
        self.posts.append((url, json or {}))
        return self._response(url)

    async def patch(self, url, json=None, **kwargs):
        self.patches.append((url, json or {}))
        return self._response(url)

    def _response(self, url):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=self.responses.get(url, {}))
        return resp


def test_workflow_tools_have_separate_category():
    workflows = set(get_tools_by_category("workflows"))
    fleet = set(get_tools_by_category("orchestrator"))
    catalog = set(get_tools_by_category("agent_catalog"))

    expected = {
        "list_automations",
        "get_automation",
        "list_automation_runs",
        "propose_automation",
        "get_project_loop",
        "list_project_loop_jobs",
        "explain_project_loop",
    }
    assert expected <= workflows
    assert expected.isdisjoint(fleet)
    assert expected.isdisjoint(catalog)

    # The automation bundle get+set moved to `catalog_authoring` on 2026-08-03,
    # leaving `workflows` with reads plus `propose_automation`, which drafts a
    # bundle without writing it. So the group's `true` grants no writes.
    authoring = set(get_tools_by_category("catalog_authoring"))
    assert {"get_automation_bundle", "set_automation_bundle"} <= authoring
    assert authoring.isdisjoint(workflows)


@pytest.mark.asyncio
async def test_list_automations_formats_visible_rows(monkeypatch):
    url = "http://localhost:8085/api/automations"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": "auto-1",
                    "name": "Weekly review",
                    "enabled": True,
                    "trigger_type": "cron",
                    "cron_expr": "0 9 * * 1",
                    "timezone": "UTC",
                    "expert": "critic",
                    "prompt": "Review the project state.",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "src.tools.orchestrator.workflows._get_client", lambda **kw: cap
    )

    tools = create_workflow_tools(ToolContext(user_id="user-xyz"))
    list_automations = _tool_by_name(tools, "list_automations")

    result = await list_automations.ainvoke({"project_id": "project-1"})

    assert cap.gets == [(url, {"project_id": "project-1"})]
    assert "Weekly review" in result
    assert "Schedule: 0 9 * * 1 (UTC)" in result


@pytest.mark.asyncio
async def test_propose_automation_returns_disabled_bundle():
    tools = create_workflow_tools(
        ToolContext(user_id="user-xyz", _project_ids=["project-1"])
    )
    propose_automation = _tool_by_name(tools, "propose_automation")

    result = await propose_automation.ainvoke(
        {
            "name": "Daily standup",
            "prompt": "Summarize yesterday's project changes.",
            "expert": "scholar",
            "cron_expr": "0 8 * * 1-5",
        }
    )
    payload = json.loads(result)

    assert payload["kind"] == "automation_proposal"
    assert payload["bundle"]["enabled"] is False
    assert payload["bundle"]["project_id"] == "project-1"
    assert payload["bundle_hash"]


@pytest.mark.asyncio
async def test_set_automation_bundle_create_forces_disabled_by_default(monkeypatch):
    url = "http://localhost:8085/api/automations"
    cap = _CapturingClient({url: {"id": "auto-2"}})
    monkeypatch.setattr(
        "src.tools.orchestrator.workflows._get_client", lambda **kw: cap
    )

    tools = create_workflow_tools(ToolContext(user_id="user-xyz"))
    set_automation_bundle = _tool_by_name(tools, "set_automation_bundle")

    result = await set_automation_bundle.ainvoke(
        {
            "mode": "create",
            "dry_run": False,
            "bundle": {
                "name": "Hourly check",
                "cron_expr": "0 * * * *",
                "expert": "critic",
                "prompt": "Check the queue.",
                "enabled": True,
            },
        }
    )

    assert "Automation create succeeded." in result
    assert "Created disabled by default." in result
    assert cap.posts == [
        (
            url,
            {
                "name": "Hourly check",
                "cron_expr": "0 * * * *",
                "expert": "critic",
                "prompt": "Check the queue.",
                "enabled": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_explain_project_loop_includes_current_jobs(monkeypatch):
    project_id = "project-1"
    loop_url = f"http://localhost:8085/api/projects/{project_id}/loop"
    jobs_url = f"http://localhost:8085/api/projects/{project_id}/loop/jobs"
    cap = _CapturingClient(
        {
            loop_url: {
                "id": "loop-1",
                "project_id": project_id,
                "status": "running",
                "current_job_id": "job-1",
                "remaining_iterations": 3,
            },
            jobs_url: [
                {
                    "id": "job-1",
                    "status": "processing",
                    "description": "Loop developer pass",
                }
            ],
        }
    )
    monkeypatch.setattr(
        "src.tools.orchestrator.workflows._get_client", lambda **kw: cap
    )

    tools = create_workflow_tools(ToolContext(user_id="user-xyz"))
    explain_project_loop = _tool_by_name(tools, "explain_project_loop")

    result = await explain_project_loop.ainvoke({"project_id": project_id})

    assert cap.gets == [(loop_url, {}), (jobs_url, {"limit": 5})]
    assert "Project loop loop-1 is running." in result
    assert "Current job: job-1" in result
    assert "Loop developer pass" in result
