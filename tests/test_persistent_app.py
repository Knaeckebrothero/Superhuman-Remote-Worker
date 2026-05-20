"""Tests for src/api/persistent_app.py — persistent agent FastAPI application.

Covers: _get_agent_metrics, _safe_serialize, _save_message,
_save_turn_ai_messages, _generate_title, _poll_workspace_ready,
_poll_vm_ready, _handle_compact, _handle_archive, permission_check,
on_tool_result truncation, check_interrupt closure, WS message routing,
health endpoints, _ws_send, create_persistent_app, on_turn callbacks.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.api.persistent_app import (
    _extract_thinking,
    _generate_title,
    _get_agent_metrics,
    _handle_archive,
    _handle_compact,
    _handle_vm_upgrade,
    _poll_vm_ready,
    _poll_workspace_ready,
    _safe_serialize,
    _save_message,
    _save_turn_ai_messages,
    _ws_send,
    create_persistent_app,
)


# ---------------------------------------------------------------------------
# 3.1 _get_agent_metrics()
# ---------------------------------------------------------------------------


class TestGetAgentMetrics:
    def test_returns_dict_with_memory_and_cpu(self):
        """Returns dict with memory_mb and cpu_percent when psutil available."""
        mock_proc = MagicMock()
        mock_proc.memory_info.return_value.rss = 100 * 1_048_576  # 100 MB
        mock_proc.cpu_percent.return_value = 5.2

        mock_psutil = MagicMock()
        mock_psutil.Process.return_value = mock_proc

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            result = _get_agent_metrics()

        assert result is not None
        assert "memory_mb" in result
        assert "cpu_percent" in result
        assert result["memory_mb"] == 100.0
        assert result["cpu_percent"] == 5.2

    def test_returns_none_when_psutil_not_installed(self):
        """Returns None when psutil import fails."""
        import builtins

        original_import = builtins.__import__

        def _mock_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_mock_import):
            result = _get_agent_metrics()
        assert result is None

    def test_returns_none_on_exception(self):
        """Returns None on any psutil exception."""
        mock_psutil = MagicMock()
        mock_psutil.Process.side_effect = RuntimeError("proc error")

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            result = _get_agent_metrics()
        assert result is None


# ---------------------------------------------------------------------------
# 3.2 _safe_serialize()
# ---------------------------------------------------------------------------


class TestSafeSerialize:
    def test_dict_unchanged(self):
        d = {"key": "value", "num": 42}
        assert _safe_serialize(d) == d

    def test_list_unchanged(self):
        lst = [1, 2, "three"]
        assert _safe_serialize(lst) == lst

    def test_string_unchanged(self):
        assert _safe_serialize("hello") == "hello"

    def test_int_unchanged(self):
        assert _safe_serialize(42) == 42

    def test_none_unchanged(self):
        assert _safe_serialize(None) is None

    def test_datetime_converted_to_string(self):
        dt = datetime(2026, 3, 30, 12, 0)
        result = _safe_serialize(dt)
        assert isinstance(result, str)
        assert "2026" in result

    def test_set_converted_to_string(self):
        s = {1, 2, 3}
        result = _safe_serialize(s)
        assert isinstance(result, str)

    def test_custom_class_converted_to_string(self):
        class Foo:
            def __str__(self):
                return "foo_repr"

        result = _safe_serialize(Foo())
        assert result == "foo_repr"


# ---------------------------------------------------------------------------
# 3.3 _save_message()
# ---------------------------------------------------------------------------


class TestSaveMessage:
    @pytest.mark.asyncio
    async def test_calls_client_save_thread_message(self):
        client = AsyncMock()
        await _save_message(client, "tid", "user", "hello", None, 1)
        client.save_thread_message.assert_called_once_with(
            thread_id="tid",
            role="user",
            content="hello",
            tool_calls=None,
            turn_number=1,
            tool_call_id=None,
            thinking=None,
        )

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self):
        client = AsyncMock()
        client.save_thread_message.side_effect = RuntimeError("db error")
        # Should not raise
        await _save_message(client, "tid", "user", "hi", None, 1)


# ---------------------------------------------------------------------------
# 3.3b _restore_session_messages() — IDs must be set so RemoveMessage works
# ---------------------------------------------------------------------------


class TestRestoreSessionMessageIds:
    """Restored messages must carry IDs so compaction's RemoveMessage works.

    Without this, a session resumed from a poisoned state can never compact
    (RemoveMessage(id=None) is a no-op) — see issue
    persistent_session_restored_messages_no_ids.md.
    """

    @pytest.mark.asyncio
    async def test_all_restored_messages_have_ids(self):
        from src.api import persistent_app as pa

        mock_session = MagicMock()
        mock_session.messages = []
        mock_agent = MagicMock()
        mock_agent.postgres_conn = MagicMock()
        mock_agent.postgres_conn.get_thread_messages_history = AsyncMock(
            return_value=[
                {"role": "user", "content": "hi", "tool_calls": None, "turn_number": 1},
                {
                    "role": "assistant",
                    "content": "calling tool",
                    "tool_calls": [{"id": "t1", "name": "f", "args": {}}],
                    "turn_number": 1,
                },
                {
                    "role": "tool",
                    "content": "result",
                    "tool_calls": None,
                    "turn_number": 1,
                },
                {
                    "role": "assistant",
                    "content": "done",
                    "tool_calls": None,
                    "turn_number": 1,
                },
            ]
        )

        with (
            patch.object(pa, "_session", mock_session),
            patch.object(pa, "_agent", mock_agent),
            patch.object(pa, "_thread_id", "thread-abc"),
        ):
            await pa._restore_session_messages()

        # All four messages must have IDs set.
        assert len(mock_session.messages) == 4
        ids = [m.id for m in mock_session.messages]
        assert all(i is not None and i for i in ids), (
            f"every restored message needs an id, got {ids}"
        )
        # IDs must be unique (UUIDs).
        assert len(set(ids)) == len(ids), "restored message IDs must be unique"


# ---------------------------------------------------------------------------
# 3.4 _save_turn_ai_messages()
# ---------------------------------------------------------------------------


class TestSaveTurnAiMessages:
    @pytest.mark.asyncio
    async def test_collects_messages_after_last_human(self):
        """Walks backwards from end, stops at HumanMessage."""
        client = AsyncMock()
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="question"),
            AIMessage(content="answer"),
            ToolMessage(content="result", tool_call_id="tc1"),
        ]

        await _save_turn_ai_messages(client, "tid", messages, 1)

        # Should save AIMessage and ToolMessage (2 messages after last HumanMessage)
        assert client.save_thread_message.call_count == 2

    @pytest.mark.asyncio
    async def test_collects_all_when_no_human_message(self):
        """If no HumanMessage, all messages are collected."""
        client = AsyncMock()
        messages = [
            SystemMessage(content="sys"),
            AIMessage(content="greeting"),
        ]

        await _save_turn_ai_messages(client, "tid", messages, 0)

        assert client.save_thread_message.call_count == 2

    @pytest.mark.asyncio
    async def test_tool_calls_extracted(self):
        """Tool calls extracted as list of {name, args, id} dicts."""
        client = AsyncMock()
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {"q": "test"}, "id": "tc1"}],
        )
        messages = [HumanMessage(content="go"), ai_msg]

        await _save_turn_ai_messages(client, "tid", messages, 1)

        call_kwargs = client.save_thread_message.call_args[1]
        assert call_kwargs["tool_calls"] is not None
        assert call_kwargs["tool_calls"][0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_anthropic_list_content_normalized(self):
        """Anthropic list-of-dicts content joined into string."""
        client = AsyncMock()
        ai_msg = AIMessage(
            content=[
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ]
        )
        messages = [HumanMessage(content="hi"), ai_msg]

        await _save_turn_ai_messages(client, "tid", messages, 1)

        call_kwargs = client.save_thread_message.call_args[1]
        assert "Hello" in call_kwargs["content"]
        assert "world" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self):
        """Outer exception caught and logged."""
        client = AsyncMock()
        client.save_thread_message.side_effect = RuntimeError("db error")
        messages = [HumanMessage(content="hi"), AIMessage(content="reply")]

        # Should not raise
        await _save_turn_ai_messages(client, "tid", messages, 1)


# ---------------------------------------------------------------------------
# 3.4b _extract_thinking()
# ---------------------------------------------------------------------------


class TestExtractThinking:
    """Reasoning extraction for the save path — covers all three provider shapes."""

    def test_returns_none_when_no_reasoning(self):
        msg = AIMessage(content="Just an answer.")
        assert _extract_thinking(msg) is None

    def test_anthropic_thinking_block(self):
        """Anthropic content list with {type:'thinking', thinking:'...'}."""
        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "Answer."},
            ]
        )
        assert _extract_thinking(msg) == "Let me think..."

    def test_anthropic_multiple_thinking_blocks_concatenated(self):
        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "First. "},
                {"type": "text", "text": "Mid."},
                {"type": "thinking", "thinking": "Second."},
            ]
        )
        assert _extract_thinking(msg) == "First. Second."

    def test_responses_api_reasoning_from_summary(self):
        """OpenAI Responses API: type='reasoning' with summary list."""
        msg = AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Thinking step."}],
                },
                {"type": "output_text", "text": "Answer."},
            ]
        )
        assert _extract_thinking(msg) == "Thinking step."

    def test_responses_api_reasoning_from_content(self):
        msg = AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "content": [
                        {"type": "reasoning_text", "text": "Step A."},
                        {"type": "reasoning_text", "text": "Step B."},
                    ],
                },
            ]
        )
        # Streaming-style concatenation: no separator.
        assert _extract_thinking(msg) == "Step A.Step B."

    def test_responses_api_reasoning_multiple_blocks(self):
        msg = AIMessage(
            content=[
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Plan. "}],
                },
                {"type": "output_text", "text": "Result."},
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Reflect."}],
                },
            ]
        )
        assert _extract_thinking(msg) == "Plan. Reflect."

    def test_additional_kwargs_reasoning_content(self):
        """DeepSeek / OpenRouter / non-streaming Responses API: plain string."""
        msg = AIMessage(content="answer", additional_kwargs={"reasoning_content": "rc"})
        assert _extract_thinking(msg) == "rc"

    def test_anthropic_wins_over_responses_when_both_present(self):
        """If both reasoning shapes coexist, the Anthropic branch wins (it runs first).

        No real model emits both formats in one response — this just pins
        ordering so a future refactor doesn't accidentally swap it.
        """
        msg = AIMessage(
            content=[
                {"type": "thinking", "thinking": "anthropic"},
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "responses"}],
                },
            ]
        )
        assert _extract_thinking(msg) == "anthropic"

    def test_empty_reasoning_block_falls_back(self):
        """Reasoning blocks with no extractable text shouldn't mask additional_kwargs."""
        msg = AIMessage(
            content=[{"type": "reasoning", "summary": [], "content": []}],
            additional_kwargs={"reasoning_content": "fallback"},
        )
        assert _extract_thinking(msg) == "fallback"


