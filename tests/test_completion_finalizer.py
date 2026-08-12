"""Focused unit contracts for the durable completion finalizer.

The fake below intentionally models the state transitions guarded by the SQL,
not a queue of canned return values.  That lets the tests exercise overlapping
claims and stale terms while also pinning the load-bearing SQL predicates.
Real-Postgres DDL coverage remains in ``test_completion_command_schema.py``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.completion_finalizer import (
    CompletionEffectInFlight,
    CompletionEffectRunner,
    CompletionEffectVersionError,
    CompletionFinalizer,
    CompletionFinalizerError,
    CompletionLeaseLost,
    EFFECT_DETAIL_LIMIT_BYTES,
    LeaderTerm,
)
from orchestrator.services.job_completion_commands import COMPLETION_CODE_VERSION


NOW = datetime.now(UTC)
JOB_ID = UUID("11111111-aaaa-4111-8111-111111111111")
COMMAND_ID = UUID("22222222-bbbb-4222-8222-222222222222")
SECOND_COMMAND_ID = UUID("33333333-cccc-4333-8333-333333333333")


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).lower()


def _command(
    command_id: UUID = COMMAND_ID,
    *,
    report_seq: int = 1,
    state: str = "pending",
    attempts: int = 0,
    max_attempts: int = 5,
    run_after: datetime | None = None,
    lease_expires_at: datetime | None = None,
    finalizing_by: str | None = None,
    deadline_at: datetime | None = None,
    code_version: str = COMPLETION_CODE_VERSION,
) -> dict[str, Any]:
    return {
        "id": command_id,
        "job_id": JOB_ID,
        "report_seq": report_seq,
        "client_report_id": UUID("44444444-dddd-4444-8444-444444444444"),
        "payload": {"should_stop": True},
        "payload_digest": "digest",
        "reported_at": NOW,
        "accepted_lease_token": 9,
        "accepted_agent_id": None,
        "origin": "agent",
        "requested_by": "test-agent",
        "state": state,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "run_after": run_after or NOW,
        "lease_expires_at": lease_expires_at,
        "deadline_at": deadline_at or NOW + timedelta(hours=1),
        "finalizing_by": finalizing_by,
        "code_version": code_version,
        "outcome": None,
        "finalized_at": None,
        "error_code": None,
    }


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _StatefulConnection:
    """Small asyncpg stand-in that enforces the finalizer's SQL predicates."""

    def __init__(self, *commands: dict[str, Any]) -> None:
        self.now = NOW
        self.commands = {UUID(str(row["id"])): dict(row) for row in commands}
        self.effects: dict[tuple[UUID, str], dict[str, Any]] = {}
        self.leader: dict[str, Any] | None = None
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)

    def _record(self, operation: str, sql: str, args: tuple[Any, ...]) -> str:
        normalized = _normalized(sql)
        self.calls.append((operation, normalized, args))
        return normalized

    def _row(self, command_id: Any) -> dict[str, Any] | None:
        row = self.commands.get(UUID(str(command_id)))
        return dict(row) if row is not None else None

    def _has_predecessor(self, command: dict[str, Any]) -> bool:
        return any(
            candidate["job_id"] == command["job_id"]
            and candidate["report_seq"] < command["report_seq"]
            and candidate["state"] in {"pending", "finalizing", "parked"}
            for candidate in self.commands.values()
        )

    def _command_term_live(self, command_id: UUID, owner: str) -> bool:
        command = self.commands[command_id]
        return (
            command["state"] == "finalizing"
            and command["finalizing_by"] == owner
            and command["lease_expires_at"] is not None
            and command["lease_expires_at"] > self.now
        )

    def _has_live_effect(self, command_id: UUID) -> bool:
        return any(
            producer_id == command_id
            and effect["state"] == "pending"
            and effect["complete_by"] is not None
            and effect["complete_by"] > self.now
            for (producer_id, _), effect in self.effects.items()
        )

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        normalized = self._record("fetchrow", sql, args)

        if normalized.startswith("select * from job_completion_commands"):
            return self._row(args[0])

        if normalized.startswith(
            "update job_completion_commands as command set state = 'parked'"
        ):
            command_id, error_code, version_mismatch, code_version = args
            command = self.commands[UUID(str(command_id))]
            claimable_state = command["state"] == "pending" or (
                command["state"] == "finalizing"
                and (
                    command["lease_expires_at"] is None
                    or command["lease_expires_at"] < self.now
                )
            )
            exhausted = (
                command["deadline_at"] <= self.now
                or command["attempts"] >= command["max_attempts"]
            )
            version_failed = command["code_version"] != code_version
            eligible_reason = (version_mismatch and version_failed) or (
                not version_mismatch and exhausted
            )
            if (
                not claimable_state
                or self._has_predecessor(command)
                or not eligible_reason
            ):
                return None
            command.update(
                state="parked",
                error_code=error_code,
                finalizing_by=None,
                lease_expires_at=None,
            )
            return dict(command)

        if normalized.startswith(
            "update job_completion_commands as command set state = 'finalizing'"
        ):
            command_id, owner, code_version, lease_seconds, inline = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            claimable_state = command["state"] == "pending" or (
                command["state"] == "finalizing"
                and (
                    command["lease_expires_at"] is None
                    or command["lease_expires_at"] < self.now
                )
            )
            if not all(
                (
                    command["code_version"] == code_version,
                    command["deadline_at"] > self.now,
                    command["attempts"] < command["max_attempts"],
                    claimable_state,
                    inline or command["run_after"] <= self.now,
                    not self._has_predecessor(command),
                    not self._has_live_effect(command_id),
                )
            ):
                return None
            command.update(
                state="finalizing",
                attempts=command["attempts"] + 1,
                finalizing_by=owner,
                lease_expires_at=min(
                    command["deadline_at"],
                    self.now + timedelta(seconds=float(lease_seconds)),
                ),
            )
            return dict(command)

        if normalized.startswith("insert into completion_effects"):
            command_id, owner, name, group, lease_seconds = args
            command_id = UUID(str(command_id))
            if not self._command_term_live(command_id, owner):
                return None
            key = (command_id, name)
            if key in self.effects:
                return None
            requested_complete_by = self.now + timedelta(seconds=float(lease_seconds))
            if "command.lease_expires_at" in normalized and "least(" in normalized:
                # Model the correctness property of the production expression;
                # its exact write margin is asserted through SQL shape below.
                requested_complete_by = min(
                    requested_complete_by,
                    self.commands[command_id]["lease_expires_at"]
                    - timedelta(seconds=5),
                )
            effect = {
                "effect_name": name,
                "effect_group": group,
                "state": "pending",
                "attempts": 1,
                "max_attempts": 5,
                "intent_at": self.now,
                "complete_by": requested_complete_by,
                "completed_at": None,
                "detail": {},
                "error_code": None,
                "remaining_seconds": (requested_complete_by - self.now).total_seconds(),
            }
            self.effects[key] = effect
            return dict(effect)

        if normalized.startswith("select effect_group, state, attempts"):
            command_id, name, owner = args
            command_id = UUID(str(command_id))
            if not self._command_term_live(command_id, owner):
                return None
            effect = self.effects.get((command_id, name))
            return dict(effect) if effect is not None else None

        if normalized.startswith("select effect_name, effect_group, state"):
            command_id, name, owner = args
            command_id = UUID(str(command_id))
            if not self._command_term_live(command_id, owner):
                return None
            effect = self.effects.get((command_id, name))
            return dict(effect) if effect is not None else None

        if normalized.startswith(
            "select effect.detail from completion_effects as effect"
        ):
            command_id, owner, name = args
            command_id = UUID(str(command_id))
            effect = self.effects.get((command_id, name))
            if (
                not self._command_term_live(command_id, owner)
                or effect is None
                or effect["state"] != "pending"
                or effect["complete_by"] is None
                or effect["complete_by"] <= self.now
            ):
                return None
            return {"detail": dict(effect["detail"])}

        if normalized.startswith("update completion_effects as effect set detail"):
            command_id, owner, name, detail_json = args
            command_id = UUID(str(command_id))
            effect = self.effects.get((command_id, name))
            if (
                not self._command_term_live(command_id, owner)
                or effect is None
                or effect["state"] != "pending"
                or effect["complete_by"] is None
                or effect["complete_by"] <= self.now
            ):
                return None
            effect["detail"] = json.loads(detail_json)
            return {"detail": dict(effect["detail"])}

        if normalized.startswith("update completion_effects as effect set attempts"):
            command_id, owner, name, lease_seconds = args
            command_id = UUID(str(command_id))
            effect = self.effects[(command_id, name)]
            if (
                not self._command_term_live(command_id, owner)
                or effect["state"] != "pending"
                or (
                    effect["complete_by"] is not None
                    and effect["complete_by"] > self.now
                )
            ):
                return None
            requested_complete_by = min(
                self.now + timedelta(seconds=float(lease_seconds)),
                self.commands[command_id]["lease_expires_at"] - timedelta(seconds=5),
            )
            effect.update(
                attempts=effect["attempts"] + 1,
                intent_at=self.now,
                complete_by=requested_complete_by,
                error_code=None,
            )
            return {
                "attempts": effect["attempts"],
                "complete_by": effect["complete_by"],
                "remaining_seconds": (effect["complete_by"] - self.now).total_seconds(),
            }

        if normalized.startswith("update job_completion_commands set state = 'done'"):
            command_id, owner, outcome_json = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            if (
                not self._command_term_live(command_id, owner)
                or command["deadline_at"] <= self.now
            ):
                return None
            command.update(
                state="done",
                outcome=json.loads(outcome_json),
                finalized_at=self.now,
                error_code=None,
                finalizing_by=None,
                lease_expires_at=None,
            )
            return dict(command)

        if normalized.startswith("update job_completion_commands set state = 'parked'"):
            command_id, owner, error_code = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            if not self._command_term_live(command_id, owner) or not (
                command["attempts"] >= command["max_attempts"]
                or command["deadline_at"] <= self.now
            ):
                return None
            command.update(
                state="parked",
                error_code=error_code,
                finalizing_by=None,
                lease_expires_at=None,
            )
            return dict(command)

        if normalized.startswith(
            "update job_completion_commands set state = 'pending'"
        ):
            command_id, owner, jitter = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            if (
                not self._command_term_live(command_id, owner)
                or command["attempts"] >= command["max_attempts"]
                or command["deadline_at"] <= self.now
            ):
                return None
            command.update(
                state="pending",
                run_after=self.now
                + timedelta(seconds=5.0 * command["attempts"] * (1.0 + float(jitter))),
                error_code=None,
                finalizing_by=None,
                lease_expires_at=None,
            )
            return dict(command)

        if normalized.startswith("insert into completion_finalizer_leases"):
            lease_name, leader_id, lease_seconds = args
            if self.leader is not None:
                return None
            self.leader = {
                "lease_name": lease_name,
                "leader_id": leader_id,
                "elected_at": self.now,
                "expires_at": self.now + timedelta(seconds=float(lease_seconds)),
            }
            return {"elected_at": self.now}

        raise AssertionError(f"unexpected fetchrow query: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        normalized = self._record("fetchval", sql, args)

        if normalized.startswith("select complete_by > now()"):
            command_id, name = args
            effect = self.effects.get((UUID(str(command_id)), name))
            return bool(
                effect is not None
                and effect["complete_by"] is not None
                and effect["complete_by"] > self.now
            )

        if normalized.startswith("with completed_effect as"):
            command_id, owner, name, detail_json, base_lease_seconds = args
            command_id = UUID(str(command_id))
            effect = self.effects[(command_id, name)]
            if (
                not self._command_term_live(command_id, owner)
                or effect["state"] != "pending"
                or effect["complete_by"] is None
                or effect["complete_by"] <= self.now
            ):
                return None
            effect.update(
                state="done",
                completed_at=self.now,
                complete_by=None,
                detail=json.loads(detail_json),
                error_code=None,
            )
            self.commands[command_id]["lease_expires_at"] = min(
                self.commands[command_id]["deadline_at"],
                self.now + timedelta(seconds=float(base_lease_seconds)),
            )
            return 1

        if normalized.startswith("update job_completion_commands set lease_expires_at"):
            command_id, owner, lease_seconds, *minimum_budget = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            if (
                command["state"] != "finalizing"
                or command["finalizing_by"] != owner
                or command["lease_expires_at"] is None
                or command["lease_expires_at"] < self.now
                or command["deadline_at"] <= self.now
                or (
                    minimum_budget
                    and command["deadline_at"]
                    <= self.now + timedelta(seconds=float(minimum_budget[0]))
                )
            ):
                return None
            command["lease_expires_at"] = min(
                command["deadline_at"],
                max(
                    command["lease_expires_at"],
                    self.now + timedelta(seconds=float(lease_seconds)),
                ),
            )
            return 1

        if normalized.startswith("select command.id from job_completion_commands"):
            candidates = [
                command
                for command in self.commands.values()
                if (
                    (command["state"] == "pending" and command["run_after"] <= self.now)
                    or (
                        command["state"] == "finalizing"
                        and (
                            command["lease_expires_at"] is None
                            or command["lease_expires_at"] < self.now
                        )
                    )
                    or (
                        command["state"] in {"pending", "finalizing"}
                        and command["deadline_at"] <= self.now
                    )
                )
                and not self._has_predecessor(command)
                and not self._has_live_effect(UUID(str(command["id"])))
            ]
            if not candidates:
                return None
            candidates.sort(
                key=lambda row: (
                    row["run_after"],
                    row["reported_at"],
                    row["job_id"],
                    row["report_seq"],
                )
            )
            return candidates[0]["id"]

        if normalized.startswith("update completion_finalizer_leases"):
            lease_name, leader_id, elected_at, lease_seconds = args
            if (
                self.leader is None
                or self.leader["lease_name"] != lease_name
                or self.leader["leader_id"] != leader_id
                or self.leader["elected_at"] != elected_at
                or self.leader["expires_at"] < self.now
            ):
                return None
            self.leader["expires_at"] = self.now + timedelta(
                seconds=float(lease_seconds)
            )
            return 1

        raise AssertionError(f"unexpected fetchval query: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        normalized = self._record("execute", sql, args)

        if normalized.startswith("update completion_effects as effect"):
            command_id, owner, name, error_code = args
            command_id = UUID(str(command_id))
            effect = self.effects[(command_id, name)]
            complete_by_guard = "effect.complete_by > now()" not in normalized or (
                effect["complete_by"] is not None and effect["complete_by"] > self.now
            )
            if (
                self._command_term_live(command_id, owner)
                and effect["state"] == "pending"
                and complete_by_guard
            ):
                effect["error_code"] = error_code
                return "UPDATE 1"
            return "UPDATE 0"

        if normalized.startswith("delete from completion_finalizer_leases"):
            if "expires_at < now()" in normalized:
                if self.leader is not None and self.leader["expires_at"] < self.now:
                    self.leader = None
                    return "DELETE 1"
                return "DELETE 0"
            lease_name, leader_id, elected_at = args
            if (
                self.leader is not None
                and self.leader["lease_name"] == lease_name
                and self.leader["leader_id"] == leader_id
                and self.leader["elected_at"] == elected_at
            ):
                self.leader = None
                return "DELETE 1"
            return "DELETE 0"

        raise AssertionError(f"unexpected execute query: {sql}")


def _finalizing(
    *,
    owner: str = "owner-a",
    attempts: int = 1,
    max_attempts: int = 5,
) -> dict[str, Any]:
    return _command(
        state="finalizing",
        attempts=attempts,
        max_attempts=max_attempts,
        finalizing_by=owner,
        lease_expires_at=NOW + timedelta(minutes=2),
    )


@pytest.mark.asyncio
async def test_effect_intent_precedes_callback_and_completed_detail_replays() -> None:
    conn = _StatefulConnection(_finalizing())
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
        effect_lease_seconds=30,
    )
    calls = 0

    async def effect() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        row = conn.effects[(COMMAND_ID, "workspace_archive")]
        assert row["state"] == "pending"
        assert row["intent_at"] == NOW
        assert row["complete_by"] == NOW + timedelta(seconds=30)
        return {"pod_uid": "uid-a"}

    assert await runner.run(
        name="workspace_archive", group="teardown", callback=effect
    ) == {"pod_uid": "uid-a"}
    assert conn.effects[(COMMAND_ID, "workspace_archive")]["state"] == "done"

    # A successor command term resumes from the durable detail and never calls
    # the external effect again.
    conn.commands[COMMAND_ID].update(
        finalizing_by="owner-b", lease_expires_at=NOW + timedelta(minutes=2)
    )
    successor = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-b",
        effect_lease_seconds=30,
    )

    async def must_not_run() -> None:
        raise AssertionError("completed effect callback was replayed")

    assert await successor.run(
        name="workspace_archive", group="teardown", callback=must_not_run
    ) == {"pod_uid": "uid-a"}
    assert calls == 1


