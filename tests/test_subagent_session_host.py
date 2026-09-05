"""True thread-parent semantics shared by the U5 session integration."""

from __future__ import annotations

import asyncio

import pytest

from agent.subagents import SessionHost, SubagentRuntime
from tests._fake_chat_model import FakeChatModel, text_turn
from tests.test_subagent_background_runtime import StrictLedger
from tests.test_subagent_runtime import call, make_parent

PARENT_THREAD_ID = "11111111-2222-4333-8444-555555555555"


def _session_runtime(ctx, ledger, event_fn=None) -> SubagentRuntime:
    host = SessionHost(
        thread_id=PARENT_THREAD_ID,
        agent_type="persistent",
        tool_context=ctx,
        admission_fn=lambda: True,
        effect_authority_fn=lambda: True,
        event_fn=event_fn,
    )
    runtime = SubagentRuntime.from_context(
        ctx,
        host,
        ledger=ledger,
        llm_factory=lambda config, limits: FakeChatModel([text_turn("evidence")]),
        driver_kwargs={
            "watcher_poll_interval": 0.01,
            "archiver": None,
            "archive_fn": lambda **kwargs: None,
        },
    )
    ctx._parent_host = host
    ctx.subagent_runtime = runtime
    return runtime


@pytest.mark.asyncio
async def test_session_child_uses_only_parent_thread_identity(tmp_path):
    ctx, _ = make_parent(tmp_path)
    ledger = StrictLedger()
    runtime = _session_runtime(ctx, ledger)

    await runtime.run_background(call(run_in_background=True))
    await asyncio.wait_for(ledger.terminal.wait(), 5)

    _child_id, opened = ledger.opened[0]
    assert opened["parent_thread_id"] == PARENT_THREAD_ID
    assert opened["parent_job_id"] is None
    assert runtime._key(call("same-call")) == (PARENT_THREAD_ID, "same-call")
    await runtime.close()


@pytest.mark.asyncio
async def test_durable_session_event_is_woken_without_lane_b_duplicate(tmp_path):
    ctx, _ = make_parent(tmp_path, max_concurrent=1)
    ledger = StrictLedger()
    woken: list[str] = []

    async def wake(envelope: str) -> None:
        await asyncio.sleep(0)
        woken.append(envelope)

    runtime = _session_runtime(ctx, ledger, wake)
    await runtime.run_background(call(run_in_background=True))
    await asyncio.wait_for(ledger.terminal.wait(), 5)
    tasks = list(runtime._background_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)

    assert len(woken) == 1
    assert woken[0].startswith("[subagent explorer-")
    assert runtime.drain_local_deliveries() == []
    assert not runtime.has_completion_blockers()
    await runtime.close()
