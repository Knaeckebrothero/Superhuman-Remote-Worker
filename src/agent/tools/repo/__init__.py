"""Repository toolkit — operations on attached repository datasources.

Distinct from the `git` toolkit, which is read-only and targets the internal
workspace repo. See knowledge-base/knowledge/features/self_development_workflow.md.
"""

from typing import Any, Dict, List

from agent.tools.context import ToolContext


def create_repo_tools(context: ToolContext) -> List[Any]:
    """Create all repo tools with injected context."""
    from agent.tools.repo.repo_tools import create_repo_tools as _create

    return _create(context)


def get_repo_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all repo tools."""
    from agent.tools.repo.repo_tools import REPO_TOOLS_METADATA

    return REPO_TOOLS_METADATA
