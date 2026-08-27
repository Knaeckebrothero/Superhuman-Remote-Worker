"""Focused unit contracts for the independent session-memory effect drain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from orchestrator.services.session_memory_effects import (
    SESSION_MEMORY_EFFECT_NAME,
    SessionMemoryEffect,
    SessionMemoryEffectDrain,
    SessionMemoryEffectPermanentError,
)


PRODUCER_ID = UUID("11111111-aaaa-4111-8111-111111111111")
THREAD_ID = UUID("22222222-bbbb-4222-8222-222222222222")
INPUT_ID = UUID("33333333-cccc-4333-8333-333333333333")


def _row(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    result = {
        "producer_id": PRODUCER_ID,
        "scope_id": THREAD_ID,
        "effect_name": SESSION_MEMORY_EFFECT_NAME,
        "effect_group": "memory_extraction",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "run_after": now,
        "created_at": now,
        "intent_at": now,
        "complete_by": now + timedelta(minutes=2),
        "completed_at": None,
        "detail": {
            "input_message_id": str(INPUT_ID),
            "turn_number": 4,
            "boundary_seq": 17,
            "end_seq": 19,
            "memory_scope_kind": "thread",
            "memory_scope_id": str(THREAD_ID),
        },
        "error_code": None,
    }
    result.update(overrides)
    return result


class _DrainDB:
    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = list(rows)
        self.claim_session_memory_effects = AsyncMock(side_effect=self._claim)
        self.renew_session_memory_effect = AsyncMock(return_value=True)
        self.finish_session_memory_effect = AsyncMock(return_value=True)
        self.retry_session_memory_effect = AsyncMock(return_value="pending")
        self.prune_session_memory_effects = AsyncMock(return_value=0)

    async def _claim(self, **_kwargs: Any) -> list[dict[str, Any]]:
        rows, self.rows = self.rows, []
        return rows


@pytest.mark.asyncio
async def test_drain_executes_and_replays_one_terminal_receipt() -> None:
    db = _DrainDB(_row())
    executor = AsyncMock(return_value={"stored": 2})
    drain = SessionMemoryEffectDrain(db, executor)

    result = await drain.drain_once()
    replay = await drain.drain_once()

    assert (result.claimed, result.done, result.lease_lost) == (1, 1, 0)
    assert replay.claimed == 0
    executor.assert_awaited_once()
    effect = executor.await_args.args[0]
    assert isinstance(effect, SessionMemoryEffect)
    assert effect.idempotency_key == (
        f"session_turn:{PRODUCER_ID}:final_memory_extraction"
    )
    settle = db.finish_session_memory_effect.await_args.kwargs
    assert settle["detail"] == {
        "input_message_id": str(INPUT_ID),
        "turn_number": 4,
        "boundary_seq": 17,
        "end_seq": 19,
        "memory_scope_kind": "thread",
        "memory_scope_id": str(THREAD_ID),
        "output": {"stored": 2},
    }
    # One explicit pre-call renewal; a fast callback finishes before the
    # periodic heartbeat needs another.
    db.renew_session_memory_effect.assert_awaited_once()


@pytest.mark.asyncio
async def test_lost_pre_call_claim_never_invokes_external_executor() -> None:
    db = _DrainDB(_row())
    db.renew_session_memory_effect.return_value = False
    executor = AsyncMock()

    result = await SessionMemoryEffectDrain(db, executor).drain_once()

    assert result.claimed == 1
    assert result.lease_lost == 1
    executor.assert_not_awaited()
    db.finish_session_memory_effect.assert_not_awaited()
    db.retry_session_memory_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_permit_renews_the_same_exact_owner_claim() -> None:
    db = _DrainDB(_row())

    async def executor(effect: SessionMemoryEffect) -> dict[str, int]:
        assert effect.authority_permit is not None
        await effect.authority_permit()
        return {"stored": 0}

    result = await SessionMemoryEffectDrain(db, executor).drain_once()

    assert result.done == 1
    assert db.renew_session_memory_effect.await_count == 2
    initial, permit = db.renew_session_memory_effect.await_args_list
    assert initial.kwargs == permit.kwargs
    assert initial.kwargs["producer_id"] == str(PRODUCER_ID)
    assert initial.kwargs["effect_name"] == SESSION_MEMORY_EFFECT_NAME
    assert initial.kwargs["claimed_by"]


@pytest.mark.asyncio
async def test_permit_database_error_is_typed_lease_loss_not_retry() -> None:
    db = _DrainDB(_row())
    db.renew_session_memory_effect.side_effect = [True, RuntimeError("db down")]

    async def executor(effect: SessionMemoryEffect) -> dict[str, int]:
        assert effect.authority_permit is not None
        await effect.authority_permit()
        return {"stored": 0}

    result = await SessionMemoryEffectDrain(db, executor).drain_once()

    assert result.lease_lost == 1
    db.retry_session_memory_effect.assert_not_awaited()
    db.finish_session_memory_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_failure_releases_exact_claim_on_backoff() -> None:
    db = _DrainDB(_row(attempts=2))
    executor = AsyncMock(side_effect=TimeoutError("auxiliary timed out"))
    drain = SessionMemoryEffectDrain(db, executor, random_source=lambda: 0.0)

    result = await drain.drain_once()

    assert (result.retried, result.dead) == (1, 0)
    retry = db.retry_session_memory_effect.await_args.kwargs
    assert retry["producer_id"] == str(PRODUCER_ID)
    assert retry["error_code"] == "TimeoutError"
    assert retry["backoff_seconds"] == 10.0
    assert retry["force_dead"] is False
    db.finish_session_memory_effect.assert_not_awaited()


def test_retry_delay_is_linear_before_the_cap() -> None:
    from orchestrator.services.session_memory_effects import _retry_delay

    assert [
        _retry_delay(
            attempt,
            random_source=lambda: 0.0,
            base_seconds=5.0,
            max_seconds=300.0,
        )
        for attempt in (1, 2, 3)
    ] == [5.0, 10.0, 15.0]


@pytest.mark.asyncio
async def test_permanent_output_shape_failure_is_parked_dead() -> None:
    db = _DrainDB(_row())
    executor = AsyncMock(return_value={"unbounded": "x" * (9 * 1024)})
    db.retry_session_memory_effect.return_value = "dead"

    result = await SessionMemoryEffectDrain(db, executor).drain_once()

    assert result.dead == 1
    retry = db.retry_session_memory_effect.await_args.kwargs
    assert retry["force_dead"] is True
    assert retry["error_code"] == "SessionMemoryEffectPermanentError"


@pytest.mark.asyncio
async def test_response_loss_keeps_obligation_for_stable_idempotent_replay() -> None:
    db = _DrainDB(_row())
    db.finish_session_memory_effect.side_effect = RuntimeError("db unavailable")
    logical_writes: set[str] = set()
    calls = 0

    async def executor(effect: SessionMemoryEffect) -> dict[str, int]:
        nonlocal calls
        calls += 1
        logical_writes.add(effect.idempotency_key)
        return {"stored": 1}

    first = await SessionMemoryEffectDrain(db, executor).drain_once()
    # Model the expired claim becoming visible to a successor. The producer id
    # is unchanged, so a destination-side idempotency key absorbs ambiguity.
    db.rows = [_row(attempts=2)]
    db.finish_session_memory_effect.side_effect = None
    db.finish_session_memory_effect.return_value = True
    second = await SessionMemoryEffectDrain(db, executor).drain_once()

    assert first.lease_lost == 1
    assert second.done == 1
    assert calls == 2
    assert logical_writes == {f"session_turn:{PRODUCER_ID}:final_memory_extraction"}
    db.retry_session_memory_effect.assert_not_awaited()


def test_effect_rejects_malformed_or_wrong_stable_identity() -> None:
    with pytest.raises(SessionMemoryEffectPermanentError, match="malformed"):
        SessionMemoryEffect.from_row(_row(detail="not-json"))
    with pytest.raises(SessionMemoryEffectPermanentError, match="unsupported"):
        SessionMemoryEffect.from_row(_row(effect_group="wrong"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"claim_batch": 0},
        {"lease_seconds": 0},
        {"heartbeat_seconds": 120, "lease_seconds": 120},
        {"retry_base_seconds": 10, "retry_max_seconds": 5},
    ],
)
def test_drain_rejects_unsafe_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SessionMemoryEffectDrain(_DrainDB(), AsyncMock(), **kwargs)