@pytest.mark.asyncio
async def test_effect_capture_preserves_first_identity_and_completion_merges_it() -> (
    None
):
    conn = _StatefulConnection(_finalizing())
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
        effect_lease_seconds=30,
    )
    identity = {
        "kind": "kubernetes",
        "pod_uid": "pod-a",
        "pvc_uid": "pvc-a",
        "service_uid": "service-a",
    }

    async def teardown() -> dict[str, Any]:
        assert await runner.capture_intent("workspace_teardown") is None
        assert await runner.capture_intent("workspace_teardown", identity) == identity
        assert await runner.capture_intent("workspace_teardown") == identity
        with pytest.raises(CompletionEffectVersionError):
            await runner.capture_intent(
                "workspace_teardown",
                {**identity, "pod_uid": "replacement-pod"},
            )
        return {"actions": ["k8s workspace released"]}

    assert await runner.run(
        name="workspace_teardown",
        group="teardown",
        callback=teardown,
    ) == {"actions": ["k8s workspace released"]}
    effect = conn.effects[(COMMAND_ID, "workspace_teardown")]
    assert effect["detail"] == {
        "intent": identity,
        "output": {"actions": ["k8s workspace released"]},
    }


@pytest.mark.asyncio
async def test_effect_detail_truncates_only_allowlisted_diagnostics() -> None:
    conn = _StatefulConnection(_finalizing())
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
        effect_lease_seconds=30,
    )
    identity = {
        "kind": "kubernetes",
        "pod_uid": "pod-a",
        "pvc_uid": "pvc-a",
        "service_uid": "service-a",
    }
    huge = "🔥" * 5000
    original = {
        "released": False,
        "actions": [f"workspace cleanup failed: {huge}"],
        "error": huge,
    }

    async def teardown() -> dict[str, Any]:
        await runner.capture_intent("workspace_archive_teardown", identity)
        return original

    # The live caller sees its original diagnostic. Only the durable replay
    # representation is compacted.
    assert (
        await runner.run(
            name="workspace_archive_teardown",
            group="workspace_teardown",
            callback=teardown,
        )
        == original
    )

    detail = conn.effects[(COMMAND_ID, "workspace_archive_teardown")]["detail"]
    assert detail["intent"] == identity
    assert detail["output"]["released"] is False
    assert detail["output"]["error"] != huge
    assert detail["output"]["error"].endswith("…")
    assert detail["output"]["actions"][0].endswith("…")
    fields = detail["diagnostic_truncation"]["fields"]
    assert (
        fields["output.error"]["original_bytes"]
        > fields["output.error"]["stored_bytes"]
    )
    assert (
        fields["output.actions"]["original_bytes"]
        > fields["output.actions"]["stored_bytes"]
    )
    assert fields["output.actions"]["original_items"] == 1
    assert fields["output.actions"]["stored_items"] == 1
    assert len(json.dumps(detail, ensure_ascii=False).encode("utf-8")) <= (
        EFFECT_DETAIL_LIMIT_BYTES
    )

    conn.commands[COMMAND_ID].update(
        finalizing_by="owner-b", lease_expires_at=NOW + timedelta(minutes=2)
    )
    successor = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-b",
        effect_lease_seconds=30,
    )

    async def must_not_run() -> None:
        raise AssertionError("completed external effect was replayed")

    replay = await successor.run(
        name="workspace_archive_teardown",
        group="workspace_teardown",
        callback=must_not_run,
    )
    assert replay == detail["output"]
    assert replay["released"] is False


