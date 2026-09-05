"""Tests for persistent-session expert and skill catalog tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent.tools.context import ToolContext  # noqa: E402
from agent.tools.orchestrator import create_orchestrator_tools  # noqa: E402
from agent.tools.registry import get_tools_by_category  # noqa: E402


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


def test_catalog_tools_have_separate_category():
    catalog = set(get_tools_by_category("agent_catalog"))
    fleet = set(get_tools_by_category("orchestrator"))

    expected = {
        "list_experts",
        "get_expert",
        "list_skills",
        "search_skills",
        "get_skill",
    }
    assert expected <= catalog
    assert expected.isdisjoint(fleet)

    # The four bundle tools left this category on 2026-08-03 for
    # `catalog_authoring`, so `agent_catalog` is now reads-only and a
    # category-level `true` on it is safe without an exception list.
    authoring = set(get_tools_by_category("catalog_authoring"))
    assert {
        "get_expert_bundle",
        "set_expert_bundle",
        "get_skill_bundle",
        "set_skill_bundle",
    } <= authoring
    assert authoring.isdisjoint(catalog)
    assert authoring.isdisjoint(fleet)


class _CapturingClient:
    def __init__(self, responses=None):
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[tuple[str, dict]] = []
        self.responses = responses or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        return self._response(url)

    async def post(self, url, json=None, files=None, **kwargs):
        self.posts.append((url, {"json": json, "files": files}))
        return self._response(url)

    async def put(self, url, json=None, **kwargs):
        self.puts.append((url, json or {}))
        return self._response(url)

    def _response(self, url):
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
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

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
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

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
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

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
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    get_skill = _tool_by_name(tools, "get_skill")

    result = await get_skill.ainvoke({"skill_id": skill_id})

    assert cap.gets == [(url, {})]
    assert "--- Skill: hotel-ops ---" in result
    assert "Files: 2" in result
    assert "SKILL.md preview:" in result
    assert "Use hotel ERP workflows." in result
    assert "Selected file contents:" not in result


@pytest.mark.asyncio
async def test_get_expert_bundle_returns_portable_json(monkeypatch):
    expert_id = "developer"
    url = f"http://localhost:8085/api/experts/{expert_id}/export"
    cap = _CapturingClient(
        {
            url: {
                "name": "developer",
                "display_name": "Developer",
                "expert_type": "worker",
                "description": "Builds software",
                "icon": "code",
                "color": "#6B7280",
                "tags": ["code"],
                "config": {"tools": {"shell": ["run_command"]}},
                "prompts": {"instructions": "Build carefully."},
            }
        }
    )
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    get_expert_bundle = _tool_by_name(tools, "get_expert_bundle")

    result = await get_expert_bundle.ainvoke({"expert_id": expert_id})
    payload = json.loads(result)

    assert cap.gets == [(url, {})]
    assert payload["kind"] == "expert_bundle"
    assert payload["bundle_hash"]
    assert payload["bundle"]["name"] == "developer"
    assert payload["bundle"]["config"] == {"tools": {"shell": ["run_command"]}}


@pytest.mark.asyncio
async def test_set_expert_bundle_update_sends_only_editable_fields(monkeypatch):
    expert_id = "11111111-1111-1111-1111-111111111111"
    url = f"http://localhost:8085/api/experts/{expert_id}"
    cap = _CapturingClient({url: {"id": expert_id}})
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    set_expert_bundle = _tool_by_name(tools, "set_expert_bundle")

    result = await set_expert_bundle.ainvoke(
        {
            "mode": "update",
            "target_id": expert_id,
            "dry_run": False,
            "bundle": {
                "bundle": {
                    "name": "cannot-change",
                    "display_name": "Updated",
                    "expert_type": "worker",
                    "description": "New description",
                    "config": {"tools": {"research": ["web_search"]}},
                    "prompts": {"instructions": "New instructions."},
                }
            },
        }
    )

    assert "Expert update succeeded." in result
    assert len(cap.puts) == 1
    assert cap.puts[0][0] == url
    body = cap.puts[0][1]
    assert body["display_name"] == "Updated"
    assert body["description"] == "New description"
    assert body["config"] == {"tools": {"research": ["web_search"]}}
    assert body["prompts"] == {"instructions": "New instructions."}
    assert "name" not in body
    assert "expert_type" not in body
    assert not cap.posts


@pytest.mark.asyncio
async def test_set_skill_bundle_dry_run_validates_file_tree(monkeypatch):
    cap = _CapturingClient()
    monkeypatch.setattr(
        "agent.tools.orchestrator.catalog._get_client", lambda **kw: cap
    )

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    set_skill_bundle = _tool_by_name(tools, "set_skill_bundle")

    result = await set_skill_bundle.ainvoke(
        {
            "mode": "create",
            "bundle": {
                "display_name": "Hotel Ops",
                "files": {
                    "SKILL.md": (
                        "---\n"
                        "name: hotel-ops\n"
                        "description: Use for hotel ERP workflows.\n"
                        "---\n"
                        "Follow hotel operations practices.\n"
                    )
                },
            },
        }
    )

    assert "Dry run OK: would create skill." in result
    assert "Name: hotel-ops" in result
    assert "Bundle hash:" in result
    assert not cap.posts
    assert not cap.puts


class TestFalseIsLegibleToTheAgent:
    """`tools.<c>: false` must reach the agent as *disabled*, not as silence.

    ``_format_expert_detail`` renders the **stored** expert row — no
    normalisation runs on this path, so every policy spelling arrives verbatim.
    The old `disabled` predicate was ``value == []``, and ``False == []`` is
    ``False`` in Python, so an expert authored with ``tools.shell: false``
    appeared in neither the Enabled nor the Disabled line: the one place an
    agent can read another expert's tool policy said nothing at all about a
    category its author had deliberately turned off.

    This is the prerequisite the cockpit work needed before the settings forms
    could write ``true``/``false`` instead of name lists.
    """

    @staticmethod
    def _lines(tools: dict) -> str:
        from agent.tools.orchestrator.catalog import _format_expert_detail

        return _format_expert_detail("e1", {"id": "e1", "config": {"tools": tools}})

    def test_false_renders_as_disabled(self):
        out = self._lines({"shell": False})
        assert "Disabled tool categories: shell" in out
        assert "Enabled tool categories" not in out

    def test_the_legacy_empty_list_still_renders_as_disabled(self):
        assert "Disabled tool categories: citation" in self._lines({"citation": []})

    def test_true_renders_as_enabled(self):
        out = self._lines({"orchestrator": True})
        assert "Enabled tool categories: orchestrator" in out
        assert "Disabled tool categories" not in out

    def test_an_only_mapping_renders_as_enabled(self):
        out = self._lines({"shell": {"only": ["run_command"]}})
        assert "Enabled tool categories: shell" in out

    def test_every_key_lands_in_exactly_one_line(self):
        out = self._lines(
            {"shell": False, "citation": [], "research": True, "canvas": ["get_canvas"]}
        )
        enabled = next(
            line for line in out.splitlines() if line.startswith("Enabled tool")
        )
        disabled = next(
            line for line in out.splitlines() if line.startswith("Disabled tool")
        )
        assert enabled == "Enabled tool categories: canvas, research"
        assert disabled == "Disabled tool categories: citation, shell"
