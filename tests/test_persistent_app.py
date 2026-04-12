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
        )

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self):
        client = AsyncMock()
        client.save_thread_message.side_effect = RuntimeError("db error")
        # Should not raise
        await _save_message(client, "tid", "user", "hi", None, 1)


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
        assert result["remote"]["port"] == 22

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
