"""SessionSubagentLedger: thread-only parents and per-operation authority."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from shared.session_subagent_authority import (
    SessionParentAuthority,
    SessionParentAuthorityRefused,
    session_subagent_delivery_id,
)
from agent.subagents.ledger import SubagentLedger
from agent.subagents.persistence import SubagentPersistenceRefused
from agent.subagents.session_persistence import SessionSubagentLedger

PARENT = "aaaaaaaa-1111-4222-8333-444444444444"
OTHER_PARENT = "bbbbbbbb-1111-4222-8333-444444444444"
CHILD = "cccccccc-1111-4222-8333-444444444444"
GENERATION = "dddddddd-1111-4222-8333-444444444444"
NEXT_GENERATION = "eeeeeeee-1111-4222-8333-444444444444"
AGENT = "ffffffff-1111-4222-8333-444444444444"
ATTACH = "11111111-1111-4111-8111-111111111111"
PARENT_INPUT = "22222222-1111-4111-8111-111111111111"
PARENT_AI = "33333333-1111-4111-8111-111111111111"


def _pinned() -> SessionParentAuthority:
    return SessionParentAuthority(
        execution_lane="pinned",
        parent_thread_id=PARENT,
        agent_id=AGENT,
        pod_uid="pod-uid",
        session_runtime_generation=GENERATION,
        runtime_attach_token=ATTACH,
    )


def _stateless(token: int) -> SessionParentAuthority:
    return SessionParentAuthority(
        execution_lane="stateless",
        parent_thread_id=PARENT,
        lease_token=token,
        executor_id=f"worker-{token}",
        executor_pod_uid=f"pod-{token}",
    )


def _client() -> SimpleNamespace:
    return SimpleNamespace(
        create_session_subagent_thread=AsyncMock(
            return_value={
                "thread_id": CHILD,
                "runtime_generation": GENERATION,
            }
        ),
        terminalize_session_subagent_thread=AsyncMock(
            return_value={
                "result": "applied",
                "thread_id": CHILD,
                "runtime_generation": GENERATION,
                "delivery_id": str(session_subagent_delivery_id(CHILD, GENERATION)),
                "delivery_state": "queued",
            }
        ),
        reopen_session_subagent_thread=AsyncMock(
            return_value={
                "result": "reopened",
                "thread_id": CHILD,
                "runtime_generation": NEXT_GENERATION,
            }
        ),
        list_live_session_subagent_threads=AsyncMock(return_value=[]),
    )


def _pool(row=None) -> SimpleNamespace:
    return SimpleNamespace(
        session_parent_authority_current=AsyncMock(return_value=True),
        save_session_subagent_thread_message=AsyncMock(
            return_value={"id": CHILD, "seq": 1}
        ),
        save_session_subagent_thread_messages=AsyncMock(return_value=True),
        update_session_subagent_thread=AsyncMock(return_value=True),
        get_session_subagent_thread_by_call=AsyncMock(return_value=row),
    )


def _fields(**overrides):
    fields = {
        "status": "running",
        "handle": "reviewer-1a2b",
        "subagent_type": "reviewer",
        "parent_job_id": None,
        "parent_thread_id": PARENT,
        "parent_tool_call_id": "call-1",
        "parent_input_message_id": PARENT_INPUT,
        "parent_ai_message_id": PARENT_AI,
        "parent_iteration": 2,
        "isolation": "shared",
        "write_policy": "none",
        "brief_description": "review the change",
        "fork": False,
        "run_in_background": False,
    }
    fields.update(overrides)
    return fields


def test_construction_requires_durable_halves_and_stateless_provider():
    with pytest.raises(ValueError):
        SessionSubagentLedger(
            None, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
        )
    with pytest.raises(ValueError):
        SessionSubagentLedger(
            _client(), None, parent_thread_id=PARENT, parent_authority=_pinned()
        )
    with pytest.raises(ValueError, match="per-operation"):
        SessionSubagentLedger(
            _client(),
            _pool(),
            parent_thread_id=PARENT,
            parent_authority=_stateless(1),
        )
    ledger = SessionSubagentLedger(
        _client(), _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )
    assert isinstance(ledger, SubagentLedger)


def test_from_context_fails_closed_without_authority_provider():
    assert SessionSubagentLedger.from_context(SimpleNamespace()) is None
    assert (
        SessionSubagentLedger.from_context(
            SimpleNamespace(
                orchestrator_client=_client(),
                postgres_db=_pool(),
                thread_id=PARENT,
            )
        )
        is None
    )
    ctx = SimpleNamespace(
        orchestrator_client=_client(),
        postgres_db=_pool(),
        thread_id=PARENT,
        _session_parent_authority_provider=lambda: _stateless(1),
    )
    assert isinstance(SessionSubagentLedger.from_context(ctx), SessionSubagentLedger)


@pytest.mark.asyncio
async def test_open_uses_thread_only_and_exact_create_receipt():
    client = _client()
    ledger = SessionSubagentLedger(
        client, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )

    receipt = await ledger.open(CHILD, **_fields())

    assert receipt == {"thread_id": CHILD, "runtime_generation": GENERATION}
    client.create_session_subagent_thread.assert_awaited_once_with(
        PARENT,
        parent_authority=_pinned(),
        subagent_id=CHILD,
        handle="reviewer-1a2b",
        subagent_type="reviewer",
        parent_tool_call_id="call-1",
        parent_input_message_id=PARENT_INPUT,
        parent_ai_message_id=PARENT_AI,
        isolation="shared",
        write_policy="none",
        owned_paths=[],
        brief_description="review the change",
        parent_iteration=2,
        fork=False,
        run_in_background=False,
        initial_status="running",
    )
    assert ledger.rows == {CHILD: CHILD}
    assert ledger.generations == {CHILD: GENERATION}


@pytest.mark.asyncio
async def test_open_normalizes_the_durable_tool_call_key():
    client = _client()
    ledger = SessionSubagentLedger(
        client, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )

    await ledger.open(CHILD, **_fields(parent_tool_call_id="  call-1  "))

    assert (
        client.create_session_subagent_thread.await_args.kwargs["parent_tool_call_id"]
        == "call-1"
    )


@pytest.mark.asyncio
async def test_worker_parent_shape_is_never_silently_reused():
    client = _client()
    ledger = SessionSubagentLedger(
        client, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )
    with pytest.raises(SubagentPersistenceRefused, match="worker-job"):
        await ledger.open(CHILD, **_fields(parent_job_id=OTHER_PARENT))
    client.create_session_subagent_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_authority_is_refreshed_for_every_operation():
    issued: list[int] = []

    def provider() -> SessionParentAuthority:
        token = len(issued) + 1
        issued.append(token)
        return _stateless(token)

    client, pool = _client(), _pool()
    ledger = SessionSubagentLedger(
        client,
        pool,
        parent_thread_id=PARENT,
        authority_provider=provider,
    )
    await ledger.open(CHILD, **_fields())
    await ledger.persist_message(CHILD, HumanMessage(content="hello"), 1)
    await ledger.update(CHILD, status="running", turns=1)
    await ledger.lookup(PARENT, "call-1")

    assert issued == [1, 2, 3, 4]
    assert (
        client.create_session_subagent_thread.await_args.kwargs[
            "parent_authority"
        ].lease_token
        == 1
    )
    assert (
        pool.save_session_subagent_thread_message.await_args.kwargs[
            "parent_authority"
        ].lease_token
        == 2
    )
    assert (
        pool.update_session_subagent_thread.await_args.kwargs[
            "parent_authority"
        ].lease_token
        == 3
    )
    assert (
        pool.get_session_subagent_thread_by_call.await_args.kwargs[
            "parent_authority"
        ].lease_token
        == 4
    )


@pytest.mark.asyncio
async def test_authority_provider_may_observe_an_async_lease_handle():
    calls = 0

    async def provider() -> SessionParentAuthority:
        nonlocal calls
        calls += 1
        return _stateless(calls)

    ledger = SessionSubagentLedger(
        _client(),
        _pool(),
        parent_thread_id=PARENT,
        authority_provider=provider,
    )
    await ledger.open(CHILD, **_fields())
    await ledger.update(CHILD, status="running")
    assert calls == 2


@pytest.mark.asyncio
async def test_transcript_seed_and_generation_refusal_are_strict():
    client, pool = _client(), _pool()
    ledger = SessionSubagentLedger(
        client, pool, parent_thread_id=PARENT, parent_authority=_pinned()
    )
    await ledger.open(CHILD, **_fields(fork=True))

    await ledger.persist_message(CHILD, AIMessage(content="answer", id="msg-ai"), 2)
    assert (
        pool.save_session_subagent_thread_message.await_args.kwargs["parent_thread_id"]
        == PARENT
    )
    assert await ledger.persist_seed(
        CHILD, [HumanMessage(content="seed", id="msg-seed")]
    )
    seed = pool.save_session_subagent_thread_messages.await_args.kwargs["messages"]
    assert seed[0]["provider_raw"]["_srw_subagent_fork_seed_v1"]

    pool.save_session_subagent_thread_messages.return_value = False
    with pytest.raises(SubagentPersistenceRefused, match="seed generation"):
        await ledger.persist_seed(CHILD, [HumanMessage(content="stale")])


@pytest.mark.asyncio
async def test_transcript_and_lifecycle_never_succeed_without_an_open_generation():
    ledger = SessionSubagentLedger(
        _client(), _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )

    with pytest.raises(SubagentPersistenceRefused, match="transcript has no durable"):
        await ledger.persist_message(CHILD, HumanMessage(content="orphan"), 1)
    with pytest.raises(SubagentPersistenceRefused, match="lifecycle has no durable"):
        await ledger.update(CHILD, status="completed")
    assert not await ledger.persist_seed(CHILD, [HumanMessage(content="orphan seed")])


@pytest.mark.asyncio
async def test_background_terminal_uses_stable_server_delivery_and_pinned_authority():
    client = _client()
    ledger = SessionSubagentLedger(
        client, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )
    await ledger.open(CHILD, **_fields(status="queued", run_in_background=True))
    await ledger.update(CHILD, status="running")
    expected = str(session_subagent_delivery_id(CHILD, GENERATION))

    result = await ledger.terminalize_and_enqueue(
        CHILD,
        delivery_id=expected,
        message="evidence",
        timestamp="2026-09-02T01:00:00+00:00",
        status="completed",
        turns=2,
    )

    assert result["result"] == "applied"
    client.terminalize_session_subagent_thread.assert_awaited_once_with(
        PARENT,
        CHILD,
        parent_authority=_pinned(),
        runtime_generation=GENERATION,
        subagent_status="completed",
        run_in_background=True,
        message="evidence",
        turns=2,
    )
    with pytest.raises(SubagentPersistenceRefused, match="generation-stable"):
        await ledger.terminalize_and_enqueue(
            CHILD,
            delivery_id=OTHER_PARENT,
            message="evidence",
            timestamp="now",
            status="completed",
        )


@pytest.mark.asyncio
async def test_foreground_orphan_terminal_uses_explicit_stable_delivery_path():
    client = _client()
    ledger = SessionSubagentLedger(
        client, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )
    await ledger.open(CHILD, **_fields(status="running"))
    expected = str(session_subagent_delivery_id(CHILD, GENERATION))

    result = await ledger.terminalize_foreground_orphan_and_enqueue(
        CHILD,
        delivery_id=expected,
        message="durable partial evidence",
        status="interrupted",
        outcome="interrupted:parent_restart",
        turns=2,
    )

    assert result["result"] == "applied"
    client.terminalize_session_subagent_thread.assert_awaited_once_with(
        PARENT,
        CHILD,
        parent_authority=_pinned(),
        runtime_generation=GENERATION,
        subagent_status="interrupted",
        run_in_background=False,
        message="durable partial evidence",
        foreground_orphan_recovery=True,
        outcome="interrupted:parent_restart",
        turns=2,
    )
    with pytest.raises(SubagentPersistenceRefused, match="terminal status"):
        await ledger.terminalize_foreground_orphan_and_enqueue(
            CHILD,
            delivery_id=expected,
            message="evidence",
            status="running",
        )


@pytest.mark.asyncio
async def test_stateless_background_is_a_typed_refusal_before_create():
    client = _client()
    ledger = SessionSubagentLedger(
        client,
        _pool(),
        parent_thread_id=PARENT,
        authority_provider=lambda: _stateless(7),
    )
    with pytest.raises(SessionParentAuthorityRefused) as excinfo:
        await ledger.open(CHILD, **_fields(status="queued", run_in_background=True))
    assert excinfo.value.reason == "stateless_background_unsupported"
    client.create_session_subagent_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_live_validates_every_row_before_adoption():
    client = _client()
    valid = {
        "thread_id": CHILD,
        "id": CHILD,
        "parent_job_id": None,
        "parent_thread_id": PARENT,
        "runtime_generation": GENERATION,
        "handle": "reviewer-1a2b",
        "subagent_type": "reviewer",
        "status": "running",
        "thread_status": "active",
        "run_in_background": True,
    }
    client.list_live_session_subagent_threads.return_value = [
        valid,
        {**valid, "thread_id": "not-a-uuid"},
    ]
    ledger = SessionSubagentLedger(
        client, _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )
    with pytest.raises(SubagentPersistenceRefused, match="malformed"):
        await ledger.list_live(PARENT)
    assert ledger.rows == {}

    client.list_live_session_subagent_threads.return_value = [valid]
    assert await ledger.list_live(PARENT) == [valid]
    assert ledger.generations == {CHILD: GENERATION}


@pytest.mark.asyncio
async def test_lookup_requires_thread_parent_and_terminal_shape():
    row = {
        "id": CHILD,
        "parent_job_id": None,
        "parent_thread_id": PARENT,
        "parent_tool_call_id": "call-1",
        "subagent_status": "completed",
    }
    pool = _pool(row)
    ledger = SessionSubagentLedger(
        _client(), pool, parent_thread_id=PARENT, parent_authority=_pinned()
    )
    assert await ledger.lookup(PARENT, "call-1") == row
    with pytest.raises(SessionParentAuthorityRefused, match="parent_mismatch"):
        await ledger.lookup(OTHER_PARENT, "call-1")

    pool.get_session_subagent_thread_by_call.return_value = {
        **row,
        "parent_job_id": OTHER_PARENT,
    }
    with pytest.raises(SubagentPersistenceRefused, match="another parent"):
        await ledger.lookup(PARENT, "call-1")

    live = {
        **row,
        "parent_job_id": None,
        "subagent_status": "running",
    }
    pool.get_session_subagent_thread_by_call.return_value = live
    assert await ledger.lookup(PARENT, "call-1") == live

    pool.get_session_subagent_thread_by_call.return_value = {
        **live,
        "parent_tool_call_id": "another-call",
    }
    with pytest.raises(SubagentPersistenceRefused, match="another parent"):
        await ledger.lookup(PARENT, "call-1")


@pytest.mark.asyncio
async def test_reopen_rotates_only_the_exact_generation():
    ledger = SessionSubagentLedger(
        _client(), _pool(), parent_thread_id=PARENT, parent_authority=_pinned()
    )
    await ledger.open(CHILD, **_fields())
    assert (await ledger.reopen(CHILD))["runtime_generation"] == NEXT_GENERATION
    assert ledger.runtime_generation_for(CHILD) == NEXT_GENERATION


@pytest.mark.asyncio
async def test_reopen_retries_lost_ack_with_fresh_parent_authority():
    client = _client()
    client.reopen_session_subagent_thread.side_effect = [
        RuntimeError("response lost after commit"),
        {
            "result": "reopened",
            "thread_id": CHILD,
            "runtime_generation": NEXT_GENERATION,
            "reconciled": True,
        },
    ]
    authority = AsyncMock(return_value=_pinned())
    ledger = SessionSubagentLedger(
        client,
        _pool(),
        parent_thread_id=PARENT,
        authority_provider=authority,
    )
    await ledger.open(CHILD, **_fields())
    authority.reset_mock()

    result = await ledger.reopen(CHILD)

    assert result["runtime_generation"] == NEXT_GENERATION
    assert ledger.runtime_generation_for(CHILD) == NEXT_GENERATION
    assert client.reopen_session_subagent_thread.await_count == 2
    assert authority.await_count == 2
