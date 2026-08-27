"""run_queue reaper loop + post-steal journal writes (stateless_agents.md
§5.2, M4).

The service layers ON TOP of the shared substrate: ``reap_expired`` (per-row
CAS steals) is the boundary, and for every stolen session unit the reaper —
in one transaction per unit — bumps the thread's events epoch and appends the
``turn.interrupted`` / ``turn.parked`` system frame (post-steal is the
sanctioned writer-exclusion context). Non-session kinds are reaped only; a
row erroring must not stop the others; the loop is leader-gated on
RUN_QUEUE_REAPER_ID and never dies before shutdown. Durable unreceipted stop
intent settles applied/hard on owner loss and consumes its exact target input.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from orchestrator.services import run_queue_reaper as mod
from src.shared import session_permission_retirement as permission_retirement
from src.shared.run_queue import StolenUnit
from src.shared.run_queue import queries as queue_queries

UNIT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
UNIT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
REQUEST_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
REQUEST_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
CLIENT_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
CLIENT_B = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _stolen(
    unit_id=UNIT_A,
    kind="session_turn",
    state="queued",
    attempts=2,
    leased_by="pod-1",
    token=9,
    previous_token=8,
    admission_turn_id=None,
):
    return StolenUnit(
        unit_id=unit_id,
        unit_kind=kind,
        state=state,
        attempts_since_completion=attempts,
        leased_by=leased_by,
        lease_token=token,
        previous_lease_token=previous_token,
        interrupt_admission_turn_id=admission_turn_id,
    )


def _pending(
    request_id,
    client_request_id,
    *,
    turn_id,
    receipt_epoch=None,
    receipt_seq=None,
    receipt_kind=None,
    receipt_payload=None,
    accepted_token=8,
    accepted_leased_by="pod-1",
    outcome=None,
    result=None,
):
    return {
        "id": request_id,
        "client_request_id": client_request_id,
        "target_turn_id": turn_id,
        "accepted_lease_token": accepted_token,
        "accepted_leased_by": accepted_leased_by,
        "request_outcome": outcome,
        "request_result": result,
        "receipt_epoch": receipt_epoch,
        "receipt_seq": receipt_seq,
        "receipt_kind": receipt_kind,
        "receipt_payload": receipt_payload,
    }


def _thread(*, lane="stateless", agent_id=None, metadata=None):
    return {
        "execution_lane": lane,
        "agent_id": agent_id,
        "metadata": metadata or {},
    }


def _queue(
    *,
    state="queued",
    token=9,
    leased_by=None,
    last_leased_by=None,
    attempts=2,
    input_seq=7,
    consumed_seq=6,
    control_input_seq=0,
    control_consumed_seq=0,
):
    return {
        "unit_kind": "session_turn",
        "state": state,
        "lease_token": token,
        "leased_by": leased_by,
        "last_leased_by": last_leased_by,
        "attempts_since_completion": attempts,
        "input_seq": input_seq,
        "consumed_seq": consumed_seq,
        "control_input_seq": control_input_seq,
        "control_consumed_seq": control_consumed_seq,
        "max_attempts": 5,
        "queued_at": None,
        "run_after": None,
        "leased_until": None,
        "interrupt_admission_lease_token": None,
        "interrupt_admission_turn_id": None,
    }


class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.txn_enters += 1
        return self

    async def __aexit__(self, *exc):
        self._conn.txn_exits += 1
        if exc[0] is not None:
            self._conn.txn_failures += 1
        return False


def _conn():
    conn = MagicMock()
    conn.txn_enters = 0
    conn.txn_exits = 0
    conn.txn_failures = 0
    conn.transaction = lambda: _FakeTxn(conn)
    conn.stolen_interrupts = []
    conn.retry_candidates = []
    conn.stale_permission_candidates = []
    conn.stale_permissions = []
    conn.done_permission_proof = None
    conn.retry_interrupts = []
    conn.claim_loss_candidates = []
    conn.thread_row = _thread()
    conn.queue_row = _queue()

    def _fetch(sql, *_args):
        if sql == mod._PENDING_STOLEN_INTERRUPTS_SQL:
            return conn.stolen_interrupts
        if sql == mod._STALE_INTERRUPT_RETRY_CANDIDATES_SQL:
            return conn.retry_candidates
        if sql == mod._STALE_PERMISSION_RETRY_CANDIDATES_SQL:
            return conn.stale_permission_candidates
        if sql == permission_retirement._LOCK_STALE_PENDING_SQL:
            return conn.stale_permissions
        if sql == mod._PENDING_STALE_INTERRUPT_RETRY_SQL:
            return conn.retry_interrupts
        if sql == mod._CLAIM_LOSS_HOLD_CANDIDATES_SQL:
            return conn.claim_loss_candidates
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    def _fetchrow(sql, *_args):
        if sql == mod._LOCK_THREAD_SQL:
            return conn.thread_row
        if sql == mod._LOCK_QUEUE_UNIT_SQL:
            return conn.queue_row
        if sql == mod._LOCK_DONE_PERMISSION_RECOVERY_PROOF_SQL:
            return conn.done_permission_proof
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    def _fetchval(sql, *args):
        if sql == mod._TERMINALIZE_STOLEN_INTERRUPT_SQL:
            return args[0]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    return conn


@pytest.mark.asyncio
async def test_reap_cas_returns_exact_pre_steal_admission_turn():
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "unit_id": UNIT_A,
                "unit_kind": "session_turn",
                "leased_by": "pod-1",
                "lease_token": 8,
                "attempts_since_completion": 2,
                "max_attempts": 5,
            }
        ]
    )
    conn.fetchrow = AsyncMock(
        return_value={
            "unit_id": UNIT_A,
            "unit_kind": "session_turn",
            "state": "queued",
            "attempts_since_completion": 2,
            "lease_token": 9,
            "interrupt_admission_lease_token": 8,
            "interrupt_admission_turn_id": 42,
        }
    )

    stolen = await queue_queries.reap_expired(
        conn,
        grace_seconds=0,
        backoff_base_seconds=0,
        jitter=0,
    )

    assert len(stolen) == 1
    assert stolen[0].previous_lease_token == 8
    assert stolen[0].interrupt_admission_turn_id == 42
    steal_sql = conn.fetchrow.await_args.args[0]
    assert "WITH previous AS" in steal_sql
    assert "previous.interrupt_admission_turn_id" in steal_sql


@pytest.mark.asyncio
async def test_atomic_session_steal_parks_exact_uid_debt_after_settlement(monkeypatch):
    conn = _conn()
    conn.thread_row = _thread(
        metadata={
            "_stateless_active_claim": {
                "lease_token": 8,
                "pod": "pod-1",
                "pod_uid": "uid-old",
            }
        }
    )
    leased = _queue(state="leased", token=8, leased_by="pod-1")
    leased["leased_until"] = "expired"
    settled = _queue(state="queued", token=9, leased_by=None, attempts=2)
    settled["queued_at"] = "2026-08-11T12:00:00+00:00"
    settled["run_after"] = "2026-08-11T12:00:03+00:00"

    async def _fetchrow(sql, *_args):
        if sql == mod._LOCK_THREAD_SQL:
            return conn.thread_row
        if sql == mod._LOCK_QUEUE_UNIT_SQL:
            _fetchrow.queue_reads += 1
            return leased if _fetchrow.queue_reads == 1 else settled
        if sql == mod._STEAL_LOCKED_SESSION_SQL:
            return {
                "unit_id": UNIT_A,
                "unit_kind": "session_turn",
                "state": "queued",
                "attempts_since_completion": 2,
                "lease_token": 9,
            }
        if sql == mod._PARK_CLAIM_LOSS_HOLD_SQL:
            return {"unit_id": UNIT_A}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    _fetchrow.queue_reads = 0
    conn.fetchrow.side_effect = _fetchrow
    conn.fetchval.side_effect = lambda sql, *_args: (
        UNIT_A
        if sql == mod._STORE_CLAIM_LOSS_HOLD_SQL
        else (_ for _ in ()).throw(AssertionError(f"unexpected fetchval SQL: {sql}"))
    )
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=2))
    monkeypatch.setattr(mod, "append_system_frame", AsyncMock(return_value=(2, 1)))

    unit = await mod._steal_session_with_claim_loss(
        conn,
        candidate={
            "unit_id": UNIT_A,
            "lease_token": 8,
            "leased_by": "pod-1",
        },
        backoff_seconds=3,
        grace_seconds=0,
    )

    assert unit is not None and unit.lease_token == 9
    stored = json.loads(conn.fetchval.await_args.args[2])
    assert stored["_stateless_claim_losses"]["8"] == {
        "pod": "pod-1",
        "pod_uid": "uid-old",
        "quiesced": False,
    }
    assert stored["_stateless_claim_loss_hold"] == {
        "lease_token": 9,
        "intended_state": "queued",
        "attempts_since_completion": 2,
        "queued_at": "2026-08-11T12:00:00+00:00",
        "run_after": "2026-08-11T12:00:03+00:00",
    }
    assert "_stateless_active_claim" not in stored
    assert conn.fetchrow.await_args_list[-1].args[0] == mod._PARK_CLAIM_LOSS_HOLD_SQL
    assert "run_after = NULL" not in mod._PARK_CLAIM_LOSS_HOLD_SQL


@pytest.mark.asyncio
async def test_post_eviction_404_does_not_settle_claimant_debt(monkeypatch):
    import services.agent_provisioner as provisioner_module
    import src.shared.session_retirement as retirement

    conn = _conn()
    conn.claim_loss_candidates = [
        {
            "id": UNIT_A,
            "metadata": {
                "_stateless_claim_losses": {
                    "8": {
                        "pod": "pod-1",
                        "pod_uid": "uid-old",
                        "quiesced": False,
                        "eviction_requested_at": "2026-08-11T12:00:00Z",
                    }
                }
            },
        }
    ]
    authority = AsyncMock(return_value="exact_absent")
    monkeypatch.setattr(
        provisioner_module.agent_provisioner, "agent_pod_authority", authority
    )
    ack = AsyncMock(return_value=True)
    monkeypatch.setattr(retirement, "acknowledge_session_claim_quiesced", ack)

    assert await mod.reconcile_claim_loss_holds(conn) == 0
    authority.assert_awaited_once_with("pod-1", expected_pod_uid="uid-old")
    ack.assert_not_awaited()


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
    # Exact permission/interrupt rows plus both bounded repair probes and
    # claimant-loss reconciliation.
    assert conn.fetch.await_count == 5
    assert conn.fetch.await_args_list[0].args[1:] == (str(UNIT_A), 9)
    assert conn.fetch.await_args_list[1].args[1:] == (str(UNIT_A), 8)
    conn.fetchval.assert_not_awaited()
    # grace flows through to the substrate
    assert mod.reap_expired.await_args.kwargs["grace_seconds"] == 30


@pytest.mark.asyncio
async def test_crash_frame_uses_pre_steal_admission_turn_without_request(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        mod,
        "reap_expired",
        AsyncMock(return_value=[_stolen(admission_turn_id=42)]),
    )
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=3))
    frame = AsyncMock(return_value=(3, 1))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.reap_cycle(conn) == 1

    assert frame.await_args.kwargs["kind"] == "turn.interrupted"
    assert frame.await_args.kwargs["payload"]["target_turn_id"] == 42
    assert frame.await_args.kwargs["payload"]["turn_id"] == 42


@pytest.mark.asyncio
async def test_steal_of_parked_session_unit_journals_turn_parked(monkeypatch):
    conn = _conn()
    conn.queue_row = _queue(state="parked", attempts=5)
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
async def test_unreceipted_stop_intents_each_ack_applied_and_consume_once(monkeypatch):
    conn = _conn()
    conn.stolen_interrupts = [
        _pending(REQUEST_A, CLIENT_A, turn_id=17),
        _pending(REQUEST_B, CLIENT_B, turn_id=17),
    ]
    conn.fetchval.side_effect = [REQUEST_A, REQUEST_B]
    monkeypatch.setattr(
        mod, "reap_expired", AsyncMock(return_value=[_stolen(state="queued")])
    )
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=6))
    consume = AsyncMock(return_value=MagicMock(state="done"))
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)
    frame = AsyncMock(side_effect=[(6, 1), (6, 2), (6, 3)])
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.reap_cycle(conn) == 1

    payload_a = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 17,
        "applied": True,
        "mode": "hard",
        "reason": "owner_lost",
        "owner_loss_reason": "lease_expired",
    }
    payload_b = {
        "request_id": str(REQUEST_B),
        "client_request_id": str(CLIENT_B),
        "target_turn_id": 17,
        "applied": True,
        "mode": "hard",
        "reason": "owner_lost",
        "owner_loss_reason": "lease_expired",
    }
    assert frame.await_args_list == [
        call(
            conn,
            thread_id=str(UNIT_A),
            kind="interrupt.ack",
            payload=payload_a,
            interrupt_request_id=str(REQUEST_A),
        ),
        call(
            conn,
            thread_id=str(UNIT_A),
            kind="interrupt.ack",
            payload=payload_b,
            interrupt_request_id=str(REQUEST_B),
        ),
        call(
            conn,
            thread_id=str(UNIT_A),
            kind="turn.interrupted",
            payload={
                "reason": "lease_expired",
                "attempts": 2,
                "stolen_from": "pod-1",
                "target_turn_id": 17,
                "turn_id": 17,
                "interrupt_request_ids": [str(REQUEST_A), str(REQUEST_B)],
            },
        ),
    ]
    assert conn.fetchval.await_count == 2
    first_update = conn.fetchval.await_args_list[0]
    assert "accepted_lease_token = $3::bigint" in first_update.args[0]
    assert first_update.args[1:5] == (REQUEST_A, str(UNIT_A), 8, "applied")
    assert json.loads(first_update.args[5]) == payload_a
    assert first_update.args[6:] == ("hard", 6, 1, None)
    second_update = conn.fetchval.await_args_list[1]
    assert second_update.args[1:5] == (REQUEST_B, str(UNIT_A), 8, "applied")
    assert json.loads(second_update.args[5]) == payload_b
    assert second_update.args[6:] == ("hard", 6, 2, None)
    consume.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        current_lease_token=9,
        accepted_lease_token=8,
        target_turn_id=17,
        request_id=str(REQUEST_A),
        terminal=False,
    )
    assert conn.txn_enters == conn.txn_exits == 1
    assert conn.txn_failures == 0


@pytest.mark.asyncio
async def test_existing_receipt_is_terminalized_without_duplicate_frame(monkeypatch):
    conn = _conn()
    receipt_payload = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 21,
        "applied": True,
        "mode": "hard",
    }
    conn.stolen_interrupts = [
        _pending(
            REQUEST_A,
            CLIENT_A,
            turn_id=21,
            receipt_epoch=4,
            receipt_seq=12,
            receipt_kind="interrupt.ack",
            receipt_payload=receipt_payload,
        )
    ]
    conn.fetchval.return_value = REQUEST_A
    monkeypatch.setattr(mod, "reap_expired", AsyncMock(return_value=[_stolen()]))
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=5))
    consume = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)
    frame = AsyncMock(return_value=(5, 1))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.reap_cycle(conn) == 1

    frame.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        kind="turn.interrupted",
        payload={
            "reason": "lease_expired",
            "attempts": 2,
            "stolen_from": "pod-1",
            "target_turn_id": 21,
            "turn_id": 21,
            "interrupt_request_id": str(REQUEST_A),
            "interrupt_request_ids": [str(REQUEST_A)],
        },
    )
    update = conn.fetchval.await_args
    assert update.args[1:5] == (REQUEST_A, str(UNIT_A), 8, "applied")
    assert json.loads(update.args[5]) == receipt_payload
    assert update.args[6:] == ("hard", 4, 12, None)
    consume.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        current_lease_token=9,
        accepted_lease_token=8,
        target_turn_id=21,
        request_id=str(REQUEST_A),
        terminal=False,
    )


@pytest.mark.asyncio
async def test_interrupt_owner_loss_ack_fault_rolls_back_unit_journal(
    monkeypatch, caplog
):
    conn = _conn()
    conn.stolen_interrupts = [_pending(REQUEST_A, CLIENT_A, turn_id=9)]
    monkeypatch.setattr(mod, "reap_expired", AsyncMock(return_value=[_stolen()]))
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=3))
    frame = AsyncMock(side_effect=RuntimeError("event insert failed"))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    with caplog.at_level("ERROR", logger=mod.logger.name):
        assert await mod.reap_cycle(conn) == 1

    frame.assert_awaited_once()
    assert frame.await_args.kwargs["kind"] == "interrupt.ack"
    conn.fetchval.assert_not_awaited()
    assert conn.txn_enters == conn.txn_exits == conn.txn_failures == 1
    assert "journal write failed" in caplog.text


@pytest.mark.asyncio
async def test_zero_row_terminal_update_aborts_before_turn_frame(monkeypatch, caplog):
    conn = _conn()
    conn.stolen_interrupts = [_pending(REQUEST_A, CLIENT_A, turn_id=9)]
    conn.fetchval.side_effect = lambda *_args: None
    monkeypatch.setattr(mod, "reap_expired", AsyncMock(return_value=[_stolen()]))
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=3))
    frame = AsyncMock(return_value=(3, 1))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    with caplog.at_level("ERROR", logger=mod.logger.name):
        assert await mod.reap_cycle(conn) == 1

    frame.assert_awaited_once()
    assert frame.await_args.kwargs["kind"] == "interrupt.ack"
    assert conn.txn_failures == 1
    assert "journal write failed" in caplog.text


@pytest.mark.asyncio
async def test_successor_claim_wins_before_post_steal_lock(monkeypatch):
    conn = _conn()
    conn.queue_row = _queue(state="leased", token=10, leased_by="pod-2")
    bump = AsyncMock()
    frame = AsyncMock()
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    result = await mod._journal_steal(conn, _stolen(token=9))

    assert result is mod.JournalStealResult.SKIPPED_SUCCESSOR_CLAIMED
    assert [entry.args[0] for entry in conn.fetchrow.await_args_list] == [
        mod._LOCK_THREAD_SQL,
        mod._LOCK_QUEUE_UNIT_SQL,
    ]
    bump.assert_not_awaited()
    frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_steal_waits_for_claim_then_skips_without_epoch_bump(monkeypatch):
    conn = _conn()
    queue_waiting = asyncio.Event()
    claim_committed = asyncio.Event()

    async def _fetchrow(sql, *_args):
        if sql == mod._LOCK_THREAD_SQL:
            return _thread()
        if sql == mod._LOCK_QUEUE_UNIT_SQL:
            queue_waiting.set()
            await claim_committed.wait()
            return _queue(state="leased", token=10, leased_by="pod-2")
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    conn.fetchrow.side_effect = _fetchrow
    bump = AsyncMock()
    frame = AsyncMock()
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    task = asyncio.create_task(mod._journal_steal(conn, _stolen(token=9)))
    await asyncio.wait_for(queue_waiting.wait(), timeout=1)
    bump.assert_not_awaited()
    frame.assert_not_awaited()
    claim_committed.set()

    assert await asyncio.wait_for(task, timeout=1) is (
        mod.JournalStealResult.SKIPPED_SUCCESSOR_CLAIMED
    )
    bump.assert_not_awaited()
    frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_thread_is_not_journaled_by_stateless_reaper(monkeypatch):
    conn = _conn()
    conn.thread_row = _thread(lane="pinned", agent_id=uuid.uuid4())
    bump = AsyncMock()
    frame = AsyncMock()
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    result = await mod._journal_steal(conn, _stolen())

    assert result is mod.JournalStealResult.SKIPPED_QUEUE_CHANGED
    bump.assert_not_awaited()
    frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_receipt_never_consumes_human_input(monkeypatch):
    conn = _conn()
    receipt_payload = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 21,
        "applied": False,
        "error_code": "target_turn_not_active",
    }
    conn.stolen_interrupts = [
        _pending(
            REQUEST_A,
            CLIENT_A,
            turn_id=21,
            receipt_epoch=4,
            receipt_seq=12,
            receipt_kind="interrupt.ack",
            receipt_payload=receipt_payload,
        )
    ]
    consume = AsyncMock()
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=5))
    monkeypatch.setattr(mod, "append_system_frame", AsyncMock(return_value=(5, 1)))

    assert await mod._journal_steal(conn, _stolen()) is mod.JournalStealResult.WRITTEN

    consume.assert_not_awaited()
    update = conn.fetchval.await_args
    assert update.args[4] == "rejected"
    assert update.args[-1] == "target_turn_not_active"


@pytest.mark.asyncio
async def test_applied_sibling_receipts_settle_one_input_group(monkeypatch):
    conn = _conn()
    payload_a = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 21,
        "applied": True,
        "mode": "hard",
    }
    payload_b = {
        "request_id": str(REQUEST_B),
        "client_request_id": str(CLIENT_B),
        "target_turn_id": 21,
        "applied": True,
        "mode": "hard",
    }
    conn.stolen_interrupts = [
        _pending(
            REQUEST_A,
            CLIENT_A,
            turn_id=21,
            receipt_epoch=4,
            receipt_seq=12,
            receipt_kind="interrupt.ack",
            receipt_payload=payload_a,
        ),
        _pending(
            REQUEST_B,
            CLIENT_B,
            turn_id=21,
            receipt_epoch=4,
            receipt_seq=13,
            receipt_kind="interrupt.ack",
            receipt_payload=payload_b,
        ),
    ]
    consume = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=5))
    frame = AsyncMock(return_value=(5, 1))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod._journal_steal(conn, _stolen()) is mod.JournalStealResult.WRITTEN

    assert conn.fetchval.await_count == 2
    consume.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        current_lease_token=9,
        accepted_lease_token=8,
        target_turn_id=21,
        request_id=str(REQUEST_A),
        terminal=False,
    )
    frame.assert_awaited_once()
    assert frame.await_args.kwargs["payload"]["target_turn_id"] == 21
    assert frame.await_args.kwargs["payload"]["interrupt_request_ids"] == [
        str(REQUEST_A),
        str(REQUEST_B),
    ]


@pytest.mark.asyncio
async def test_terminal_applied_row_without_input_marker_is_recovered(monkeypatch):
    conn = _conn()
    receipt_payload = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 21,
        "applied": True,
        "mode": "graceful",
    }
    conn.stolen_interrupts = [
        _pending(
            REQUEST_A,
            CLIENT_A,
            turn_id=21,
            receipt_epoch=4,
            receipt_seq=12,
            receipt_kind="interrupt.ack",
            receipt_payload=receipt_payload,
            outcome="applied",
            result=receipt_payload,
        )
    ]
    consume = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=5))
    monkeypatch.setattr(mod, "append_system_frame", AsyncMock(return_value=(5, 1)))

    assert await mod._journal_steal(conn, _stolen()) is mod.JournalStealResult.WRITTEN

    conn.fetchval.assert_not_awaited()  # already terminal; receipt is authoritative
    consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_parked_journal_transaction_is_rebuilt_in_periodic_retry(
    monkeypatch, caplog
):
    conn = _conn()
    conn.queue_row = _queue(state="parked", attempts=5)
    pending = _pending(REQUEST_A, CLIENT_A, turn_id=17)
    conn.stolen_interrupts = [pending]
    conn.retry_candidates = [{"thread_id": UNIT_A}]
    conn.retry_interrupts = [pending]
    monkeypatch.setattr(
        mod,
        "reap_expired",
        AsyncMock(return_value=[_stolen(state="parked", attempts=5)]),
    )
    bump = AsyncMock(side_effect=[6, 7])
    frame = AsyncMock(side_effect=[RuntimeError("first ack failed"), (7, 1), (7, 2)])
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)
    consume = AsyncMock(return_value=MagicMock(state="done"))
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)

    with caplog.at_level("ERROR", logger=mod.logger.name):
        assert await mod.reap_cycle(conn) == 1

    assert bump.await_count == 2
    assert [entry.kwargs["kind"] for entry in frame.await_args_list] == [
        "interrupt.ack",
        "interrupt.ack",
        "turn.interrupted",
    ]
    consume.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        current_lease_token=9,
        accepted_lease_token=8,
        target_turn_id=17,
        request_id=str(REQUEST_A),
        terminal=False,
    )
    assert conn.txn_enters == conn.txn_exits == 2
    assert conn.txn_failures == 1
    assert "journal write failed" in caplog.text


@pytest.mark.asyncio
async def test_parked_retry_reconstructs_epoch_and_turn_frame_for_existing_receipt(
    monkeypatch,
):
    conn = _conn()
    conn.queue_row = _queue(state="parked", attempts=5)
    receipt_payload = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 17,
        "applied": False,
        "error_code": "lease_expired",
    }
    conn.retry_candidates = [{"thread_id": UNIT_A}]
    conn.retry_interrupts = [
        _pending(
            REQUEST_A,
            CLIENT_A,
            turn_id=17,
            receipt_epoch=6,
            receipt_seq=1,
            receipt_kind="interrupt.ack",
            receipt_payload=receipt_payload,
        )
    ]
    bump = AsyncMock(return_value=7)
    frame = AsyncMock(return_value=(7, 1))
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.retry_stale_interrupt_requests(conn) == 1

    bump.assert_awaited_once_with(conn, thread_id=str(UNIT_A))
    frame.assert_awaited_once()
    assert frame.await_args.kwargs["kind"] == "turn.parked"
    assert frame.await_args.kwargs["payload"]["target_turn_id"] == 17
    assert frame.await_args.kwargs["payload"]["turn_id"] == 17
    assert conn.txn_enters == conn.txn_exits == 1


@pytest.mark.asyncio
async def test_applied_settlement_overrides_parked_lifecycle_to_interrupted(
    monkeypatch,
):
    conn = _conn()
    conn.queue_row = _queue(state="parked", attempts=5)
    receipt_payload = {
        "request_id": str(REQUEST_A),
        "client_request_id": str(CLIENT_A),
        "target_turn_id": 17,
        "applied": True,
        "mode": "hard",
    }
    conn.retry_candidates = [{"thread_id": UNIT_A}]
    conn.retry_interrupts = [
        _pending(
            REQUEST_A,
            CLIENT_A,
            turn_id=17,
            receipt_epoch=6,
            receipt_seq=1,
            receipt_kind="interrupt.ack",
            receipt_payload=receipt_payload,
        )
    ]
    consume = AsyncMock(return_value=MagicMock(state="done"))
    monkeypatch.setattr(mod, "consume_applied_interrupt_input_idle", consume)
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=7))
    frame = AsyncMock(return_value=(7, 1))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.retry_stale_interrupt_requests(conn) == 1

    consume.assert_awaited_once()
    assert frame.await_args.kwargs["kind"] == "turn.interrupted"


@pytest.mark.asyncio
async def test_parked_retry_drains_more_than_old_per_thread_limit(monkeypatch):
    conn = _conn()
    conn.queue_row = _queue(state="parked", attempts=5)
    conn.retry_candidates = [{"thread_id": UNIT_A}]
    rows = []
    for turn in range(1, 102):
        request_id = uuid.uuid4()
        client_id = uuid.uuid4()
        payload = {
            "request_id": str(request_id),
            "client_request_id": str(client_id),
            "target_turn_id": turn,
            "applied": False,
            "error_code": "lease_expired",
        }
        rows.append(
            _pending(
                request_id,
                client_id,
                turn_id=turn,
                receipt_epoch=6,
                receipt_seq=turn,
                receipt_kind="interrupt.ack",
                receipt_payload=payload,
            )
        )
    conn.retry_interrupts = rows
    monkeypatch.setattr(mod, "bump_epoch", AsyncMock(return_value=7))
    frame = AsyncMock(return_value=(7, 1))
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.retry_stale_interrupt_requests(conn) == 101

    assert conn.fetchval.await_count == 101
    assert "LIMIT" not in mod._PENDING_STALE_INTERRUPT_RETRY_SQL
    assert frame.await_count == 101
    assert [
        entry.kwargs["payload"]["target_turn_id"] for entry in frame.await_args_list
    ] == list(range(1, 102))
    assert all(
        "target_turn_ids" not in entry.kwargs["payload"]
        for entry in frame.await_args_list
    )


@pytest.mark.asyncio
async def test_queued_rows_are_deferred_to_live_successor_without_system_write(
    monkeypatch,
):
    conn = _conn()
    conn.queue_row = _queue(state="queued", last_leased_by=None)
    conn.retry_candidates = [{"thread_id": UNIT_A}]
    bump = AsyncMock()
    frame = AsyncMock()
    monkeypatch.setattr(mod, "bump_epoch", bump)
    monkeypatch.setattr(mod, "append_system_frame", frame)

    assert await mod.retry_stale_interrupt_requests(conn) == 0

    bump.assert_not_awaited()
    frame.assert_not_awaited()
    assert "queue.state = 'parked'" in mod._STALE_INTERRUPT_RETRY_CANDIDATES_SQL


@pytest.mark.asyncio
async def test_parked_permission_retry_retires_null_only_rolling_residue(
    monkeypatch,
):
    conn = _conn()
    conn.queue_row = _queue(
        state="parked", token=9, leased_by=None, last_leased_by=None
    )
    conn.stale_permission_candidates = [{"thread_id": UNIT_A}]
    retirement = MagicMock(count=1)
    retire = AsyncMock(return_value=retirement)
    monkeypatch.setattr(mod, "retire_stale_stateless_permissions", retire)

    assert await mod.retry_stale_permission_requests(conn) == 1

    retire.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        retired_lease_token=8,
        successor_lease_token=9,
        reason="lease_expired",
        epoch_already_bumped=False,
    )
    candidate_sql = mod._STALE_PERMISSION_RETRY_CANDIDATES_SQL
    assert "accepted_lease_token IS NULL" in candidate_sql
    assert "queue.lease_token > 0" in candidate_sql
    assert "queue.state = 'parked'" in candidate_sql


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "leased", "done"])
async def test_permission_retry_never_system_writes_outside_parked_boundary(
    monkeypatch, state
):
    conn = _conn()
    conn.queue_row = _queue(
        state=state,
        token=9,
        leased_by="successor" if state == "leased" else None,
        last_leased_by=None,
    )
    retire = AsyncMock()
    monkeypatch.setattr(mod, "retire_stale_stateless_permissions", retire)

    assert await mod._retry_stale_permission_thread(conn, thread_id=str(UNIT_A)) == 0

    retire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thread_metadata",
    [
        {},  # old reaper settled directly to done
        {"claim_loss_acknowledged": True},  # parked hold ACK restored done
    ],
)
async def test_done_permission_retry_requires_exact_interrupt_recovery_proof(
    monkeypatch, thread_metadata
):
    conn = _conn()
    conn.thread_row = _thread(metadata=thread_metadata)
    conn.queue_row = _queue(state="done", token=9, leased_by=None, last_leased_by=None)
    conn.done_permission_proof = {"id": REQUEST_A}
    retirement = MagicMock(count=1)
    retire = AsyncMock(return_value=retirement)
    monkeypatch.setattr(mod, "retire_stale_stateless_permissions", retire)

    assert await mod._retry_stale_permission_thread(conn, thread_id=str(UNIT_A)) == 1

    retire.assert_awaited_once_with(
        conn,
        thread_id=str(UNIT_A),
        retired_lease_token=8,
        successor_lease_token=9,
        reason="lease_expired",
        epoch_already_bumped=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"_stateless_active_claim": {"lease_token": 9}},
        {"_stateless_claim_losses": {"8": {}}},
        {"_stateless_claim_loss_hold": {"lease_token": 9}},
    ],
)
async def test_done_permission_retry_excludes_any_claim_authority(
    monkeypatch, metadata
):
    conn = _conn()
    conn.thread_row = _thread(metadata=metadata)
    conn.queue_row = _queue(state="done", token=9, leased_by=None, last_leased_by=None)
    conn.done_permission_proof = {"id": REQUEST_A}
    retire = AsyncMock()
    monkeypatch.setattr(mod, "retire_stale_stateless_permissions", retire)

    assert await mod._retry_stale_permission_thread(conn, thread_id=str(UNIT_A)) == 0
    retire.assert_not_awaited()


def test_done_permission_candidate_requires_exact_linked_recovery_marker():
    candidate = mod._STALE_PERMISSION_RETRY_CANDIDATES_SQL
    proof = mod._LOCK_DONE_PERMISSION_RECOVERY_PROOF_SQL

    assert "queue.state = 'done'" in candidate
    assert "proof_receipt.interrupt_request_id = proof_request.id" in candidate
    assert "proof_receipt.epoch = proof_request.journal_epoch" in candidate
    assert "proof_receipt.seq = proof_request.journal_seq" in candidate
    assert "input_settlement' = 'lease_recovery'" in proof
    assert "input_settled_by_lease_token" in proof
    assert "accepted_lease_token = $2::bigint - 1" in proof
    assert "to_jsonb($2::bigint)" in proof
    assert ")::bigint" not in proof
    assert "proof_receipt.kind = 'interrupt.ack'" in proof
    assert "FOR SHARE OF proof_request, proof_receipt" in proof


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

    async def _reap(c, *, grace_seconds, session_steal=None):
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

    async def _reap(c, *, grace_seconds, session_steal=None):
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
