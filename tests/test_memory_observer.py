"""Tests for MemoryObserver (Memory Light Phase 3).

Validates extraction prompt parsing, should_observe() interval logic,
observe() flow with mocked LLM, phase boundary observation, and
message formatting for the extraction LLM.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.memory_observer import (
    EXTRACTION_PROMPT,
    MemoryObserver,
    _get_message_role,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_observer(interval: int = 5) -> MemoryObserver:
    """Create a MemoryObserver with mocked dependencies."""
    mock_recall = AsyncMock()
    mock_recall.store = AsyncMock(return_value="mem-uuid-123")
    mock_llm = AsyncMock()
    return MemoryObserver(
        recall_store=mock_recall,
        llm=mock_llm,
        observer_interval=interval,
    )


def _make_llm_response(memories: list) -> MagicMock:
    """Create a mock LLM response containing JSON array."""
    response = MagicMock()
    response.content = json.dumps(memories)
    return response


def _sample_memories():
    """Return a typical extraction response."""
    return [
        {
            "content": "The users table uses soft deletes via deleted_at.",
            "summary": "Users table has soft deletes",
            "keywords": ["users", "soft_delete", "deleted_at"],
            "importance": 0.8,
            "type": "factual",
        },
        {
            "content": "Rate limiting is configured at 100 req/min per API key.",
            "summary": "Rate limit: 100 req/min",
            "keywords": ["rate_limit", "api"],
            "importance": 0.6,
            "type": "procedural",
        },
    ]


# =============================================================================
# should_observe() tests
# =============================================================================


class TestShouldObserve:
    """Tests for the interval-based observation trigger."""

    def test_triggers_at_interval(self):
        obs = _make_observer(interval=5)
        assert obs.should_observe(5) is True

    def test_does_not_trigger_between_intervals(self):
        obs = _make_observer(interval=5)
        assert obs.should_observe(1) is False
        assert obs.should_observe(3) is False
        assert obs.should_observe(4) is False

    def test_triggers_at_multiples(self):
        obs = _make_observer(interval=5)
        assert obs.should_observe(5) is True
        assert obs.should_observe(10) is True
        assert obs.should_observe(15) is True

    def test_zero_turn_never_triggers(self):
        obs = _make_observer(interval=5)
        assert obs.should_observe(0) is False

    def test_negative_turn_never_triggers(self):
        obs = _make_observer(interval=5)
        assert obs.should_observe(-1) is False

    def test_does_not_retrigger_same_turn(self):
        obs = _make_observer(interval=5)
        assert obs.should_observe(5) is True
        # After observe() runs, _last_observed_turn = 5
        obs._last_observed_turn = 5
        assert obs.should_observe(5) is False

    def test_interval_of_one(self):
        obs = _make_observer(interval=1)
        assert obs.should_observe(1) is True
        assert obs.should_observe(2) is True
        assert obs.should_observe(3) is True


# =============================================================================
# _parse_extraction_response() tests
# =============================================================================


class TestParseExtractionResponse:
    """Tests for JSON parsing robustness."""

    def test_clean_json_array(self):
        obs = _make_observer()
        result = obs._parse_extraction_response(json.dumps(_sample_memories()))
        assert len(result) == 2
        assert result[0]["content"] == "The users table uses soft deletes via deleted_at."

    def test_markdown_code_fence(self):
        obs = _make_observer()
        raw = "```json\n" + json.dumps(_sample_memories()) + "\n```"
        result = obs._parse_extraction_response(raw)
        assert len(result) == 2

    def test_markdown_fence_no_language(self):
        obs = _make_observer()
        raw = "```\n" + json.dumps(_sample_memories()) + "\n```"
        result = obs._parse_extraction_response(raw)
        assert len(result) == 2

    def test_json_with_surrounding_text(self):
        obs = _make_observer()
        raw = "Here are the memories:\n" + json.dumps(_sample_memories()) + "\nDone."
        result = obs._parse_extraction_response(raw)
        assert len(result) == 2

    def test_empty_array(self):
        obs = _make_observer()
        result = obs._parse_extraction_response("[]")
        assert result == []

    def test_invalid_json(self):
        obs = _make_observer()
        result = obs._parse_extraction_response("not json at all")
        assert result == []

    def test_missing_content_field(self):
        obs = _make_observer()
        raw = json.dumps([{"summary": "no content", "importance": 0.5}])
        result = obs._parse_extraction_response(raw)
        assert result == []

    def test_empty_content_field(self):
        obs = _make_observer()
        raw = json.dumps([{"content": "", "importance": 0.5}])
        result = obs._parse_extraction_response(raw)
        assert result == []

    def test_importance_clamped_to_range(self):
        obs = _make_observer()
        raw = json.dumps([
            {"content": "test", "importance": 1.5},
            {"content": "test2", "importance": -0.3},
        ])
        result = obs._parse_extraction_response(raw)
        assert result[0]["importance"] == 1.0
        assert result[1]["importance"] == 0.0

    def test_invalid_importance_defaults(self):
        obs = _make_observer()
        raw = json.dumps([{"content": "test", "importance": "not-a-number"}])
        result = obs._parse_extraction_response(raw)
        assert result[0]["importance"] == 0.5

    def test_invalid_type_defaults_to_factual(self):
        obs = _make_observer()
        raw = json.dumps([{"content": "test", "type": "invalid_type"}])
        result = obs._parse_extraction_response(raw)
        assert result[0]["type"] == "factual"

    def test_valid_types_preserved(self):
        obs = _make_observer()
        for valid_type in ["factual", "procedural", "error_solution", "vocabulary", "relational"]:
            raw = json.dumps([{"content": "test", "type": valid_type}])
            result = obs._parse_extraction_response(raw)
            assert result[0]["type"] == valid_type

    def test_keywords_lowercased_and_capped(self):
        obs = _make_observer()
        raw = json.dumps([{
            "content": "test",
            "keywords": ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],
        }])
        result = obs._parse_extraction_response(raw)
        assert all(k == k.lower() for k in result[0]["keywords"])
        assert len(result[0]["keywords"]) <= 8

    def test_summary_truncated(self):
        obs = _make_observer()
        raw = json.dumps([{"content": "test", "summary": "x" * 200}])
        result = obs._parse_extraction_response(raw)
        assert len(result[0]["summary"]) <= 100

    def test_non_dict_items_skipped(self):
        obs = _make_observer()
        raw = json.dumps([{"content": "valid"}, "not a dict", 42, None])
        result = obs._parse_extraction_response(raw)
        assert len(result) == 1


# =============================================================================
# _format_messages_for_extraction() tests
# =============================================================================


class TestFormatMessages:
    """Tests for conversation formatting."""

    def test_formats_human_and_ai_messages(self):
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there"),
        ]
        result = MemoryObserver._format_messages_for_extraction(messages)
        assert "[User] Hello" in result
        assert "[Agent] Hi there" in result

    def test_skips_empty_messages(self):
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content=""),
            HumanMessage(content="World"),
        ]
        result = MemoryObserver._format_messages_for_extraction(messages)
        assert "[User] Hello" in result
        assert "[User] World" in result
        lines = [l for l in result.split("\n\n") if l.strip()]
        assert len(lines) == 2

    def test_truncates_long_tool_results(self):
        messages = [
            ToolMessage(content="x" * 2000, tool_call_id="tc_123"),
        ]
        result = MemoryObserver._format_messages_for_extraction(messages)
        assert "... [truncated]" in result
        # Content should be roughly 1000 chars + truncation marker
        assert len(result) < 1100

    def test_skips_workspace_injection_messages(self):
        from src.core.workspace_injection import create_workspace_tool_messages
        ai_msg, tool_msg = create_workspace_tool_messages("workspace content")
        messages = [
            HumanMessage(content="real message"),
            ai_msg,
            tool_msg,
        ]
        result = MemoryObserver._format_messages_for_extraction(messages)
        assert "workspace content" not in result
        assert "[User] real message" in result

    def test_skips_memory_injection_messages(self):
        from src.core.memory_injection import create_memory_injection_messages
        ai_msg, tool_msg = create_memory_injection_messages("memory content")
        messages = [
            HumanMessage(content="real message"),
            ai_msg,
            tool_msg,
        ]
        result = MemoryObserver._format_messages_for_extraction(messages)
        assert "memory content" not in result
        assert "[User] real message" in result

    def test_ai_with_tool_calls_shows_tool_names(self):
        msg = AIMessage(
            content="Let me check that.",
            tool_calls=[{"name": "read_file", "args": {}, "id": "tc_1"}],
        )
        result = MemoryObserver._format_messages_for_extraction([msg])
        assert "read_file" in result


# =============================================================================
# observe() integration tests (mocked LLM)
# =============================================================================


class TestObserve:
    """Tests for the full observe() flow."""

    @pytest.mark.asyncio
    async def test_extracts_and_stores_memories(self):
        obs = _make_observer(interval=5)
        obs.llm.ainvoke = AsyncMock(return_value=_make_llm_response(_sample_memories()))

        messages = [
            HumanMessage(content="How does the users table work?"),
            AIMessage(content="The users table uses soft deletes via deleted_at column."),
        ]

        result = await obs.observe(messages, current_turn=5, phase=2)

        assert len(result) == 2
        assert obs.recall_store.store.call_count == 2

        # Verify first store call
        first_call = obs.recall_store.store.call_args_list[0]
        assert first_call.kwargs["source"] == "observer"
        assert first_call.kwargs["source_phase"] == 2
        assert first_call.kwargs["source_turn_end"] == 5

    @pytest.mark.asyncio
    async def test_updates_last_observed_turn(self):
        obs = _make_observer(interval=5)
        obs.llm.ainvoke = AsyncMock(return_value=_make_llm_response([]))

        await obs.observe(
            [HumanMessage(content="test")],
            current_turn=5,
        )

        assert obs._last_observed_turn == 5

    @pytest.mark.asyncio
    async def test_empty_messages_returns_empty(self):
        obs = _make_observer(interval=5)
        result = await obs.observe([], current_turn=5)
        assert result == []
        obs.llm.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self):
        obs = _make_observer(interval=5)
        obs.llm.ainvoke = AsyncMock(side_effect=Exception("LLM down"))

        messages = [HumanMessage(content="test")]
        result = await obs.observe(messages, current_turn=5)

        assert result == []

    @pytest.mark.asyncio
    async def test_store_failure_continues(self):
        """Individual store failures should not abort the loop."""
        obs = _make_observer(interval=5)
        obs.llm.ainvoke = AsyncMock(return_value=_make_llm_response(_sample_memories()))
        # First store succeeds, second fails
        obs.recall_store.store = AsyncMock(side_effect=[
            "mem-id-1",
            Exception("DB error"),
        ])

        messages = [HumanMessage(content="test")]
        result = await obs.observe(messages, current_turn=5)

        # Should still return all extracted memories
        assert len(result) == 2
        assert obs.recall_store.store.call_count == 2


# =============================================================================
# observe_phase_boundary() tests
# =============================================================================


class TestObservePhaseBoundary:
    """Tests for phase boundary observation."""

    @pytest.mark.asyncio
    async def test_extracts_from_recent_messages(self):
        obs = _make_observer()
        obs.llm.ainvoke = AsyncMock(return_value=_make_llm_response(_sample_memories()))

        messages = [
            HumanMessage(content="Phase work item 1"),
            AIMessage(content="Completed phase work"),
        ]

        result = await obs.observe_phase_boundary(messages, phase=3)

        assert len(result) == 2
        # Verify phase is passed to store
        first_call = obs.recall_store.store.call_args_list[0]
        assert first_call.kwargs["source_phase"] == 3

    @pytest.mark.asyncio
    async def test_empty_messages_returns_empty(self):
        obs = _make_observer()
        result = await obs.observe_phase_boundary([], phase=1)
        assert result == []

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        obs = _make_observer()
        obs.llm.ainvoke = AsyncMock(side_effect=Exception("fail"))

        result = await obs.observe_phase_boundary(
            [HumanMessage(content="test")], phase=1,
        )
        assert result == []


# =============================================================================
# _get_message_role() tests
# =============================================================================


class TestGetMessageRole:
    """Tests for role label extraction."""

    def test_human_message(self):
        assert _get_message_role(HumanMessage(content="hi")) == "User"

    def test_ai_message_without_tools(self):
        assert _get_message_role(AIMessage(content="hi")) == "Agent"

    def test_ai_message_with_tools(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {}, "id": "tc_1"}],
        )
        role = _get_message_role(msg)
        assert "read_file" in role

    def test_tool_message(self):
        msg = ToolMessage(content="result", tool_call_id="tc_1")
        assert _get_message_role(msg) == "Tool Result"


# =============================================================================
# Extraction prompt sanity check
# =============================================================================


class TestExtractionPrompt:
    """Basic validation of the extraction prompt constant."""

    def test_prompt_not_empty(self):
        assert len(EXTRACTION_PROMPT) > 100

    def test_prompt_mentions_json(self):
        assert "JSON" in EXTRACTION_PROMPT

    def test_prompt_mentions_all_types(self):
        for mem_type in ["factual", "procedural", "error_solution", "vocabulary", "relational"]:
            assert mem_type in EXTRACTION_PROMPT

    def test_prompt_mentions_importance(self):
        assert "importance" in EXTRACTION_PROMPT
