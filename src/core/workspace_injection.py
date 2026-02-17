"""Helper functions for workspace.md injection as synthetic tool calls.

This module provides utilities to inject workspace.md content as a fake tool call
result, making it appear as if the agent already read the file at the start of
each conversation turn. This approach:

1. Avoids duplication (agent won't redundantly call read_file("workspace.md"))
2. Removes workspace content from system prompt (cleaner separation)
3. Ensures workspace isn't included in summarization (re-injected fresh each turn)

Also handles injection of instruction files triggered by phase transitions
(active injection for enforce=false entries with phase triggers).
"""

import uuid
from typing import List, Tuple

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# Prefix for identifying synthetic workspace tool calls
# Used to exclude these messages from summarization
WORKSPACE_TOOL_CALL_ID_PREFIX = "workspace_init_"
INSTRUCTION_TOOL_CALL_ID_PREFIX = "instruction_inject_"


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


def create_instruction_tool_messages(
    file_path: str,
    content: str,
) -> Tuple[AIMessage, ToolMessage]:
    """Create synthetic AIMessage + ToolMessage pair for instruction file injection.

    Creates a fake tool call that makes it appear as if the agent already
    called read_file on the instruction file and received the content.
    Used for active injection (enforce=false) of phase-triggered instruction files.

    Args:
        file_path: Workspace-relative path of the instruction file
        content: Content of the instruction file

    Returns:
        Tuple of (AIMessage with tool_call, ToolMessage with instruction content)
    """
    tool_call_id = f"{INSTRUCTION_TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:8]}"

    ai_message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"path": file_path},
                "id": tool_call_id,
            }
        ],
    )

    tool_message = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
    )

    return ai_message, tool_message


def is_workspace_injection_message(message: BaseMessage) -> bool:
    """Check if a message is part of a transient injection pair.

    Used to identify and exclude workspace/instruction injection messages from
    summarization, since they will be re-injected fresh after summarization.

    Args:
        message: A LangChain message object

    Returns:
        True if this message is a synthetic injection message
    """
    prefixes = (WORKSPACE_TOOL_CALL_ID_PREFIX, INSTRUCTION_TOOL_CALL_ID_PREFIX)

    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        return tool_call_id.startswith(prefixes)

    if isinstance(message, AIMessage):
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.get("id", "").startswith(prefixes):
                    return True

    return False
