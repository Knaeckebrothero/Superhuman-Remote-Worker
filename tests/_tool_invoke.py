"""Direct-invocation helper for tools that declare ``InjectedToolCallId``.

LangChain refuses a plain-dict ``.ainvoke({...})`` on such tools — the input
must be a full ToolCall envelope, and the output comes back as a ToolMessage.
Kept out of conftest so importing it is explicit; deliberately unreachable
from ``src/`` (test-only seam, same convention as ``_fs_backend``).
"""

from typing import Any, Dict


async def invoke_tool(tool: Any, args: Dict[str, Any], call_id: str = "test-call-1"):
    """Invoke ``tool`` with a full ToolCall envelope; return its string result."""
    message = await tool.ainvoke(
        {
            "name": tool.name,
            "type": "tool_call",
            "id": call_id,
            "args": dict(args),
        }
    )
    return getattr(message, "content", message)
