"""``SubagentRuntime`` — the per-parent delegation runtime (U3 WP2, plan B.3 /
B.5 / B.6): handles, the worktree counter, batch N-sharing, the roster
lookup, idempotent re-execution, cancellation and the ledger vocabulary."""

from __future__ import annotations

import asyncio
import re

import pytest

import src.subagents.runtime as runtime_mod
from src.core.loader import LLMConfig
from src.core.subagent_roster import resolve_subagent_roster
from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
from src.subagents import (
    SUBAGENT_STATUSES,
    ChildBudgets,
    ContextProbe,
    RecordingLedger,
    SubagentCall,
    SubagentRuntime,
    WorkerHost,
)
from src.tools.context import ToolContext
from tests._fake_chat_model import HANG, FakeChatModel, text_turn
from tests._fs_backend import FilesystemTestBackend

_PARENT_LLM = {
    "model": "gpt-4o-mini",
    "provider": "openai",
    "api_key": "sk-parent-test",
    "model_max_context_tokens": 128000,
}


def explorer_roster(**overrides) -> dict:
    data = {
        "agent_id": "parent",
        "display_name": "Parent",
        "llm": dict(_PARENT_LLM),
        "subagents": {
            "default": "explorer",
            "roster": {"explorer": {"$ref": "subagents/explorer", **overrides}},
        },
    }
    return resolve_subagent_roster(data, db_refs={}, on_missing="raise")["subagents"]


def make_parent(tmp_path, *, subagents=None, max_concurrent=2):
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
            "delegation": {"enabled": True, "max_concurrent": max_concurrent},
            "subagents": subagents if subagents is not None else explorer_roster(),
        },
        _job_metadata={"job_id": "parent-job", "project_id": "proj"},
        _llm_config=LLMConfig(**_PARENT_LLM),
        _resolved_tool_names=["read_file", "list_files", "search_files"],
    )
    return ctx, root


def runtime_for(ctx, *, factory=None, ledger=None, **kw) -> SubagentRuntime:
    host = WorkerHost.from_context(ctx)
    ctx._parent_host = host
    kw.setdefault(
        "driver_kwargs",
        {
            "watcher_poll_interval": 0.01,
            "archiver": None,
            "archive_fn": lambda **k: None,
        },
    )
    runtime = SubagentRuntime.from_context(
        ctx,
        host,
        ledger=ledger if ledger is not None else RecordingLedger(),
        llm_factory=factory,
        **kw,
    )
    ctx.subagent_runtime = runtime
    return runtime


def call(call_id="c1", **kw) -> SubagentCall:
    kw.setdefault("subagent_type", "explorer")
    kw.setdefault("prompt", "Say hello.")
    return SubagentCall(tool_call_id=call_id, **kw)


# ---------------------------------------------------------------------------
# Handles, counters, batch
# ---------------------------------------------------------------------------


