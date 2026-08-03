"""Helper functions for transient injection as synthetic tool calls.

This module provides utilities to inject content as fake tool call results,
making it appear as if the agent already read certain files. This approach:

1. Avoids duplication (agent won't redundantly call read_file)
2. Ensures injected content isn't included in summarization (re-injected fresh each turn)

Handles injection of:
- Todo lists (as transient HumanMessage)
- Instruction files delivered once at a concrete phase start
- Memory and knowledge injection use their own modules (memory_injection.py, knowledge_injection.py)
"""

import hashlib
from typing import List, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

# Prefix for identifying synthetic instruction tool calls
# Used to exclude these messages from summarization
INSTRUCTION_TOOL_CALL_ID_PREFIX = "instruction_inject_"

# Prefix for identifying transient todo injection messages
TODOS_INJECTION_CONTENT_PREFIX = "<active_tasks>\n"


def content_hash_id(content: str) -> str:
    """Deterministic 8-hex-char id suffix derived from the injected content.

    Injection tool_call_ids must be deterministic (not uuid4): identical
    injected content must produce a byte-identical request payload so
    provider prompt caches can reuse the prefix, and so payloads are
    reproducible when debugging. Each injection type is injected at most
    once per request under a distinct prefix, so collisions within one
    payload are not possible.
    """
    return hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:8]


def find_tail_injection_anchor(messages: List[BaseMessage]) -> int:
    """Index at which to insert the transient injection block, at the tail.

    Transient injections (todos, memory, knowledge, citation feedback,
    instruction files) are placed AFTER the conversation, not before it:
    provider prompt caches match on a strict left-to-right prefix, so a
    block that changes every turn must sit below the stable history or it
    invalidates the cache for everything after it.

    The anchor is the position just after the last Human/Tool message
    rather than blindly ``len(messages)``: the injected memory/knowledge
    pairs are synthetic ``AIMessage(tool_call)`` + ``ToolMessage`` turns,
    and Gemini rejects a function-call turn that does not immediately
    follow a user or function-response turn ("Please ensure that function
    call turn comes immediately after a user turn or after a function
    response turn."). Every real path into an LLM call ends the history
    with a HumanMessage (new task/turn, transition, reminder) or the
    ToolMessages of the previous iteration — so the anchor is normally the
    end of the list; the walk-back only matters for degenerate histories
    that end in a bare model turn.
    """
    for i in range(len(messages), 0, -1):
        if isinstance(messages[i - 1], (HumanMessage, ToolMessage)):
            return i
    return len(messages)


def create_todos_human_message(todos_content: str) -> HumanMessage:
    """Create a transient HumanMessage for todo list injection.

    The message is placed at the very end of the request payload — after
    the conversation and the other transient injections. The todo list is
    the agent's current "query", and models weight the end of the prompt
    highest (see Anthropic's long-context guidance: query at the end).
    It gives the agent an up-to-date view of all todos (including
    completed items) every turn.

    Args:
        todos_content: Formatted todo list from TodoManager.format_for_injection()

    Returns:
        HumanMessage with content prefixed by TODOS_INJECTION_CONTENT_PREFIX
    """
    return HumanMessage(
        content=f"{TODOS_INJECTION_CONTENT_PREFIX}{todos_content}\n</active_tasks>"
    )


def create_instruction_tool_messages(
    file_path: str,
    content: str,
) -> Tuple[AIMessage, ToolMessage]:
    """Create synthetic AIMessage + ToolMessage pair for instruction file injection.

    Creates a fake tool call that makes it appear as if the agent already
    called read_file on the instruction file and received the content.
    Used for checkpointed, once-per-phase-start instruction delivery.

    Args:
        file_path: Workspace-relative path of the instruction file
        content: Content of the instruction file

    Returns:
        Tuple of (AIMessage with tool_call, ToolMessage with instruction content)
    """
    id_suffix = content_hash_id(file_path + "\n" + content)
    tool_call_id = f"{INSTRUCTION_TOOL_CALL_ID_PREFIX}{id_suffix}"

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
    from src.core.knowledge_injection import (
        CHARTER_TOOL_CALL_ID_PREFIX,
        KNOWLEDGE_TOOL_CALL_ID_PREFIX,
    )
    from src.core.citation_feedback_injection import (
        CITATION_FEEDBACK_TOOL_CALL_ID_PREFIX,
    )

    prefixes = (
        INSTRUCTION_TOOL_CALL_ID_PREFIX,
        MEMORY_TOOL_CALL_ID_PREFIX,
        KNOWLEDGE_TOOL_CALL_ID_PREFIX,
        CHARTER_TOOL_CALL_ID_PREFIX,
        CITATION_FEEDBACK_TOOL_CALL_ID_PREFIX,
    )

    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        return tool_call_id.startswith(prefixes)

    if isinstance(message, AIMessage):
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.get("id", "").startswith(prefixes):
                    return True

    return False
