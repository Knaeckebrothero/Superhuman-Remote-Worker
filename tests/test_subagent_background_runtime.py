"""U4-B background runtime: durable admission, delivery, controls and teardown."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from src.subagents import NullLedger, RecordingLedger
from tests._fake_chat_model import HANG, FakeChatModel, text_turn
from tests.test_subagent_runtime import call, make_parent, runtime_for


class GateModel(FakeChatModel):
    """Pause the first provider response until a control wins the race."""

    def __init__(self, final: str):
        super().__init__([])
        self.started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.final = final

    async def astream(self, messages, **kwargs):
        del kwargs
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            self.started.set()
            await self.release_first.wait()
            yield AIMessageChunk(content="unfinished")
            return
        for chunk in text_turn(self.final):
            await asyncio.sleep(0)
            yield chunk


class StrictLedger(RecordingLedger):
    def __init__(self) -> None:
        super().__init__()
        self.open_started = asyncio.Event()
        self.allow_open = asyncio.Event()
        self.allow_open.set()
        self.terminal = asyncio.Event()
        self.terminal_calls: list[tuple[str, dict[str, Any]]] = []
        self.live: list[dict[str, Any]] = []
        self.refuse_open = False

    async def open(self, subagent_id: str, **fields: Any) -> dict[str, str] | None:
        self.open_started.set()
        await self.allow_open.wait()
        self.opened.append((subagent_id, dict(fields)))
        if self.refuse_open:
            return None
        return {
            "thread_id": subagent_id,
            "runtime_generation": "aaaaaaaa-1111-4222-8333-444444444444",
        }

    async def terminalize_and_enqueue(
        self, subagent_id: str, **fields: Any
    ) -> dict[str, Any]:
        self.terminal_calls.append((subagent_id, dict(fields)))
        opened = next(data for child, data in self.opened if child == subagent_id)
        delivery = {
            "id": fields["delivery_id"],
            "source": "subagent",
            "thread_id": subagent_id,
            "handle": opened["handle"],
            "run_generation": "aaaaaaaa-1111-4222-8333-444444444444",
            "message": fields["message"],
            "timestamp": fields["timestamp"],
        }
        self.terminal.set()
        return {
            "result": "applied",
            "delivery_state": "queued",
            "delivery": delivery,
        }

    async def list_live(self, parent_job_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.live]


@pytest.mark.asyncio
async def test_provider_is_not_constructed_before_durable_receipt(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    ledger.allow_open.clear()
    factory_called = asyncio.Event()

    def factory(config, limits):
        factory_called.set()
        return FakeChatModel([text_turn("done")])

    runtime = runtime_for(ctx, factory=factory, ledger=ledger)
    spawn = asyncio.create_task(
        runtime.run_background(call(run_in_background=True, description="durable"))
    )
    await ledger.open_started.wait()
    await asyncio.sleep(0)
    assert not factory_called.is_set()
    ledger.allow_open.set()
    receipt = await spawn
    assert "· queued]" in receipt
    assert "aaaaaaaa-1111-4222-8333-444444444444" in receipt
    await asyncio.wait_for(factory_called.wait(), 5)
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    await runtime.close()


@pytest.mark.asyncio
async def test_create_refusal_never_schedules_provider(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    ledger.refuse_open = True
    made: list[FakeChatModel] = []
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: made.append(FakeChatModel([text_turn("never")])),
        ledger=ledger,
    )
    result = await runtime.run_background(call(run_in_background=True))
    assert result.startswith("Error: background subagent")
    assert made == []
    assert await runtime.list_agents() == []
    assert not runtime.has_completion_blockers()


@pytest.mark.asyncio
async def test_null_ledger_refuses_before_build_or_provider_task(tmp_path):
    ctx, _ = make_parent(tmp_path)
    made: list[Any] = []
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: made.append(object()),
        ledger=NullLedger(),
    )
    result = await runtime.run_background(call(run_in_background=True))
    assert "returned no create receipt" in result
    assert made == []
    assert runtime.active == {}
    assert await runtime.list_agents() == []


@pytest.mark.asyncio
async def test_terminal_commit_precedes_local_publish_and_drain_releases_backpressure(
    tmp_path,
):
    ctx, _ = make_parent(tmp_path, max_concurrent=1)
    ledger = StrictLedger()
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: FakeChatModel([text_turn("evidence")]),
        ledger=ledger,
    )
    await runtime.run_background(call("one", run_in_background=True))
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    assert runtime.has_completion_blockers()
    refused = await runtime.run_background(call("two", run_in_background=True))
    assert "backlog is full (1)" in refused
    deliveries = runtime.drain_local_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0]["source"] == "subagent"
    assert deliveries[0]["message"].startswith("[subagent explorer-")
    assert not runtime.has_completion_blockers()
    ledger.terminal.clear()
    accepted = await runtime.run_background(call("three", run_in_background=True))
    assert "· queued]" in accepted
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    await runtime.close()


@pytest.mark.asyncio
async def test_wait_uses_condition_and_wakes_on_terminal_commit(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    fake = FakeChatModel([HANG])
    runtime = runtime_for(ctx, factory=lambda config, limits: fake, ledger=ledger)
    receipt = await runtime.run_background(call(run_in_background=True))
    handle = receipt.split()[1]
    waiter = asyncio.create_task(runtime.wait_agent(handle, 10))
    await asyncio.wait_for(fake.hang_started.wait(), 5)
    await runtime.stop_agent(handle, 0.1)
    result = await asyncio.wait_for(waiter, 5)
    assert result["result"] == "ready"
    assert result["timed_out"] is False
    assert result["status"] in {"interrupted", "cancelled"}
    await runtime.close()


@pytest.mark.asyncio
async def test_steer_wins_before_finish_and_is_included_before_terminalization(
    tmp_path,
):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    model = GateModel("steered evidence")
    runtime = runtime_for(ctx, factory=lambda config, limits: model, ledger=ledger)
    receipt = await runtime.run_background(call(run_in_background=True))
    handle = receipt.split()[1]
    await asyncio.wait_for(model.started.wait(), 5)
    assert (await runtime.message_agent(handle, "new evidence"))["result"] == "accepted"
    model.release_first.set()
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    assert "steered evidence" in ledger.terminal_calls[0][1]["message"]
    assert (await runtime.message_agent(handle, "too late"))["result"] == "not_live"
    assert len(ledger.terminal_calls) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_multiple_live_steers_remain_in_one_tracked_brief(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    model = GateModel("steered evidence")
    runtime = runtime_for(ctx, factory=lambda config, limits: model, ledger=ledger)
    receipt = await runtime.run_background(call(run_in_background=True))
    handle = receipt.split()[1]
    await asyncio.wait_for(model.started.wait(), 5)
    assert (await runtime.message_agent(handle, "first steer"))["result"] == "accepted"
    assert (await runtime.message_agent(handle, "second steer"))["result"] == (
        "accepted"
    )
    model.release_first.set()
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    assert len(model.calls) == 3
    # The interrupted first stream has no completed usage record; both
    # accepted steer turns are nevertheless inside the terminalized brief.
    assert ledger.terminal_calls[0][1]["turns"] == 2
    tasks = list(runtime._background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
    assert runtime._background_tasks == {}
    await runtime.close()


@pytest.mark.asyncio
async def test_stop_attempts_one_toolless_partial_before_terminal_delivery(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    model = GateModel("partial before stop")
    runtime = runtime_for(ctx, factory=lambda config, limits: model, ledger=ledger)
    receipt = await runtime.run_background(call(run_in_background=True))
    handle = receipt.split()[1]
    await asyncio.wait_for(model.started.wait(), 5)
    stopping = asyncio.create_task(runtime.stop_agent(handle, 2))
    await asyncio.sleep(0)
    model.release_first.set()
    stopped = await asyncio.wait_for(stopping, 5)
    assert stopped["result"] == "stopped"
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    terminal = ledger.terminal_calls[0][1]
    assert terminal["outcome"] == "interrupted:stopped"
    assert "partial before stop" in terminal["message"]
    assert len(model.calls) == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_stop_before_driver_start_never_reaches_provider(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    model = FakeChatModel([text_turn("must not run")])
    runtime = runtime_for(ctx, factory=lambda config, limits: model, ledger=ledger)
    published = asyncio.Event()
    release_notification = asyncio.Event()
    original_notify = runtime._notify_changed

    async def gated_notify():
        if runtime.active and not ledger.terminal.is_set():
            published.set()
            await release_notification.wait()
        await original_notify()

    runtime._notify_changed = gated_notify
    spawning = asyncio.create_task(runtime.run_background(call(run_in_background=True)))
    await asyncio.wait_for(published.wait(), 5)
    agents = await runtime.list_agents()
    handle = agents[0]["handle"]
    stopping = asyncio.create_task(runtime.stop_agent(handle, 0.1))
    await asyncio.sleep(0)
    release_notification.set()
    receipt = await asyncio.wait_for(spawning, 5)
    assert handle in receipt
    await asyncio.wait_for(stopping, 5)
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    assert model.calls == []
    assert ledger.terminal_calls[0][1]["outcome"] == "interrupted:stopped"
    await runtime.close()


@pytest.mark.asyncio
async def test_stop_discards_queued_steer_before_single_synthesis(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    model = GateModel("partial before stop")
    runtime = runtime_for(ctx, factory=lambda config, limits: model, ledger=ledger)
    receipt = await runtime.run_background(call(run_in_background=True))
    handle = receipt.split()[1]
    await asyncio.wait_for(model.started.wait(), 5)
    assert (await runtime.message_agent(handle, "stale queued steer"))["result"] == (
        "accepted"
    )
    stopping = asyncio.create_task(runtime.stop_agent(handle, 2))
    await asyncio.sleep(0)
    model.release_first.set()
    await asyncio.wait_for(stopping, 5)
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    assert len(model.calls) == 2
    assert "stale queued steer" not in str(model.calls[1])
    assert "Do NOT call any more tools" in str(model.calls[1])
    assert ledger.terminal_calls[0][1]["outcome"] == "interrupted:stopped"
    await runtime.close()


@pytest.mark.asyncio
async def test_abandon_cancels_local_work_without_terminal_or_delivery(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    fake = FakeChatModel([HANG])
    runtime = runtime_for(ctx, factory=lambda config, limits: fake, ledger=ledger)
    await runtime.run_background(call(run_in_background=True))
    await asyncio.wait_for(fake.hang_started.wait(), 5)
    await runtime.abandon("lease stolen")
    assert ledger.terminal_calls == []
    assert runtime.drain_local_deliveries() == []
    assert runtime.active == {}


@pytest.mark.asyncio
async def test_orphan_recovery_delivers_background_once_and_never_calls_provider(
    tmp_path,
):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    child = "bbbbbbbb-1111-4222-8333-444444444444"
    ledger.opened.append(
        (
            child,
            {"handle": "explorer-dead", "subagent_type": "explorer"},
        )
    )
    ledger.live = [
        {
            "thread_id": child,
            "runtime_generation": "aaaaaaaa-1111-4222-8333-444444444444",
            "parent_job_id": "parent-job",
            "handle": "explorer-dead",
            "subagent_type": "explorer",
            "parent_tool_call_id": "old-call",
            "run_in_background": True,
            "total_turns": 2,
            "total_tokens": 50,
        }
    ]
    made: list[Any] = []
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: made.append(object()),
        ledger=ledger,
    )
    recovered = await runtime.recover_orphans()
    assert recovered[0]["status"] == "interrupted"
    assert made == []
    assert len(ledger.terminal_calls) == 1
    assert "interrupted:parent_restart" in ledger.terminal_calls[0][1]["message"]
    assert await runtime.recover_orphans() == []
    assert len(ledger.terminal_calls) == 1
    replay = await runtime.run_background(call("old-call", run_in_background=True))
    assert child in replay
    assert "aaaaaaaa-1111-4222-8333-444444444444" in replay
    assert len(ledger.opened) == 1
    assert made == []


@pytest.mark.asyncio
async def test_quiesce_adopts_inflight_durable_create_and_settles_without_provider(
    tmp_path,
):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    ledger.allow_open.clear()
    made: list[Any] = []
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: made.append(
            FakeChatModel([text_turn("must not run")])
        ),
        ledger=ledger,
    )
    spawning = asyncio.create_task(runtime.run_background(call(run_in_background=True)))
    await ledger.open_started.wait()
    quiescing = asyncio.create_task(runtime.quiesce("rotation"))
    await asyncio.sleep(0)
    assert not quiescing.done()
    ledger.allow_open.set()
    result = await asyncio.wait_for(spawning, 5)
    await asyncio.wait_for(quiescing, 5)
    assert "fenced before provider start" in result
    assert made == []
    assert len(ledger.terminal_calls) == 1
    assert ledger.terminal_calls[0][1]["outcome"] == "interrupted:stopped"


@pytest.mark.asyncio
async def test_quiesce_awaits_terminal_delivery(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    fake = FakeChatModel([HANG])
    runtime = runtime_for(ctx, factory=lambda config, limits: fake, ledger=ledger)
    await runtime.run_background(call(run_in_background=True))
    await asyncio.wait_for(fake.hang_started.wait(), 5)
    await runtime.quiesce("rotation")
    assert ledger.terminal.is_set()
    assert runtime.active == {}
    assert "no new work accepted" in await runtime.run_background(
        call("later", run_in_background=True)
    )


@pytest.mark.asyncio
async def test_quiesce_fails_closed_when_terminal_delivery_cannot_commit(tmp_path):
    class RefusingTerminalLedger(StrictLedger):
        def __init__(self):
            super().__init__()
            self.attempted = asyncio.Event()

        async def terminalize_and_enqueue(self, subagent_id: str, **fields: Any):
            del subagent_id, fields
            self.attempted.set()
            raise RuntimeError("transport unavailable")

    ctx, _ = make_parent(tmp_path)
    ledger = RefusingTerminalLedger()
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: FakeChatModel([text_turn("evidence")]),
        ledger=ledger,
    )
    await runtime.run_background(call(run_in_background=True))
    await asyncio.wait_for(ledger.attempted.wait(), 5)
    with pytest.raises(RuntimeError, match="could not commit terminal delivery"):
        await runtime.quiesce("rotation")


@pytest.mark.asyncio
async def test_quiesce_joins_delivery_pending_task_before_any_retry(tmp_path):
    class GatedTerminalLedger(StrictLedger):
        def __init__(self):
            super().__init__()
            self.commit_started = asyncio.Event()
            self.allow_commit = asyncio.Event()
            self.commit_calls = 0

        async def terminalize_and_enqueue(self, subagent_id: str, **fields: Any):
            self.commit_calls += 1
            self.commit_started.set()
            await self.allow_commit.wait()
            return await super().terminalize_and_enqueue(subagent_id, **fields)

    ctx, _ = make_parent(tmp_path)
    ledger = GatedTerminalLedger()
    runtime = runtime_for(
        ctx,
        factory=lambda config, limits: FakeChatModel([text_turn("evidence")]),
        ledger=ledger,
    )
    await runtime.run_background(call(run_in_background=True))
    await asyncio.wait_for(ledger.commit_started.wait(), 5)
    quiescing = asyncio.create_task(runtime.quiesce("rotation"))
    await asyncio.sleep(0)
    assert not quiescing.done()
    assert ledger.commit_calls == 1
    ledger.allow_commit.set()
    await asyncio.wait_for(quiescing, 5)
    assert ledger.commit_calls == 1
    assert runtime._background_tasks == {}
