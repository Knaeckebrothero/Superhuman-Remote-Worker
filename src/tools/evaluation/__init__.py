"""Evaluation toolkit - verdict tools for critic/reviewer agents.

This toolkit provides tools for agents that review other jobs:
- approve_job: Approve a target job (transitions pending_review → completed)
- return_job_with_feedback: Resume a target job with feedback for the original agent

Tools call the orchestrator REST API via OrchestratorClient.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_evaluation_tools(context: ToolContext) -> List[Any]:
    """Create all evaluation tools with injected context.

    Args:
        context: ToolContext (must have orchestrator_url in config)

    Returns:
        List of LangChain tool functions
    """
    from .evaluation_tools import create_evaluation_tools as _create

    return _create(context)


def get_evaluation_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all evaluation tools."""
    from .evaluation_tools import EVALUATION_TOOLS_METADATA

    return EVALUATION_TOOLS_METADATA
