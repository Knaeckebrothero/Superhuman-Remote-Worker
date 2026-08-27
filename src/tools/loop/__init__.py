"""Loop toolkit — campaign-scheduling tools for project-loop jobs.

Currently one tool: ``loop_plan``, the checkpoint Critic's campaign-filing
verb (knowledge-base/knowledge/features/loop_campaign_scheduling.md). The category is never listed
in any bundled expert config — the orchestrator injects it per-job via
``config_override.tools.loop`` only when spawning a planner loop's checkpoint
critic, so campaign members (whatever their role) and rotation loops never
see it. Server-side intake gating is the backup (defense in depth, mirroring
phase-restricted tools).
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_loop_tools(context: ToolContext) -> List[Any]:
    """Create loop campaign tools with injected context."""
    from .plan import create_loop_plan_tools

    return create_loop_plan_tools(context)


def get_loop_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all loop tools."""
    from .plan import LOOP_PLAN_TOOLS_METADATA

    return {**LOOP_PLAN_TOOLS_METADATA}