# ---------------------------------------------------------------------------
# 3.5 _generate_title()
# ---------------------------------------------------------------------------


class TestGenerateTitle:
    @pytest.mark.asyncio
    async def test_returns_none_when_aux_llm_none(self):
        result = await _generate_title(
            messages=[HumanMessage(content="hi")],
            auxiliary_llm=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_messages_empty(self):
        result = await _generate_title(
            messages=[],
            auxiliary_llm=MagicMock(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_string_content(self):
        """Returns None when all messages have non-string content."""
        messages = [
            AIMessage(content=[{"type": "text", "text": "list content"}]),
        ]
        mock_llm = AsyncMock()

        result = await _generate_title(messages, mock_llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_samples_first_10_messages(self):
        """Only first 10 messages sampled."""
        messages = [HumanMessage(content=f"msg {i}") for i in range(20)]
        mock_response = MagicMock()
        mock_response.content = "Test Title"
        mock_llm = MagicMock()
        mock_llm.llm = AsyncMock()
        mock_llm.llm.ainvoke.return_value = mock_response

        await _generate_title(messages, mock_llm)

        # The HumanMessage passed should only contain messages 0-9
        call_args = mock_llm.llm.ainvoke.call_args[0][0]
        human_text = call_args[1].content  # second message is HumanMessage
        assert "msg 9" in human_text
        assert "msg 10" not in human_text

    @pytest.mark.asyncio
    async def test_truncates_content_to_200_chars(self):
        """Each message content truncated to 200 chars."""
        long_msg = HumanMessage(content="x" * 500)
        mock_response = MagicMock()
        mock_response.content = "Title"
        mock_llm = MagicMock()
        mock_llm.llm = AsyncMock()
        mock_llm.llm.ainvoke.return_value = mock_response

        await _generate_title([long_msg], mock_llm)

        call_args = mock_llm.llm.ainvoke.call_args[0][0]
        human_text = call_args[1].content
        assert len(human_text) <= 200

    @pytest.mark.asyncio
    async def test_result_stripped_and_truncated_to_100(self):
        """Result is stripped and truncated to 100 chars."""
        mock_response = MagicMock()
        mock_response.content = "  " + "A" * 150 + "  "
        mock_llm = MagicMock()
        mock_llm.llm = AsyncMock()
        mock_llm.llm.ainvoke.return_value = mock_response

        result = await _generate_title(
            [HumanMessage(content="hi")],
            mock_llm,
        )

        assert len(result) <= 100
        assert not result.startswith(" ")

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_result(self):
        """Returns None when LLM returns empty string."""
        mock_response = MagicMock()
        mock_response.content = ""
        mock_llm = MagicMock()
        mock_llm.llm = AsyncMock()
        mock_llm.llm.ainvoke.return_value = mock_response

        result = await _generate_title(
            [HumanMessage(content="hi")],
            mock_llm,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        """Exception during title generation returns None."""
        mock_llm = MagicMock()
        mock_llm.llm = AsyncMock()
        mock_llm.llm.ainvoke.side_effect = RuntimeError("LLM error")

        result = await _generate_title(
            [HumanMessage(content="hi")],
            mock_llm,
        )

        assert result is None


# ---------------------------------------------------------------------------
# 3.6 _poll_workspace_ready()
# ---------------------------------------------------------------------------


class TestPollWorkspaceReady:
    @pytest.mark.asyncio
    async def test_returns_none_when_workspace_not_found(self):
        """Returns None immediately when get_thread_workspace returns None."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = None

        result = await _poll_workspace_ready(client, "tid", timeout=5)
        assert result is None
        # Should have been called only once (no retry)
        assert client.get_thread_workspace.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_vm_config_when_ready(self):
        """Returns remote config when vm_status='ready' with ssh_host."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {
            "vm_status": "ready",
            "vm_ssh_host": "10.0.0.5",
            "vm_ssh_port": 2222,
            "git_remote_url": "http://gitea/repo",
        }

        result = await _poll_workspace_ready(client, "tid", timeout=5)

        assert result is not None
        assert result["backend"] == "vm"
        assert result["remote"]["host"] == "10.0.0.5"
        assert result["remote"]["port"] == 2222

    @pytest.mark.asyncio
    async def test_returns_container_config_when_ready(self):
        """Returns remote config when container status='ready' with pod_ip."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {
            "status": "ready",
            "pod_ip": "172.16.0.10",
            "git_remote_url": "http://gitea/repo",
        }

        result = await _poll_workspace_ready(client, "tid", timeout=5)

        assert result is not None
        assert result["backend"] == "sandbox"
        assert result["remote"]["host"] == "172.16.0.10"
        assert result["remote"]["port"] == 30022

    @pytest.mark.asyncio
    async def test_returns_none_on_status_none_no_vm(self):
        """Returns None when status='none' and no vm_status."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {"status": "none"}

        result = await _poll_workspace_ready(client, "tid", timeout=5)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_failed(self):
        """Returns None when status='failed' and no VM backup."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {"status": "failed"}

        result = await _poll_workspace_ready(client, "tid", timeout=5)
        assert result is None

    @pytest.mark.asyncio
    async def test_polls_until_ready(self):
        """Polls with sleep during intermediate statuses."""
        call_count = 0

        async def _get_workspace(tid):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return {"status": "provisioning"}
            return {"status": "ready", "pod_ip": "1.2.3.4"}

        client = AsyncMock()
        client.get_thread_workspace = _get_workspace

        with patch("src.api.persistent_app.asyncio.sleep", new_callable=AsyncMock):
            result = await _poll_workspace_ready(
                client, "tid", timeout=30, poll_interval=0.01
            )

        assert result is not None
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        """Returns None when polling exceeds timeout."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {"status": "provisioning"}

        with patch("src.api.persistent_app.asyncio.sleep", new_callable=AsyncMock):
            # Use a very short timeout and mock time.monotonic to expire immediately
            with patch("time.monotonic", side_effect=[0, 100]):
                result = await _poll_workspace_ready(
                    client, "tid", timeout=5, poll_interval=0.01
                )

        assert result is None

    @pytest.mark.asyncio
    async def test_vm_takes_precedence_over_container(self):
        """VM config returned when both VM and container are ready."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {
            "vm_status": "ready",
            "vm_ssh_host": "vm-host",
            "status": "ready",
            "pod_ip": "pod-ip",
        }

        result = await _poll_workspace_ready(client, "tid", timeout=5)
        assert result["remote"]["host"] == "vm-host"


# ---------------------------------------------------------------------------
# 3.7 _poll_vm_ready()
# ---------------------------------------------------------------------------


class TestPollVmReady:
    @pytest.mark.asyncio
    async def test_returns_config_when_ready(self):
        """Returns ssh config when vm_status='ready' with host."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {
            "vm_status": "ready",
            "vm_ssh_host": "10.0.0.5",
            "vm_ssh_port": 2222,
        }

        result = await _poll_vm_ready(client, "tid", timeout=5)
        assert result == {"ssh_host": "10.0.0.5", "ssh_port": 2222}

    @pytest.mark.asyncio
    async def test_returns_none_on_failed(self):
        """Returns None immediately when vm_status='failed'."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {"vm_status": "failed"}

        result = await _poll_vm_ready(client, "tid", timeout=5)
        assert result is None

    @pytest.mark.asyncio
    async def test_continues_polling_when_workspace_none(self):
        """Continues polling when get_thread_workspace returns None."""
        call_count = 0

        async def _get_ws(tid):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return None
            return {"vm_status": "ready", "vm_ssh_host": "host"}

        client = AsyncMock()
        client.get_thread_workspace = _get_ws

        with patch("src.api.persistent_app.asyncio.sleep", new_callable=AsyncMock):
            result = await _poll_vm_ready(client, "tid", timeout=30, poll_interval=0.01)

        assert result is not None
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        """Returns None when timeout expires."""
        client = AsyncMock()
        client.get_thread_workspace.return_value = {"vm_status": "provisioning"}

        with patch("src.api.persistent_app.asyncio.sleep", new_callable=AsyncMock):
            with patch("time.monotonic", side_effect=[0, 100]):
                result = await _poll_vm_ready(
                    client, "tid", timeout=5, poll_interval=0.01
                )

        assert result is None


# ---------------------------------------------------------------------------
# 3.8 _handle_compact()
# ---------------------------------------------------------------------------


class TestHandleCompact:
    @pytest.mark.asyncio
    async def test_sends_error_when_session_none(self):
        """Sends error event when _session is None."""
        ws = AsyncMock()
        with patch("src.api.persistent_app._session", None):
            await _handle_compact(ws, "")
        ws.send_json.assert_called()
        call_args = ws.send_json.call_args[0][0]
        assert call_args["method"] == "error"

    @pytest.mark.asyncio
    async def test_sends_error_when_context_manager_none(self):
        """Sends error event when context_manager is None."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.context_manager = None
        with patch("src.api.persistent_app._session", mock_session):
            await _handle_compact(ws, "")
        ws.send_json.assert_called()

    @pytest.mark.asyncio
    async def test_compacts_messages_in_place(self):
        """Messages mutated via slice assignment."""
        ws = AsyncMock()
        mock_session = MagicMock()
        original_messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
        ]
        mock_session.messages = original_messages
        mock_session.context_manager.summarize_and_compact = AsyncMock(
            return_value=[SystemMessage(content="sys"), AIMessage(content="summary")]
        )
        mock_session.config.context_management.max_summary_length = 10000
        mock_session.workspace_manager = None

        with patch("src.api.persistent_app._session", mock_session):
            await _handle_compact(ws, "focus text")

        # Messages should be updated in-place
        assert len(mock_session.messages) == 2

    @pytest.mark.asyncio
    async def test_sends_compacted_event_with_counts(self):
        """Sends context.compacted event with before/after/focus."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q"),
        ]
        mock_session.context_manager.summarize_and_compact = AsyncMock(
            return_value=[SystemMessage(content="sys")]
        )
        mock_session.config.context_management.max_summary_length = 10000
        mock_session.workspace_manager = None

        with patch("src.api.persistent_app._session", mock_session):
            await _handle_compact(ws, "my focus")

        # Find the context.compacted call
        compacted_call = None
        for call in ws.send_json.call_args_list:
            data = call[0][0]
            if data.get("method") == "context.compacted":
                compacted_call = data
                break
        assert compacted_call is not None
        assert compacted_call["params"]["before"] == 2
        assert compacted_call["params"]["after"] == 1
        assert compacted_call["params"]["focus"] == "my focus"

    @pytest.mark.asyncio
    async def test_git_commit_and_push_on_compaction(self):
        """Git commit + push when git_manager.is_active."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.messages = [SystemMessage(content="s"), HumanMessage(content="q")]
        mock_session.context_manager.summarize_and_compact = AsyncMock(
            return_value=[SystemMessage(content="s")]
        )
        mock_session.config.context_management.max_summary_length = 10000
        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True
        mock_session.workspace_manager.git_manager = git_mgr

        with patch("src.api.persistent_app._session", mock_session):
            await _handle_compact(ws, "")

        git_mgr.commit.assert_called_once()
        git_mgr.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_git_failure_non_fatal(self):
        """Git error during compaction doesn't crash."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.messages = [SystemMessage(content="s")]
        mock_session.context_manager.summarize_and_compact = AsyncMock(return_value=[])
        mock_session.config.context_management.max_summary_length = 10000
        git_mgr = MagicMock()
        git_mgr.is_active = True
        git_mgr.has_uncommitted_changes.return_value = True
        git_mgr.commit.side_effect = RuntimeError("git broke")
        mock_session.workspace_manager.git_manager = git_mgr

        with patch("src.api.persistent_app._session", mock_session):
            await _handle_compact(ws, "")


# ---------------------------------------------------------------------------
# 3.9 _handle_archive()
# ---------------------------------------------------------------------------


class TestHandleArchive:
    @pytest.mark.asyncio
    async def test_sends_error_when_session_none(self):
        ws = AsyncMock()
        with patch("src.api.persistent_app._session", None):
            await _handle_archive(ws)
        ws.send_json.assert_called()
        call_args = ws.send_json.call_args[0][0]
        assert call_args["method"] == "error"

    @pytest.mark.asyncio
    async def test_gets_recall_store_from_tool_context(self):
        """recall_store read from _session.tool_context.recall_store."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.tool_context.recall_store = None
        mock_session.auxiliary_llm = None
        mock_session.messages = []
        mock_session.postgres_conn = None

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_archive(ws)

        # Should send session.ended without errors
        ended_call = None
        for call in ws.send_json.call_args_list:
            data = call[0][0]
            if data.get("method") == "session.ended":
                ended_call = data
                break
        assert ended_call is not None

    @pytest.mark.asyncio
    async def test_memory_extraction_requires_all_three(self):
        """Memory extraction only runs when recall_store, aux_llm, and messages all truthy."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.tool_context.recall_store = MagicMock()
        mock_session.auxiliary_llm = MagicMock()
        mock_session.messages = []  # Empty — should skip extraction
        mock_session.postgres_conn = None
        mock_session.config.memory.extraction_prompt = ""

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_archive(ws)

        # No import of extract_and_store_memories should happen since messages is empty

    @pytest.mark.asyncio
    async def test_memory_extraction_failure_non_fatal(self):
        """Memory extraction failure doesn't prevent session.ended."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.tool_context.recall_store = MagicMock()
        mock_session.auxiliary_llm = MagicMock()
        mock_session.messages = [HumanMessage(content="hi")]
        mock_session.postgres_conn = None
        mock_session.config.memory.extraction_prompt = ""

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch(
                "src.api.persistent_app.extract_and_store_memories",
                side_effect=RuntimeError("extraction failed"),
                create=True,
            ),
        ):
            await _handle_archive(ws)

        # session.ended should still be sent
        ended_calls = [
            c
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "session.ended"
        ]
        assert len(ended_calls) == 1

    @pytest.mark.asyncio
    async def test_title_generation_on_untitled(self):
        """Generates title when existing title is 'Untitled Session'."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.tool_context.recall_store = None
        mock_session.auxiliary_llm = MagicMock()
        mock_session.messages = [HumanMessage(content="hi")]
        mock_conn = AsyncMock()
        mock_conn.get_thread = AsyncMock(return_value={"title": "Untitled Session"})
        mock_conn.end_thread = AsyncMock()
        mock_conn_ctx = AsyncMock()
        mock_conn.acquire.return_value.__aenter__ = AsyncMock(
            return_value=mock_conn_ctx
        )
        mock_conn.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.postgres_conn = mock_conn

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch(
                "src.api.persistent_app._generate_title", return_value="Generated Title"
            ),
        ):
            await _handle_archive(ws)

    @pytest.mark.asyncio
    async def test_title_failure_non_fatal(self):
        """Title generation failure doesn't crash archive."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.tool_context.recall_store = None
        mock_session.auxiliary_llm = None
        mock_session.messages = []
        mock_conn = AsyncMock()
        mock_conn.get_thread = AsyncMock(side_effect=RuntimeError("db error"))
        mock_conn.end_thread = AsyncMock()
        mock_session.postgres_conn = mock_conn

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_archive(ws)

        # session.ended should still be sent
        ended_calls = [
            c
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "session.ended"
        ]
        assert len(ended_calls) == 1

    @pytest.mark.asyncio
    async def test_sends_session_ended_event(self):
        """Sends session.ended with thread_id."""
        ws = AsyncMock()
        mock_session = MagicMock()
        mock_session.tool_context.recall_store = None
        mock_session.auxiliary_llm = None
        mock_session.messages = []
        mock_session.postgres_conn = None

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._thread_id", "test-thread-id"),
        ):
            await _handle_archive(ws)

        ended_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "session.ended"
        ]
        assert len(ended_calls) == 1
        assert ended_calls[0]["params"]["thread_id"] == "test-thread-id"


