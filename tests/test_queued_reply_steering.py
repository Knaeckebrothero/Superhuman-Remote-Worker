"""Tests for steering lane B — queued replies delivered at a natural break.

Steering has two lanes. Lane A (``pending_guidance``) is urgent and renders as
a transient block on every LLM turn; it is deliberately untouched by this work.
Lane B is non-urgent mail, which used to be delivered only at a
tactical->strategic phase boundary. That stops being a usable cadence as
tactical phases grow — at three phases a job has exactly one such boundary, and
a reply sent during the review phase would never be delivered at all. It is now
keyed to a completed todo, with a wall-clock floor for the stuck case.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graph import _deliver_queued_replies, _replies_overdue
from src.tools.context import ToolContext


def iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def make_config(max_wait=300):
    return SimpleNamespace(
        limits=SimpleNamespace(queued_reply_max_wait_seconds=max_wait)
    )


def make_context(workspace=None, *, stateless=False):
    ctx = ToolContext(workspace_manager=workspace or MagicMock())
    ctx._stateless_worker = stateless
    return ctx


@pytest.fixture
def acks(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "src.graph._ack_supervisor_guidance",
        lambda job_id, guidance_ids=None, reply_threads=None: recorded.append(
            (job_id, reply_threads)
        ),
    )
    return recorded


@pytest.fixture
def written(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "src.graph._write_reply_files",
        lambda job_id, workspace, replies: recorded.append(list(replies)),
    )
    return recorded


def set_inbox(monkeypatch, replies):
    monkeypatch.setattr("src.graph._get_queued_replies", lambda job_id: list(replies))


class TestRepliesOverdue:
    def test_fresh_replies_are_not_overdue(self):
        assert _replies_overdue([{"timestamp": iso(10)}], 300) is False

    def test_old_reply_is_overdue(self):
        assert _replies_overdue([{"timestamp": iso(400)}], 300) is True

    def test_oldest_wins(self):
        replies = [{"timestamp": iso(5)}, {"timestamp": iso(9999)}]
        assert _replies_overdue(replies, 300) is True

    def test_zero_disables_the_floor(self):
        assert _replies_overdue([{"timestamp": iso(9999)}], 0) is False

    def test_unparseable_timestamp_counts_as_overdue(self):
        """Delivering twice is tolerated (acks are at-least-once); stranding is not."""
        assert _replies_overdue([{"timestamp": "not-a-date"}], 300) is True

    def test_missing_timestamp_counts_as_overdue(self):
        assert _replies_overdue([{}], 300) is True

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = (datetime.now(timezone.utc) - timedelta(seconds=400)).replace(
            tzinfo=None
        )
        assert _replies_overdue([{"timestamp": naive.isoformat()}], 300) is True

    def test_z_suffix_parses(self):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=400)).replace(
            microsecond=0, tzinfo=None
        ).isoformat() + "Z"
        assert _replies_overdue([{"timestamp": stamp}], 300) is True


class TestDelivery:
    def test_completed_todo_delivers(self, monkeypatch, acks, written):
        set_inbox(
            monkeypatch,
            [{"thread_id": "t1", "message": "check X", "timestamp": iso(5)}],
        )
        ctx = make_context()
        ctx.request_reply_drain()  # todo_complete fired
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert len(result["messages"]) == 1
        assert "check X" in result["messages"][0].content
        assert "[QUEUED MESSAGES]" in result["messages"][0].content
        assert acks == [("job-1", ["t1"])]
        assert written and written[0][0]["message"] == "check X"

    def test_no_break_and_not_overdue_holds_the_reply(self, monkeypatch, acks, written):
        """The whole point of lane B: don't interrupt mid-task."""
        set_inbox(
            monkeypatch, [{"thread_id": "t1", "message": "later", "timestamp": iso(5)}]
        )
        ctx = make_context()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert result["messages"] == []
        assert acks == []
        assert written == []

    def test_wall_clock_floor_delivers_without_a_break(
        self, monkeypatch, acks, written
    ):
        """The stuck agent — never completes a todo, must still get its mail."""
        set_inbox(
            monkeypatch,
            [{"thread_id": "t1", "message": "urgent-ish", "timestamp": iso(400)}],
        )
        ctx = make_context()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert len(result["messages"]) == 1
        assert acks == [("job-1", ["t1"])]

    def test_floor_disabled_holds_forever(self, monkeypatch, acks, written):
        set_inbox(
            monkeypatch, [{"thread_id": "t1", "message": "x", "timestamp": iso(9999)}]
        )
        ctx = make_context()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(max_wait=0), result)

        assert result["messages"] == []

    def test_empty_inbox_clears_a_stale_break_flag(self, monkeypatch, acks, written):
        """A break with no mail must not arm delivery for mail that arrives later."""
        set_inbox(monkeypatch, [])
        ctx = make_context()
        ctx.request_reply_drain()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert ctx.consume_reply_drain() is False
        assert result["messages"] == []

    def test_redelivery_window_does_not_duplicate(self, monkeypatch, acks, written):
        """The ack is fire-and-forget, so the inbox keeps returning delivered mail.

        Lane A survives this because its block is transient. Lane B appends
        persistent messages, so an unfiltered redelivery window would stack the
        same reply into history once per completed todo for up to a heartbeat.
        """
        reply = {"thread_id": "t1", "message": "check X", "timestamp": iso(5)}
        set_inbox(monkeypatch, [reply])
        ctx = make_context()

        # Three todos complete before the ack round-trips.
        seen = []
        for _ in range(3):
            ctx.request_reply_drain()
            result = {"messages": []}
            _deliver_queued_replies("job-1", ctx, make_config(), result)
            seen.append(len(result["messages"]))

        assert seen == [1, 0, 0]
        assert len(acks) == 1

    def test_new_mail_after_a_delivery_still_arrives(self, monkeypatch, acks, written):
        """Suppression must key on the message, not latch the whole lane off."""
        first = {"thread_id": "t1", "message": "one", "timestamp": iso(5)}
        set_inbox(monkeypatch, [first])
        ctx = make_context()
        ctx.request_reply_drain()
        _deliver_queued_replies("job-1", ctx, make_config(), {"messages": []})

        second = {"thread_id": "t1", "message": "two", "timestamp": iso(1)}
        set_inbox(monkeypatch, [first, second])
        ctx.request_reply_drain()
        result = {"messages": []}
        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert len(result["messages"]) == 1
        body = result["messages"][0].content
        assert "two" in body and "one" not in body

    def test_break_flag_is_consumed_once(self, monkeypatch, acks, written):
        set_inbox(
            monkeypatch, [{"thread_id": "t1", "message": "one", "timestamp": iso(5)}]
        )
        ctx = make_context()
        ctx.request_reply_drain()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)
        assert len(result["messages"]) == 1

        # Second turn, no new todo completed: the same mail must not re-deliver
        # off a stale flag. (Redelivery is the orchestrator's job if the ack
        # was lost — it keeps sending the entry on the heartbeat.)
        result2 = {"messages": []}
        _deliver_queued_replies("job-1", ctx, make_config(), result2)
        assert result2["messages"] == []

    def test_existing_tool_messages_are_preserved(self, monkeypatch, acks, written):
        set_inbox(
            monkeypatch, [{"thread_id": "t1", "message": "m", "timestamp": iso(5)}]
        )
        ctx = make_context()
        ctx.request_reply_drain()
        result = {"messages": ["existing-tool-message"]}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert result["messages"][0] == "existing-tool-message"
        assert len(result["messages"]) == 2

    def test_archive_failure_still_delivers(self, monkeypatch, acks):
        """The workspace file is the record; the message is what makes it act."""

        def boom(job_id, workspace, replies):
            raise RuntimeError("workspace unreachable")

        monkeypatch.setattr("src.graph._write_reply_files", boom)
        set_inbox(
            monkeypatch, [{"thread_id": "t1", "message": "m", "timestamp": iso(5)}]
        )
        ctx = make_context()
        ctx.request_reply_drain()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert len(result["messages"]) == 1
        assert acks == [("job-1", ["t1"])]

    def test_multiple_threads_all_acked(self, monkeypatch, acks, written):
        set_inbox(
            monkeypatch,
            [
                {"thread_id": "t1", "message": "a", "timestamp": iso(5)},
                {"thread_id": "t2", "message": "b", "timestamp": iso(5)},
            ],
        )
        ctx = make_context()
        ctx.request_reply_drain()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert acks == [("job-1", ["t1", "t2"])]
        body = result["messages"][0].content
        assert "a" in body and "b" in body

    def test_stateless_reply_waits_for_checkpoint_ack(self, monkeypatch, acks, written):
        reply = {
            "id": "reply-1",
            "thread_id": "t1",
            "message": "checkpoint me",
            "timestamp": iso(5),
        }
        set_inbox(monkeypatch, [reply])
        ctx = make_context(stateless=True)
        ctx.request_reply_drain()
        result = {"messages": []}

        _deliver_queued_replies("job-1", ctx, make_config(), result)

        assert len(result["messages"]) == 1
        assert result["delivered_reply_keys"] == ["id:reply-1"]
        assert acks == [], "stateless ack must wait for fenced aput commit"