@pytest.mark.asyncio
async def test_effect_detail_never_truncates_replay_identity_or_intent() -> None:
    conn = _StatefulConnection(_finalizing())
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
        effect_lease_seconds=30,
    )

    async def oversized_identity() -> dict[str, Any]:
        return {"new_status": "completed", "identity": "x" * 9000}

    with pytest.raises(CompletionFinalizerError, match="replay-critical"):
        await runner.run(
            name="main_status_write",
            group="job_disposition",
            callback=oversized_identity,
        )

    conn.effects.pop((COMMAND_ID, "main_status_write"))

    async def oversized_intent() -> dict[str, Any]:
        await runner.capture_intent(
            "workspace_archive_teardown", {"pod_uid": "x" * 9000}
        )
        return {"actions": []}

    with pytest.raises(CompletionFinalizerError, match="correctness cap"):
        await runner.run(
            name="workspace_archive_teardown",
            group="workspace_teardown",
            callback=oversized_intent,
        )


@pytest.mark.asyncio
async def test_effect_capture_requires_live_prepared_effect_term() -> None:
    conn = _StatefulConnection(_finalizing())
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
    )

    with pytest.raises(CompletionLeaseLost):
        await runner.capture_intent("not_prepared", {"pod_uid": "pod-a"})


@pytest.mark.asyncio
async def test_stale_owner_cannot_complete_effect_or_command() -> None:
    conn = _StatefulConnection(_finalizing(owner="owner-b"))
    conn.effects[(COMMAND_ID, "notify")] = {
        "effect_name": "notify",
        "effect_group": "notifications",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": NOW + timedelta(seconds=30),
        "completed_at": None,
        "detail": {},
        "error_code": None,
    }
    stale = CompletionEffectRunner(
        conn, command=conn.commands[COMMAND_ID], owner="owner-a"
    )
    finalizer = CompletionFinalizer(conn)

    with pytest.raises(CompletionLeaseLost):
        await stale._complete("notify", {"sent": True})
    with pytest.raises(CompletionLeaseLost):
        await finalizer._finish(COMMAND_ID.hex, "owner-a", {"status": "handled"})

    assert conn.effects[(COMMAND_ID, "notify")]["state"] == "pending"
    assert conn.commands[COMMAND_ID]["state"] == "finalizing"


