"""Tests for src/core/knowledge_injection.py.

Covers section 12 of persistent_agent_tests.md:
  12.1 KNOWLEDGE_TOOL_CALL_ID_PREFIX
  12.2 create_knowledge_injection_messages()
  12.3 is_knowledge_injection_message()
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.knowledge_injection import (
    KNOWLEDGE_TOOL_CALL_ID_PREFIX,
    create_knowledge_injection_messages,
    is_knowledge_injection_message,
)


# =============================================================================
# 12.1: KNOWLEDGE_TOOL_CALL_ID_PREFIX
# =============================================================================


class TestPrefix:
    """Tests for the module-level prefix constant."""

    def test_prefix_value(self):
        assert KNOWLEDGE_TOOL_CALL_ID_PREFIX == "knowledge_inject_"


# =============================================================================
# 12.2: create_knowledge_injection_messages()
# =============================================================================


class TestCreateKnowledgeInjectionMessages:
    """Tests for create_knowledge_injection_messages()."""

    def test_returns_tuple(self):
        result = create_knowledge_injection_messages("content")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_ai_and_tool_messages(self):
        ai, tool = create_knowledge_injection_messages("content")
        assert isinstance(ai, AIMessage)
        assert isinstance(tool, ToolMessage)

    def test_ai_message_has_empty_content(self):
        ai, _ = create_knowledge_injection_messages("content")
        assert ai.content == ""

    def test_ai_message_has_kb_search_tool_call(self):
        ai, _ = create_knowledge_injection_messages("content")
        assert len(ai.tool_calls) == 1
        tc = ai.tool_calls[0]
        assert tc["name"] == "kb_search"
        assert tc["args"] == {"query": "current_task_context"}

    def test_tool_call_id_starts_with_prefix(self):
        ai, _ = create_knowledge_injection_messages("content")
        tc_id = ai.tool_calls[0]["id"]
        assert tc_id.startswith(KNOWLEDGE_TOOL_CALL_ID_PREFIX)

    def test_tool_call_id_has_hex_suffix(self):
        ai, _ = create_knowledge_injection_messages("content")
        tc_id = ai.tool_calls[0]["id"]
        suffix = tc_id[len(KNOWLEDGE_TOOL_CALL_ID_PREFIX):]
        assert len(suffix) == 8
        int(suffix, 16)  # Should not raise — valid hex

    def test_tool_message_content_is_input(self):
        _, tool = create_knowledge_injection_messages("my knowledge block")
        assert tool.content == "my knowledge block"

    def test_tool_call_ids_match(self):
        ai, tool = create_knowledge_injection_messages("content")
        assert tool.tool_call_id == ai.tool_calls[0]["id"]

    def test_each_call_generates_unique_id(self):
        _, tool1 = create_knowledge_injection_messages("a")
        _, tool2 = create_knowledge_injection_messages("b")
        assert tool1.tool_call_id != tool2.tool_call_id


# =============================================================================
# 12.3: is_knowledge_injection_message()
# =============================================================================


class TestIsKnowledgeInjectionMessage:
    """Tests for is_knowledge_injection_message()."""

    def test_true_for_matching_tool_message(self):
        msg = ToolMessage(
            content="knowledge",
            tool_call_id=f"{KNOWLEDGE_TOOL_CALL_ID_PREFIX}abcd1234",
        )
        assert is_knowledge_injection_message(msg) is True

    def test_true_for_matching_ai_message(self):
        msg = AIMessage(
            content="",
            tool_calls=[{
                "name": "kb_search",
                "args": {},
                "id": f"{KNOWLEDGE_TOOL_CALL_ID_PREFIX}abcd1234",
            }],
        )
        assert is_knowledge_injection_message(msg) is True

    def test_false_for_ai_with_no_tool_calls(self):
        msg = AIMessage(content="plain response")
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_ai_with_non_matching_tool_calls(self):
        msg = AIMessage(
            content="",
            tool_calls=[{
                "name": "web_search",
                "args": {},
                "id": "regular_tool_call_123",
            }],
        )
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_non_matching_tool_message(self):
        msg = ToolMessage(
            content="result",
            tool_call_id="regular_tool_call_123",
        )
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_human_message(self):
        msg = HumanMessage(content="hello")
        assert is_knowledge_injection_message(msg) is False

    def test_false_for_system_message(self):
        msg = SystemMessage(content="system prompt")
        assert is_knowledge_injection_message(msg) is False

    def test_roundtrip_with_created_messages(self):
        """Messages created by create_* are detected by is_*."""
        ai, tool = create_knowledge_injection_messages("test")
        assert is_knowledge_injection_message(ai) is True
        assert is_knowledge_injection_message(tool) is True

    def test_handles_missing_tool_call_id(self):
        msg = ToolMessage(content="x", tool_call_id="")
        assert is_knowledge_injection_message(msg) is False

    def test_handles_ai_without_tool_calls_attr(self):
        msg = AIMessage(content="plain")
        # Ensure tool_calls is empty, not missing
        assert is_knowledge_injection_message(msg) is False
