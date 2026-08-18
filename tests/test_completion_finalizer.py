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
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.completion_finalizer import (
    CompletionDispositionSuperseded,
    CompletionEffectInFlight,
    CompletionEffectRunner,
    CompletionEffectVersionError,
    CompletionFinalizer,
    CompletionFinalizerError,
    CompletionLeaseLost,
    CompletionTeardownSupersedeBlocked,
    EFFECT_DETAIL_LIMIT_BYTES,
    LeaderTerm,
)
from orchestrator.services.job_completion_commands import (
    COMPLETION_CODE_VERSION,
    COMPLETION_STATUS_REORDER_CODE_VERSION,
)


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
    status_reorder_enabled: bool = False,
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
        "accepted_job_status": "processing",
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
        "status_reorder_enabled": status_reorder_enabled,
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

    def __init__(
        self,
        *commands: dict[str, Any],
        job_status: str = "processing",
        job_context: dict[str, Any] | None = None,
    ) -> None:
        self.now = NOW
        self.job_status = job_status
        self.job_context = dict(job_context or {})
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

        if normalized.startswith("select status::text as status"):
            return {
                "status": self.job_status,
                "execution_lane": "pinned",
                "context": dict(self.job_context),
                "db_now_epoch": self.now.timestamp(),
            }

        if normalized.startswith("select min(effect.run_after) as run_after"):
            command_id, owner = args[:2]
            group = args[2] if len(args) == 3 else None
            command_id = UUID(str(command_id))
            if not self._command_term_live(command_id, owner):
                return None
            pending = [
                effect
                for (producer_id, _effect_name), effect in self.effects.items()
                if producer_id == command_id
                and (group is None or effect["effect_group"] == group)
                and effect["state"] == "pending"
            ]
            if not pending:
                return None
            return {
                "run_after": min(effect["run_after"] for effect in pending),
                "exhausted": any(
                    effect["attempts"] >= effect["max_attempts"] for effect in pending
                ),
            }

        if normalized.startswith(
            "select command.id, command.job_id, command.report_seq"
        ):
            command_id, job_id, owner = args
            command_id = UUID(str(command_id))
            command = self.commands.get(command_id)
            if (
                command is None
                or command["job_id"] != UUID(str(job_id))
                or not self._command_term_live(command_id, owner)
                or command["deadline_at"] <= self.now
            ):
                return None
            s1 = self.effects.get((command_id, "late_callback_guard"))
            predecessors = sorted(
                (
                    candidate
                    for candidate in self.commands.values()
                    if candidate["job_id"] == command["job_id"]
                    and candidate["report_seq"] < command["report_seq"]
                ),
                key=lambda row: row["report_seq"],
                reverse=True,
            )
            predecessor = predecessors[0] if predecessors else None
            return {
                "id": command["id"],
                "job_id": command["job_id"],
                "report_seq": command["report_seq"],
                "accepted_job_status": command.get("accepted_job_status"),
                "payload": command.get("payload"),
                "s1_state": s1.get("state") if s1 else None,
                "s1_detail": dict(s1.get("detail") or {}) if s1 else None,
                "predecessor_report_seq": (
                    predecessor["report_seq"] if predecessor else None
                ),
                "predecessor_state": (predecessor["state"] if predecessor else None),
                "predecessor_outcome": (
                    predecessor.get("outcome") if predecessor else None
                ),
            }

        if normalized.startswith("select command.id, disposition.state"):
            command_id, job_id, owner = args
            command_id = UUID(str(command_id))
            command = self.commands.get(command_id)
            if (
                command is None
                or command["job_id"] != UUID(str(job_id))
                or not self._command_term_live(command_id, owner)
                or command["deadline_at"] <= self.now
            ):
                return None
            disposition = self.effects.get((command_id, "main_status_write"))
            auto_deny = self.effects.get((command_id, "auto_deny_resume"))
            teardown = self.effects.get((command_id, "workspace_archive_teardown"))
            return {
                "id": command["id"],
                "disposition_state": (
                    disposition.get("state") if disposition else None
                ),
                "disposition_detail": (
                    dict(disposition.get("detail") or {}) if disposition else None
                ),
                "auto_deny_state": auto_deny.get("state") if auto_deny else None,
                "auto_deny_detail": (
                    dict(auto_deny.get("detail") or {}) if auto_deny else None
                ),
                "teardown_state": teardown.get("state") if teardown else None,
                "teardown_detail": (
                    dict(teardown.get("detail") or {}) if teardown else None
                ),
            }

        if normalized.startswith(
            "select id, job_id, report_seq, accepted_job_status, payload "
            "from job_completion_commands"
        ):
            command_id, job_id, owner = args
            command_id = UUID(str(command_id))
            command = self.commands.get(command_id)
            if (
                command is None
                or command["job_id"] != UUID(str(job_id))
                or not self._command_term_live(command_id, owner)
                or command["deadline_at"] <= self.now
            ):
                return None
            return {
                key: command.get(key)
                for key in (
                    "id",
                    "job_id",
                    "report_seq",
                    "accepted_job_status",
                    "payload",
                )
            }

        if normalized.startswith(
            "update job_completion_commands as command set state = 'parked'"
        ):
            (
                command_id,
                error_code,
                version_mismatch,
                code_versions,
                capability_mismatch,
                legacy_version,
                reorder_version,
            ) = args
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
            version_failed = command["code_version"] not in code_versions
            pair_failed = not (
                (
                    command["code_version"] == legacy_version
                    and not command["status_reorder_enabled"]
                )
                or (
                    command["code_version"] == reorder_version
                    and command["status_reorder_enabled"]
                )
            )
            eligible_reason = (
                version_mismatch
                and (version_failed or (capability_mismatch and pair_failed))
            ) or (not version_mismatch and exhausted)
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
            (
                command_id,
                owner,
                code_versions,
                lease_seconds,
                inline,
                legacy_version,
                reorder_version,
            ) = args
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
                    command["code_version"] in code_versions,
                    (
                        command["code_version"] == legacy_version
                        and not command["status_reorder_enabled"]
                    )
                    or (
                        command["code_version"] == reorder_version
                        and command["status_reorder_enabled"]
                    ),
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
                "run_after": self.now,
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

        if normalized.startswith(
            "update job_completion_commands set state = 'superseded'"
        ):
            command_id, owner, outcome_json, error_code = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            if (
                not self._command_term_live(command_id, owner)
                or command["deadline_at"] <= self.now
            ):
                return None
            command.update(
                state="superseded",
                outcome=json.loads(outcome_json),
                finalized_at=self.now,
                error_code=error_code,
                finalizing_by=None,
                lease_expires_at=None,
            )
            return dict(command)

        if normalized.startswith("update job_completion_commands set state = 'parked'"):
            if "completion_decision_authority_unresolved" in normalized:
                command_id, owner = args
                command_id = UUID(str(command_id))
                command = self.commands[command_id]
                if not self._command_term_live(command_id, owner):
                    return None
                command.update(
                    state="parked",
                    error_code="completion_decision_authority_unresolved",
                    finalizing_by=None,
                    lease_expires_at=None,
                )
                return dict(command)
            if "effect_group_attempts_exhausted" in normalized:
                command_id, owner = args
                command_id = UUID(str(command_id))
                command = self.commands[command_id]
                exhausted_effect = any(
                    producer_id == command_id
                    and effect["state"] == "pending"
                    and effect["attempts"] >= effect["max_attempts"]
                    for (producer_id, _), effect in self.effects.items()
                )
                if (
                    not self._command_term_live(command_id, owner)
                    or not exhausted_effect
                ):
                    return None
                command.update(
                    state="parked",
                    error_code="effect_group_attempts_exhausted",
                    finalizing_by=None,
                    lease_expires_at=None,
                )
                return dict(command)
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
            command_id, owner, third = args
            command_id = UUID(str(command_id))
            command = self.commands[command_id]
            if "attempts = greatest" in normalized:
                pending_effects = [
                    effect
                    for (producer_id, _), effect in self.effects.items()
                    if producer_id == command_id and effect["state"] == "pending"
                ]
                if (
                    not self._command_term_live(command_id, owner)
                    or command["deadline_at"] <= self.now
                    or not pending_effects
                    or any(
                        effect["attempts"] >= effect["max_attempts"]
                        for effect in pending_effects
                    )
                ):
                    return None
                command.update(
                    state="pending",
                    attempts=max(command["attempts"] - 1, 0),
                    run_after=third,
                    error_code=None,
                    finalizing_by=None,
                    lease_expires_at=None,
                )
                return dict(command)
            jitter = third
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

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        normalized = self._record("fetch", sql, args)
        if normalized.startswith(
            "select effect_name, state, detail from completion_effects"
        ):
            command_id = UUID(str(args[0]))
            return [
                {
                    "effect_name": effect_name,
                    "state": effect["state"],
                    "detail": dict(effect.get("detail") or {}),
                }
                for (producer_id, effect_name), effect in sorted(
                    self.effects.items(), key=lambda item: item[0][1]
                )
                if producer_id == command_id
                and effect["state"] not in {"done", "superseded"}
            ]
        raise AssertionError(f"unexpected fetch query: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        normalized = self._record("fetchval", sql, args)

        if normalized.startswith("select min(report_seq)"):
            job_id, report_seq = args
            higher = [
                int(command["report_seq"])
                for command in self.commands.values()
                if command["job_id"] == UUID(str(job_id))
                and int(command["report_seq"]) > int(report_seq)
                and command["state"] != "superseded"
            ]
            return min(higher) if higher else None

        if normalized.startswith("update jobs set context = jsonb_set"):
            (
                command_id,
                job_id,
                owner,
                version,
                source,
                expected_status,
                expected_lane,
                lease_seconds,
            ) = args
            command_id = UUID(str(command_id))
            if (
                UUID(str(job_id)) != JOB_ID
                or not self._command_term_live(command_id, owner)
                or self.job_status != expected_status
            ):
                return None
            self.job_context["_completion_control_claim"] = {
                "version": version,
                "claim_id": str(command_id),
                "source": source,
                "expected_status": expected_status,
                "expected_lane": expected_lane,
                "fence_kind": "completion_command",
                "fence_value": str(command_id),
                "expires_epoch": self.now.timestamp() + float(lease_seconds),
            }
            return 1

        if normalized.startswith("select 1 from job_completion_commands"):
            command_id, job_id, owner = args
            command_id = UUID(str(command_id))
            command = self.commands.get(command_id)
            return (
                1
                if command is not None
                and command["job_id"] == UUID(str(job_id))
                and self._command_term_live(command_id, owner)
                and command["deadline_at"] > self.now
                else None
            )

        if normalized.startswith("select complete_by > now()"):
            command_id, name = args
            effect = self.effects.get((UUID(str(command_id)), name))
            return bool(
                effect is not None
                and effect["complete_by"] is not None
                and effect["complete_by"] > self.now
            )

        if normalized.startswith("with completed_effect as"):
            command_id, owner, name, detail_json, base_lease_seconds, state = args
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
                state=state,
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

        if normalized.startswith(
            "update jobs set context = coalesce(context, '{}'::jsonb)"
        ):
            _job_id, expected_status, tool_call_id = args
            decision = self.job_context.get("completion_decision")
            if (
                self.job_status == expected_status
                and isinstance(decision, dict)
                and decision.get("tool_call_id") == tool_call_id
            ):
                self.job_context.pop("completion_decision", None)
                return "UPDATE 1"
            return "UPDATE 0"

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
async def test_superseded_effect_replays_but_is_not_done_authority() -> None:
    conn = _StatefulConnection(_finalizing())
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
        effect_lease_seconds=30,
    )

    async def lose_world_state_cas() -> dict[str, Any]:
        return {"won": False, "status": "pending_review"}

    output = await runner.run(
        name="world_state_cas",
        group="synthesizer",
        callback=lose_world_state_cas,
        supersede_if=lambda value: value["won"] is False,
    )

    assert output == {"won": False, "status": "pending_review"}
    assert conn.effects[(COMMAND_ID, "world_state_cas")]["state"] == "superseded"
    assert await runner.has_started("world_state_cas")
    assert not await runner.has_completed("world_state_cas")
    assert await runner.completed_detail("world_state_cas") is None
    assert await runner.terminal_detail("world_state_cas") == {
        "won": False,
        "status": "pending_review",
    }

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
        raise AssertionError("superseded effect callback was replayed")

    assert await successor.run(
        name="world_state_cas",
        group="synthesizer",
        callback=must_not_run,
        supersede_if=lambda value: value["won"] is False,
    ) == {"won": False, "status": "pending_review"}


