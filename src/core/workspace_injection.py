"""Helper functions for transient injection as synthetic tool calls.

This module provides utilities to inject content as fake tool call results,
making it appear as if the agent already read certain files. This approach:

1. Avoids duplication (agent won't redundantly call read_file)
2. Ensures injected content isn't included in summarization (re-injected fresh each turn)

Handles injection of:
- Todo lists (as transient HumanMessage)
- Instruction files triggered by phase transitions (active injection for enforce=false)
- Memory and knowledge injection use their own modules (memory_injection.py, knowledge_injection.py)
"""

import uuid
from typing import List, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

# Prefix for identifying synthetic instruction tool calls
# Used to exclude these messages from summarization
INSTRUCTION_TOOL_CALL_ID_PREFIX = "instruction_inject_"

# Prefix for identifying transient todo injection messages
TODOS_INJECTION_CONTENT_PREFIX = "<active_tasks>\n"


def create_todos_human_message(todos_content: str) -> HumanMessage:
    """Create a transient HumanMessage for todo list injection.

    The message is placed after summary SystemMessages and before other
    transient injections, giving the agent an up-to-date view of
    all todos (including completed items) every turn.

    Args:
        todos_content: Formatted todo list from TodoManager.format_for_injection()

    Returns:
        HumanMessage with content prefixed by TODOS_INJECTION_CONTENT_PREFIX
    """
    return HumanMessage(content=f"{TODOS_INJECTION_CONTENT_PREFIX}{todos_content}\n</active_tasks>")


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

    Used to identify and exclude instruction/todo/memory/knowledge injection
    messages from summarization, since they will be re-injected fresh after
    summarization.

    Args:
        message: A LangChain message object

    Returns:
        True if this message is a synthetic injection message
    """
    # Detect transient HumanMessage injections (todos)
    if isinstance(message, HumanMessage):
        content = getattr(message, "content", "")
        if isinstance(content, str):
            if content.startswith(TODOS_INJECTION_CONTENT_PREFIX):
                return True

    from src.core.memory_injection import MEMORY_TOOL_CALL_ID_PREFIX
    from src.core.knowledge_injection import KNOWLEDGE_TOOL_CALL_ID_PREFIX
    prefixes = (INSTRUCTION_TOOL_CALL_ID_PREFIX, MEMORY_TOOL_CALL_ID_PREFIX, KNOWLEDGE_TOOL_CALL_ID_PREFIX)

    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        return tool_call_id.startswith(prefixes)

    if isinstance(message, AIMessage):
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.get("id", "").startswith(prefixes):
                    return True

    return False