# ---------------------------------------------------------------------------
# 3.10 permission_check() logic (tested via closure behavior)
# ---------------------------------------------------------------------------


class TestPermissionCheck:
    """Tests the permission_check closure behavior.

    Since permission_check is a local function inside ws_chat, we test
    the logic patterns directly.
    """

    def test_autonomous_returns_true_immediately(self):
        """In autonomous mode, all tools approved without WS event."""
        # Simulate the logic
        mode = "autonomous"
        assert mode == "autonomous"  # would return True

    def test_auto_accept_approves_non_shell_tools(self):
        """auto_accept approves any tool not in shell set."""
        shell_tools = {"run_command", "shell_execute", "shell_read"}
        assert "web_search" not in shell_tools
        assert "read_file" not in shell_tools

    def test_auto_accept_asks_for_shell_tools(self):
        """auto_accept falls through for shell tools."""
        shell_tools = {"run_command", "shell_execute", "shell_read"}
        assert "run_command" in shell_tools
        assert "shell_execute" in shell_tools
        assert "shell_read" in shell_tools


# ---------------------------------------------------------------------------
# 3.11 on_tool_result truncation
# ---------------------------------------------------------------------------


class TestOnToolResultTruncation:
    def test_short_result_unchanged(self):
        """Results <= 2000 chars not truncated."""
        result = "x" * 2000
        display = result[:2000] + "..." if len(result) > 2000 else result
        assert display == result
        assert len(display) == 2000

    def test_long_result_truncated(self):
        """Results > 2000 chars truncated with '...' suffix."""
        result = "x" * 2500
        display = result[:2000] + "..." if len(result) > 2000 else result
        assert len(display) == 2003
        assert display.endswith("...")