@pytest.mark.asyncio
async def test_effect_error_write_is_fenced_by_its_complete_by_deadline() -> None:
    conn = _StatefulConnection(_finalizing())
    conn.effects[(COMMAND_ID, "cloud_delivery")] = {
        "effect_name": "cloud_delivery",
        "effect_group": "delivery",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": NOW + timedelta(seconds=2),
        "completed_at": None,
        "detail": {},
        "error_code": None,
    }
    runner = CompletionEffectRunner(
        conn, command=conn.commands[COMMAND_ID], owner="owner-a"
    )
    conn.advance(seconds=3)

    await runner._record_error("cloud_delivery", RuntimeError("late"))

    assert conn.effects[(COMMAND_ID, "cloud_delivery")]["error_code"] is None
    error_sql = next(
        sql
        for operation, sql, _ in conn.calls
        if operation == "execute" and sql.startswith("update completion_effects")
    )
    assert "effect.complete_by > now()" in error_sql


@pytest.mark.asyncio
async def test_effect_deadline_is_strictly_shorter_than_actual_command_term() -> None:
    command = _finalizing()
    command["lease_expires_at"] = NOW + timedelta(seconds=5)
    conn = _StatefulConnection(command)
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
        effect_lease_seconds=30,
    )
    observed_complete_by: datetime | None = None

    async def quick_effect() -> str:
        nonlocal observed_complete_by
        observed_complete_by = conn.effects[(COMMAND_ID, "deadline_probe")][
            "complete_by"
        ]
        return "ok"

    await runner.run(name="deadline_probe", group="delivery", callback=quick_effect)

    assert observed_complete_by is not None
    assert NOW < observed_complete_by
    assert observed_complete_by < conn.commands[COMMAND_ID]["lease_expires_at"]
    insert_sql = next(
        sql
        for operation, sql, _ in conn.calls
        if operation == "fetchrow" and sql.startswith("insert into completion_effects")
    )
    assert "least(" in insert_sql
    assert "command.lease_expires_at" in insert_sql
    assert "extract(epoch from complete_by - now())" in insert_sql


