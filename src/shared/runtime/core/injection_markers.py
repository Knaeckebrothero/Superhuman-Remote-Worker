"""Stable markers for transient message injection.

Readers can recognize injected messages without importing the services that
construct their content. Keep these values in sync through imports, not copies.
"""

INSTRUCTION_TOOL_CALL_ID_PREFIX = "instruction_inject_"
TODOS_INJECTION_CONTENT_PREFIX = "<active_tasks>\n"
MEMORY_TOOL_CALL_ID_PREFIX = "memory_inject_"
KNOWLEDGE_TOOL_CALL_ID_PREFIX = "knowledge_inject_"
CHARTER_TOOL_CALL_ID_PREFIX = "charter_inject_"
CITATION_FEEDBACK_TOOL_CALL_ID_PREFIX = "citation_feedback_inject_"
