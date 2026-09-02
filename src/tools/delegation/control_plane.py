"""Model-facing controls for durable background subagents (U4).

The tools stay deliberately thin.  The per-parent :class:`SubagentRuntime`
owns addressability, fencing, durable revival, stop synthesis and bounded
status rendering; this module only validates the public arguments and calls
that runtime.  Importing the runtime remains lazy through ``ensure_runtime``
so the tools registry does not close the registry -> delegation -> subagents
-> persistent-graph import cycle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..context import ToolContext


CONTROL_TOOL_NAMES = (
    "wait_agent",
    "message_agent",
    "stop_agent",
    "list_agents",
)


def _metadata(
    description: str,
    short_description: str,
) -> Dict[str, Any]:
    return {
        "module": "delegation.control_plane",
        "description": description,
        "short_description": short_description,
        "category": "delegation",
        "phases": ["strategic", "tactical"],
        "grant": "explicit",
        "gate": (
            "named outright in a tools.delegation list AND "
            "delegation.enabled is true; children never receive delegation "
            "or its control plane"
        ),
    }


CONTROL_PLANE_METADATA: Dict[str, Dict[str, Any]] = {
    "wait_agent": {
        **_metadata(
            "Wait for one background subagent, or for any background "
            "subagent when handle is omitted. Use this only when its next "
            "update is immediately blocking your work. Completion reports "
            "are pushed into your next turn automatically, so do not poll "
            "with wait_agent or list_agents. Timeout is 10-3600 seconds.",
            "Wait once for a blocking subagent update; never poll.",
        ),
        "function": "wait_agent",
    },
    "message_agent": {
        **_metadata(
            "Send a concise steering message to an addressable background "
            "subagent by handle. A queued or running child accepts steering. "
            "A terminal child is durably revived on its existing transcript "
            "and worktree with a new fenced generation. Consume its prior "
            "report first. The next report is pushed automatically; do not "
            "poll after messaging.",
            "Steer a live child or durably revive a terminal one.",
        ),
        "function": "message_agent",
    },
    "stop_agent": {
        **_metadata(
            "Ask a background subagent to stop by handle. It gets a bounded "
            "grace window for a tool-less partial synthesis before a hard "
            "stop. The terminal report is pushed automatically; do not poll "
            "for it.",
            "Stop a background subagent after a bounded synthesis grace.",
        ),
        "function": "stop_agent",
    },
    "list_agents": {
        **_metadata(
            "List this parent's addressable background subagents as a "
            "bounded status view. It never returns transcripts or full "
            "reports. Completions are pushed automatically; use this for an "
            "occasional roster check, never as a polling loop.",
            "List bounded background-subagent statuses; never transcripts.",
        ),
        "function": "list_agents",
    },
}


def create_control_plane_tools(context: ToolContext) -> List[Any]:
    """Create all four controls when delegation is enabled.

    Membership is filtered against the expert's explicit grant by
    :func:`src.tools.delegation.create_delegation_tools`; returning the full
    enabled family here keeps each StructuredTool factory uncomplicated.
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    from .delegate_agent import _delegation_settings, ensure_runtime

    if _delegation_settings(context).get("enabled") is not True:
        return []

    class WaitAgentInput(BaseModel):
        handle: Optional[str] = Field(
            default=None,
            description=(
                "One background subagent handle. Omit to wait for the next "
                "update from any child."
            ),
        )
        timeout_s: float = Field(
            default=30.0,
            ge=10.0,
            le=3600.0,
            description=(
                "How long to wait, in seconds (10-3600). Use one meaningful "
                "wait only when blocked; never call in a polling loop."
            ),
        )

    class MessageAgentInput(BaseModel):
        handle: str = Field(description="The target subagent handle.")
        message: str = Field(
            min_length=1,
            description=(
                "A concise follow-up or correction for the child. Include "
                "enough context to act without reading the parent transcript."
            ),
        )

    class StopAgentInput(BaseModel):
        handle: str = Field(description="The target subagent handle.")
        grace_s: float = Field(
            default=30.0,
            ge=0.1,
            le=300.0,
            description=(
                "Seconds allowed for a tool-less partial synthesis before a "
                "hard stop (0.1-300)."
            ),
        )

    class ListAgentsInput(BaseModel):
        pass

    async def _wait_agent(
        handle: Optional[str] = None,
        timeout_s: float = 30.0,
    ) -> Dict[str, Any]:
        runtime = ensure_runtime(context)
        return await runtime.wait_agent(
            str(handle).strip() if handle and str(handle).strip() else None,
            float(timeout_s),
        )

    async def _message_agent(handle: str, message: str) -> Dict[str, Any]:
        runtime = ensure_runtime(context)
        return await runtime.message_agent(str(handle).strip(), str(message))

    async def _stop_agent(
        handle: str,
        grace_s: float = 30.0,
    ) -> Dict[str, Any]:
        runtime = ensure_runtime(context)
        return await runtime.stop_agent(str(handle).strip(), float(grace_s))

    async def _list_agents() -> List[Dict[str, Any]]:
        runtime = ensure_runtime(context)
        return await runtime.list_agents()

    return [
        StructuredTool.from_function(
            coroutine=_wait_agent,
            name="wait_agent",
            description=CONTROL_PLANE_METADATA["wait_agent"]["description"],
            args_schema=WaitAgentInput,
        ),
        StructuredTool.from_function(
            coroutine=_message_agent,
            name="message_agent",
            description=CONTROL_PLANE_METADATA["message_agent"]["description"],
            args_schema=MessageAgentInput,
        ),
        StructuredTool.from_function(
            coroutine=_stop_agent,
            name="stop_agent",
            description=CONTROL_PLANE_METADATA["stop_agent"]["description"],
            args_schema=StopAgentInput,
        ),
        StructuredTool.from_function(
            coroutine=_list_agents,
            name="list_agents",
            description=CONTROL_PLANE_METADATA["list_agents"]["description"],
            args_schema=ListAgentsInput,
        ),
    ]


__all__ = [
    "CONTROL_PLANE_METADATA",
    "CONTROL_TOOL_NAMES",
    "create_control_plane_tools",
]
