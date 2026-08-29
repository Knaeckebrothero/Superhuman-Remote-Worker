"""Delegation toolkit — subagent spawning tools.

Three primitives (two of them on their way out):

- `delegate_agent` — the built-in subagents (U3): one bounded brief to a
  roster subagent running in-process on `run_persistent_loop`; the report
  returns as the tool result. Created only when `delegation.enabled`. See
  knowledge-base/knowledge/features/universal_experts_and_subagents.md.
- `delegate_work` / `resume_delegation_child` — the heavyweight path: spawn 1-5
  child jobs as git worktree branches; the parent suspends (checkpoint + wake)
  and resumes to review/merge results. Deleted in U3 WP4.
- `spawn_subagent` — the light in-process reader (`delegation.mode: light`).
  Deleted in U3 WP4 once `delegate_agent` replaces it in the experts.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_delegation_tools(context: ToolContext) -> List[Any]:
    """Create all delegation tools with injected context.

    Args:
        context: ToolContext (must have workspace_manager, orchestrator_client, job_metadata)

    Returns:
        List of LangChain tool functions
    """
    from .delegate_agent import create_delegate_agent_tools
    from .delegate_work import create_delegation_tools as _create_heavy
    from .spawn_subagent import create_spawn_subagent_tools

    tools = list(_create_heavy(context))
    tools.extend(create_spawn_subagent_tools(context))
    tools.extend(create_delegate_agent_tools(context))
    return tools


def get_delegation_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all delegation tools."""
    from .delegate_agent import DELEGATE_AGENT_METADATA
    from .delegate_work import DELEGATION_TOOLS_METADATA
    from .spawn_subagent import SPAWN_SUBAGENT_METADATA

    return {
        **DELEGATION_TOOLS_METADATA,
        **SPAWN_SUBAGENT_METADATA,
        **DELEGATE_AGENT_METADATA,
    }
