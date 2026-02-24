"""Tests for shell state injection as transient SystemMessage.

Validates that create_shell_state_message() produces a SystemMessage with
usage instructions and tab content, and that shell injections are properly
excluded from summarization via is_workspace_injection_message().
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.shell_injection import (
    SHELL_INJECTION_CONTENT_PREFIX,
    create_shell_state_message,
)
from src.core.workspace_injection import is_workspace_injection_message


class TestCreateShellStateMessage:
    """Tests for create_shell_state_message()."""

    def test_creates_system_message(self):
        content = "[shell] (shell)\n$ echo hello\nhello"
        msg = create_shell_state_message(content, tab_count=1)
        assert isinstance(msg, SystemMessage)

    def test_message_starts_with_prefix(self):
        msg = create_shell_state_message("[shell] (shell)\n$ ls", tab_count=1)
        assert msg.content.startswith(SHELL_INJECTION_CONTENT_PREFIX)

    def test_message_contains_terminal_state_tags(self):
        msg = create_shell_state_message("[shell] (shell)\n$ ls", tab_count=1)
        assert msg.content.startswith("<terminal_state>\n")
        assert msg.content.endswith("</terminal_state>")

    def test_message_contains_tab_content(self):
        content = "[shell] (shell)\n$ echo hello\nhello"
        msg = create_shell_state_message(content, tab_count=1)
        assert content in msg.content

    def test_message_contains_usage_instructions(self):
        """SystemMessage includes tool usage guidance."""
        msg = create_shell_state_message("[shell] (shell)\n$ ls", tab_count=1)
        assert "shell(command, tab)" in msg.content
        assert "shell_send" in msg.content
        assert "shell_read" in msg.content
        assert "shell_open" in msg.content

    def test_message_explains_new_output_flag(self):
        msg = create_shell_state_message("[shell] (shell)", tab_count=1)
        assert "[NEW OUTPUT]" in msg.content

    def test_message_mentions_tmux(self):
        msg = create_shell_state_message("[shell] (shell)", tab_count=1)
        assert "tmux" in msg.content


class TestSummarizationExclusion:
    """Tests that shell injections are excluded from summarization."""

    def test_workspace_injection_check_catches_shell_message(self):
        msg = create_shell_state_message("content", tab_count=1)
        assert is_workspace_injection_message(msg) is True

    def test_regular_system_message_not_caught(self):
        msg = SystemMessage(content="You are a helpful assistant.")
        assert is_workspace_injection_message(msg) is False

    def test_regular_human_message_not_caught(self):
        msg = HumanMessage(content="hello")
        assert is_workspace_injection_message(msg) is False

    def test_regular_tool_message_not_caught(self):
        msg = ToolMessage(content="result", tool_call_id="regular_call_123")
        assert is_workspace_injection_message(msg) is False

    def test_regular_ai_message_not_caught(self):
        msg = AIMessage(content="hello")
        assert is_workspace_injection_message(msg) is False

    def test_workspace_injection_still_catches_workspace_messages(self):
        from src.core.workspace_injection import create_workspace_tool_messages
        ai_msg, tool_msg = create_workspace_tool_messages("workspace content")
        assert is_workspace_injection_message(ai_msg) is True
        assert is_workspace_injection_message(tool_msg) is True

    def test_workspace_injection_still_catches_memory_messages(self):
        from src.core.memory_injection import create_memory_injection_messages
        ai_msg, tool_msg = create_memory_injection_messages("memory content")
        assert is_workspace_injection_message(ai_msg) is True
        assert is_workspace_injection_message(tool_msg) is True

    def test_workspace_injection_still_catches_todos_messages(self):
        from src.core.workspace_injection import create_todos_human_message
        msg = create_todos_human_message("todo content")
        assert is_workspace_injection_message(msg) is True
