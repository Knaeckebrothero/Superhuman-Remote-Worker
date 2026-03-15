"""Tests for workspace injection — synthetic tool messages for transient injection.

Tests create_todos_human_message, create_instruction_tool_messages,
and is_workspace_injection_message.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.workspace_injection import (
    INSTRUCTION_TOOL_CALL_ID_PREFIX,
    TODOS_INJECTION_CONTENT_PREFIX,
    create_todos_human_message,
    create_instruction_tool_messages,
    is_workspace_injection_message,
)


# =============================================================================
# create_todos_human_message
# =============================================================================


class TestCreateTodosHumanMessage:
    """Tests for todo list injection as transient HumanMessage."""

    def test_returns_human_message(self):
        msg = create_todos_human_message("- [ ] Task 1")
        assert isinstance(msg, HumanMessage)

    def test_content_has_prefix(self):
        msg = create_todos_human_message("- [ ] Task 1")
        assert msg.content.startswith(TODOS_INJECTION_CONTENT_PREFIX)

    def test_content_has_closing_tag(self):
        msg = create_todos_human_message("todos here")
        assert msg.content.endswith("</active_tasks>")

    def test_contains_todo_content(self):
        msg = create_todos_human_message("- [x] Done\n- [ ] Pending")
        assert "- [x] Done" in msg.content
        assert "- [ ] Pending" in msg.content


# =============================================================================
# create_instruction_tool_messages
# =============================================================================


class TestCreateInstructionToolMessages:
    """Tests for instruction file injection as fake tool call."""

    def test_returns_ai_and_tool_message_pair(self):
        ai_msg, tool_msg = create_instruction_tool_messages("guide.md", "# Guide")
        assert isinstance(ai_msg, AIMessage)
        assert isinstance(tool_msg, ToolMessage)

    def test_tool_call_id_has_instruction_prefix(self):
        ai_msg, tool_msg = create_instruction_tool_messages("file.md", "content")
        tc_id = ai_msg.tool_calls[0]["id"]
        assert tc_id.startswith(INSTRUCTION_TOOL_CALL_ID_PREFIX)
        assert tool_msg.tool_call_id == tc_id

    def test_tool_call_uses_correct_path(self):
        ai_msg, _ = create_instruction_tool_messages("instructions/todo_guide.md", "x")
        tc = ai_msg.tool_calls[0]
        assert tc["args"]["path"] == "instructions/todo_guide.md"

    def test_tool_message_contains_instruction_content(self):
        _, tool_msg = create_instruction_tool_messages("f.md", "instruction body")
        assert tool_msg.content == "instruction body"


# =============================================================================
# is_workspace_injection_message
# =============================================================================


class TestIsWorkspaceInjectionMessage:
    """Tests for detecting transient injection messages."""

    def test_detects_instruction_tool_message(self):
        _, tool_msg = create_instruction_tool_messages("f.md", "x")
        assert is_workspace_injection_message(tool_msg) is True

    def test_detects_instruction_ai_message(self):
        ai_msg, _ = create_instruction_tool_messages("f.md", "x")
        assert is_workspace_injection_message(ai_msg) is True

    def test_detects_todos_human_message(self):
        msg = create_todos_human_message("todos")
        assert is_workspace_injection_message(msg) is True

    def test_regular_human_message_not_injection(self):
        msg = HumanMessage(content="Hello, help me with this task.")
        assert is_workspace_injection_message(msg) is False

    def test_regular_ai_message_not_injection(self):
        msg = AIMessage(content="Sure, I'll help you.")
        assert is_workspace_injection_message(msg) is False

    def test_regular_tool_message_not_injection(self):
        msg = ToolMessage(content="file contents", tool_call_id="call_abc123")
        assert is_workspace_injection_message(msg) is False

    def test_system_message_not_injection(self):
        msg = SystemMessage(content="You are an agent.")
        assert is_workspace_injection_message(msg) is False

    def test_ai_with_regular_tool_calls_not_injection(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {}, "id": "call_regular"}],
        )
        assert is_workspace_injection_message(msg) is False