@pytest.mark.asyncio
async def test_class_c_authority_guard_detects_post_s17_control_write() -> None:
    conn = _StatefulConnection(_finalizing(), job_status="cancelled")
    conn.effects[(COMMAND_ID, "main_status_write")] = {
        "effect_name": "main_status_write",
        "effect_group": "job_disposition",
        "state": "done",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": NOW,
        "detail": {"output": {"new_status": "completed"}},
        "error_code": None,
    }
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
    )

    with pytest.raises(CompletionDispositionSuperseded) as raised:
        await runner.assert_disposition_authority()

    assert raised.value.observed_status == "cancelled"
    assert raised.value.expected_statuses == ("completed",)


@pytest.mark.asyncio
async def test_class_c_authority_guard_is_vacuous_without_s17() -> None:
    conn = _StatefulConnection(_finalizing(), job_status="cancelled")
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
    )

    await runner.assert_disposition_authority()


@pytest.mark.asyncio
async def test_pre_s17_entry_authority_requires_exact_status_and_live_term() -> None:
    conn = _StatefulConnection(_finalizing(), job_status="processing")
    command = conn.commands[COMMAND_ID]
    command["resolved_entry_status"] = "processing"
    runner = CompletionEffectRunner(conn, command=command, owner="owner-a")

    await runner.assert_entry_authority()

    conn.job_status = "cancelled"
    with pytest.raises(CompletionDispositionSuperseded) as raised:
        await runner.assert_entry_authority()
    assert raised.value.observed_status == "cancelled"
    assert raised.value.expected_statuses == ("processing",)

    conn.job_status = "processing"
    conn.commands[COMMAND_ID]["lease_expires_at"] = NOW - timedelta(seconds=1)
    with pytest.raises(CompletionLeaseLost):
        await runner.assert_entry_authority()


