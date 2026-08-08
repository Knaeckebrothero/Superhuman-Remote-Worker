"""run_queue reaper loop + post-steal journal writes (stateless_agents.md
§5.2, M4).

The service layers ON TOP of the shared substrate: ``reap_expired`` (per-row
CAS steals) is the boundary, and for every stolen session unit the reaper —
in one transaction per unit — bumps the thread's events epoch and appends the
``turn.interrupted`` / ``turn.parked`` system frame (post-steal is the
sanctioned writer-exclusion context). Non-session kinds are reaped only; a
row erroring must not stop the others; the loop is leader-gated on
RUN_QUEUE_REAPER_ID and never dies before shutdown.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from orchestrator.services import run_queue_reaper as mod
from src.shared.run_queue import StolenUnit

UNIT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
UNIT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _stolen(
    unit_id=UNIT_A,
    kind="session_turn",
    state="queued",
    attempts=2,
    leased_by="pod-1",
    token=9,
):
    return StolenUnit(
        unit_id=unit_id,
        unit_kind=kind,
        state=state,
        attempts_since_completion=attempts,
        leased_by=leased_by,
        lease_token=token,
    )


class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.txn_enters += 1
        return self

    async def __aexit__(self, *exc):
        return False


def _conn():
    conn = MagicMock()
    conn.txn_enters = 0
    conn.transaction = lambda: _FakeTxn(conn)
    return conn


@pytest.mark.asyncio
async def test_steal_of_requeued_session_unit_journals_turn_interrupted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        mod, "reap_expired", AsyncMock(return_value=[_stolen(state="queued")])
    )
    bump = AsyncMock(return_value=3)
    frame = AsyncMock(return_value=(3, 1))
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    n = await mod.reap_cycle(conn, grace_seconds=30)

    assert n == 1
    bump.assert_awaited_once_with(conn, thread_id=str(UNIT_A))
    frame.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        kind="turn.interrupted",
        payload={"reason": "lease_expired", "attempts": 2, "stolen_from": "pod-1"},
    )
    assert conn.txn_enters == 1, "bump + frame share ONE transaction per unit"
    # grace flows through to the substrate
    assert mod.reap_expired.await_args.kwargs["grace_seconds"] == 30


@pytest.mark.asyncio
async def test_steal_of_parked_session_unit_journals_turn_parked(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        mod,
        "reap_expired",
        AsyncMock(return_value=[_stolen(state="parked", attempts=5, leased_by="p9")]),
    )
    bump = AsyncMock(return_value=4)
    frame = AsyncMock(return_value=(4, 1))
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    await mod.reap_cycle(conn)

    frame.assert_awaited_once()
    assert frame.await_args.kwargs["kind"] == "turn.parked"
    assert frame.await_args.kwargs["payload"] == {
        "reason": "lease_expired",
        "attempts": 5,
        "stolen_from": "p9",
    }


@pytest.mark.asyncio
async def test_non_session_kinds_are_reaped_without_journal_writes(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        mod,
        "reap_expired",
        AsyncMock(
            return_value=[
                _stolen(kind="worker_batch"),
                _stolen(unit_id=UNIT_B, kind="bg_task"),
            ]
        ),
    )
    bump = AsyncMock()
    frame = AsyncMock()
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    n = await mod.reap_cycle(conn)

    assert n == 2
    bump.assert_not_awaited()
    frame.assert_not_awaited()
    assert conn.txn_enters == 0


@pytest.mark.asyncio
async def test_one_erroring_row_does_not_stop_the_others(monkeypatch, caplog):
    conn = _conn()
    monkeypatch.setattr(
        mod,
        "reap_expired",
        AsyncMock(
            return_value=[
                _stolen(unit_id=UNIT_A),
                _stolen(unit_id=UNIT_B, leased_by="pod-2"),
            ]
        ),
    )
    bump = AsyncMock(side_effect=[RuntimeError("journal down"), 5])
    frame = AsyncMock(return_value=(5, 1))
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    with caplog.at_level("INFO", logger=mod.logger.name):
        n = await mod.reap_cycle(conn)

    assert n == 2
    assert bump.await_args_list == [
        call(conn, thread_id=str(UNIT_A)),
        call(conn, thread_id=str(UNIT_B)),
    ]
    # Only the second unit reached the frame append.
    frame.assert_awaited_once()
    assert frame.await_args.kwargs["thread_id"] == str(UNIT_B)
    # Greppable steal line emitted for BOTH units despite the error.
    steal_lines = [r for r in caplog.records if "run_queue steal" in r.getMessage()]
    assert len(steal_lines) == 2


@pytest.mark.asyncio
async def test_loop_leader_gates_on_reaper_advisory_lock(monkeypatch):
    """The loop acquires RUN_QUEUE_REAPER_ID on its dedicated connection,
    sweeps while holding it, and unlocks + releases on shutdown."""
    shutdown = asyncio.Event()
    conn = _conn()
    lock_calls = []

    async def _fetchval(q, *a):
        lock_calls.append((q, a))
        assert "pg_try_advisory_lock" in q
        assert a == (mod.RUN_QUEUE_REAPER_ID,)
        return True

    conn.fetchval = _fetchval
    conn.execute = AsyncMock()

    async def _reap(c, *, grace_seconds):
        assert c is conn
        shutdown.set()  # one cycle, then stop
        return []

    monkeypatch.setattr(mod, "reap_expired", _reap)

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    db = MagicMock()
    db._pool = pool

    await asyncio.wait_for(
        mod.run_queue_reaper_loop(db, shutdown, interval_seconds=0.01), timeout=5
    )

    assert len(lock_calls) == 1
    unlock = [
        c for c in conn.execute.await_args_list if "pg_advisory_unlock" in c.args[0]
    ]
    assert len(unlock) == 1 and unlock[0].args[1] == mod.RUN_QUEUE_REAPER_ID
    pool.release.assert_awaited_once_with(conn)


@pytest.mark.asyncio
async def test_loop_as_follower_never_sweeps(monkeypatch):
    shutdown = asyncio.Event()
    conn = _conn()
    attempts = []

    async def _fetchval(q, *a):
        attempts.append(q)
        if len(attempts) >= 2:
            shutdown.set()
        return False  # somebody else holds the lock

    conn.fetchval = _fetchval
    conn.execute = AsyncMock()
    reap = AsyncMock()
    monkeypatch.setattr(mod, "reap_expired", reap)

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    db = MagicMock()
    db._pool = pool

    await asyncio.wait_for(
        mod.run_queue_reaper_loop(db, shutdown, interval_seconds=0.01), timeout=5
    )

    reap.assert_not_awaited()
    assert pool.release.await_count == len(attempts)  # conn returned each round


@pytest.mark.asyncio
async def test_loop_survives_cycle_errors_and_recontends(monkeypatch):
    """A cycle-level failure (dead conn) must not kill the loop: it drops
    leadership, logs, and re-contends on the next round."""
    shutdown = asyncio.Event()
    conn = _conn()
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()
    rounds = []

    async def _reap(c, *, grace_seconds):
        rounds.append(1)
        if len(rounds) == 1:
            raise ConnectionError("db blip")
        shutdown.set()
        return []

    monkeypatch.setattr(mod, "reap_expired", _reap)

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    db = MagicMock()
    db._pool = pool

    await asyncio.wait_for(
        mod.run_queue_reaper_loop(db, shutdown, interval_seconds=0.01), timeout=5
    )

    assert len(rounds) == 2  # first cycle blew up, loop came back for another