class TestTodoCompleteSetsTheBreak:
    def _tool(self, context):
        from src.tools.core.todo import create_todo_tools

        return next(t for t in create_todo_tools(context) if t.name == "todo_complete")

    def test_completing_a_todo_requests_a_drain(self):
        from src.managers.todo import TodoManager

        mgr = TodoManager(workspace=MagicMock())
        mgr.stage_tactical_todos(
            [f"Do the thing number {i} properly" for i in range(5)]
        )
        mgr.apply_staged_todos()

        ctx = ToolContext(workspace_manager=MagicMock(), todo_manager=mgr)
        assert ctx.consume_reply_drain() is False

        self._tool(ctx).invoke({})
        assert ctx.consume_reply_drain() is True


class TestBoundaryBackstop:
    """The phase-boundary drain survives as a backstop for non-dual-app runs.

    It reads the DB directly, so it cannot see what the natural-break path has
    already delivered. Since the natural-break ack is fire-and-forget, a
    boundary reached before that ack lands would re-append mail the agent
    already has — the two paths must share the delivered-keys set.
    """

    def _node(self, tool_context, monkeypatch, returned):
        from unittest.mock import MagicMock

        from src.graph import _reply_key, create_handle_transition_node
        from src.managers.todo import TodoManager

        async def fake_process(
            job_id,
            workspace,
            postgres_db,
            *,
            delivered_reply_keys=None,
        ):
            return [
                reply
                for reply in returned
                if not delivered_reply_keys
                or _reply_key(reply) not in delivered_reply_keys
            ]

        monkeypatch.setattr("src.graph._process_queued_replies", fake_process)

        config = MagicMock()
        config.phase_settings = SimpleNamespace(min_todos=5, max_todos=20)
        return create_handle_transition_node(
            MagicMock(),
            TodoManager(workspace=MagicMock()),
            config,
            min_todos=5,
            max_todos=20,
            postgres_db=MagicMock(),
            tool_context=tool_context,
        )

    @pytest.mark.asyncio
    async def test_backstop_delivers_undelivered_mail(self, monkeypatch, acks):
        reply = {"thread_id": "t1", "message": "from the db", "timestamp": iso(5)}
        ctx = make_context()
        node = self._node(ctx, monkeypatch, [reply])

        result = await node(
            {
                "job_id": "job-1",
                "is_strategic_phase": False,
                "phase_number": 2,
                "iteration": 5,
            }
        )

        bodies = [getattr(m, "content", "") for m in result.get("messages", [])]
        assert any("from the db" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_backstop_suppresses_already_delivered_mail(self, monkeypatch, acks):
        from src.graph import _reply_key

        reply = {"thread_id": "t1", "message": "already seen", "timestamp": iso(5)}
        ctx = make_context()
        ctx._delivered_reply_keys.add(_reply_key(reply))
        node = self._node(ctx, monkeypatch, [reply])

        result = await node(
            {
                "job_id": "job-1",
                "is_strategic_phase": False,
                "phase_number": 2,
                "iteration": 5,
            }
        )

        bodies = [getattr(m, "content", "") for m in result.get("messages", [])]
        assert not any("already seen" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_checkpointed_reply_is_filtered_before_workspace_write(
        self, monkeypatch
    ):
        from src.graph import _process_queued_replies, _reply_key

        reply = {
            "id": "reply-committed-before-ack",
            "thread_id": "t1",
            "message": "already absorbed",
            "timestamp": iso(5),
        }
        postgres_db = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"context": {"queued_replies": [reply]}})
        )
        write_files = MagicMock()
        monkeypatch.setattr("src.graph._write_reply_files", write_files)

        result = await _process_queued_replies(
            "job-1",
            MagicMock(),
            postgres_db,
            delivered_reply_keys={_reply_key(reply)},
        )

        assert result == []
        write_files.assert_not_called()


class TestInboxContract:
    """The heartbeat prune contract, shared by both lanes."""

    def test_list_overwrites_empty_clears_none_keeps(self):
        from src.api.dual_app import _replace_inbox

        inbox = {}
        _replace_inbox(inbox, "job-1", [{"thread_id": "t1"}], "Queued replies")
        assert inbox["job-1"] == [{"thread_id": "t1"}]

        # None = "no information" (older orchestrator / failed lookup) — keep.
        _replace_inbox(inbox, "job-1", None, "Queued replies")
        assert inbox["job-1"] == [{"thread_id": "t1"}]

        # Empty list = authoritative "nothing pending" — prune.
        _replace_inbox(inbox, "job-1", [], "Queued replies")
        assert "job-1" not in inbox
