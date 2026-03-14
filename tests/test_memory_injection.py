"""Tests for memory injection as synthetic tool calls.

Validates that create_memory_injection_messages() and is_memory_injection_message()
work correctly, and that memory injections are properly excluded from summarization
via is_workspace_injection_message().
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.memory_injection import (
    MEMORY_TOOL_CALL_ID_PREFIX,
    create_memory_injection_messages,
    is_memory_injection_message,
)
from src.core.workspace_injection import is_workspace_injection_message


class TestCreateMemoryInjectionMessages:
    """Tests for create_memory_injection_messages()."""

    def test_creates_valid_ai_and_tool_messages(self):
        content = "--- Relevant Memories ---\n[1] some memory\n--- End ---"
        ai_msg, tool_msg = create_memory_injection_messages(content)

        assert isinstance(ai_msg, AIMessage)
        assert isinstance(tool_msg, ToolMessage)

    def test_ai_message_has_recall_memories_tool_call(self):
        ai_msg, _ = create_memory_injection_messages("test content")

        assert len(ai_msg.tool_calls) == 1
        assert ai_msg.tool_calls[0]["name"] == "recall_memories"
        assert ai_msg.tool_calls[0]["id"].startswith(MEMORY_TOOL_CALL_ID_PREFIX)

    def test_tool_message_contains_content(self):
        content = "memory block content"
        _, tool_msg = create_memory_injection_messages(content)

        assert tool_msg.content == content

    def test_tool_call_ids_match(self):
        ai_msg, tool_msg = create_memory_injection_messages("content")

        assert tool_msg.tool_call_id == ai_msg.tool_calls[0]["id"]

    def test_unique_ids_per_call(self):
        _, tool1 = create_memory_injection_messages("a")
        _, tool2 = create_memory_injection_messages("b")

        assert tool1.tool_call_id != tool2.tool_call_id


class TestIsMemoryInjectionMessage:
    """Tests for is_memory_injection_message()."""

    def test_detects_memory_tool_message(self):
        _, tool_msg = create_memory_injection_messages("content")
        assert is_memory_injection_message(tool_msg) is True

    def test_detects_memory_ai_message(self):
        ai_msg, _ = create_memory_injection_messages("content")
        assert is_memory_injection_message(ai_msg) is True

    def test_non_memory_tool_message_returns_false(self):
        msg = ToolMessage(content="result", tool_call_id="regular_call_123")
        assert is_memory_injection_message(msg) is False

    def test_non_memory_ai_message_returns_false(self):
        msg = AIMessage(content="hello")
        assert is_memory_injection_message(msg) is False

    def test_human_message_returns_false(self):
        msg = HumanMessage(content="hi")
        assert is_memory_injection_message(msg) is False

    def test_ai_message_without_tool_calls_returns_false(self):
        msg = AIMessage(content="thinking...")
        assert is_memory_injection_message(msg) is False


class TestSummarizationExclusion:
    """Tests that memory injections are excluded from summarization."""

    def test_workspace_injection_check_catches_memory_tool_message(self):
        _, tool_msg = create_memory_injection_messages("content")
        assert is_workspace_injection_message(tool_msg) is True

    def test_workspace_injection_check_catches_memory_ai_message(self):
        ai_msg, _ = create_memory_injection_messages("content")
        assert is_workspace_injection_message(ai_msg) is True

    def test_workspace_injection_still_catches_instruction_messages(self):
        from src.core.workspace_injection import create_instruction_tool_messages
        ai_msg, tool_msg = create_instruction_tool_messages("guide.md", "content")
        assert is_workspace_injection_message(ai_msg) is True
        assert is_workspace_injection_message(tool_msg) is True