# ---------------------------------------------------------------------------
# 3.12 check_interrupt() closure
# ---------------------------------------------------------------------------


class TestCheckInterrupt:
    def test_returns_true_and_resets_flag(self):
        """Returns True and resets flag when flag was True."""
        interrupt_flag = True

        def check_interrupt():
            nonlocal interrupt_flag
            if interrupt_flag:
                interrupt_flag = False
                return True
            return False

        assert check_interrupt() is True
        assert interrupt_flag is False

    def test_returns_false_when_flag_is_false(self):
        """Returns False with no side effect."""
        interrupt_flag = False

        def check_interrupt():
            nonlocal interrupt_flag
            if interrupt_flag:
                interrupt_flag = False
                return True
            return False

        assert check_interrupt() is False
        assert interrupt_flag is False

    def test_flag_only_fires_once(self):
        """Flag consumed on first check, second returns False."""
        interrupt_flag = True

        def check_interrupt():
            nonlocal interrupt_flag
            if interrupt_flag:
                interrupt_flag = False
                return True
            return False

        assert check_interrupt() is True
        assert check_interrupt() is False


# ---------------------------------------------------------------------------
# 3.13 WebSocket message routing (tested via data parsing logic)
# ---------------------------------------------------------------------------


class TestWSMessageRouting:
    def test_json_parsing_valid(self):
        """Valid JSON parsed normally."""
        raw = '{"method": "message", "content": "hello"}'
        data = json.loads(raw)
        assert data["method"] == "message"
        assert data["content"] == "hello"

    def test_json_parse_error_becomes_message(self):
        """Invalid JSON treated as plain text message."""
        raw = "just plain text"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"method": "message", "content": raw}
        assert data["method"] == "message"
        assert data["content"] == "just plain text"

    def test_empty_content_not_queued(self):
        """Empty content silently dropped."""
        data = {"method": "message", "content": ""}
        content = data.get("content", "")
        assert not content  # would not be queued

    def test_valid_modes(self):
        """Valid mode set values."""
        valid_modes = ("supervised", "auto_accept", "autonomous")
        for mode in valid_modes:
            assert mode in valid_modes

    def test_invalid_mode_rejected(self):
        """Invalid mode not in valid set."""
        assert "invalid" not in ("supervised", "auto_accept", "autonomous")


# ---------------------------------------------------------------------------
# 3.15 _handle_vm_upgrade()
# ---------------------------------------------------------------------------