@pytest.mark.asyncio
async def test_pre_s17_entry_authority_fails_closed_on_active_control_claim() -> None:
    conn = _StatefulConnection(
        _finalizing(),
        job_status="processing",
        job_context={
            "_completion_control_claim": {
                "version": 1,
                "claim_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "expires_epoch": NOW.timestamp() + 60,
            }
        },
    )
    command = conn.commands[COMMAND_ID]
    command["resolved_entry_status"] = "processing"
    runner = CompletionEffectRunner(conn, command=command, owner="owner-a")

    with pytest.raises(CompletionDispositionSuperseded) as raised:
        await runner.assert_entry_authority()

    assert raised.value.observed_status == "processing:control_claimed"
    assert raised.value.expected_statuses == ("processing",)


@pytest.mark.asyncio
async def test_delivery_control_installs_adopts_and_fences_expiry() -> None:
    conn = _StatefulConnection(_finalizing(), job_status="processing")
    command = conn.commands[COMMAND_ID]
    command["resolved_entry_status"] = "processing"
    runner = CompletionEffectRunner(conn, command=command, owner="owner-a")

    assert await runner.acquire_delivery_control("processing") == str(COMMAND_ID)
    marker = conn.job_context["_completion_control_claim"]
    assert marker["claim_id"] == str(COMMAND_ID)
    assert marker["source"] == "completion_delivery"
    await runner.assert_delivery_control("processing")

    # A successor term adopts the same stable command marker after a crash.
    conn.commands[COMMAND_ID].update(
        finalizing_by="owner-b", lease_expires_at=NOW + timedelta(minutes=2)
    )
    successor = CompletionEffectRunner(
        conn, command=conn.commands[COMMAND_ID], owner="owner-b"
    )
    assert await successor.acquire_delivery_control("processing") == str(COMMAND_ID)
    await successor.assert_delivery_control("processing")

    conn.job_context["_completion_control_claim"]["expires_epoch"] = NOW.timestamp() - 1
    with pytest.raises(CompletionDispositionSuperseded) as raised:
        await successor.assert_delivery_control("processing")
    assert raised.value.reason == "delivery_control_superseded"


