"""Tests for the pause-aware delegation timeout (outage-paused children).

A delegation child paused for an LLM outage/cooldown must not be cancelled by
the parent's delegation timeout while legitimately waiting (2h timeout vs the
12h outage pause budget), and a child that resumed at wake W must get a full
timeout of ACTIVE time before the parent can expire — a naive skip-while-paused
still fires the moment the child resumes (the fire-on-resume trap).

Implemented as a derived anchor: effective start = max(freeze.timestamp,
latest child ``context.llm_outage.next_retry_at``). A paused child's future
wake parks the timer; a resumed child's recent wake grants a full window from
resume (K8s suspend reset-on-resume semantics); an overdue/never-resuming
child still terminates at wake + timeout. No extra writes, no dual-leader
write races. knowledge-base/knowledge/features/llm_outage_subjob_resilience.md (#6, LOCKED:
rebase semantics).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import tests.conftest  # noqa: E402,F401 — applies license/crypto/env shims + sys.path
import orchestrator.main as main  # noqa: E402


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


def _parent_row(*, started_ago_s, timeout=7200):
    now = datetime.now(timezone.utc)
    return {
        "id": "par-1",
        "freeze_data": {
            "freeze_type": "delegation",
            "child_job_ids": ["c1"],
            "child_count": 1,
            "timeout": timeout,
            "timestamp": (now - timedelta(seconds=started_ago_s)).isoformat(),
        },
        "config_override": {},
        "context": {},
        "execution_lane": "pinned",
        "priority": 5,
        "user_id": None,
    }


def _child(status, *, wake_in_s=None):
    """Delegation child row; wake_in_s places llm_outage.next_retry_at
    relative to now (negative = already woke)."""
    ctx = {}
    if wake_in_s is not None:
        ctx["llm_outage"] = {
            "attempt": 1,
            "next_retry_at": (
                datetime.now(timezone.utc) + timedelta(seconds=wake_in_s)
            ).isoformat(),
        }
    return {
        "id": "c1",
        "status": status,
        "creation_order": 0,
        "description": "task",
        "config_name": "defaults",
        "branch_name": None,
        "context": ctx,
        "freeze_data": {},
    }


@pytest.fixture
def wired(monkeypatch):
    """Wire the sweep against a fake parent row + children; return mocks."""

    def _apply(parent_row, children):
        db = main.postgres_db
        monkeypatch.setattr(
            db, "acquire", lambda: _FakeAcquire(_FakeConn([parent_row]))
        )
        monkeypatch.setattr(
            db, "get_delegation_children", AsyncMock(return_value=children)
        )
        cancel = AsyncMock()
        claim = AsyncMock(return_value=True)
        monkeypatch.setattr(db, "cancel_job", cancel)
        monkeypatch.setattr(db, "claim_delegation_resume", claim)
        monkeypatch.setattr(db, "merge_job_context", AsyncMock())
        monkeypatch.setattr(main, "_trigger_dispatch", MagicMock())
        return cancel, claim

    return _apply


@pytest.mark.asyncio
async def test_paused_child_future_wake_parks_the_timer(wired):
    # Child paused for a 3h cooldown (wake +2h from now), naive elapsed 3h > 2h
    # timeout — the timer must NOT fire while the child legitimately waits.
    cancel, claim = wired(
        _parent_row(started_ago_s=3 * 3600),
        [_child("paused", wake_in_s=2 * 3600)],
    )
    assert await main._check_delegation_timeouts() == 0
    cancel.assert_not_awaited()
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_child_gets_full_window_from_wake(wired):
    # Fire-on-resume trap: the child woke 10min ago and is processing again;
    # naive elapsed (3h) exceeds the timeout, but active time since wake is
    # only 10min — the parent must keep waiting.
    cancel, claim = wired(
        _parent_row(started_ago_s=3 * 3600),
        [_child("processing", wake_in_s=-600)],
    )
    assert await main._check_delegation_timeouts() == 0
    cancel.assert_not_awaited()
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_window_exhausted_fires(wired):
    # The child woke 3h ago (full 2h window consumed) and the parent is still
    # waiting — the timeout fires normally.
    cancel, claim = wired(
        _parent_row(started_ago_s=5 * 3600),
        [_child("processing", wake_in_s=-3 * 3600)],
    )
    assert await main._check_delegation_timeouts() == 1
    cancel.assert_awaited_once_with("c1")
    claim.assert_awaited_once_with("par-1")


@pytest.mark.asyncio
async def test_never_resuming_paused_child_terminates_at_wake_plus_timeout(wired):
    # Overdue guard: a child stuck 'paused' long past its wake (outage sweeper
    # broken) must still be reaped — the derived anchor bounds it at
    # wake + timeout, never forever.
    cancel, claim = wired(
        _parent_row(started_ago_s=6 * 3600),
        [_child("paused", wake_in_s=-3 * 3600)],
    )
    assert await main._check_delegation_timeouts() == 1
    cancel.assert_awaited_once_with("c1")
    claim.assert_awaited_once_with("par-1")


@pytest.mark.asyncio
async def test_children_without_outage_state_fire_as_before(wired):
    cancel, claim = wired(
        _parent_row(started_ago_s=3 * 3600),
        [_child("processing")],
    )
    assert await main._check_delegation_timeouts() == 1
    cancel.assert_awaited_once_with("c1")
    claim.assert_awaited_once_with("par-1")


@pytest.mark.asyncio
async def test_wake_older_than_freeze_timestamp_is_ignored(wired):
    # A wake from a previous delegation round (re-suspend wrote a fresh freeze
    # timestamp after it) must not extend the current round's deadline.
    cancel, claim = wired(
        _parent_row(started_ago_s=3 * 3600),
        [_child("processing", wake_in_s=-4 * 3600)],
    )
    assert await main._check_delegation_timeouts() == 1
    cancel.assert_awaited_once_with("c1")
    claim.assert_awaited_once_with("par-1")


@pytest.mark.asyncio
async def test_under_timeout_never_touches_children(wired, monkeypatch):
    # Cheap path: while the naive timer hasn't expired, the sweep must not
    # fetch children at all (one query per waiting parent per tick suffices).
    cancel, claim = wired(
        _parent_row(started_ago_s=600),
        [_child("paused", wake_in_s=2 * 3600)],
    )
    assert await main._check_delegation_timeouts() == 0
    main.postgres_db.get_delegation_children.assert_not_awaited()
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_timeout_reenqueues_without_dispatcher(wired, monkeypatch):
    parent = _parent_row(started_ago_s=3 * 3600)
    parent.update(
        {
            "execution_lane": "stateless",
            "priority": 8,
            "user_id": "44444444-4444-4444-4444-444444444444",
        }
    )
    cancel, claim = wired(parent, [_child("processing")])
    queue = AsyncMock(return_value=True)
    monkeypatch.setattr(main.postgres_db, "queue_stateless_job_for_resume", queue)

    assert await main._check_delegation_timeouts() == 1

    cancel.assert_awaited_once_with("c1")
    claim.assert_not_awaited()
    queued = queue.await_args
    assert queued.args[0] == "par-1"
    assert queued.args[1]["delegation_timed_out"] is True
    assert queued.kwargs == {
        "priority": 8,
        "fair_key": "44444444-4444-4444-4444-444444444444",
        "expected_status": "waiting",
    }
    main._trigger_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_stateless_timeout_finalizes_even_an_already_closed_queue(
    wired, monkeypatch
):
    """Queue closure is not checkpoint/workspace cleanup completion.

    A queued child is closed synchronously by cancellation, but the durable
    cleanup marker still requires the shared finalizer before its parent can
    resume against the same workspace.
    """
    child = _child("processing")
    child["execution_lane"] = "stateless"
    _cancel, claim = wired(_parent_row(started_ago_s=3 * 3600), [child])
    cancel_stateless = AsyncMock(return_value=(True, True))
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(
        main.postgres_db,
        "cancel_stateless_job",
        cancel_stateless,
    )
    monkeypatch.setattr(main, "_wait_for_stateless_cancel_settle", settle)

    assert await main._check_delegation_timeouts() == 1

    cancel_stateless.assert_awaited_once_with("c1")
    settle.assert_awaited_once_with("c1")
    claim.assert_awaited_once_with("par-1")


@pytest.mark.asyncio
async def test_stateless_timeout_retry_settles_already_cancelled_child(
    wired, monkeypatch
):
    child = _child("cancelled")
    child.update(
        {
            "execution_lane": "stateless",
            "context": {"_stateless_cancel_cleanup_pending": True},
        }
    )
    _cancel, claim = wired(_parent_row(started_ago_s=3 * 3600), [child])
    cancel_stateless = AsyncMock()
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(
        main.postgres_db,
        "cancel_stateless_job",
        cancel_stateless,
    )
    monkeypatch.setattr(main, "_wait_for_stateless_cancel_settle", settle)

    assert await main._check_delegation_timeouts() == 1

    cancel_stateless.assert_not_awaited()
    settle.assert_awaited_once_with("c1")
    claim.assert_awaited_once_with("par-1")
