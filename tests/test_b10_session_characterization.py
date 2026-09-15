"""Characterize B10 session-wake, transport, and permission boundaries.

The wake cases deliberately pin the caller-closure defect on the comparison
base: a ceiling check must receive its owning application's ledger explicitly,
never discover ``orchestrator.main`` through a process-global fallback.  The
transport and permission cases preserve adjacent behavior before extraction.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from orchestrator.services import session_wake


def _officer_thread(thread_id: str, *, ceiling: int = 100) -> dict:
    return {
        "id": thread_id,
        "project_id": None,
        "user_id": None,
        "metadata": {
            "config_override": {
                "officer": {
                    "enabled": True,
                    "daily_token_ceiling": ceiling,
                }
            }
        },
    }


def _usage_ledger(tokens: int, *, available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        is_available=available,
        query_usage=AsyncMock(
            return_value={
                "by_category": [
                    {"unit": "prompt-token", "quantity": tokens},
                ]
            }
        ),
    )


def _event_db(thread: dict, *, delivery_state: str = "admitted") -> SimpleNamespace:
    event_id = 41
    delivery_id = f"delivery-{thread['id']}"
    claimed = [
        {
            "id": event_id,
            "thread_id": str(thread["id"]),
            "source": "timer",
            "dedup_key": "timer",
        }
    ]
    assigned = [{**claimed[0], "delivery_id": delivery_id}]
    return SimpleNamespace(
        claim_pending_session_wake_events=AsyncMock(return_value=claimed),
        assign_session_wake_delivery_groups=AsyncMock(return_value=assigned),
        get_session_wake_delivery_group=AsyncMock(return_value=assigned),
        get_thread=AsyncMock(return_value=thread),
        defer_session_wake_events=AsyncMock(),
        defer_session_wake_events_for_input=AsyncMock(),
        finish_session_wake_events=AsyncMock(),
        release_session_wake_events=AsyncMock(),
        merge_thread_officer_state=AsyncMock(),
        persist_thread_input_delivery=AsyncMock(
            return_value={
                "thread_id": str(thread["id"]),
                "delivery_id": delivery_id,
                "state": delivery_state,
            }
        ),
    )


@pytest.mark.asyncio
async def test_missing_explicit_ledger_never_reaches_application_startup(monkeypatch):
    """Metering absence is fail-open, not permission to import app globals."""

    from orchestrator import main

    process_global = _usage_ledger(10**9)
    monkeypatch.setattr(main, "usage_ledger", process_global)

    assert (
        await session_wake._officer_ceiling_deferral(
            None,
            _officer_thread("thread-no-ledger", ceiling=1),
            usage_ledger=None,
        )
        is None
    )
    process_global.query_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_drain_uses_the_explicit_application_ledger():
    thread = _officer_thread("thread-over", ceiling=100)
    db = _event_db(thread)
    ledger = _usage_ledger(100)

    delivered = await session_wake.drain_pending_event_wakes(
        db,
        usage_ledger=ledger,
    )

    assert delivered == 0
    ledger.query_usage.assert_awaited_once()
    assert ledger.query_usage.await_args.kwargs["ref_id"] == "thread-over"
    db.defer_session_wake_events.assert_awaited_once()
    db.persist_thread_input_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_drain_fails_open_when_application_metering_is_unavailable(
    monkeypatch,
):
    from orchestrator.services import sitrep

    monkeypatch.setattr(
        sitrep,
        "build_wake_message",
        AsyncMock(return_value=("bounded wake", None)),
    )
    thread = _officer_thread("thread-metering-down", ceiling=1)
    db = _event_db(thread)
    ledger = _usage_ledger(10**9, available=False)

    delivered = await session_wake.drain_pending_event_wakes(
        db,
        usage_ledger=ledger,
    )

    assert delivered == 1
    ledger.query_usage.assert_not_awaited()
    db.defer_session_wake_events.assert_not_awaited()
    db.persist_thread_input_delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_concurrent_event_drains_keep_application_ledgers_isolated(monkeypatch):
    """One app's over-budget ledger cannot defer another app's officer."""

    from orchestrator.services import sitrep

    monkeypatch.setattr(
        sitrep,
        "build_wake_message",
        AsyncMock(return_value=("bounded wake", None)),
    )
    over_thread = _officer_thread("thread-app-a", ceiling=100)
    under_thread = _officer_thread("thread-app-b", ceiling=100)
    over_db = _event_db(over_thread)
    under_db = _event_db(under_thread)
    over_ledger = _usage_ledger(100)
    under_ledger = _usage_ledger(99)

    over_result, under_result = await asyncio.gather(
        session_wake.drain_pending_event_wakes(
            over_db,
            usage_ledger=over_ledger,
        ),
        session_wake.drain_pending_event_wakes(
            under_db,
            usage_ledger=under_ledger,
        ),
    )

    assert (over_result, under_result) == (0, 1)
    assert over_ledger.query_usage.await_args.kwargs["ref_id"] == "thread-app-a"
    assert under_ledger.query_usage.await_args.kwargs["ref_id"] == "thread-app-b"
    over_db.defer_session_wake_events.assert_awaited_once()
    over_db.persist_thread_input_delivery.assert_not_awaited()
    under_db.defer_session_wake_events.assert_not_awaited()
    under_db.persist_thread_input_delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_opportunistic_event_drain_forwards_the_explicit_ledger(monkeypatch):
    drain = AsyncMock(return_value=0)
    monkeypatch.setattr(session_wake, "drain_pending_event_wakes", drain)
    db = object()
    ledger = object()

    session_wake.kick_event_drain(db, usage_ledger=ledger)
    await asyncio.sleep(0)

    drain.assert_awaited_once_with(db, usage_ledger=ledger)


@pytest.mark.asyncio
async def test_application_kick_adapter_reads_its_ledger_at_call_time(monkeypatch):
    """The main composition adapter must not freeze or omit the current ledger."""

    from orchestrator import main

    drain = AsyncMock(return_value=0)
    monkeypatch.setattr(session_wake, "drain_pending_event_wakes", drain)
    db = object()
    ledger = object()
    monkeypatch.setattr(main, "usage_ledger", ledger)

    main._kick_officer_event_drain(db)
    await asyncio.sleep(0)

    drain.assert_awaited_once_with(db, usage_ledger=ledger)


@pytest.mark.asyncio
async def test_periodic_sweeper_forwards_the_explicit_ledger(monkeypatch):
    shutdown = asyncio.Event()

    async def _job_drain(_db):
        shutdown.set()
        return 0

    event_drain = AsyncMock(return_value=0)
    monkeypatch.setattr(session_wake, "drain_pending_wakes", _job_drain)
    monkeypatch.setattr(session_wake, "drain_pending_event_wakes", event_drain)
    db = object()
    ledger = object()

    await session_wake.session_wake_sweeper_loop(
        db,
        shutdown,
        usage_ledger=ledger,
    )

    event_drain.assert_awaited_once_with(db, usage_ledger=ledger)


@pytest.mark.asyncio
async def test_direct_legate_note_bypasses_the_autonomous_ceiling(monkeypatch):
    """Direct human direction queues even while autonomous wakes are braked."""

    ceiling_gate = AsyncMock(side_effect=AssertionError("direct input hit ceiling"))
    monkeypatch.setattr(session_wake, "_officer_ceiling_deferral", ceiling_gate)
    db = SimpleNamespace(enqueue_session_wake_event=AsyncMock(return_value=True))
    thread = _officer_thread("thread-legate", ceiling=1)

    assert await session_wake.deliver_officer_note(db, thread, "Stop now.") == "queued"
    db.enqueue_session_wake_event.assert_awaited_once()
    ceiling_gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_magic_link_get_is_read_only_prefetch_safe_and_escaped(monkeypatch):
    from orchestrator import main

    permission_row = {
        "id": "approval-1",
        "tool_name": "<script>alert('tool')</script>",
        "tool_args": {"value": "</pre><script>alert('args')</script>"},
        "status": "pending",
    }
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=permission_row))

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *_exc):
            return False

    db = SimpleNamespace(acquire=lambda: _Acquire())
    validate = AsyncMock(
        return_value={
            "id": "token-row",
            "approval_id": "approval-1",
            "thread_id": "thread-permission",
            "intended_decision": "approved",
        }
    )
    consume = AsyncMock(side_effect=AssertionError("GET consumed token"))
    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(
        main,
        "headless_notifications",
        SimpleNamespace(validate_magic_link=validate, consume_magic_link=consume),
    )
    monkeypatch.setattr(
        main,
        "email_service",
        SimpleNamespace(cockpit_url="https://cockpit.example.test"),
    )

    response = await main.magic_link_get('tok\"><script>alert(1)</script>')
    body = response.body.decode()

    assert response.status_code == 200
    validate.assert_awaited_once()
    consume.assert_not_awaited()
    conn.fetchrow.assert_awaited_once()
    assert "SELECT id, tool_name, tool_args, status" in conn.fetchrow.await_args.args[0]
    assert "UPDATE " not in conn.fetchrow.await_args.args[0]
    assert "<script>alert('tool')</script>" not in body
    assert "</pre><script>alert('args')</script>" not in body
    assert 'tok\"><script>' not in body
    assert "&lt;script&gt;alert(&#x27;tool&#x27;)&lt;/script&gt;" in body


@pytest.mark.asyncio
async def test_failed_permission_decision_does_not_resolve_notification(monkeypatch):
    from orchestrator import main

    monkeypatch.setattr(
        main,
        "require_thread_owner",
        AsyncMock(return_value=({"id": "user-1"}, {"id": "thread-1"})),
    )
    decide = AsyncMock(
        side_effect=HTTPException(status_code=409, detail="Already decided")
    )
    monkeypatch.setattr(main, "_decide_permission_request", decide)
    resolve = AsyncMock()
    monkeypatch.setattr(main.notification_service, "resolve_source", resolve)

    with pytest.raises(HTTPException) as caught:
        await main.thread_approve(
            "thread-1",
            "approval-1",
            main.ThreadApproveRequest(decision="approve"),
            MagicMock(),
        )

    assert caught.value.status_code == 409
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_pinned_input_releases_inflight_owner_immediately(monkeypatch):
    """Cancellation releases both the lock and its ownership marker."""

    from orchestrator import main

    thread_id = "thread-cancelled-input"
    thread = {"id": thread_id, "execution_lane": "pinned", "total_turns": 0}
    binding = object()
    main._thread_turn_locks.clear()
    main._thread_turn_inflight.clear()
    monkeypatch.setattr(
        main,
        "require_approved_user",
        AsyncMock(return_value={"id": "user-1"}),
    )
    monkeypatch.setattr(
        main,
        "_load_thread_for_owner",
        AsyncMock(return_value=thread),
    )
    monkeypatch.setattr(
        main,
        "_resolve_thread_for_forwarding",
        AsyncMock(return_value=(thread, binding)),
    )
    monkeypatch.setattr(
        main,
        "_revalidate_pinned_forwarding_binding",
        AsyncMock(return_value=binding),
    )
    monkeypatch.setattr(
        main,
        "_forward_to_agent",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    cleanup = MagicMock()
    monkeypatch.setattr(main, "_schedule_turn_lock_cleanup", cleanup)

    try:
        with pytest.raises(asyncio.CancelledError):
            await main.thread_input(
                thread_id,
                main.ThreadInputRequest(content="cancel me", turn_id=1),
                MagicMock(),
            )

        lock = main._thread_turn_locks[(thread_id, 1)]
        assert not lock.locked()
        assert thread_id not in main._thread_turn_inflight
        cleanup.assert_called_once_with(thread_id, 1)
    finally:
        main._thread_turn_locks.clear()
        main._thread_turn_inflight.clear()