@pytest.mark.asyncio
async def test_long_effect_refuses_insufficient_absolute_budget_before_callback() -> (
    None
):
    command = _finalizing()
    command["deadline_at"] = NOW + timedelta(seconds=895)
    conn = _StatefulConnection(command)
    runner = CompletionEffectRunner(
        conn, command=conn.commands[COMMAND_ID], owner="owner-a"
    )
    called = False

    async def terminal_snapshot() -> None:
        nonlocal called
        called = True

    with pytest.raises(CompletionLeaseLost, match="insufficient fenced budget"):
        await runner.run(
            name="workspace_teardown",
            group="teardown",
            callback=terminal_snapshot,
            effect_timeout_seconds=890,
            command_lease_seconds=900,
        )

    assert called is False


@pytest.mark.asyncio
async def test_background_retry_bucket_caps_burst_and_refills(monkeypatch) -> None:
    import orchestrator.services.completion_finalizer as module

    monotonic = 100.0
    monkeypatch.setattr(
        "orchestrator.services.completion_finalizer.time.monotonic",
        lambda: monotonic,
    )
    monkeypatch.setattr(module, "_RETRY_BUCKET_TOKENS", 10.0)
    monkeypatch.setattr(module, "_RETRY_BUCKET_UPDATED", monotonic)
    first = CompletionFinalizer(_StatefulConnection())
    second = CompletionFinalizer(_StatefulConnection())

    # The budget is shared across all finalizer instances in this process.
    assert [await first._take_retry_token() for _ in range(5)] == [True] * 5
    assert [await second._take_retry_token() for _ in range(5)] == [True] * 5
    assert await first._take_retry_token() is False

    monotonic += 0.5
    assert await second._take_retry_token() is False
    monotonic += 0.5
    assert await first._take_retry_token() is True
    assert await second._take_retry_token() is False


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_winner_and_live_term_is_not_stolen() -> None:
    conn = _StatefulConnection(_command())
    first = CompletionFinalizer(conn)
    second = CompletionFinalizer(conn)

    left, right = await asyncio.gather(
        first._claim(str(COMMAND_ID), inline=False),
        second._claim(str(COMMAND_ID), inline=False),
    )

    owners = [owner for _, owner in (left, right) if owner is not None]
    assert len(owners) == 1
    assert conn.commands[COMMAND_ID]["finalizing_by"] == owners[0]
    assert conn.commands[COMMAND_ID]["attempts"] == 1
    loser_command, loser_owner = right if left[1] is not None else left
    assert loser_owner is None
    assert loser_command is not None
    assert loser_command["state"] == "finalizing"