@pytest.mark.asyncio
async def test_pending_group_query_is_exact_command_term_and_group() -> None:
    conn = _StatefulConnection(_finalizing())
    conn.effects[(COMMAND_ID, "graft")] = {
        "effect_name": "graft",
        "effect_group": "subjob_graft",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "run_after": NOW + timedelta(seconds=5),
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": None,
        "detail": {},
        "error_code": "retry",
    }
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
    )

    assert await runner.has_pending_group("subjob_graft")
    assert runner.requires_retry
    assert not await runner.has_pending_group("terminal_delivery")

    conn.commands[COMMAND_ID]["lease_expires_at"] = NOW - timedelta(seconds=1)
    # Once observed, the durable block remains the release signal even if the
    # exact term expires before CompletionFinalizer handles the workflow result.
    assert await runner.has_pending_group("subjob_graft")


@pytest.mark.asyncio
async def test_preexisting_pending_group_releases_command_end_to_end() -> None:
    conn = _StatefulConnection(_command())
    retry_at = NOW + timedelta(seconds=5)
    conn.effects[(COMMAND_ID, "graft")] = {
        "effect_name": "graft",
        "effect_group": "subjob_graft",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "run_after": retry_at,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": None,
        "detail": {"output": {"status": "retry"}},
        "error_code": "retry",
    }

    async def workflow(runner: CompletionEffectRunner) -> dict[str, Any]:
        assert await runner.has_pending_group("subjob_graft")
        return {"status": "handled", "new_status": "processing"}

    result = await CompletionFinalizer(
        conn,
        workflow=workflow,
    ).finalize_command(str(COMMAND_ID))

    assert result.disposition == "effects_pending"
    assert result.state == "pending"
    assert result.run_after == retry_at
    command = conn.commands[COMMAND_ID]
    assert command["state"] == "pending"
    assert command["attempts"] == 0
    assert command["finalizing_by"] is None
    assert command["lease_expires_at"] is None