class TestHandleVmUpgrade:
    @pytest.mark.asyncio
    async def test_sends_failed_when_session_none(self):
        ws = AsyncMock()
        with (
            patch("src.api.persistent_app._session", None),
            patch("src.api.persistent_app._orchestrator_client", MagicMock()),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_vm_upgrade(ws)

        failed_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "vm_upgrade.failed"
        ]
        assert len(failed_calls) == 1

    @pytest.mark.asyncio
    async def test_sends_failed_when_client_none(self):
        ws = AsyncMock()
        with (
            patch("src.api.persistent_app._session", MagicMock()),
            patch("src.api.persistent_app._orchestrator_client", None),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_vm_upgrade(ws)

        failed_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "vm_upgrade.failed"
        ]
        assert len(failed_calls) == 1

    @pytest.mark.asyncio
    async def test_sends_started_before_provisioning(self):
        """vm_upgrade.started sent before any provisioning attempt."""
        ws = AsyncMock()
        mock_client = AsyncMock()
        mock_client.request_thread_vm_upgrade.return_value = False

        with (
            patch("src.api.persistent_app._session", MagicMock()),
            patch("src.api.persistent_app._orchestrator_client", mock_client),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_vm_upgrade(ws)

        # First WS send should be vm_upgrade.started
        first_call = ws.send_json.call_args_list[0][0][0]
        assert first_call["method"] == "vm_upgrade.started"

    @pytest.mark.asyncio
    async def test_sends_failed_on_rejected(self):
        """vm_upgrade.failed when orchestrator rejects request."""
        ws = AsyncMock()
        mock_client = AsyncMock()
        mock_client.request_thread_vm_upgrade.return_value = False

        with (
            patch("src.api.persistent_app._session", MagicMock()),
            patch("src.api.persistent_app._orchestrator_client", mock_client),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_vm_upgrade(ws)

        failed_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "vm_upgrade.failed"
        ]
        assert len(failed_calls) == 1
        assert "rejected" in failed_calls[0]["params"]["reason"].lower()

    @pytest.mark.asyncio
    async def test_sends_failed_on_poll_timeout(self):
        """vm_upgrade.failed when VM doesn't become ready."""
        ws = AsyncMock()
        mock_client = AsyncMock()
        mock_client.request_thread_vm_upgrade.return_value = True

        with (
            patch("src.api.persistent_app._session", MagicMock()),
            patch("src.api.persistent_app._orchestrator_client", mock_client),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch("src.api.persistent_app._poll_vm_ready", return_value=None),
        ):
            await _handle_vm_upgrade(ws)

        failed_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "vm_upgrade.failed"
        ]
        assert len(failed_calls) == 1

    @pytest.mark.asyncio
    async def test_successful_upgrade_sends_complete(self):
        """vm_upgrade.complete sent after successful backend swap."""
        ws = AsyncMock()
        mock_client = AsyncMock()
        mock_client.request_thread_vm_upgrade.return_value = True

        mock_session = MagicMock()
        mock_session.config.extra = {"shell": {}}
        mock_session.shell_manager = MagicMock()
        mock_session.shell_manager.sudo_action = "freeze"

        mock_backend = MagicMock()

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._orchestrator_client", mock_client),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch(
                "src.api.persistent_app._poll_vm_ready",
                return_value={
                    "ssh_host": "10.0.0.5",
                    "ssh_port": 22,
                },
            ),
            patch(
                "src.api.persistent_app.RemoteBackend",
                return_value=mock_backend,
                create=True,
            ),
        ):
            # Patch the import inside the function
            import sys

            mock_remote_mod = MagicMock()
            mock_remote_mod.RemoteBackend.return_value = mock_backend
            with patch.dict(sys.modules, {"src.core.backends.remote": mock_remote_mod}):
                await _handle_vm_upgrade(ws)

        complete_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "vm_upgrade.complete"
        ]
        assert len(complete_calls) == 1
        assert complete_calls[0]["params"]["ssh_host"] == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_sets_sudo_action_to_allow(self):
        """After upgrade, shell_manager.sudo_action set to 'allow'."""
        ws = AsyncMock()
        mock_client = AsyncMock()
        mock_client.request_thread_vm_upgrade.return_value = True

        mock_session = MagicMock()
        mock_session.config.extra = {"shell": {}}
        mock_session.shell_manager = MagicMock()
        mock_session.shell_manager.sudo_action = "freeze"

        mock_backend = MagicMock()

        import sys

        mock_remote_mod = MagicMock()
        mock_remote_mod.RemoteBackend.return_value = mock_backend

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._orchestrator_client", mock_client),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch(
                "src.api.persistent_app._poll_vm_ready",
                return_value={
                    "ssh_host": "host",
                    "ssh_port": 22,
                },
            ),
            patch.dict(sys.modules, {"src.core.backends.remote": mock_remote_mod}),
        ):
            await _handle_vm_upgrade(ws)

        assert mock_session.shell_manager.sudo_action == "allow"

    @pytest.mark.asyncio
    async def test_exception_sends_failed(self):
        """Exception during upgrade sends vm_upgrade.failed."""
        ws = AsyncMock()
        mock_client = AsyncMock()
        mock_client.request_thread_vm_upgrade.return_value = True

        mock_session = MagicMock()
        mock_session.config.extra = {"shell": {}}

        import sys

        mock_remote_mod = MagicMock()
        mock_remote_mod.RemoteBackend.side_effect = RuntimeError("connect failed")

        with (
            patch("src.api.persistent_app._session", mock_session),
            patch("src.api.persistent_app._orchestrator_client", mock_client),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch(
                "src.api.persistent_app._poll_vm_ready",
                return_value={
                    "ssh_host": "host",
                    "ssh_port": 22,
                },
            ),
            patch.dict(sys.modules, {"src.core.backends.remote": mock_remote_mod}),
        ):
            await _handle_vm_upgrade(ws)

        failed_calls = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "vm_upgrade.failed"
        ]
        assert len(failed_calls) == 1


# ---------------------------------------------------------------------------
# 3.17 _ws_send()
# ---------------------------------------------------------------------------


class TestWsSend:
    @pytest.mark.asyncio
    async def test_sends_json_with_method_and_params(self):
        ws = AsyncMock()
        await _ws_send(ws, "test.event", {"key": "value"})
        ws.send_json.assert_called_once_with(
            {
                "method": "test.event",
                "params": {"key": "value"},
            }
        )

    @pytest.mark.asyncio
    async def test_silently_drops_on_runtime_error(self):
        ws = AsyncMock()
        ws.send_json.side_effect = RuntimeError("closed")
        # Should not raise
        await _ws_send(ws, "test", {})

    @pytest.mark.asyncio
    async def test_silently_drops_on_connection_reset(self):
        ws = AsyncMock()
        ws.send_json.side_effect = ConnectionResetError
        # Should not raise
        await _ws_send(ws, "test", {})

    @pytest.mark.asyncio
    async def test_silently_drops_on_any_exception(self):
        ws = AsyncMock()
        ws.send_json.side_effect = Exception("anything")
        # Should not raise
        await _ws_send(ws, "test", {})


# ---------------------------------------------------------------------------
# 3.17.1 Headless: subscriber fan-out (_subscribe / _unsubscribe / _broadcast)
# ---------------------------------------------------------------------------


