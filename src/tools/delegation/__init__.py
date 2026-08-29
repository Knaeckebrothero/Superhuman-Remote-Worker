"""Delegation toolkit — the built-in subagents.

One tool: ``delegate_agent`` (U3) — one bounded brief to a roster subagent
running in-process on ``run_persistent_loop``; the child's report returns as
the tool result. Created only when ``delegation.enabled`` is true AND the
config names the tool in ``tools.delegation`` (``grant: explicit``). See
knowledge-base/knowledge/features/universal_experts_and_subagents.md.

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

from typing import Any, Dict, List

from ..context import ToolContext


def create_delegation_tools(context: ToolContext) -> List[Any]:
    """Create the delegation tools with injected context.

    Returns ``[]`` unless ``delegation.enabled`` is true on the context's
    tool config — the binding follows the settings flag as well as the
    ``tools.delegation`` grant (B.12).
    """
    from .delegate_agent import create_delegate_agent_tools

    return list(create_delegate_agent_tools(context))


def get_delegation_metadata() -> Dict[str, Dict[str, Any]]:
    """Registry metadata for the delegation category."""
    from .delegate_agent import DELEGATE_AGENT_METADATA

    return dict(DELEGATE_AGENT_METADATA)
