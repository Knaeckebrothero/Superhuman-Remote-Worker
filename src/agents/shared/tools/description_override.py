"""Apply description overrides to tools based on registry metadata.

This module enables on-demand tool description loading by replacing
full docstrings with short descriptions for deferred tools. Tools with
defer_to_workspace=True in their registry metadata will have their
descriptions shortened, with the expectation that agents will read
detailed documentation from workspace/tools/<name>.md before use.

Core tools (workspace, todo, completion) keep their full descriptions
since they're frequently used and don't need documentation lookup.
"""

import logging
from typing import Any, List

from .registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)


def apply_description_overrides(tools: List[Any]) -> List[Any]:
    """Modify tool descriptions for deferred tools.

    For tools with defer_to_workspace=True in registry metadata,
    replace the full docstring description with the short_description.

    Args:
        tools: List of LangChain tool objects from load_tools()

    Returns:
        Modified list of tools with shortened descriptions for deferred tools
    """
    modified_tools = []
    deferred_count = 0

    for tool in tools:
        tool_name = tool.name
        metadata = TOOL_REGISTRY.get(tool_name, {})

        if metadata.get("defer_to_workspace", False):
            short_desc = metadata.get("short_description")
            if short_desc:
                # Create modified tool with short description
                modified_tool = _create_tool_with_description(tool, short_desc)
                modified_tools.append(modified_tool)
                deferred_count += 1
                logger.debug(f"Tool '{tool_name}' using short description (deferred)")
            else:
                # No short description available, keep original
                modified_tools.append(tool)
                logger.warning(
                    f"Tool '{tool_name}' marked as deferred but has no short_description"
                )
        else:
            # Keep full description for core tools
            modified_tools.append(tool)

    if deferred_count > 0:
        logger.info(
            f"Applied description overrides: {deferred_count} tools deferred, "
            f"{len(tools) - deferred_count} tools with full descriptions"
        )

    return modified_tools


def _create_tool_with_description(tool: Any, new_description: str) -> Any:
    """Create a copy of a tool with a modified description.

    LangChain's @tool decorator returns StructuredTool which is a Pydantic model.
    We use model_copy() to create a modified copy with the new description.

    Args:
        tool: Original LangChain tool object
        new_description: New description to use

    Returns:
        New tool object with modified description
    """
    try:
        # StructuredTool is a Pydantic v2 model - use model_copy()
        return tool.model_copy(update={"description": new_description})
    except AttributeError:
        # Fallback for older Pydantic or different tool types
        try:
            return tool.copy(update={"description": new_description})
        except AttributeError:
            # Last resort: try direct attribute modification on a copy
            logger.warning(
                f"Could not copy tool '{tool.name}', modifying in place"
            )
            tool.description = new_description
            return tool


def get_deferred_tools() -> List[str]:
    """Get list of tool names that are deferred to workspace.

    Returns:
        List of tool names with defer_to_workspace=True
    """
    return [
        name
        for name, meta in TOOL_REGISTRY.items()
        if meta.get("defer_to_workspace", False)
    ]


def get_core_tools() -> List[str]:
    """Get list of tool names that keep full descriptions.

    Returns:
        List of tool names with defer_to_workspace=False or not set
    """
    return [
        name
        for name, meta in TOOL_REGISTRY.items()
        if not meta.get("defer_to_workspace", False)
    ]
