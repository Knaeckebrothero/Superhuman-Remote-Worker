"""Focused contracts for Gate-3 completion-command admission.

The real-Postgres schema/race coverage lives in
``test_completion_command_schema.py`` and the endpoint remains covered by the
large completion suites.  These tests pin the deliberately small M2 service
boundary: canonical fingerprints, exact-retry disposition, and the ordering of
the short fenced admission transaction.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.job_completion_commands import (
    CompletionFenceRejected,
    CompletionInProgress,
    CompletionPayloadMismatch,
    accept_completion_command,
    canonical_completion_payload,
    complete_completion_command,
    completion_payload_digest,
    fallback_client_report_id,
)


JOB_ID = UUID("11111111-aaaa-4444-8888-111111111111")
COMMAND_ID = UUID("22222222-bbbb-4444-8888-222222222222")
CLIENT_REPORT_ID = UUID("33333333-cccc-4444-8888-333333333333")
AGENT_ID = UUID("44444444-dddd-4444-8888-444444444444")


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "should_stop": True,
        "goal_achieved": True,
        "error": None,
        "freeze_data": {
            "reason": "job_complete",
            "nested": {"z": 3, "a": [2, 1]},
        },
    }
    payload.update(overrides)
    return payload


def _existing_command(
    *,
    state: str,
    payload: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    error_code: str | None = None,
    accepted_lease_token: int | None = 41,
    accepted_agent_id: UUID | None = None,
) -> dict[str, Any]:
    stored_payload = payload or _payload()
    return {
        "id": COMMAND_ID,
        "job_id": JOB_ID,
        "report_seq": 7,
        "client_report_id": CLIENT_REPORT_ID,
        "payload": stored_payload,
        "payload_digest": completion_payload_digest(JOB_ID, stored_payload),
        "accepted_lease_token": accepted_lease_token,
        "accepted_agent_id": accepted_agent_id,
        "state": state,
        "outcome": outcome,
        "error_code": error_code,
    }


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).lower()


@dataclass(frozen=True)
class _Call:
    operation: str
    sql: str
    args: tuple[Any, ...]

    @property
    def normalized_sql(self) -> str:
        return _normalized(self.sql)


class _Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, conn: "_RecordingConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> None:
        assert not self._conn.in_transaction
        self._conn.in_transaction = True
        self._conn.calls.append(_Call("transaction_enter", "", ()))

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._conn.calls.append(_Call("transaction_exit", "", (exc_type,)))
        self._conn.in_transaction = False
        return False


class _Acquire(AbstractAsyncContextManager["_RecordingConnection"]):
    def __init__(self, conn: "_RecordingConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_RecordingConnection":
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _RecordingDB:
    def __init__(self, conn: "_RecordingConnection") -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class _RecordingConnection:
    """Small asyncpg stand-in with enough state to exercise admission.

    Responses are selected from the queried relation rather than call ordinal.
    This keeps the tests tolerant of an idempotency preflight while still
    recording the load-bearing queue-before-job lock and write order.
    """

    def __init__(
        self,
        *,
        existing: dict[str, Any] | None = None,
        lane: str = "stateless",
        assigned_agent_id: UUID | None = None,
        queue_lease_token: int = 41,
        queue_state: str = "leased",
        queue_present: bool | None = None,
        completion_seq_hwm: int = 6,
    ) -> None:
        self.existing = existing
        self.lane = lane
        self.assigned_agent_id = assigned_agent_id
        self.queue_lease_token = queue_lease_token
        self.queue_state = queue_state
        self.queue_present = (
            lane == "stateless" if queue_present is None else queue_present
        )
        self.completion_seq_hwm = completion_seq_hwm
        self.in_transaction = False
        self.calls: list[_Call] = []

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def _record(self, operation: str, sql: str, args: tuple[Any, ...]) -> str:
        assert self.in_transaction, f"{operation} escaped admission transaction"
        self.calls.append(_Call(operation, sql, args))
        return _normalized(sql)

    def _job(self) -> dict[str, Any]:
        return {
            "id": JOB_ID,
            "status": "processing",
            "execution_lane": self.lane,
            "assigned_agent_id": self.assigned_agent_id,
            "completion_seq_hwm": self.completion_seq_hwm,
        }

    def _queue(self) -> dict[str, Any]:
        return {
            "unit_id": JOB_ID,
            "unit_kind": "worker_batch",
            "state": self.queue_state,
            "lease_token": self.queue_lease_token,
            "input_seq": 12,
        }

    def _inserted(self, args: tuple[Any, ...]) -> dict[str, Any]:
        # The service may return either a bare id or a full RETURNING row.  The
        # stable fields are enough for either implementation style.
        return {
            "id": COMMAND_ID,
            "job_id": JOB_ID,
            "report_seq": int(args[1]),
            "client_report_id": args[2],
            "payload": args[3],
            "payload_digest": args[4],
            "accepted_lease_token": args[5],
            "accepted_agent_id": args[6],
            "state": "pending",
            "outcome": None,
        }

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        normalized = self._record("fetchrow", sql, args)
        if normalized.startswith("insert into job_completion_commands"):
            return self._inserted(args)
        if "from job_completion_commands" in normalized:
            return self.existing
        if "from run_queue" in normalized:
            return self._queue() if self.queue_present else None
        if "from jobs" in normalized:
            return self._job()
        if normalized.startswith("update run_queue"):
            return self._queue() if self.queue_present else None
        raise AssertionError(f"unexpected fetchrow query: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        normalized = self._record("fetchval", sql, args)
        if normalized.startswith("insert into job_completion_commands"):
            return COMMAND_ID
        if "from job_completion_commands" in normalized:
            if self.existing is None:
                return None
            return self.existing.get("id")
        if "from run_queue" in normalized:
            return self.queue_lease_token if self.queue_present else None
        if "from jobs" in normalized:
            return self.completion_seq_hwm
        if normalized.startswith("update run_queue"):
            if not self.queue_present or int(args[1]) != self.queue_lease_token:
                return None
            self.queue_state = "done"
            return self.queue_state
        raise AssertionError(f"unexpected fetchval query: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        normalized = self._record("execute", sql, args)
        if normalized.startswith("update jobs"):
            self.completion_seq_hwm += 1
            return "UPDATE 1"
        if normalized.startswith("update run_queue"):
            self.queue_state = "done"
            return "UPDATE 1"
        if normalized.startswith("insert into job_completion_commands"):
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute query: {sql}")


def _sql_calls(conn: _RecordingConnection) -> list[_Call]:
    return [call for call in conn.calls if call.sql]


def _mutating_calls(conn: _RecordingConnection) -> list[_Call]:
    prefixes = ("insert ", "update ", "delete ")
    return [
        call for call in _sql_calls(conn) if call.normalized_sql.startswith(prefixes)
    ]


def test_canonical_payload_excludes_only_transport_and_fence_fields() -> None:
    payload = _payload(
        lease_token=91,
        agent_id=str(AGENT_ID),
        client_report_id=str(CLIENT_REPORT_ID),
        transport_trace_id="opaque-hop-value",
    )
    before = json.loads(json.dumps(payload))

    canonical = canonical_completion_payload(payload)

    assert canonical == {
        "should_stop": True,
        "goal_achieved": True,
        "error": None,
        "freeze_data": {
            "reason": "job_complete",
            "nested": {"z": 3, "a": [2, 1]},
        },
        # Unknown application fields remain part of the operation identity.
        "transport_trace_id": "opaque-hop-value",
    }
    assert payload == before, "canonicalization must not mutate stored payload"


def test_digest_is_canonical_job_scoped_and_transport_independent() -> None:
    left = _payload(
        lease_token=1,
        agent_id=str(AGENT_ID),
        client_report_id=str(CLIENT_REPORT_ID),
    )
    right = {
        "freeze_data": {
            "nested": {"a": [2, 1], "z": 3},
            "reason": "job_complete",
        },
        "error": None,
        "goal_achieved": True,
        "should_stop": True,
        "lease_token": 999,
        "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "client_report_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }

    digest = completion_payload_digest(JOB_ID, left)

    assert digest == completion_payload_digest(str(JOB_ID), right)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest != completion_payload_digest(
        UUID("aaaaaaaa-1111-4111-8111-111111111111"), right
    )
    assert digest != completion_payload_digest(
        JOB_ID, {**right, "goal_achieved": False}
    )


def test_fallback_report_id_is_stable_uuid5_scoped_to_job_and_sequence() -> None:
    fallback = fallback_client_report_id(JOB_ID, 7)

    assert fallback_client_report_id(str(JOB_ID), 7) == fallback
    assert UUID(fallback).version == 5
    assert fallback_client_report_id(JOB_ID, 8) != fallback
    assert (
        fallback_client_report_id(UUID("aaaaaaaa-1111-4111-8111-111111111111"), 7)
        != fallback
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "finalizing"])
async def test_equal_retry_in_flight_is_409_semantics(state: str) -> None:
    conn = _RecordingConnection(existing=_existing_command(state=state))

    with pytest.raises(CompletionInProgress):
        await accept_completion_command(
            _RecordingDB(conn),
            job_id=str(JOB_ID),
            payload=_payload(),
            lease_token="41",
            agent_id=None,
            client_report_id=str(CLIENT_REPORT_ID),
            requested_by="agent:test",
        )

    assert _mutating_calls(conn) == []


@pytest.mark.asyncio
async def test_payload_mismatch_takes_precedence_over_in_flight_state() -> None:
    conn = _RecordingConnection(existing=_existing_command(state="pending"))

    with pytest.raises(CompletionPayloadMismatch):
        await accept_completion_command(
            _RecordingDB(conn),
            job_id=str(JOB_ID),
            payload=_payload(goal_achieved=False),
            lease_token="41",
            agent_id=None,
            client_report_id=str(CLIENT_REPORT_ID),
            requested_by="agent:test",
        )

    assert _mutating_calls(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "outcome", "expected_disposition", "winning_seq", "abandoned"),
    [
        (
            "done",
            {"status": "handled", "job_id": str(JOB_ID)},
            "replay_done",
            None,
            (),
        ),
        ("parked", None, "replay_parked", None, ()),
        (
            "superseded",
            {"winning_report_seq": 4},
            "replay_superseded",
            4,
            (),
        ),
        (
            "force_resolved",
            {"abandoned_effects": ["workspace_archive", "notification"]},
            "replay_force_resolved",
            None,
            ("workspace_archive", "notification"),
        ),
    ],
)
async def test_terminal_retry_matrix_replays_stored_disposition(
    state: str,
    outcome: dict[str, Any] | None,
    expected_disposition: str,
    winning_seq: int | None,
    abandoned: tuple[str, ...],
) -> None:
    conn = _RecordingConnection(
        existing=_existing_command(
            state=state,
            outcome=outcome,
            error_code="operator_required" if state == "parked" else None,
        )
    )

    result = await accept_completion_command(
        _RecordingDB(conn),
        job_id=str(JOB_ID),
        payload=_payload(),
        lease_token="41",
        agent_id=None,
        client_report_id=str(CLIENT_REPORT_ID),
        requested_by="agent:test",
    )

    assert result.disposition == expected_disposition
    assert result.command_id == str(COMMAND_ID)
    assert result.report_seq == 7
    assert result.state == state
    assert result.stored_payload == _payload()
    assert result.outcome == outcome
    assert result.winning_report_seq == winning_seq
    assert tuple(result.abandoned_effects or ()) == abandoned
    assert result.queue_terminalized is True
    assert _mutating_calls(conn) == []


@pytest.mark.asyncio
async def test_exact_retry_uses_immutable_accepted_fence_after_queue_moves_on() -> None:
    existing = _existing_command(
        state="done",
        outcome={"status": "handled", "job_id": str(JOB_ID)},
        accepted_lease_token=41,
    )
    # Accept already closed token 41's queue unit. A later claim can bump the
    # live token, but the exact HTTP retry must still authenticate against the
    # immutable fence captured on its command row.
    conn = _RecordingConnection(
        existing=existing,
        queue_lease_token=42,
        queue_state="leased",
    )

    replay = await accept_completion_command(
        _RecordingDB(conn),
        job_id=str(JOB_ID),
        payload=_payload(),
        lease_token=41,
        agent_id=None,
        client_report_id=str(CLIENT_REPORT_ID),
        requested_by="agent:test",
    )

    assert replay.disposition == "replay_done"
    assert replay.outcome == existing["outcome"]
    assert _mutating_calls(conn) == []

    wrong_fence = _RecordingConnection(
        existing=existing,
        queue_lease_token=42,
        queue_state="leased",
    )
    with pytest.raises(CompletionFenceRejected):
        await accept_completion_command(
            _RecordingDB(wrong_fence),
            job_id=str(JOB_ID),
            payload=_payload(),
            lease_token=42,
            agent_id=None,
            client_report_id=str(CLIENT_REPORT_ID),
            requested_by="agent:test",
        )
    assert _mutating_calls(wrong_fence) == []


@pytest.mark.asyncio
async def test_stateless_admission_locks_queue_before_job_and_inserts_first() -> None:
    conn = _RecordingConnection(lane="stateless", queue_lease_token=41)

    result = await accept_completion_command(
        _RecordingDB(conn),
        job_id=str(JOB_ID),
        payload=_payload(),
        lease_token="41",
        agent_id=None,
        client_report_id=str(CLIENT_REPORT_ID),
        requested_by="agent:test",
    )

    calls = _sql_calls(conn)
    queue_lock = next(
        index
        for index, call in enumerate(calls)
        if "from run_queue" in call.normalized_sql
        and "for update" in call.normalized_sql
    )
    job_lock = next(
        index
        for index, call in enumerate(calls)
        if "from jobs" in call.normalized_sql and "for update" in call.normalized_sql
    )
    assert queue_lock < job_lock
    assert _mutating_calls(conn)[0].normalized_sql.startswith(
        "insert into job_completion_commands"
    )
    assert result.disposition == "fresh"
    assert result.report_seq == 7
    assert result.state == "pending"
    assert result.queue_terminalized is True


@pytest.mark.asyncio
async def test_missing_client_report_id_uses_commit_ordered_fallback() -> None:
    conn = _RecordingConnection(lane="stateless", queue_lease_token=41)

    result = await accept_completion_command(
        _RecordingDB(conn),
        job_id=str(JOB_ID),
        payload=_payload(),
        lease_token=41,
        agent_id=None,
        client_report_id=None,
        requested_by="agent:test",
    )

    assert result.report_seq == 7
    assert result.client_report_id == fallback_client_report_id(JOB_ID, 7)
    insert = _mutating_calls(conn)[0]
    assert insert.args[2] == UUID(fallback_client_report_id(JOB_ID, 7))


@pytest.mark.asyncio
async def test_stateless_stale_token_is_rejected_before_any_write() -> None:
    conn = _RecordingConnection(lane="stateless", queue_lease_token=42)

    with pytest.raises(CompletionFenceRejected):
        await accept_completion_command(
            _RecordingDB(conn),
            job_id=str(JOB_ID),
            payload=_payload(),
            lease_token="41",
            agent_id=None,
            client_report_id=str(CLIENT_REPORT_ID),
            requested_by="agent:test",
        )

    assert _mutating_calls(conn) == []


@pytest.mark.asyncio
async def test_pinned_agent_identity_is_fenced_under_job_lock() -> None:
    conn = _RecordingConnection(lane="pinned", assigned_agent_id=AGENT_ID)

    result = await accept_completion_command(
        _RecordingDB(conn),
        job_id=str(JOB_ID),
        payload=_payload(),
        lease_token=None,
        agent_id=str(AGENT_ID),
        client_report_id=str(CLIENT_REPORT_ID),
        requested_by="agent:test",
    )

    calls = _sql_calls(conn)
    queue_lock = next(
        index
        for index, call in enumerate(calls)
        if "from run_queue" in call.normalized_sql
        and "for update" in call.normalized_sql
    )
    job_lock = next(
        index
        for index, call in enumerate(calls)
        if "from jobs" in call.normalized_sql and "for update" in call.normalized_sql
    )
    assert queue_lock < job_lock
    assert _mutating_calls(conn)[0].normalized_sql.startswith(
        "insert into job_completion_commands"
    )
    assert result.disposition == "fresh"
    assert result.queue_terminalized is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_token", "agent_id"),
    [(None, None), ("41", str(AGENT_ID))],
)
async def test_agent_origin_requires_exactly_one_fence_arm(
    lease_token: str | None, agent_id: str | None
) -> None:
    conn = _RecordingConnection(lane="stateless", queue_lease_token=41)

    with pytest.raises(CompletionFenceRejected):
        await accept_completion_command(
            _RecordingDB(conn),
            job_id=str(JOB_ID),
            payload=_payload(),
            lease_token=lease_token,
            agent_id=agent_id,
            client_report_id=str(CLIENT_REPORT_ID),
            requested_by="agent:test",
        )

    assert _mutating_calls(conn) == []


class _SettleConnection:
    def __init__(self, updated: bool) -> None:
        self.updated = updated
        self.calls: list[_Call] = []

    async def fetchval(self, sql: str, *args: Any) -> int | None:
        self.calls.append(_Call("fetchval", sql, args))
        return 1 if self.updated else None


@pytest.mark.asyncio
@pytest.mark.parametrize(("updated", "expected"), [(True, True), (False, False)])
async def test_settle_helper_is_pending_finalizing_cas(
    updated: bool, expected: bool
) -> None:
    conn = _SettleConnection(updated)
    outcome = {
        "status": "handled",
        "actions": ["two", "one"],
        "nested": {"z": 2, "a": 1},
    }

    assert await complete_completion_command(conn, str(COMMAND_ID), outcome) is expected

    [call] = conn.calls
    assert "state in ('pending', 'finalizing')" in call.normalized_sql
    assert "set state = 'done'" in call.normalized_sql
    assert call.args[0] == COMMAND_ID
    assert call.args[1] == json.dumps(
        outcome,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
