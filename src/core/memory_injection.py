"""Helper functions for memory injection as synthetic tool calls.

Mirrors workspace_injection.py pattern. Injects recalled memories as a fake
``recall_memories`` tool-call result so the agent sees relevant past context
each turn without polluting real conversation history.

The messages are transient — re-injected fresh every execute() call and
excluded from summarization via is_memory_injection_message().
"""

import uuid
from typing import Tuple

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# Prefix for identifying synthetic memory injection tool calls
# Used to exclude these messages from summarization
MEMORY_TOOL_CALL_ID_PREFIX = "memory_inject_"


def create_memory_injection_messages(
    content: str,
) -> Tuple[AIMessage, ToolMessage]:
    """Create synthetic AIMessage + ToolMessage pair for memory injection.

    Creates a fake tool call that makes it appear as if the agent already
    called ``recall_memories`` and received the result.

    Args:
        content: Formatted memory block from RecallStore.assemble_memory_block()

    Returns:
        Tuple of (AIMessage with tool_call, ToolMessage with memory content)
    """
    tool_call_id = f"{MEMORY_TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:8]}"

    ai_message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "recall_memories",
                "args": {"context": "current_task"},
                "id": tool_call_id,
            }
        ],
    )

    tool_message = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
    )

    return ai_message, tool_message


def is_memory_injection_message(message: BaseMessage) -> bool:
    """Check if a message is part of a memory injection pair.

    Args:
        message: A LangChain message object

    Returns:
        True if this message is a synthetic memory injection message
    """
    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        return tool_call_id.startswith(MEMORY_TOOL_CALL_ID_PREFIX)

    if isinstance(message, AIMessage):
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.get("id", "").startswith(MEMORY_TOOL_CALL_ID_PREFIX):
                    return True

    return False
