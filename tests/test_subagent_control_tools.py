"""U4 model/config surface for durable background-subagent controls."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.tools.context import ToolContext
from agent.tools.delegation import create_delegation_tools, get_delegation_metadata
from agent.tools.delegation.control_plane import (
    CONTROL_PLANE_METADATA,
    CONTROL_TOOL_NAMES,
)
from agent.tools.registry import TOOL_REGISTRY, load_tools


ALL_DELEGATION_TOOLS = (
    "delegate_agent",
    "wait_agent",
    "message_agent",
    "stop_agent",
    "list_agents",
)


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def list_agents(self):
        self.calls.append(("list",))
        return [{"handle": "probe-7f3a", "status": "running"}]

    async def wait_agent(self, handle, timeout_s):
        self.calls.append(("wait", handle, timeout_s))
        return {"handle": handle, "status": "running", "timed_out": True}

    async def message_agent(self, handle, message):
        self.calls.append(("message", handle, message))
        return {"handle": handle, "accepted": True}

    async def stop_agent(self, handle, grace_s):
        self.calls.append(("stop", handle, grace_s))
        return {"handle": handle, "status": "stopping"}


def _context(*, enabled=True, granted=ALL_DELEGATION_TOOLS) -> ToolContext:
    return ToolContext(
        config={
            "delegation": {"enabled": enabled},
            "tools": {"delegation": list(granted)},
            "subagents": {"roster": {}},
        },
        subagent_runtime=_Runtime(),
    )


def test_registry_metadata_is_explicit_and_never_advertises_polling():
    metadata = get_delegation_metadata()
    assert set(CONTROL_TOOL_NAMES) == set(CONTROL_PLANE_METADATA)
    assert set(metadata) == set(ALL_DELEGATION_TOOLS)
    for name in CONTROL_TOOL_NAMES:
        assert TOOL_REGISTRY[name] == CONTROL_PLANE_METADATA[name]
        assert metadata[name]["category"] == "delegation"
        assert metadata[name]["grant"] == "explicit"
        assert metadata[name]["phases"] == ["strategic", "tactical"]
        assert "poll" in metadata[name]["description"].lower()


def test_factory_returns_only_the_explicit_resolved_grant_while_enabled():
    context = _context(granted=("list_agents", "message_agent"))
    assert [tool.name for tool in create_delegation_tools(context)] == [
        "message_agent",
        "list_agents",
    ]
    assert [tool.name for tool in load_tools(["wait_agent"], context)] == ["wait_agent"]
    assert create_delegation_tools(_context(enabled=False)) == []
    assert (
        create_delegation_tools(ToolContext(config={"delegation": {"enabled": True}}))
        == []
    )


def test_control_schemas_are_bounded_and_transcript_free():
    tools = {tool.name: tool for tool in create_delegation_tools(_context())}
    wait = tools["wait_agent"].tool_call_schema.model_json_schema()
    assert wait["properties"]["timeout_s"] == {
        "default": 30.0,
        "description": (
            "How long to wait, in seconds (10-3600). Use one meaningful wait "
            "only when blocked; never call in a polling loop."
        ),
        "maximum": 3600.0,
        "minimum": 10.0,
        "title": "Timeout S",
        "type": "number",
    }
    assert set(wait["properties"]) == {"handle", "timeout_s"}
    assert (
        tools["list_agents"].tool_call_schema.model_json_schema().get("properties")
        == {}
    )
    assert "transcript" in tools["list_agents"].description.lower()


@pytest.mark.asyncio
async def test_controls_call_the_runtime_with_validated_arguments():
    context = _context()
    runtime = context.subagent_runtime
    tools = {tool.name: tool for tool in create_delegation_tools(context)}

    assert await tools["list_agents"].ainvoke({}) == [
        {"handle": "probe-7f3a", "status": "running"}
    ]
    assert await tools["wait_agent"].ainvoke(
        {"handle": "probe-7f3a", "timeout_s": 120}
    ) == {"handle": "probe-7f3a", "status": "running", "timed_out": True}
    assert await tools["wait_agent"].ainvoke({"timeout_s": 10}) == {
        "handle": None,
        "status": "running",
        "timed_out": True,
    }
    assert await tools["message_agent"].ainvoke(
        {"handle": "probe-7f3a", "message": "Check the two-request race."}
    ) == {"handle": "probe-7f3a", "accepted": True}
    assert await tools["stop_agent"].ainvoke({"handle": "probe-7f3a"}) == {
        "handle": "probe-7f3a",
        "status": "stopping",
    }
    assert runtime.calls == [
        ("list",),
        ("wait", "probe-7f3a", 120.0),
        ("wait", None, 10.0),
        ("message", "probe-7f3a", "Check the two-request race."),
        ("stop", "probe-7f3a", 30.0),
    ]


@pytest.mark.asyncio
async def test_wait_bounds_and_nonempty_messages_are_schema_enforced():
    tools = {tool.name: tool for tool in create_delegation_tools(_context())}
    for timeout_s in (9.99, 3600.01):
        with pytest.raises(ValidationError):
            await tools["wait_agent"].ainvoke({"timeout_s": timeout_s})
    with pytest.raises(ValidationError):
        await tools["message_agent"].ainvoke({"handle": "probe-7f3a", "message": ""})
    for grace_s in (0, 300.01):
        with pytest.raises(ValidationError):
            await tools["stop_agent"].ainvoke(
                {"handle": "probe-7f3a", "grace_s": grace_s}
            )