@pytest.mark.asyncio
async def test_class_c_authority_accepts_completed_s23_owned_pause() -> None:
    conn = _StatefulConnection(_finalizing(), job_status="paused")
    conn.effects[(COMMAND_ID, "main_status_write")] = {
        "effect_name": "main_status_write",
        "effect_group": "job_disposition",
        "state": "done",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": NOW,
        "detail": {"output": {"new_status": "pending_review"}},
        "error_code": None,
    }
    conn.effects[(COMMAND_ID, "auto_deny_resume")] = {
        "effect_name": "auto_deny_resume",
        "effect_group": "auto_deny_resume",
        "state": "done",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": NOW,
        "detail": {"output": {"auto_denied": True}},
        "error_code": None,
    }
    runner = CompletionEffectRunner(
        conn,
        command=conn.commands[COMMAND_ID],
        owner="owner-a",
    )

    await runner.assert_disposition_authority()


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
async def test_entry_status_race_supersedes_whole_command_before_workflow() -> None:
    conn = _StatefulConnection(_command(), job_status="cancelled")
    workflow_called = False

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        nonlocal workflow_called
        workflow_called = True
        return {"new_status": "completed"}

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(COMMAND_ID)
    )

    assert result.disposition == "superseded"
    assert result.state == "superseded"
    assert result.error_code == "entry_status_superseded"
    assert workflow_called is False
    assert result.outcome == {
        "status": "superseded",
        "job_id": str(JOB_ID),
        "report_seq": 1,
        "reason": "entry_status_superseded",
        "accepted_job_status": "processing",
        "expected_entry_statuses": ["processing"],
        "observed_status": "cancelled",
        "winning_report_seq": None,
        "completion_decision_disposition": "not_applicable",
        "abandoned_effects": [],
    }
    command = conn.commands[COMMAND_ID]
    assert command["state"] == "superseded"
    assert command["outcome"] == result.outcome
    assert command["finalized_at"] == NOW
    assert command["error_code"] == "entry_status_superseded"
    assert command["finalizing_by"] is None
    assert command["lease_expires_at"] is None


