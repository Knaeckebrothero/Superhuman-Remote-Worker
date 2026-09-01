"""Delegation toolkit — built-in subagent spawning and controls.

``delegate_agent`` runs a roster child in the foreground or starts it durably
in the background.  ``wait_agent`` / ``message_agent`` / ``stop_agent`` /
``list_agents`` are the U4 control plane. Every member is created only when
``delegation.enabled`` is true AND explicitly named in ``tools.delegation``
(``grant: explicit``). See universal_experts_and_subagents.md.

The heavyweight pair (``delegate_work`` / ``resume_delegation_child``: child
JOBS on git worktree branches, the parent frozen until they finished) and the
light in-process reader (``spawn_subagent``, ``delegation.mode: light``) were
deleted in U3 WP4. A config layer that still names them in ``tools.delegation``
is mapped to ``delegate_agent`` by ``src.core.tool_policy.normalize_tool_policy``;
their settings keys are dropped by ``src.core.loader.normalize_delegation_block``.
The orchestrator-side child-job machinery is kept one release, inert — see
knowledge-base/knowledge/issues/delegation_child_machinery_retirement.md.

``reader_env`` (the per-child git worktree + re-rooted backend) stays: it is
the ``isolation: worktree`` path of ``src.subagents.child``.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional

from ..context import ToolContext


def _configured_grants(context: ToolContext) -> set[str]:
    """The resolved ``tools.delegation`` list when no caller supplies it."""
    config = getattr(context, "config", None) or {}
    tools = config.get("tools") if isinstance(config, Mapping) else None
    raw = tools.get("delegation") if isinstance(tools, Mapping) else None
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {str(name) for name in raw}


def create_delegation_tools(
    context: ToolContext,
    requested_names: Optional[Iterable[str]] = None,
) -> List[Any]:
    """Create the delegation tools with injected context.

    Returns only names in the already-resolved explicit category grant, and
    returns ``[]`` unless ``delegation.enabled`` is true. ``load_tools`` passes
    that resolved membership directly; standalone callers may put it under
    ``context.config.tools.delegation``.
    """
    from .delegate_agent import create_delegate_agent_tools
    from .control_plane import create_control_plane_tools

    granted = (
        {str(name) for name in requested_names}
        if requested_names is not None
        else _configured_grants(context)
    )
    if not granted:
        return []
    tools = [
        *create_delegate_agent_tools(context),
        *create_control_plane_tools(context),
    ]
    return [tool for tool in tools if tool.name in granted]


def get_delegation_metadata() -> Dict[str, Dict[str, Any]]:
    """Registry metadata for the delegation category."""
    from .delegate_agent import DELEGATE_AGENT_METADATA
    from .control_plane import CONTROL_PLANE_METADATA

    return {**DELEGATE_AGENT_METADATA, **CONTROL_PLANE_METADATA}