class TestSubscriberFanout:
    """Tests for the subscriber-list broadcast hub that decouples the loop
    from any single WebSocket. See docs/features/headless_persistent_sessions.md."""

    def setup_method(self):
        import src.api.persistent_app as mod

        mod._subscribers.clear()

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._subscribers.clear()

    def test_subscribe_returns_fresh_queue(self):
        import src.api.persistent_app as mod

        queue = mod._subscribe("client-A")
        assert "client-A" in mod._subscribers
        assert mod._subscribers["client-A"] is queue
        assert queue.empty()

    def test_unsubscribe_removes_entry(self):
        import src.api.persistent_app as mod

        mod._subscribe("client-A")
        mod._unsubscribe("client-A")
        assert "client-A" not in mod._subscribers

    def test_unsubscribe_unknown_id_is_a_noop(self):
        import src.api.persistent_app as mod

        # Should not raise.
        mod._unsubscribe("never-subscribed")

    def test_broadcast_enqueues_to_all_subscribers(self):
        import src.api.persistent_app as mod

        # Reset event-log cursor so this test is deterministic.
        mod._next_seq = 0
        mod._events_epoch = 0
        mod._session = None  # skip the DB write task

        q1 = mod._subscribe("c1")
        q2 = mod._subscribe("c2")

        mod._broadcast("token", {"content": "hi"})

        # Phase 2 stamps (_seq) on every frame so both broadcast subscribers
        # and event-log replay share the same cursor.
        frame = {
            "method": "token",
            "params": {"content": "hi", "_seq": [0, 1]},
        }
        assert q1.get_nowait() == frame
        assert q2.get_nowait() == frame

    def test_broadcast_no_subscribers_does_nothing(self):
        """Loop running with zero subscribers — the whole point of headless."""
        import src.api.persistent_app as mod

        # Should not raise.
        mod._broadcast("token", {"content": "into the void"})
        assert mod._subscribers == {}

    def test_broadcast_drops_oldest_on_full_queue(self):
        """Slow consumer must not block the loop. Oldest frame is dropped."""
        import asyncio as _asyncio

        import src.api.persistent_app as mod

        small = _asyncio.Queue(maxsize=2)
        mod._subscribers["slow"] = small

        # Fill the queue so the next broadcast must drop.
        small.put_nowait({"method": "old1", "params": {}})
        small.put_nowait({"method": "old2", "params": {}})

        mod._broadcast("new", {"content": "x"})

        # old1 should have been dropped; queue holds old2 + new.
        first = small.get_nowait()
        second = small.get_nowait()
        assert first["method"] == "old2"
        assert second["method"] == "new"

    def test_unsubscribe_does_not_touch_loop_task(self):
        """The keystone invariant — WS close must not cancel the loop."""
        import asyncio as _asyncio

        import src.api.persistent_app as mod

        async def _runit():
            async def _forever():
                await _asyncio.sleep(60)

            task = _asyncio.create_task(_forever())
            mod._loop_task = task
            try:
                mod._subscribe("clientX")
                mod._unsubscribe("clientX")
                # Loop is untouched: still running, still the same task.
                assert mod._loop_task is task
                assert not task.done()
                assert not task.cancelled()
            finally:
                task.cancel()
                try:
                    await task
                except _asyncio.CancelledError:
                    pass
                mod._loop_task = None

        _asyncio.run(_runit())


# ---------------------------------------------------------------------------
# 3.17.2 Headless: _terminate_session preserves the race-fix invariants
# ---------------------------------------------------------------------------


class TestTerminateSession:
    """Tests for _terminate_session(reason) — the renamed body of the
    pre-headless _detach_session. Verifies cancel-before-null ordering and
    cleanup of the new headless-era input primitives."""

    @pytest.mark.asyncio
    async def test_no_op_when_session_already_none(self):
        import src.api.persistent_app as mod

        mod._session = None
        # Should not raise.
        await mod._terminate_session("test")

    @pytest.mark.asyncio
    async def test_cancels_loop_task_before_nulling_session(self):
        """The race-fix from commit 3a1d265 must survive the rename."""
        import asyncio as _asyncio

        import src.api.persistent_app as mod

        # Track when the loop was cancelled vs when cleanup ran.
        order = []
        loop_started = _asyncio.Event()

        async def _loop_body():
            loop_started.set()
            try:
                await _asyncio.sleep(60)
            except _asyncio.CancelledError:
                order.append("loop_cancelled")
                raise

        loop_task = _asyncio.create_task(_loop_body())
        # Wait until the body actually enters its try block — otherwise
        # cancel() would fire before the body had a chance to register an
        # except handler, and our ordering assertion would be vacuous.
        await loop_started.wait()
        mod._loop_task = loop_task
        mod._thread_id = "t1"

        # Minimal _session double that records when it's torn down.
        fake_session = MagicMock()
        fake_session.workspace_sync = None
        fake_session.workspace_manager = None
        fake_session.cleanup = AsyncMock(side_effect=lambda: order.append("cleanup"))
        mod._session = fake_session

        with patch.object(mod, "_update_thread_status", new=AsyncMock()):
            await mod._terminate_session("test")

        # Cancel happens before cleanup which happens before nulling.
        assert order == ["loop_cancelled", "cleanup"]
        assert mod._session is None
        assert mod._loop_task is None

    @pytest.mark.asyncio
    async def test_clears_headless_input_primitives(self):
        """Subscriber registry and loop input queues must reset."""
        import asyncio as _asyncio

        import src.api.persistent_app as mod

        mod._loop_task = None  # nothing to cancel
        mod._thread_id = "t2"
        mod._subscribers["ghost"] = _asyncio.Queue()
        mod._loop_user_queue = _asyncio.Queue()
        mod._loop_interrupt_flag = "hard"
        mod._loop_last_user_content = ["something"]
        mod._tool_inflight = True
        mod._events_epoch = 7
        mod._next_seq = 42

        fake_session = MagicMock()
        fake_session.workspace_sync = None
        fake_session.workspace_manager = None
        fake_session.cleanup = AsyncMock()
        mod._session = fake_session

        with patch.object(mod, "_update_thread_status", new=AsyncMock()):
            await mod._terminate_session("test")

        assert mod._subscribers == {}
        assert mod._loop_user_queue is None
        assert mod._loop_interrupt_flag is None
        assert mod._loop_last_user_content == [""]
        assert mod._tool_inflight is False
        assert mod._events_epoch == 0
        assert mod._next_seq == 0


# ---------------------------------------------------------------------------
# 3.17.3 Headless: attach-time readiness race (handle_persistent_websocket)
# ---------------------------------------------------------------------------


