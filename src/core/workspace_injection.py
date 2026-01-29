"""Helper functions for workspace.md injection as synthetic tool calls.

This module provides utilities to inject workspace.md content as a fake tool call
result, making it appear as if the agent already read the file at the start of
each conversation turn. This approach:

1. Avoids duplication (agent won't redundantly call read_file("workspace.md"))
2. Removes workspace content from system prompt (cleaner separation)
3. Ensures workspace isn't included in summarization (re-injected fresh each turn)
"""

import uuid
from typing import Tuple

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# Prefix for identifying synthetic workspace tool calls
# Used to exclude these messages from summarization
WORKSPACE_TOOL_CALL_ID_PREFIX = "workspace_init_"


def create_workspace_tool_messages(workspace_content: str) -> Tuple[AIMessage, ToolMessage]:
    """Create synthetic AIMessage + ToolMessage pair for workspace injection.

    Creates a fake tool call that makes it appear as if the agent already
    called read_file("workspace.md") and received the content.

    Args:
        workspace_content: Content of workspace.md file

    Returns:
        Tuple of (AIMessage with tool_call, ToolMessage with workspace content)
    """
    # Generate unique tool_call_id with identifiable prefix
    tool_call_id = f"{WORKSPACE_TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:8]}"

    # Create AIMessage with tool_calls
    ai_message = AIMessage(
        content="",  # Empty content - just a tool call
        tool_calls=[
            {
                "name": "read_file",
                "args": {"path": "workspace.md"},
                "id": tool_call_id,
            }
        ],
    )

    # Create matching ToolMessage
    tool_message = ToolMessage(
        content=workspace_content,
        tool_call_id=tool_call_id,
    )

    return ai_message, tool_message


def is_workspace_injection_message(message: BaseMessage) -> bool:
    """Check if a message is part of the workspace injection pair.

    Used to identify and exclude workspace injection messages from
    summarization, since they will be re-injected fresh after summarization.

    Args:
        message: A LangChain message object

    Returns:
        True if this message is a synthetic workspace injection message
    """
    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        return tool_call_id.startswith(WORKSPACE_TOOL_CALL_ID_PREFIX)

    if isinstance(message, AIMessage):
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.get("id", "").startswith(WORKSPACE_TOOL_CALL_ID_PREFIX):
                    return True

    return False