@pytest.mark.asyncio
async def test_unproven_legacy_null_entry_status_supersedes_fail_closed() -> None:
    legacy = _command()
    legacy["accepted_job_status"] = None
    conn = _StatefulConnection(legacy)

    async def must_not_run(_runner: CompletionEffectRunner) -> dict[str, Any]:
        raise AssertionError("legacy NULL command guessed current jobs.status")

    result = await CompletionFinalizer(conn, workflow=must_not_run).finalize_command(
        str(COMMAND_ID)
    )

    assert result.disposition == "superseded"
    assert result.outcome is not None
    assert result.outcome["accepted_job_status"] is None
    assert result.outcome["expected_entry_statuses"] == []
    assert result.outcome["observed_status"] == "processing"


@pytest.mark.asyncio
async def test_newest_supersede_voids_only_its_exact_live_decision() -> None:
    command = _command()
    command["payload"] = {
        "should_stop": True,
        "_accepted_completion_decision": {"tool_call_id": "accepted-tool"},
    }
    conn = _StatefulConnection(
        command,
        job_status="paused",
        job_context={
            "completion_decision": {
                "tool_call_id": "accepted-tool",
                "summary": "durable completion",
            }
        },
    )

    result = await CompletionFinalizer(conn).finalize_command(str(COMMAND_ID))

    assert result.disposition == "superseded"
    assert result.outcome is not None
    assert (
        result.outcome["completion_decision_disposition"] == "voided_exact_acceptance"
    )
    assert "completion_decision" not in conn.job_context


@pytest.mark.asyncio
async def test_newest_supersede_parks_when_live_decision_identity_is_unproven() -> None:
    conn = _StatefulConnection(
        _command(),
        job_status="paused",
        job_context={
            "completion_decision": {
                "tool_call_id": "newer-unproven-tool",
                "summary": "must not be discarded",
            }
        },
    )

    result = await CompletionFinalizer(conn).finalize_command(str(COMMAND_ID))

    assert result.disposition == "parked"
    assert result.error_code == "completion_decision_authority_unresolved"
    assert conn.commands[COMMAND_ID]["state"] == "parked"
    assert conn.job_context["completion_decision"]["tool_call_id"] == (
        "newer-unproven-tool"
    )


