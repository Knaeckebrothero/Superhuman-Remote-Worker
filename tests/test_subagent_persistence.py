"""``DbSubagentLedger`` — the durable side of a child (U3 WP3, plan B.1).

With a fake orchestrator client (row creation) and a fake agent-side pool
(transcript + lifecycle writes): open → the row derived from the job;
persist_message → the lifted serialisers' rows; update → the thread status
stays ``active`` while running and becomes ``ended`` on ANY terminal kind,
with the kind in ``subagent_status`` and the driver's classification in
``subagent_outcome``; lookup → only a terminal row replays. Then the same
ledger under the REAL runtime with a scripted child, end to end.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.loader import LLMConfig
from src.core.message_markers import PERSIST_ROLE_KEY
from src.core.subagent_roster import resolve_subagent_roster
from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from src.subagents import (
    SUBAGENT_STATUSES,
    DbSubagentLedger,
    NullLedger,
    SubagentCall,
    SubagentLedger,
    SubagentRuntime,
    WorkerHost,
)
from src.tools.context import ToolContext
from tests._fake_chat_model import FakeChatModel, text_turn, tool_turn
from tests._fs_backend import FilesystemTestBackend

JOB = "aaaaaaaa-1111-4222-8333-444444444444"
CHILD = "bbbbbbbb-1111-4222-8333-444444444444"


def _client(thread_id: str | None = CHILD) -> SimpleNamespace:
    return SimpleNamespace(create_subagent_thread=AsyncMock(return_value=thread_id))


def _pool(row=None) -> SimpleNamespace:
    return SimpleNamespace(
        save_thread_message=AsyncMock(return_value={"id": CHILD, "seq": 1}),
        update_subagent_thread=AsyncMock(return_value=True),
        get_subagent_thread_by_call=AsyncMock(return_value=row),
    )


def _open_fields(**overrides):
    fields = {
        "status": "running",
        "handle": "explorer-7f3a",
        "subagent_type": "explorer",
        "parent_job_id": JOB,
        "parent_thread_id": None,
        "parent_tool_call_id": "call-1",
        "isolation": "shared",
        "write_policy": "none",
        "brief_description": "find the secret",
        "fork": False,
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_it_is_a_ledger(self):
        ledger = DbSubagentLedger(_client(), _pool())
        assert isinstance(ledger, SubagentLedger)
        assert callable(getattr(ledger, "lookup", None))

    def test_both_halves_are_required(self):
        with pytest.raises(ValueError):
            DbSubagentLedger(None, _pool())
        with pytest.raises(ValueError):
            DbSubagentLedger(_client(), None)

    def test_from_context_needs_the_client_and_the_pool(self):
        assert DbSubagentLedger.from_context(ToolContext()) is None
        assert (
            DbSubagentLedger.from_context(ToolContext(orchestrator_client=_client()))
            is None
        )
        assert DbSubagentLedger.from_context(ToolContext(postgres_db=_pool())) is None
        ctx = ToolContext(orchestrator_client=_client(), postgres_db=_pool())
        ledger = DbSubagentLedger.from_context(ctx)
        assert isinstance(ledger, DbSubagentLedger)
        assert ledger.client is ctx.orchestrator_client
        assert ledger.postgres is ctx.postgres_db
        assert ledger.parent_context is ctx


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


class TestOpen:
    @pytest.mark.asyncio
    async def test_the_row_is_created_through_the_orchestrator_from_the_job(self):
        client, pool = _client(), _pool()
        ledger = DbSubagentLedger(client, pool)
        await ledger.open(CHILD, **_open_fields())
        client.create_subagent_thread.assert_awaited_once_with(
            JOB,
            subagent_id=CHILD,
            handle="explorer-7f3a",
            subagent_type="explorer",
            parent_tool_call_id="call-1",
            parent_thread_id=None,
            isolation="shared",
            write_policy="none",
            brief_description="find the secret",
            parent_iteration=None,
            fork=False,
        )
        assert ledger.thread_id_for(CHILD) == CHILD
        assert ledger.rows == {CHILD: CHILD}
        assert ledger.failed == set()

    @pytest.mark.asyncio
    async def test_parent_iteration_is_the_parents_turn_counter(self):
        """WP2 §8.1 answered: the runtime does not know the graph iteration;
        the parent ToolContext's checkpointed turn counter (stamped by the
        execute / tools nodes) is what the row records."""
        ctx = ToolContext()
        ctx.set_current_phase("tactical", phase_number=3, turn_count=17)
        client = _client()
        ledger = DbSubagentLedger(client, _pool(), parent_context=ctx)
        await ledger.open(CHILD, **_open_fields())
        assert client.create_subagent_thread.await_args.kwargs["parent_iteration"] == 17
        # An explicit value wins over the context.
        await ledger.open("other", **_open_fields(parent_iteration=99))
        assert client.create_subagent_thread.await_args.kwargs["parent_iteration"] == 99

    @pytest.mark.asyncio
    async def test_a_session_parent_forwards_its_thread(self):
        client = _client()
        ledger = DbSubagentLedger(client, _pool())
        await ledger.open(CHILD, **_open_fields(parent_thread_id="thread-9", fork=True))
        kwargs = client.create_subagent_thread.await_args.kwargs
        assert kwargs["parent_thread_id"] == "thread-9" and kwargs["fork"] is True

    @pytest.mark.asyncio
    async def test_a_refused_create_leaves_no_durable_state(self):
        client, pool = _client(None), _pool()
        ledger = DbSubagentLedger(client, pool)
        await ledger.open(CHILD, **_open_fields())
        assert ledger.thread_id_for(CHILD) is None
        assert ledger.failed == {CHILD}
        await ledger.persist_message(CHILD, AIMessage(content="x", id="m1"), 1)
        await ledger.update(CHILD, status="completed", outcome="completed")
        pool.save_thread_message.assert_not_awaited()
        pool.update_subagent_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_parent_job_means_no_row(self):
        client = _client()
        ledger = DbSubagentLedger(client, _pool())
        await ledger.open(CHILD, **_open_fields(parent_job_id=None))
        client.create_subagent_thread.assert_not_awaited()
        assert ledger.failed == {CHILD}

    @pytest.mark.asyncio
    async def test_the_row_id_the_orchestrator_returns_is_what_later_writes_use(self):
        client, pool = _client("other-id"), _pool()
        ledger = DbSubagentLedger(client, pool)
        await ledger.open(CHILD, **_open_fields())
        assert ledger.thread_id_for(CHILD) == "other-id"
        await ledger.update(CHILD, status="completed")
        assert pool.update_subagent_thread.await_args.args == ("other-id",)


# ---------------------------------------------------------------------------
# persist_message
# ---------------------------------------------------------------------------


class TestPersistMessage:
    @pytest.mark.asyncio
    async def test_rows_go_through_the_lifted_serialisers(self):
        pool = _pool()
        ledger = DbSubagentLedger(_client(), pool)
        await ledger.open(CHILD, **_open_fields())

        ai = AIMessage(
            content="calling",
            id="chatcmpl-1",
            tool_calls=[{"name": "read_file", "args": {"path": "a.md"}, "id": "tc-1"}],
        )
        await ledger.persist_message(CHILD, ai, 1)
        await ledger.persist_message(
            CHILD, ToolMessage(content="the file", tool_call_id="tc-1", id="tm-1"), 1
        )
        await ledger.persist_message(CHILD, HumanMessage(content="brief", id="h-1"), 0)
        event = HumanMessage(content="steer", id="e-1")
        event.additional_kwargs[PERSIST_ROLE_KEY] = "event"
        await ledger.persist_message(CHILD, event, 2)

        calls = [c.kwargs for c in pool.save_thread_message.await_args_list]
        assert [c["thread_id"] for c in calls] == [CHILD] * 4
        assert calls[0]["role"] == "ai" and calls[0]["id"] == "chatcmpl-1"
        assert calls[0]["tool_calls"] == [
            {"name": "read_file", "args": {"path": "a.md"}, "id": "tc-1"}
        ]
        assert calls[0]["turn_number"] == 1
        assert calls[1]["role"] == "tool" and calls[1]["tool_call_id"] == "tc-1"
        assert calls[2]["role"] == "human" and calls[2]["turn_number"] == 0
        assert calls[3]["role"] == "event"
        for call in calls:
            assert set(call) == {
                "thread_id",
                "id",
                "role",
                "content",
                "tool_calls",
                "turn_number",
                "metrics",
                "tool_call_id",
                "thinking",
            }

    @pytest.mark.asyncio
    async def test_an_unopened_child_writes_nothing(self):
        pool = _pool()
        ledger = DbSubagentLedger(_client(), pool)
        await ledger.persist_message("ghost", AIMessage(content="x", id="m"), 1)
        pool.save_thread_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    @pytest.mark.asyncio
    async def test_running_keeps_the_thread_active(self):
        pool = _pool()
        ledger = DbSubagentLedger(_client(), pool)
        await ledger.open(CHILD, **_open_fields())
        await ledger.update(CHILD, status="running", turns=2, tokens=300)
        pool.update_subagent_thread.assert_awaited_once_with(
            CHILD,
            subagent_status="running",
            status="active",
            ended=False,
            turns=2,
            tokens=300,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind", [s for s in SUBAGENT_STATUSES if s != "running"] + ["exploded"]
    )
    async def test_every_terminal_kind_ends_the_thread(self, kind):
        """The closed thread vocabulary is never widened: any kind but
        running is ``ended``; the kind itself (open set) is the column."""
        pool = _pool()
        ledger = DbSubagentLedger(_client(), pool)
        await ledger.open(CHILD, **_open_fields())
        await ledger.update(
            CHILD,
            status=kind,
            outcome=f"{kind}:detail",
            turns=5,
            tokens=4000,
            report_path=".subagents/explorer-7f3a/report.md",
            error="oops" if kind == "error" else None,
        )
        kwargs = pool.update_subagent_thread.await_args.kwargs
        assert pool.update_subagent_thread.await_args.args == (CHILD,)
        assert kwargs["subagent_status"] == kind
        assert kwargs["status"] == "ended" and kwargs["ended"] is True
        assert kwargs["outcome"] == f"{kind}:detail"
        assert kwargs["turns"] == 5 and kwargs["tokens"] == 4000
        assert kwargs["report_path"] == ".subagents/explorer-7f3a/report.md"
        if kind == "error":
            assert kwargs["error"] == "oops"
        else:
            assert "error" not in kwargs

    @pytest.mark.asyncio
    async def test_none_fields_are_left_alone(self):
        pool = _pool()
        ledger = DbSubagentLedger(_client(), pool)
        await ledger.open(CHILD, **_open_fields())
        await ledger.update(CHILD, status="completed", report_path=None, error=None)
        kwargs = pool.update_subagent_thread.await_args.kwargs
        assert set(kwargs) == {"subagent_status", "status", "ended"}

    @pytest.mark.asyncio
    async def test_counters_only_updates_do_not_touch_the_status(self):
        pool = _pool()
        ledger = DbSubagentLedger(_client(), pool)
        await ledger.open(CHILD, **_open_fields())
        await ledger.update(CHILD, turns="7", tokens=None)
        pool.update_subagent_thread.assert_awaited_once_with(CHILD, turns=7)
        await ledger.update(CHILD)  # nothing to write
        assert pool.update_subagent_thread.await_count == 1


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


class TestLookup:
    @pytest.mark.asyncio
    async def test_only_a_terminal_row_replays(self):
        row = {"id": CHILD, "subagent_status": "completed", "subagent_handle": "h"}
        pool = _pool(row)
        ledger = DbSubagentLedger(_client(), pool)
        assert await ledger.lookup(JOB, "call-1") == row
        pool.get_subagent_thread_by_call.assert_awaited_once_with(JOB, "call-1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["running", None, ""])
    async def test_a_live_or_statusless_row_does_not(self, status):
        ledger = DbSubagentLedger(
            _client(), _pool({"id": CHILD, "subagent_status": status})
        )
        assert await ledger.lookup(JOB, "call-1") is None

    @pytest.mark.asyncio
    async def test_no_row_is_none(self):
        ledger = DbSubagentLedger(_client(), _pool(None))
        assert await ledger.lookup(JOB, "call-1") is None


# ---------------------------------------------------------------------------
# End to end under the real runtime
# ---------------------------------------------------------------------------

_PARENT_LLM = {
    "model": "gpt-4o-mini",
    "provider": "openai",
    "api_key": "sk-parent-test",
    "model_max_context_tokens": 128000,
}


def _explorer_roster() -> dict:
    data = {
        "agent_id": "parent",
        "display_name": "Parent",
        "llm": dict(_PARENT_LLM),
        "subagents": {
            "default": "explorer",
            "roster": {"explorer": {"$ref": "subagents/explorer"}},
        },
    }
    return resolve_subagent_roster(data, db_refs={}, on_missing="raise")["subagents"]


def _parent(tmp_path, *, client, pool) -> ToolContext:
    root = tmp_path / "ws"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "hello.md").write_text("the secret word is MARMALADE\n")
    ws = WorkspaceManager(
        job_id="parent-job",
        base_path=root,
        backend=FilesystemTestBackend(root),
        config=WorkspaceManagerConfig(git_versioning=False),
    )
    ws.initialize()
    ctx = ToolContext(
        workspace_manager=ws,
        config={
            "shell": {},
            "agent_id": "developer",
            "delegation": {"enabled": True, "max_concurrent": 2},
            "subagents": _explorer_roster(),
        },
        _job_metadata={"job_id": JOB, "project_id": "proj"},
        _job_id=JOB,
        _llm_config=LLMConfig(**_PARENT_LLM),
        _resolved_tool_names=["read_file", "list_files", "search_files"],
        orchestrator_client=client,
        postgres_db=pool,
    )
    ctx.set_current_phase("tactical", phase_number=2, turn_count=9)
    return ctx


@pytest.mark.asyncio
async def test_a_real_child_run_lands_its_row_transcript_and_terminal_update(tmp_path):
    client, pool = _client(), _pool()
    ctx = _parent(tmp_path, client=client, pool=pool)
    host = WorkerHost.from_context(ctx)
    ledger = DbSubagentLedger.from_context(ctx)
    assert isinstance(ledger, DbSubagentLedger)
    fake = FakeChatModel(
        [
            tool_turn("read_file", {"path": "notes/hello.md"}, "tc-1"),
            text_turn("The secret word is MARMALADE."),
        ]
    )
    runtime = SubagentRuntime.from_context(
        ctx,
        host,
        ledger=ledger,
        llm_factory=lambda cfg, lim: fake,
        hex_source=lambda: "7f3a",
        driver_kwargs={
            "watcher_poll_interval": 0.01,
            "archiver": None,
            "archive_fn": lambda **k: None,
        },
    )

    envelope = await runtime.run_foreground(
        SubagentCall(
            tool_call_id="call-1",
            subagent_type="explorer",
            prompt="What is the secret word in notes/hello.md?",
            description="find the secret",
        )
    )
    assert "[subagent explorer-7f3a · explorer · completed" in envelope

    # open: the row, derived from the job, with the parent's turn counter.
    create = client.create_subagent_thread.await_args
    assert create.args == (JOB,)
    record = runtime.records[(JOB, "call-1")]
    assert create.kwargs["subagent_id"] == record.subagent_id
    assert create.kwargs["handle"] == "explorer-7f3a"
    assert create.kwargs["subagent_type"] == "explorer"
    assert create.kwargs["parent_tool_call_id"] == "call-1"
    assert create.kwargs["brief_description"] == "find the secret"
    assert create.kwargs["parent_iteration"] == 9
    assert create.kwargs["isolation"] == "shared"

    # The transcript: the brief, the tool-calling turn, the tool result, the
    # answer — every row on the child's thread id, serialised by role.
    rows = [c.kwargs for c in pool.save_thread_message.await_args_list]
    assert rows and all(r["thread_id"] == CHILD for r in rows)
    roles = [r["role"] for r in rows]
    assert roles[0] == "human" and "secret word" in rows[0]["content"]
    assert "tool" in roles and "ai" in roles
    tool_rows = [r for r in rows if r["role"] == "tool"]
    assert tool_rows and tool_rows[0]["tool_call_id"]

    # The terminal update: ended, completed, the counters and the spill.
    final = pool.update_subagent_thread.await_args
    assert final.args == (CHILD,)
    assert final.kwargs["status"] == "ended" and final.kwargs["ended"] is True
    assert final.kwargs["subagent_status"] == "completed"
    assert final.kwargs["outcome"] == "completed"
    assert final.kwargs["turns"] == 2 and final.kwargs["tokens"] > 0
    assert final.kwargs["report_path"] == ".subagents/explorer-7f3a/report.md"
    assert (tmp_path / "ws" / ".subagents" / "explorer-7f3a" / "report.md").exists()


@pytest.mark.asyncio
async def test_agent_and_tool_install_the_db_ledger_when_the_context_carries_both(
    tmp_path,
):
    """``ensure_runtime`` (the tool's lazy path) makes the same choice as
    ``agent.py``: the DB ledger with both halves on the context, the null
    ledger otherwise."""
    from src.tools.delegation.delegate_agent import ensure_runtime

    ctx = _parent(tmp_path, client=_client(), pool=_pool())
    assert isinstance(ensure_runtime(ctx).ledger, DbSubagentLedger)
    bare = _parent(tmp_path / "bare", client=None, pool=None)
    assert isinstance(ensure_runtime(bare).ledger, NullLedger)