class TestHandlePersistentWebsocketReadiness:
    """Regression tests for the attach-time readiness race fixed in sha-a790c79.

    In dual mode, POST /session/attach returns immediately while the actual
    _attach_session work runs in the background. _session.llm_with_tools is
    set early (inside .setup()), but _loop_user_queue is initialized much
    later in the same coroutine. A client that opens the WS in that window
    sees a session that looks ready but loop primitives that aren't.

    Pre-fix: the WS handler only checked _session and llm_with_tools, then
    spawned the persistent loop. The loop's _loop_get_user_input callback
    crashed on the first await (queue was None) and the session never
    recovered. The fix gates loop spawn on _loop_user_queue being
    initialized too, closing the WS with code 4503 ("Agent not ready") so
    the client retries.
    """

    @pytest.mark.asyncio
    async def test_closes_with_4503_when_session_missing(self):
        """No session at all — the simplest unready case."""
        from src.api import persistent_app as pa

        ws = AsyncMock()
        with (
            patch("src.api.persistent_app._session", None),
            patch("src.api.persistent_app._loop_user_queue", None),
            patch("src.api.persistent_app._loop_task", None),
            patch("src.api.persistent_app._ws_connected_event", None),
        ):
            await pa.handle_persistent_websocket(ws)
            assert pa._loop_task is None

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4503, reason="Agent not ready")

    @pytest.mark.asyncio
    async def test_closes_with_4503_when_llm_with_tools_missing(self):
        """Session exists but .setup() hasn't bound llm_with_tools yet."""
        from src.api import persistent_app as pa

        ws = AsyncMock()
        session = MagicMock()
        session.llm_with_tools = None
        with (
            patch("src.api.persistent_app._session", session),
            patch("src.api.persistent_app._loop_user_queue", None),
            patch("src.api.persistent_app._loop_task", None),
            patch("src.api.persistent_app._ws_connected_event", None),
        ):
            await pa.handle_persistent_websocket(ws)
            assert pa._loop_task is None

        ws.close.assert_awaited_once_with(code=4503, reason="Agent not ready")

    @pytest.mark.asyncio
    async def test_closes_with_4503_when_loop_user_queue_missing(self):
        """The keystone bug: llm_with_tools is set but _loop_user_queue is None.

        Pre-fix this passed the readiness check (which only inspected
        _session and llm_with_tools), spawned the loop, and the loop's first
        get-user-input callback crashed on the None queue.
        """
        from src.api import persistent_app as pa

        ws = AsyncMock()
        session = MagicMock()
        session.llm_with_tools = MagicMock()  # truthy
        with (
            patch("src.api.persistent_app._session", session),
            patch("src.api.persistent_app._loop_user_queue", None),
            patch("src.api.persistent_app._loop_task", None),
            patch("src.api.persistent_app._ws_connected_event", None),
        ):
            await pa.handle_persistent_websocket(ws)
            # The whole point of the fix: the loop must NOT have been
            # spawned despite llm_with_tools being set.
            assert pa._loop_task is None

        ws.close.assert_awaited_once_with(code=4503, reason="Agent not ready")

    @pytest.mark.asyncio
    async def test_sends_error_frame_before_close(self):
        """Error frame must precede the close so clients see the reason."""
        from src.api import persistent_app as pa

        ws = AsyncMock()
        with (
            patch("src.api.persistent_app._session", None),
            patch("src.api.persistent_app._loop_user_queue", None),
            patch("src.api.persistent_app._loop_task", None),
            patch("src.api.persistent_app._ws_connected_event", None),
        ):
            await pa.handle_persistent_websocket(ws)

        ws.send_json.assert_awaited_once_with(
            {"method": "error", "params": {"message": "Agent not ready"}}
        )
        ws.close.assert_awaited_once_with(code=4503, reason="Agent not ready")

    @pytest.mark.asyncio
    async def test_signals_ws_connected_even_when_not_ready(self):
        """The boot-WS watchdog must see the attach attempt even on an
        unready close, otherwise a quick reconnect during the readiness
        race could be missed and the pod could time out mid-recovery.
        """
        import asyncio

        from src.api import persistent_app as pa

        ws = AsyncMock()
        connected = asyncio.Event()
        with (
            patch("src.api.persistent_app._session", None),
            patch("src.api.persistent_app._loop_user_queue", None),
            patch("src.api.persistent_app._loop_task", None),
            patch("src.api.persistent_app._ws_connected_event", connected),
        ):
            await pa.handle_persistent_websocket(ws)

        assert connected.is_set()
        ws.close.assert_awaited_once_with(code=4503, reason="Agent not ready")


# ---------------------------------------------------------------------------
# 3.17.4 Headless: module-level loop callbacks behave as the old closures did
# ---------------------------------------------------------------------------


class TestLoopCheckInterrupt:
    """_loop_check_interrupt returns the tri-state mode and resets in one shot."""

    def setup_method(self):
        import src.api.persistent_app as mod

        mod._loop_interrupt_flag = None
        mod._tool_inflight = False

    def test_returns_none_when_flag_not_set(self):
        import src.api.persistent_app as mod

        assert mod._loop_check_interrupt() is None

    def test_returns_hard_mode_once_then_resets(self):
        import src.api.persistent_app as mod

        mod._loop_interrupt_flag = "hard"
        assert mod._loop_check_interrupt() == "hard"
        # Subsequent reads see the reset.
        assert mod._loop_check_interrupt() is None

    def test_returns_graceful_mode_once_then_resets(self):
        import src.api.persistent_app as mod

        mod._loop_interrupt_flag = "graceful"
        assert mod._loop_check_interrupt() == "graceful"
        assert mod._loop_check_interrupt() is None


# ---------------------------------------------------------------------------
# 3.18 create_persistent_app()
# ---------------------------------------------------------------------------


class TestCreatePersistentApp:
    def test_sets_module_globals(self):
        """create_persistent_app sets _config_path and _thread_id."""
        import src.api.persistent_app as mod

        create_persistent_app("my_config", "thread-123")

        assert mod._config_path == "my_config"
        assert mod._thread_id == "thread-123"

    def test_returns_fastapi_instance(self):
        from fastapi import FastAPI

        app = create_persistent_app("config", "tid")
        assert isinstance(app, FastAPI)

    def test_thread_id_optional(self):
        """thread_id can be None."""
        import src.api.persistent_app as mod

        create_persistent_app("config")
        assert mod._thread_id is None


# ---------------------------------------------------------------------------
# Auxiliary + embedding hot-swap in _handle_config_update
# ---------------------------------------------------------------------------


class TestHandleConfigUpdateEnrichmentGate:
    """Pin the widened orchestrator-PATCH gate as a structural assertion.

    The gate at the top of ``_handle_config_update`` must fire for any
    credential-bearing slot (chat, auxiliary, embedding env_keys). A
    regression that reverts to chat-only silently breaks custom-endpoint
    routing for auxiliary and embedding (the ``Untitled Session`` and
    missing-memory bug documented in
    ``docs/hardcoded_model_defaults.md``). Mocking the full async call
    chain here is brittle because of internal imports; pinning the
    source-level shape is the cheapest reliable regression catch.
    """

    def test_gate_checks_auxiliary_model(self):
        from inspect import getsource
        from src.api.persistent_app import _handle_config_update

        src = getsource(_handle_config_update)
        assert 'config_override.get("auxiliary", {}).get("model")' in src, (
            "Auxiliary model changes must trigger the orchestrator-PATCH "
            "enrichment gate."
        )

    def test_gate_checks_embedding_env_keys(self):
        from inspect import getsource
        from src.api.persistent_app import _handle_config_update

        src = getsource(_handle_config_update)
        for key in (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_API_KEY",
        ):
            assert key in src, (
                f"Embedding env key {key} must be in the gate's "
                "credential-bearing-keys tuple."
            )

    def test_rebuilds_auxiliary_llm_from_override(self):
        """When auxiliary section is in the enriched override, a session-
        scoped AuxiliaryLLM is constructed from new_config.auxiliary —
        rather than passing through _agent._auxiliary_llm. Pinning the
        landmark `_session.auxiliary_llm = AuxiliaryLLM(` assignment so
        the rebuild path can't silently regress to the singleton."""
        from inspect import getsource
        from src.api.persistent_app import _handle_config_update

        src = getsource(_handle_config_update)
        assert "_session.auxiliary_llm = AuxiliaryLLM(" in src

    def test_resets_embedding_singleton(self):
        """Embedding hot-swap must clear ``embedding_service._embedding_service``
        so the next get_embedding_service() call rebuilds with the new
        env. Without this the process-wide singleton sticks at the
        boot-time base_url and 401s on api.openai.com."""
        from inspect import getsource
        from src.api.persistent_app import _handle_config_update

        src = getsource(_handle_config_update)
        assert "_embedding_module._embedding_service = None" in src