@pytest.mark.asyncio
async def test_expired_claim_resumes_but_live_effect_blocks_successor() -> None:
    expired = _command(
        state="finalizing",
        attempts=1,
        finalizing_by="dead-owner",
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    conn = _StatefulConnection(expired)
    finalizer = CompletionFinalizer(conn)

    resumed, owner = await finalizer._claim(str(COMMAND_ID), inline=False)
    assert owner is not None
    assert resumed is not None and resumed["attempts"] == 2

    # Even with an expired command claim, a predecessor still inside its
    # shorter effect ambiguity window prevents a second external call.
    conn.commands[COMMAND_ID].update(
        finalizing_by="dead-again",
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    conn.effects[(COMMAND_ID, "merge")] = {
        "effect_name": "merge",
        "effect_group": "delivery",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": NOW + timedelta(seconds=10),
        "completed_at": None,
        "detail": {},
        "error_code": None,
    }
    blocked, blocked_owner = await finalizer._claim(str(COMMAND_ID), inline=False)
    assert blocked_owner is None
    assert blocked is not None and blocked["finalizing_by"] == "dead-again"


@pytest.mark.asyncio
async def test_predecessor_and_run_after_gate_background_claim_only() -> None:
    predecessor = _command()
    successor = _command(SECOND_COMMAND_ID, report_seq=2)
    conn = _StatefulConnection(predecessor, successor)
    finalizer = CompletionFinalizer(conn)

    blocked, owner = await finalizer._claim(str(SECOND_COMMAND_ID), inline=False)
    assert owner is None
    assert blocked is not None and blocked["state"] == "pending"

    conn.commands[COMMAND_ID].update(
        state="done",
        outcome={"status": "handled"},
        finalized_at=NOW,
    )
    conn.commands[SECOND_COMMAND_ID]["run_after"] = NOW + timedelta(minutes=1)
    deferred, deferred_owner = await finalizer._claim(
        str(SECOND_COMMAND_ID), inline=False
    )
    assert deferred_owner is None
    assert deferred is not None and deferred["state"] == "pending"

    claimed, inline_owner = await finalizer._claim(str(SECOND_COMMAND_ID), inline=True)
    assert inline_owner is not None
    assert claimed is not None and claimed["state"] == "finalizing"


@pytest.mark.asyncio
async def test_retry_uses_bounded_jitter_and_cap_parks_with_alert() -> None:
    retry_conn = _StatefulConnection(_finalizing(attempts=2))
    retry = CompletionFinalizer(retry_conn, random_source=lambda: 0.5)

    released = await retry._retry_or_park(
        str(COMMAND_ID), "owner-a", RuntimeError("transient")
    )

    assert released.disposition == "retry"
    assert released.error_code == "RuntimeError"
    assert released.run_after == NOW + timedelta(seconds=11)
    assert retry_conn.commands[COMMAND_ID]["state"] == "pending"

    alerts: list[str] = []
    cap_conn = _StatefulConnection(_finalizing(attempts=5, max_attempts=5))
    capped = CompletionFinalizer(cap_conn, alert=alerts.append)
    parked = await capped._retry_or_park(
        str(COMMAND_ID), "owner-a", ValueError("permanent")
    )

    assert parked.disposition == "parked"
    assert parked.error_code == "ValueError"
    assert cap_conn.commands[COMMAND_ID]["state"] == "parked"
    assert alerts and "parked after ValueError" in alerts[0]


@pytest.mark.asyncio
async def test_version_mismatch_parks_and_alerts_without_running_workflow() -> None:
    conn = _StatefulConnection(_command(code_version="job-completion-v0"))
    alerts: list[str] = []
    workflow_called = False

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        nonlocal workflow_called
        workflow_called = True
        return {"status": "handled"}

    result = await CompletionFinalizer(
        conn, workflow=workflow, alert=alerts.append
    ).finalize_command(str(COMMAND_ID), inline=False)

    assert result.state == "parked"
    assert result.error_code == "code_version_mismatch"
    assert conn.commands[COMMAND_ID]["state"] == "parked"
    assert workflow_called is False
    assert alerts and "stored code version" in alerts[0]


@pytest.mark.asyncio
async def test_leader_renew_and_release_require_exact_elected_term() -> None:
    conn = _StatefulConnection()
    finalizer = CompletionFinalizer(conn, leader_id="pod-a")

    first = await finalizer.acquire_leader()
    assert first is not None
    assert await finalizer.renew_leader(first) is True

    conn.advance(seconds=121)
    second = await finalizer.acquire_leader()
    assert second is not None
    assert second.elected_at != first.elected_at

    assert await finalizer.renew_leader(first) is False
    assert await finalizer.release_leader(first) is False
    assert conn.leader is not None and conn.leader["elected_at"] == second.elected_at
    assert await finalizer.renew_leader(second) is True
    assert await finalizer.release_leader(second) is True

    renew_sql = next(
        sql
        for operation, sql, _ in conn.calls
        if operation == "fetchval"
        and sql.startswith("update completion_finalizer_leases")
    )
    release_sql = next(
        sql
        for operation, sql, _ in conn.calls
        if operation == "execute"
        and sql.startswith("delete from completion_finalizer_leases")
        and "leader_id" in sql
    )
    assert "leader_id = $2::text" in renew_sql
    assert "elected_at = $3::timestamptz" in renew_sql
    assert "leader_id = $2::text" in release_sql
    assert "elected_at = $3::timestamptz" in release_sql


@pytest.mark.asyncio
async def test_drain_retries_transient_leader_election_error(monkeypatch) -> None:
    shutdown = asyncio.Event()
    attempts = 0

    async def acquire_leader() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("database temporarily unavailable")
        shutdown.set()
        return None

    finalizer = CompletionFinalizer(object())
    monkeypatch.setattr(finalizer, "acquire_leader", acquire_leader)
    monkeypatch.setattr(
        "orchestrator.services.completion_finalizer.IDLE_POLL_SECONDS", 0.0
    )

    await asyncio.wait_for(finalizer.run_drain(shutdown), timeout=1.0)

    assert attempts == 2


@pytest.mark.asyncio
async def test_drain_retries_candidate_error_and_contains_release_error(
    monkeypatch,
) -> None:
    shutdown = asyncio.Event()
    term = LeaderTerm("pod-a", NOW)
    acquire_calls = 0
    candidate_calls = 0
    release_calls = 0

    async def acquire_leader() -> LeaderTerm:
        nonlocal acquire_calls
        acquire_calls += 1
        return term

    async def candidate_id() -> None:
        nonlocal candidate_calls
        candidate_calls += 1
        if candidate_calls == 1:
            raise ConnectionError("candidate scan interrupted")
        shutdown.set()
        return None

    async def release_leader(released_term: LeaderTerm) -> None:
        nonlocal release_calls
        release_calls += 1
        assert released_term == term
        raise ConnectionError("release interrupted")

    finalizer = CompletionFinalizer(object())
    monkeypatch.setattr(finalizer, "acquire_leader", acquire_leader)
    monkeypatch.setattr(finalizer, "_candidate_id", candidate_id)
    monkeypatch.setattr(finalizer, "release_leader", release_leader)
    monkeypatch.setattr(
        "orchestrator.services.completion_finalizer.IDLE_POLL_SECONDS", 0.0
    )

    await asyncio.wait_for(finalizer.run_drain(shutdown), timeout=1.0)

    assert acquire_calls == 1
    assert candidate_calls == 2
    assert release_calls == 1


@pytest.mark.asyncio
async def test_renewal_error_abandons_term_and_stops_its_drain(
    monkeypatch,
) -> None:
    shutdown = asyncio.Event()
    renewal_attempted = asyncio.Event()
    term = LeaderTerm("pod-a", NOW)
    acquire_calls = 0
    candidate_calls = 0
    release_calls = 0

    async def acquire_leader() -> LeaderTerm | None:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 1:
            return term
        shutdown.set()
        return None

    async def renew_leader(renewed_term: LeaderTerm) -> bool:
        assert renewed_term == term
        renewal_attempted.set()
        raise ConnectionError("renewal result unknown")

    async def candidate_id() -> None:
        nonlocal candidate_calls
        candidate_calls += 1
        await renewal_attempted.wait()
        return None

    async def release_leader(_released_term: LeaderTerm) -> bool:
        nonlocal release_calls
        release_calls += 1
        return True

    finalizer = CompletionFinalizer(object())
    monkeypatch.setattr(finalizer, "acquire_leader", acquire_leader)
    monkeypatch.setattr(finalizer, "renew_leader", renew_leader)
    monkeypatch.setattr(finalizer, "_candidate_id", candidate_id)
    monkeypatch.setattr(finalizer, "release_leader", release_leader)
    monkeypatch.setattr(
        "orchestrator.services.completion_finalizer.LEADER_HEARTBEAT_SECONDS", 0.0
    )
    monkeypatch.setattr(
        "orchestrator.services.completion_finalizer.IDLE_POLL_SECONDS", 0.0
    )

    await asyncio.wait_for(finalizer.run_drain(shutdown), timeout=1.0)

    assert acquire_calls == 2
    assert candidate_calls == 1
    assert release_calls == 0


@pytest.mark.asyncio
async def test_cancellation_leaves_exact_claim_for_expiry_resume() -> None:
    conn = _StatefulConnection(_command())
    entered = asyncio.Event()

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    finalizer = CompletionFinalizer(
        conn,
        workflow=workflow,
        heartbeat_seconds=30,
        command_lease_seconds=120,
    )
    task = asyncio.create_task(finalizer.finalize_command(str(COMMAND_ID), inline=True))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    command = conn.commands[COMMAND_ID]
    assert command["state"] == "finalizing"
    assert command["finalizing_by"] is not None
    assert command["lease_expires_at"] == NOW + timedelta(seconds=120)
    assert command["attempts"] == 1


@pytest.mark.asyncio
async def test_live_pending_effect_raises_in_flight_without_callback() -> None:
    conn = _StatefulConnection(_finalizing())
    conn.effects[(COMMAND_ID, "graft")] = {
        "effect_name": "graft",
        "effect_group": "delivery",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": NOW + timedelta(seconds=10),
        "completed_at": None,
        "detail": {},
        "error_code": None,
    }
    runner = CompletionEffectRunner(
        conn, command=conn.commands[COMMAND_ID], owner="owner-a"
    )
    called = False

    async def callback() -> None:
        nonlocal called
        called = True

    with pytest.raises(CompletionEffectInFlight):
        await runner.run(name="graft", group="delivery", callback=callback)
    assert called is False
