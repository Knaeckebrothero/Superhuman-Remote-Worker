"""Tests for persistent-session expert and skill catalog tools."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

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


@pytest.mark.asyncio
async def test_list_experts_filters_visible_catalog(monkeypatch):
    url = "http://localhost:8085/api/experts"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": "persistent_defaults",
                    "name": "persistent_defaults",
                    "display_name": "Persistent Defaults",
                    "description": "Interactive session expert",
                    "source": "bundled",
                    "tags": ["session"],
                },
                {
                    "id": "developer",
                    "name": "developer",
                    "display_name": "Developer",
                    "description": "Builds software",
                    "source": "bundled",
                    "tags": ["code"],
                },
            ]
        }
    )
    monkeypatch.setattr("src.tools.orchestrator.catalog._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    list_experts = _tool_by_name(tools, "list_experts")

    result = await list_experts.ainvoke(
        {"expert_type": "session", "query": "persistent"}
    )

    assert cap.gets == [(url, {"type": "session"})]
    assert "Persistent Defaults" in result
    assert "Interactive session expert" in result
    assert "Developer" not in result


@pytest.mark.asyncio
async def test_get_expert_summarizes_detail(monkeypatch):
    expert_id = "persistent_defaults"
    url = f"http://localhost:8085/api/experts/{expert_id}"
    cap = _CapturingClient(
        {
            url: {
                "id": expert_id,
                "display_name": "Persistent Defaults",
                "description": "Interactive session expert",
                "source": "bundled",
                "config": {
                    "autonomy": "partial",
                    "workspace": {"backend": "sandbox"},
                    "llm": {"model": "gpt-5"},
                    "tools": {"research": ["web_search"], "citation": []},
                },
                "effective_models": {
                    "session": {"model": "gpt-5", "source": "expert"},
                    "tactical": {"model": "gpt-5-mini", "source": "expert"},
                },
                "instructions": "Use this expert for interactive planning.",
            }
        }
    )
    monkeypatch.setattr("src.tools.orchestrator.catalog._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    get_expert = _tool_by_name(tools, "get_expert")

    result = await get_expert.ainvoke({"expert_id": expert_id})

    assert cap.gets == [(url, {})]
    assert "Expert ID: persistent_defaults" in result
    assert "Workspace backend: sandbox" in result
    assert "Enabled tool categories: research" in result
    assert "Disabled tool categories: citation" in result
    assert "session=gpt-5 (expert)" in result
    assert "Instructions preview:" in result


@pytest.mark.asyncio
async def test_search_skills_filters_catalog(monkeypatch):
    url = "http://localhost:8085/api/skills"
    cap = _CapturingClient(
        {
            url: [
                {
                    "id": "hotel-ops",
                    "name": "hotel-ops",
                    "display_name": "Hotel Operations",
                    "description": "Useful for ERP and booking workflows",
                    "source": "user",
                    "tags": ["erp", "hotel"],
                },
                {
                    "id": "paper-review",
                    "name": "paper-review",
                    "display_name": "Paper Review",
                    "description": "Academic review workflow",
                    "source": "bundled",
                    "tags": ["research"],
                },
            ]
        }
    )
    monkeypatch.setattr("src.tools.orchestrator.catalog._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    search_skills = _tool_by_name(tools, "search_skills")

    result = await search_skills.ainvoke({"query": "hotel"})

    assert cap.gets == [(url, {})]
    assert "Hotel Operations" in result
    assert "Paper Review" not in result


@pytest.mark.asyncio
async def test_get_skill_shows_file_index_and_skill_preview(monkeypatch):
    skill_id = "hotel-ops"
    url = f"http://localhost:8085/api/skills/{skill_id}"
    cap = _CapturingClient(
        {
            url: {
                "id": skill_id,
                "name": "hotel-ops",
                "display_name": "Hotel Operations",
                "description": "Useful for ERP and booking workflows",
                "source": "user",
                "files": {
                    "SKILL.md": "---\nname: hotel-ops\n---\nUse hotel ERP workflows.",
                    "references/checklist.md": "Long checklist body",
                },
            }
        }
    )
    monkeypatch.setattr("src.tools.orchestrator.catalog._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    get_skill = _tool_by_name(tools, "get_skill")

    result = await get_skill.ainvoke({"skill_id": skill_id})

    assert cap.gets == [(url, {})]
    assert "--- Skill: hotel-ops ---" in result
    assert "Files: 2" in result
    assert "SKILL.md preview:" in result
    assert "Use hotel ERP workflows." in result
    assert "Selected file contents:" not in result
