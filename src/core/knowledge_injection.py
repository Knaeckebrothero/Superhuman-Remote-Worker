"""Helper functions for knowledge base injection as synthetic tool calls.

Mirrors memory_injection.py pattern. Injects project knowledge (summary +
retrieved notes) as a fake ``kb_search`` tool-call result so the agent sees
relevant project context each turn without polluting real conversation history.

The messages are transient — re-injected fresh every execute() call and
excluded from summarization via is_knowledge_injection_message().
"""

import uuid
from typing import Tuple

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# Prefix for identifying synthetic knowledge injection tool calls
KNOWLEDGE_TOOL_CALL_ID_PREFIX = "knowledge_inject_"


def create_knowledge_injection_messages(
    content: str,
) -> Tuple[AIMessage, ToolMessage]:
    """Create synthetic AIMessage + ToolMessage pair for knowledge injection.

    Creates a fake tool call that makes it appear as if the agent already
    called ``kb_search`` and received the result.

    Args:
        content: Formatted knowledge block from KnowledgeStore.assemble_knowledge_block()

    Returns:
        Tuple of (AIMessage with tool_call, ToolMessage with knowledge content)
    """
    tool_call_id = f"{KNOWLEDGE_TOOL_CALL_ID_PREFIX}{uuid.uuid4().hex[:8]}"

    ai_message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "kb_search",
                "args": {"query": "current_task_context"},
                "id": tool_call_id,
            }
        ],
    )

    tool_message = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
    )

    return ai_message, tool_message


def is_knowledge_injection_message(message: BaseMessage) -> bool:
    """Check if a message is part of a knowledge injection pair.

    Args:
        message: A LangChain message object

    Returns:
        True if this message is a synthetic knowledge injection message
    """
    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        return tool_call_id.startswith(KNOWLEDGE_TOOL_CALL_ID_PREFIX)

    if isinstance(message, AIMessage):
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.get("id", "").startswith(KNOWLEDGE_TOOL_CALL_ID_PREFIX):
                    return True

    return False