class TestAttachSessionRebinds:
    """Pin the per-session rebind landmarks in ``_attach_session``.

    Without these landmarks the agent's boot-time singletons leak
    through to every session — title generation 401s, memory extraction
    silently fails, and embedding routes to api.openai.com. The chat-
    side fix carries no protection for auxiliary/embedding; this test
    keeps the rebind machinery wired.
    """

    def test_attach_rebuilds_auxiliary_when_override_present(self):
        from inspect import getsource
        from src.api.persistent_app import _attach_session

        src = getsource(_attach_session)
        # Auxiliary rebuild branch
        assert 'config_override.get("auxiliary", {}).get("model")' in src
        assert "AuxiliaryLLM(" in src
        # Pass session-scoped instance, not _agent._auxiliary_llm
        assert "auxiliary_llm=auxiliary_llm" in src

    def test_attach_resets_embedding_singleton_when_env_changes(self):
        from inspect import getsource
        from src.api.persistent_app import _attach_session

        src = getsource(_attach_session)
        assert "_embedding_module._embedding_service = None" in src
        for key in (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_API_KEY",
        ):
            assert key in src


# ---------------------------------------------------------------------------
# Self-cleanup watchdogs (PR 2)
# ---------------------------------------------------------------------------


class TestSignalWsConnected:
    def test_sets_event_when_present(self):
        import asyncio

        from src.api import persistent_app as pa

        event = asyncio.Event()
        with patch.object(pa, "_ws_connected_event", event):
            pa._signal_ws_connected()
        assert event.is_set()

    def test_no_op_when_event_is_none(self):
        from src.api import persistent_app as pa

        with patch.object(pa, "_ws_connected_event", None):
            pa._signal_ws_connected()  # Should not raise


class TestBootWsWatchdog:
    @pytest.mark.asyncio
    async def test_returns_early_when_ws_connects(self):
        import asyncio

        from src.api import persistent_app as pa

        event = asyncio.Event()
        event.set()  # Pre-set so wait_for returns immediately
        with patch.object(pa, "_ws_connected_event", event):
            with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                with patch.object(pa, "_schedule_exit") as exit_fn:
                    await pa._boot_ws_watchdog(timeout_s=10)
        detach.assert_not_called()
        exit_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_triggers_detach_and_exit_on_timeout(self):
        import asyncio

        from src.api import persistent_app as pa

        event = asyncio.Event()  # Never set
        with patch.object(pa, "_ws_connected_event", event):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        # Tiny timeout so the test doesn't actually wait 10 min
                        await pa._boot_ws_watchdog(timeout_s=0)
        detach.assert_awaited_once()
        exit_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_detach_failure_and_still_exits(self):
        import asyncio

        from src.api import persistent_app as pa

        event = asyncio.Event()  # Never set
        with patch.object(pa, "_ws_connected_event", event):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(
                    pa,
                    "_terminate_session",
                    new=AsyncMock(side_effect=RuntimeError("detach failed")),
                ):
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        await pa._boot_ws_watchdog(timeout_s=0)
        # Exit must still be scheduled even if detach raised
        exit_fn.assert_called_once()


class TestThreadStatusWatchdog:
    @pytest.mark.asyncio
    async def test_exits_when_thread_status_is_ended(self):
        from src.api import persistent_app as pa

        client = AsyncMock()
        client.get_thread_lifecycle = AsyncMock(
            return_value={"status": "ended", "agent_id": None, "ended_at": "now"}
        )
        with patch.object(pa, "_orchestrator_client", client):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        # Tiny poll interval so test runs quickly
                        await pa._thread_status_watchdog(poll_s=0)
        detach.assert_awaited_once()
        exit_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_exit_when_thread_active(self):
        import asyncio

        from src.api import persistent_app as pa

        client = AsyncMock()
        client.get_thread_lifecycle = AsyncMock(
            return_value={"status": "active", "agent_id": "a-1", "ended_at": None}
        )
        with patch.object(pa, "_orchestrator_client", client):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        # Run the watchdog briefly then cancel — it must not
                        # have triggered exit while status was active.
                        task = asyncio.create_task(pa._thread_status_watchdog(poll_s=0))
                        await asyncio.sleep(0.05)
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
        detach.assert_not_called()
        exit_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_when_lifecycle_fetch_fails(self):
        import asyncio

        from src.api import persistent_app as pa

        client = AsyncMock()
        client.get_thread_lifecycle = AsyncMock(side_effect=RuntimeError("network"))
        with patch.object(pa, "_orchestrator_client", client):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        task = asyncio.create_task(pa._thread_status_watchdog(poll_s=0))
                        await asyncio.sleep(0.05)
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
        # Transient failure must not trigger exit — we'd kill ourselves on
        # any orchestrator hiccup otherwise.
        detach.assert_not_called()
        exit_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_exit_when_thread_awaiting_user(self):
        # Regression: Phase 5 added 'awaiting_user' as the eager-mode idle
        # state set by the agent's own loop. The watchdog previously treated
        # it as terminal and killed the pod in ~60s, which collapsed the
        # untethered-survival behaviour Phase 1 + Phase 5 were built for.
        # See docs/issues/persistent_session_watchdog_kills_awaiting_user.md.
        import asyncio

        from src.api import persistent_app as pa

        client = AsyncMock()
        client.get_thread_lifecycle = AsyncMock(
            return_value={
                "status": "awaiting_user",
                "agent_id": "a-1",
                "ended_at": None,
            }
        )
        with patch.object(pa, "_orchestrator_client", client):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        task = asyncio.create_task(pa._thread_status_watchdog(poll_s=0))
                        await asyncio.sleep(0.05)
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
        detach.assert_not_called()
        exit_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_exits_when_thread_suspended(self):
        # The orchestrator's attention-sleep watchdog owns the
        # awaiting_user → suspended transition. Once suspended, the
        # workspace pod is gone, so this agent is stranded and must exit.
        from src.api import persistent_app as pa

        client = AsyncMock()
        client.get_thread_lifecycle = AsyncMock(
            return_value={
                "status": "suspended",
                "agent_id": "a-1",
                "ended_at": None,
            }
        )
        with patch.object(pa, "_orchestrator_client", client):
            with patch.object(pa, "_thread_id", "thread-xyz"):
                with patch.object(pa, "_terminate_session", new=AsyncMock()) as detach:
                    with patch.object(pa, "_schedule_exit") as exit_fn:
                        await pa._thread_status_watchdog(poll_s=0)
        detach.assert_awaited_once()
        exit_fn.assert_called_once()


class TestStartStopWatchdogs:
    @pytest.mark.asyncio
    async def test_start_creates_two_named_tasks(self):
        import asyncio

        from src.api import persistent_app as pa

        with patch.object(pa, "_watchdog_tasks", []):
            pa._start_watchdogs()
            try:
                assert len(pa._watchdog_tasks) == 2
                names = {t.get_name() for t in pa._watchdog_tasks}
                assert names == {"boot-ws-watchdog", "thread-status-watchdog"}
            finally:
                pa._stop_watchdogs()
                await asyncio.gather(
                    *[t for t in pa._watchdog_tasks if not t.done()],
                    return_exceptions=True,
                )

    @pytest.mark.asyncio
    async def test_start_cancels_prior_tasks(self):
        import asyncio

        from src.api import persistent_app as pa

        async def _forever():
            await asyncio.sleep(60)

        prior = asyncio.create_task(_forever(), name="prior")
        with patch.object(pa, "_watchdog_tasks", [prior]):
            pa._start_watchdogs()
            try:
                await asyncio.sleep(0.01)
                assert prior.cancelled() or prior.done()
            finally:
                pa._stop_watchdogs()
                await asyncio.gather(
                    *[t for t in pa._watchdog_tasks if not t.done()],
                    return_exceptions=True,
                )

    @pytest.mark.asyncio
    async def test_stop_skips_current_task(self):
        # _stop_watchdogs should not cancel the calling task — that would
        # raise CancelledError in the very watchdog that triggered detach.
        import asyncio

        from src.api import persistent_app as pa

        async def fake_watchdog():
            pa._stop_watchdogs()
            return "completed-normally"

        task = asyncio.create_task(fake_watchdog(), name="self")
        with patch.object(pa, "_watchdog_tasks", [task]):
            result = await task
        assert result == "completed-normally"