@pytest.mark.asyncio
async def test_successor_adopts_immediate_done_predecessor_status_in_order() -> None:
    predecessor = _command(state="done")
    predecessor.update(
        outcome={"status": "handled", "new_status": "completed"},
        finalized_at=NOW,
    )
    successor = _command(SECOND_COMMAND_ID, report_seq=2)
    conn = _StatefulConnection(predecessor, successor, job_status="completed")
    observed_entry: str | None = None

    async def workflow(runner: CompletionEffectRunner) -> dict[str, Any]:
        nonlocal observed_entry
        observed_entry = str(runner.command["resolved_entry_status"])
        return {"status": "handled", "new_status": "completed"}

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(SECOND_COMMAND_ID)
    )

    assert result.disposition == "done"
    assert observed_entry == "completed"
    assert conn.commands[SECOND_COMMAND_ID]["state"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor_state", ["superseded", "force_resolved"])
async def test_successor_uses_own_unchanged_accept_snapshot_after_predecessor(
    predecessor_state: str,
) -> None:
    predecessor = _command(state=predecessor_state)
    predecessor.update(
        outcome={"status": predecessor_state, "new_status": "completed"},
        finalized_at=NOW,
        error_code=(
            "entry_status_superseded"
            if predecessor_state == "superseded"
            else "forced_by_operator"
        ),
    )
    successor = _command(SECOND_COMMAND_ID, report_seq=2)
    # The predecessor is not sequential authority, but admission's own locked
    # snapshot is: the jobs row has not moved since this successor was accepted.
    conn = _StatefulConnection(predecessor, successor, job_status="processing")
    workflow_called = False

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        nonlocal workflow_called
        workflow_called = True
        return {"new_status": "completed"}

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(SECOND_COMMAND_ID)
    )

    assert result.disposition == "done"
    assert workflow_called is True


@pytest.mark.asyncio
async def test_feedback_round_uses_own_accept_snapshot_not_completed_predecessor() -> (
    None
):
    predecessor = _command(state="done")
    predecessor.update(
        outcome={"status": "handled", "new_status": "reviewing"},
        finalized_at=NOW,
    )
    successor = _command(SECOND_COMMAND_ID, report_seq=2)
    successor["payload"] = {
        "should_stop": True,
        "_accepted_completion_decision": {"tool_call_id": "round-2-tool"},
    }
    conn = _StatefulConnection(
        predecessor,
        successor,
        job_status="processing",
        job_context={
            "completion_decision": {
                "tool_call_id": "round-2-tool",
                "summary": "round 2 complete",
            }
        },
    )

    async def workflow(runner: CompletionEffectRunner) -> dict[str, Any]:
        assert runner.command["resolved_entry_status"] == "processing"
        conn.job_status = "completed"
        conn.job_context.pop("completion_decision", None)
        return {"status": "handled", "new_status": "completed"}

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(SECOND_COMMAND_ID)
    )

    assert result.disposition == "done"
    assert conn.job_status == "completed"
    assert "completion_decision" not in conn.job_context


@pytest.mark.asyncio
async def test_completed_s1_proof_keeps_same_command_retry_resumable() -> None:
    conn = _StatefulConnection(_command(), job_status="paused")
    conn.effects[(COMMAND_ID, "late_callback_guard")] = {
        "effect_name": "late_callback_guard",
        "effect_group": "entry",
        "state": "done",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": NOW,
        "detail": {"output": {"entry_status": "processing", "matched": False}},
        "error_code": None,
    }

    async def workflow(runner: CompletionEffectRunner) -> dict[str, Any]:
        assert runner.command["resolved_entry_status"] == "processing"
        return {"status": "handled", "new_status": "paused"}

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(COMMAND_ID)
    )

    assert result.disposition == "done"
    assert result.outcome == {"status": "handled", "new_status": "paused"}


@pytest.mark.asyncio
async def test_typed_workflow_race_supersedes_with_abandoned_effects_not_retry() -> (
    None
):
    conn = _StatefulConnection(_command())
    conn.effects[(COMMAND_ID, "workspace_archive_teardown")] = {
        "effect_name": "workspace_archive_teardown",
        "effect_group": "workspace_teardown",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": None,
        "detail": {},
        "error_code": None,
    }

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        conn.job_status = "cancelled"
        raise CompletionDispositionSuperseded(
            observed_status="cancelled",
            expected_statuses=("processing",),
        )

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(COMMAND_ID)
    )

    assert result.disposition == "superseded"
    assert result.outcome is not None
    assert result.outcome["observed_status"] == "cancelled"
    assert result.outcome["abandoned_effects"] == ["workspace_archive_teardown"]
    assert conn.commands[COMMAND_ID]["attempts"] == 1
    assert conn.commands[COMMAND_ID]["state"] == "superseded"