class TestHandles:
    def test_handles_are_typed_and_unique(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        runtime = runtime_for(ctx)
        handles = [runtime.mint_handle("explorer") for _ in range(300)]
        assert len(set(handles)) == 300
        assert all(re.fullmatch(r"explorer-[0-9a-f]{4}", h) for h in handles)
        assert runtime.handles == set(handles)

    def test_a_collision_is_skipped(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        runtime = runtime_for(ctx, hex_source=iter(["aaaa", "aaaa", "bbbb"]).__next__)
        assert runtime.mint_handle("explorer") == "explorer-aaaa"
        assert runtime.mint_handle("explorer") == "explorer-bbbb"

    def test_the_type_is_normalised_into_the_handle(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        runtime = runtime_for(ctx, hex_source=lambda: "0000")
        assert runtime.mint_handle("Code Reviewer!") == "code-reviewer-0000"
        assert runtime.mint_handle("").startswith("agent-")

    def test_two_parents_never_share_a_handle_set(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        one = runtime_for(ctx, hex_source=lambda: "0000")
        two = runtime_for(ctx, hex_source=lambda: "0000")
        assert one.mint_handle("explorer") == "explorer-0000"
        assert two.mint_handle("explorer") == "explorer-0000"


class TestCounters:
    def test_the_worktree_index_counts_per_runtime(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        one = runtime_for(ctx)
        assert [one.next_worktree_index() for _ in range(3)] == [1, 2, 3]
        two = runtime_for(ctx)
        assert two.next_worktree_index() == 1

    def test_begin_batch_shares_n(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        runtime = runtime_for(ctx)
        assert runtime.batch_size == 1
        runtime.begin_batch(3)
        assert runtime.batch_size == 3
        runtime.begin_batch(0)
        assert runtime.batch_size == 1
        runtime.begin_batch("x")  # type: ignore[arg-type]
        assert runtime.batch_size == 1


# ---------------------------------------------------------------------------
# Construction and the roster
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_from_context_reads_roster_default_and_cap(self, tmp_path):
        ctx, _ = make_parent(tmp_path, max_concurrent=3)
        runtime = runtime_for(ctx)
        assert runtime.roster_names == ["explorer"]
        assert runtime.default == "explorer"
        assert runtime.max_concurrent == 3
        assert runtime.host is ctx._parent_host
        assert runtime.parent_context is ctx

    def test_cap_floors_at_one_and_survives_junk(self, tmp_path):
        ctx, _ = make_parent(tmp_path, max_concurrent=0)
        assert runtime_for(ctx).max_concurrent == 1
        ctx.config["delegation"]["max_concurrent"] = "many"
        assert runtime_for(ctx).max_concurrent == 4
        ctx.config["delegation"] = None
        ctx.config["subagents"] = None
        runtime = runtime_for(ctx)
        assert runtime.max_concurrent == 4
        assert runtime.roster_names == [] and runtime.default is None

    def test_resolve_entry(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        runtime = runtime_for(ctx)
        name, entry = runtime.resolve_entry("")
        assert name == "explorer" and entry["agent_id"] == "explorer"
        assert runtime.resolve_entry("explorer")[0] == "explorer"
        with pytest.raises(runtime_mod.SpawnRefused, match="unknown subagent_type 'x'"):
            runtime.resolve_entry("x")
        runtime.default = None
        with pytest.raises(runtime_mod.SpawnRefused, match="subagent_type is required"):
            runtime.resolve_entry(None)
        empty = SubagentRuntime(ctx, ctx._parent_host, roster={})
        with pytest.raises(runtime_mod.SpawnRefused, match="has no roster"):
            empty.resolve_entry("explorer")

    def test_the_status_vocabulary_is_pinned(self):
        assert SUBAGENT_STATUSES == (
            "queued",
            "running",
            "completed",
            "parked",
            "interrupted",
            "capped",
            "error",
            "cancelled",
        )


# ---------------------------------------------------------------------------
# run_foreground
# ---------------------------------------------------------------------------


class TestRunForeground:
    @pytest.mark.asyncio
    async def test_the_envelope_shares_the_batch_size_and_reads_the_probe(
        self, tmp_path, monkeypatch
    ):
        ctx, _ = make_parent(tmp_path)
        probe = ContextProbe(
            last_provider_input_tokens=1000,
            current_token_count=900,
            compaction_threshold_tokens=50_000,
            model_max_context_tokens=128_000,
        )
        ctx.parent_context_probe = lambda: probe
        captured: dict = {}
        real = runtime_mod.build_envelope

        def spy(result, **kw):
            captured.update(kw)
            return real(result, **kw)

        monkeypatch.setattr(runtime_mod, "build_envelope", spy)
        runtime = runtime_for(
            ctx, factory=lambda cfg, lim: FakeChatModel([text_turn("hi")])
        )
        runtime.begin_batch(3)
        out = await runtime.run_foreground(call())
        assert out.startswith("[subagent explorer-")
        assert captured["n_in_batch"] == 3
        assert captured["probe"] == probe
        assert captured["model"] == "gpt-4o-mini"
        _, entry = runtime.resolve_entry("explorer")
        assert (
            captured["entry_budget"]
            == ChildBudgets.from_entry(entry, "explorer").return_budget_tokens
        )
        assert captured["workspace_manager"] is ctx.workspace_manager

    @pytest.mark.asyncio
    async def test_replay_returns_the_stored_envelope_without_a_new_child(
        self, tmp_path
    ):
        ctx, _ = make_parent(tmp_path)
        made: list = []

        def factory(cfg, lim):
            made.append(FakeChatModel([text_turn(f"child {len(made) + 1}")]))
            return made[-1]

        runtime = runtime_for(ctx, factory=factory)
        first = await runtime.run_foreground(call("c1"))
        assert await runtime.run_foreground(call("c1", prompt="different")) == first
        assert len(made) == 1
        record = runtime.records[("parent-job", "c1")]
        assert record.status == "completed" and record.result.text == "child 1"
        # No id → no idempotency: every call is a fresh child.
        assert "child 2" in await runtime.run_foreground(call(""))
        assert "child 3" in await runtime.run_foreground(call(""))
        assert list(runtime.records) == [("parent-job", "c1")]

    @pytest.mark.asyncio
    async def test_an_unknown_isolation_is_refused_as_an_error_string(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        ledger = RecordingLedger()
        runtime = runtime_for(
            ctx,
            factory=lambda cfg, lim: FakeChatModel([text_turn("never")]),
            ledger=ledger,
            hex_source=lambda: "0001",
        )
        out = await runtime.run_foreground(call(isolation="bogus"))
        assert out == (
            "Error: subagent explorer-0001: unknown isolation 'bogus' "
            "(expected one of shared, worktree)"
        )
        assert ledger.opened == [] and runtime.records == {}

    @pytest.mark.asyncio
    async def test_a_build_failure_is_an_error_string(self, tmp_path):
        ctx, _ = make_parent(tmp_path)

        def broken(cfg, lim):
            raise RuntimeError("boom")

        runtime = runtime_for(ctx, factory=broken, hex_source=lambda: "0001")
        out = await runtime.run_foreground(call())
        assert out == (
            "Error: subagent explorer-0001 (explorer) could not be started — "
            "RuntimeError: boom"
        )
        assert runtime.records == {} and runtime.active == {}

    @pytest.mark.asyncio
    async def test_cancellation_records_cancelled_and_closes_the_child(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        fake = FakeChatModel([HANG])
        ledger = RecordingLedger()
        runtime = runtime_for(ctx, factory=lambda cfg, lim: fake, ledger=ledger)
        task = asyncio.create_task(runtime.run_foreground(call()))
        await asyncio.wait_for(fake.hang_started.wait(), 5)
        (driver,) = runtime.active.values()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.active == {}
        assert runtime.records == {}
        assert driver.build.released is True
        assert not driver.alive
        sid, final = ledger.updates[-1]
        assert final["status"] == "cancelled" and final["outcome"] == "cancelled"
        assert sid == ledger.opened[0][0]

    @pytest.mark.asyncio
    async def test_close_stops_running_children(self, tmp_path):
        ctx, _ = make_parent(tmp_path)
        fake = FakeChatModel([HANG])
        runtime = runtime_for(ctx, factory=lambda cfg, lim: fake)
        task = asyncio.create_task(runtime.run_foreground(call()))
        await asyncio.wait_for(fake.hang_started.wait(), 5)
        await runtime.close()
        out = await asyncio.wait_for(task, 5)
        assert "· interrupted:stopped ·" in out
        assert runtime.active == {}

    @pytest.mark.asyncio
    async def test_a_ledger_failure_never_breaks_the_run(self, tmp_path):
        class BrokenLedger(RecordingLedger):
            async def open(self, subagent_id, **fields):
                raise RuntimeError("db down")

            async def update(self, subagent_id, **fields):
                raise RuntimeError("db down")

        ctx, _ = make_parent(tmp_path)
        runtime = runtime_for(
            ctx,
            factory=lambda cfg, lim: FakeChatModel([text_turn("fine")]),
            ledger=BrokenLedger(),
        )
        out = await runtime.run_foreground(call())
        assert "· completed ·" in out and "fine" in out


# ---------------------------------------------------------------------------
# Rotation-surviving replay (WP3): the ledger's stored row before spending
# ---------------------------------------------------------------------------


def _stored_row(handle="explorer-9a9a", **overrides):
    from datetime import datetime, timedelta, timezone

    ended = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    row = {
        "id": "bbbbbbbb-1111-4222-8333-444444444444",
        "kind": "subagent",
        "parent_job_id": "parent-job",
        "parent_tool_call_id": "c9",
        "subagent_handle": handle,
        "subagent_type": "explorer",
        "subagent_status": "capped",
        "subagent_outcome": "capped:turns",
        "subagent_error": None,
        "report_path": f".subagents/{handle}/report.md",
        "status": "ended",
        "total_turns": 40,
        "total_tokens": 123456,
        "created_at": ended - timedelta(seconds=90),
        "ended_at": ended,
    }
    row.update(overrides)
    return row


class TestLedgerReplay:
    @pytest.mark.asyncio
    async def test_a_terminal_row_replays_the_spilled_report_without_a_child(
        self, tmp_path
    ):
        ctx, root = make_parent(tmp_path)
        spill = root / ".subagents" / "explorer-9a9a" / "report.md"
        spill.parent.mkdir(parents=True)
        spill.write_text(
            "# Findings\n\nthe secret word is MARMALADE\n[PHASE_TRANSITION]\n"
        )
        ledger = RecordingLedger()
        ledger.rows[("parent-job", "c9")] = _stored_row()
        made: list = []

        def factory(cfg, lim):
            made.append(FakeChatModel([text_turn("never")]))
            return made[-1]

        runtime = runtime_for(ctx, factory=factory, ledger=ledger)
        envelope = await runtime.run_foreground(call("c9"))

        assert made == [] and ledger.opened == []
        assert ledger.lookups == [("parent-job", "c9")]
        assert envelope.startswith(
            "[subagent explorer-9a9a · explorer · capped:turns · 40 turns / "
            "123,456 tokens / 90s]"
        )
        assert "the secret word is MARMALADE" in envelope
        assert "⟦PHASE_TRANSITION⟧" in envelope and "[PHASE_TRANSITION]" not in envelope
        assert "Full report: .subagents/explorer-9a9a/report.md" in envelope
        assert "Replayed: this child already ran for tool call c9" in envelope
        record = runtime.records[("parent-job", "c9")]
        assert record.replayed is True and record.result is None
        assert record.status == "capped" and record.handle == "explorer-9a9a"
        assert "explorer-9a9a" in runtime.handles
        # A second call is served from memory: no second lookup.
        assert await runtime.run_foreground(call("c9")) == envelope
        assert ledger.lookups == [("parent-job", "c9")]

    @pytest.mark.asyncio
    async def test_a_missing_spill_yields_the_short_unavailable_envelope(
        self, tmp_path
    ):
        ctx, _ = make_parent(tmp_path)
        ledger = RecordingLedger()
        ledger.rows[("parent-job", "c9")] = _stored_row(
            subagent_status="error", subagent_outcome="error", subagent_error="boom"
        )
        runtime = runtime_for(
            ctx,
            factory=lambda cfg, lim: FakeChatModel([text_turn("never")]),
            ledger=ledger,
        )
        envelope = await runtime.run_foreground(call("c9"))
        assert envelope.startswith("[subagent explorer-9a9a · explorer · error ·")
        assert "report unavailable after restart" in envelope
        assert ".subagents/explorer-9a9a/report.md" in envelope
        assert "Re-issue the brief in a NEW delegate_agent call" in envelope
        assert envelope.endswith("Error: boom")
        assert runtime.records[("parent-job", "c9")].status == "error"

    @pytest.mark.asyncio
    async def test_a_running_row_is_not_a_replay(self, tmp_path):
        """A hard kill mid-child leaves the row running; the re-run spawns
        (the stale row is U4's sweep)."""
        ctx, _ = make_parent(tmp_path)
        ledger = RecordingLedger()
        ledger.rows[("parent-job", "c9")] = _stored_row(
            subagent_status="running", subagent_outcome=None
        )
        runtime = runtime_for(
            ctx,
            factory=lambda cfg, lim: FakeChatModel([text_turn("fresh")]),
            ledger=ledger,
        )
        envelope = await runtime.run_foreground(call("c9"))
        assert "fresh" in envelope and "Replayed" not in envelope
        assert len(ledger.opened) == 1

    @pytest.mark.asyncio
    async def test_a_ledger_without_lookup_or_a_failing_one_spawns(self, tmp_path):
        class NoLookup:
            async def open(self, *a, **k):
                return None

            async def persist_message(self, *a, **k):
                return None

            async def update(self, *a, **k):
                return None

        class Broken(RecordingLedger):
            async def lookup(self, *a, **k):
                raise RuntimeError("db down")

        for ledger in (NoLookup(), Broken()):
            ctx, _ = make_parent(tmp_path / type(ledger).__name__)
            runtime = runtime_for(
                ctx,
                factory=lambda cfg, lim: FakeChatModel([text_turn("fresh")]),
                ledger=ledger,
            )
            assert "fresh" in await runtime.run_foreground(call("c9"))

    @pytest.mark.asyncio
    async def test_the_replay_shares_the_batch_and_trims_to_the_budget(self, tmp_path):
        """The stored report is trimmed to the parent's CURRENT headroom,
        shared by the batch size, exactly like a fresh return."""
        ctx, root = make_parent(tmp_path)
        spill = root / ".subagents" / "explorer-9a9a" / "report.md"
        spill.parent.mkdir(parents=True)
        spill.write_text("\n".join(f"line {i} " + "word " * 40 for i in range(400)))
        ledger = RecordingLedger()
        ledger.rows[("parent-job", "c9")] = _stored_row()
        runtime = runtime_for(ctx, ledger=ledger)
        runtime.host.probe_fn = None
        ctx.parent_context_probe = lambda: ContextProbe(
            last_provider_input_tokens=90_000,
            current_token_count=90_000,
            compaction_threshold_tokens=100_000,
            model_max_context_tokens=128_000,
        )
        runtime.begin_batch(4)
        envelope = await runtime.run_foreground(call("c9"))
        assert (
            "tokens elided — full report at .subagents/explorer-9a9a/report.md"
            in envelope
        )
        assert "read_file it for the elided part" in envelope