@pytest.mark.asyncio
async def test_whole_command_supersede_cannot_abandon_authorized_s36() -> None:
    conn = _StatefulConnection(_command())
    conn.effects[(COMMAND_ID, "workspace_archive_teardown")] = {
        "effect_name": "workspace_archive_teardown",
        "effect_group": "workspace_teardown",
        "state": "pending",
        "attempts": 1,
        "max_attempts": 5,
        "intent_at": NOW,
        "complete_by": None,
        "completed_at": None,
        "detail": {"teardown_authorization": {"active": True, "report_seq": 1}},
        "error_code": None,
    }

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        conn.job_status = "cancelled"
        raise CompletionDispositionSuperseded(
            observed_status="cancelled",
            expected_statuses=("processing",),
        )

    result = await CompletionFinalizer(conn, workflow=workflow).finalize_command(
        str(COMMAND_ID)
    )

    assert result.disposition == "retry"
    assert result.error_code == CompletionTeardownSupersedeBlocked.__name__
    assert conn.commands[COMMAND_ID]["state"] == "pending"
    assert conn.effects[(COMMAND_ID, "workspace_archive_teardown")]["state"] == (
        "pending"
    )


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
async def test_new_default_accepts_reorder_v2_while_explicit_old_v1_parks_it() -> None:
    reordered = _command(
        code_version=COMPLETION_STATUS_REORDER_CODE_VERSION,
        status_reorder_enabled=True,
    )
    new_conn = _StatefulConnection(reordered)

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        return {"status": "handled", "new_status": "completed"}

    accepted = await CompletionFinalizer(
        new_conn,
        workflow=workflow,
    ).finalize_command(str(COMMAND_ID))
    assert accepted.disposition == "done"

    old_conn = _StatefulConnection(reordered)
    refused = await CompletionFinalizer(
        old_conn,
        workflow=workflow,
        code_version=COMPLETION_CODE_VERSION,
    ).finalize_command(str(COMMAND_ID), inline=False)
    assert refused.state == "parked"
    assert refused.error_code == "code_version_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code_version", "status_reorder_enabled"),
    [
        (COMPLETION_CODE_VERSION, True),
        (COMPLETION_STATUS_REORDER_CODE_VERSION, False),
    ],
)
async def test_malformed_persisted_version_capability_pair_parks(
    code_version: str,
    status_reorder_enabled: bool,
) -> None:
    conn = _StatefulConnection(
        _command(
            code_version=code_version,
            status_reorder_enabled=status_reorder_enabled,
        )
    )

    async def must_not_run(_runner: CompletionEffectRunner) -> dict[str, Any]:
        raise AssertionError("malformed capability pair reached the workflow")

    result = await CompletionFinalizer(
        conn,
        workflow=must_not_run,
    ).finalize_command(str(COMMAND_ID), inline=False)

    assert result.state == "parked"
    assert result.error_code == "code_version_mismatch"


@pytest.mark.asyncio
async def test_preclaim_supersede_refetches_terminal_without_claiming() -> None:
    conn = _StatefulConnection(_command())
    workflow_called = False

    async def preclaim(command_id: str) -> SimpleNamespace:
        assert command_id == str(COMMAND_ID)
        command = conn.commands[COMMAND_ID]
        command.update(
            state="superseded",
            outcome={"status": "superseded", "reason": "safety_net"},
            error_code="safety_net_superseded",
            finalized_at=NOW,
        )
        return SimpleNamespace(disposition="superseded")

    async def workflow(_runner: CompletionEffectRunner) -> dict[str, Any]:
        nonlocal workflow_called
        workflow_called = True
        return {"status": "handled"}

    result = await CompletionFinalizer(
        conn,
        workflow=workflow,
        preclaim=preclaim,
    ).finalize_command(str(COMMAND_ID))

    assert result.state == "superseded"
    assert result.disposition == "terminal"
    assert result.error_code == "safety_net_superseded"
    assert workflow_called is False
    assert conn.commands[COMMAND_ID]["attempts"] == 0


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
