"""Linearization helpers for durable completion workspace teardown.

S36 is an external effect, so its Kubernetes/VM/Docker work cannot run while a
Postgres transaction holds the job row.  These helpers split that operation at
the only safe boundary:

* an exact finalizer attempt first installs a durable authorization marker
  while holding ``jobs FOR UPDATE``;
* fresh report admission takes the same jobs-row lock and refuses while that
  marker is active;
* if admission committed first, the older report observes the higher
  ``completion_seq_hwm`` and durably defers teardown to the later report.

The active marker intentionally has no lease, deadline, command-state, or
``complete_by`` predicate.  Once an external callback has been authorized, a
stopped process may resume after every one of those clocks expires.  Only
settling the S36 effect row clears the admission barrier.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping
from uuid import UUID


WORKSPACE_TEARDOWN_EFFECT = "workspace_archive_teardown"
_AUTHORIZATION_KEY = "teardown_authorization"


class CompletionTeardownAuthorityLost(RuntimeError):
    """The exact finalizer/effect attempt cannot authorize teardown."""


@dataclass(frozen=True, slots=True)
class ActiveTeardownAuthorization:
    """One durable S36 admission barrier."""

    command_id: str
    report_seq: int
    marker_report_seq: int | None


@dataclass(frozen=True, slots=True)
class TeardownAuthorizationDecision:
    """Whether this report may perform S36's external archive/teardown work."""

    authorized: bool
    command_id: str
    report_seq: int
    completion_seq_hwm: int
    disposition: str
    observed_status: str
    expected_status: str | None

    @property
    def higher_report_seq(self) -> int | None:
        return self.completion_seq_hwm if self.disposition == "deferred" else None

    @property
    def superseded(self) -> bool:
        return self.disposition == "world_state_superseded"

    @property
    def operator_hold(self) -> bool:
        return self.disposition == "operator_hold"


@dataclass(frozen=True, slots=True)
class TeardownHandoff:
    """S36 disposition carried by the immediate predecessor command."""

    required: bool
    source_command_id: str | None = None
    source_report_seq: int | None = None
    disposition: str | None = None


@asynccontextmanager
async def _connection(source: Any) -> AsyncIterator[Any]:
    acquire = getattr(source, "acquire", None)
    if acquire is None:
        yield source
        return
    async with acquire() as conn:
        yield conn


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def active_workspace_teardown_authorization(
    conn: Any,
    *,
    job_id: str,
) -> ActiveTeardownAuthorization | None:
    """Return an active S36 marker while admission holds the jobs-row lock.

    Fresh admission must call this only *after* the lane fence and exact-key
    replay checks, and before allocating a report sequence.  The caller already
    owns ``jobs FOR UPDATE`` in the binding queue-then-jobs lock order.  No row
    here is locked: the shared jobs lock is the linearization point with
    :func:`authorize_workspace_teardown`.

    Only the effect's ``pending`` state matters.  In particular, an expired or
    parked command still blocks fresh admission while its previously
    authorized external callback may exist.
    """

    row = await conn.fetchrow(
        """
        SELECT command.id AS command_id,
               command.report_seq,
               effect.detail #>>
                   '{teardown_authorization,report_seq}' AS marker_report_seq
        FROM completion_effects AS effect
        JOIN job_completion_commands AS command
          ON command.id = effect.producer_id
        WHERE effect.producer_kind = 'job_completion'
          AND command.job_id = $1::uuid
          AND effect.effect_name = $2::text
          AND effect.state = 'pending'
          AND effect.detail @> $3::jsonb
        ORDER BY command.report_seq DESC, command.id DESC
        LIMIT 1
        """,
        UUID(str(job_id)),
        WORKSPACE_TEARDOWN_EFFECT,
        json.dumps({_AUTHORIZATION_KEY: {"active": True}}),
    )
    if row is None:
        return None
    return ActiveTeardownAuthorization(
        command_id=str(row["command_id"]),
        report_seq=int(row["report_seq"]),
        marker_report_seq=_optional_int(row["marker_report_seq"]),
    )


async def authorize_workspace_teardown(
    db: Any,
    *,
    job_id: str,
    command_id: str,
    owner: str,
) -> TeardownAuthorizationDecision:
    """Authorize this exact S36 attempt or defer it to a higher report.

    This must be called from inside S36's already-prepared callback.  The
    jobs-row lock orders it against fresh admission.  A higher committed HWM
    wins by making this attempt a no-I/O deferred effect; otherwise the durable
    active marker commits before the callback may archive or destroy anything.
    """

    job_uuid = UUID(str(job_id))
    command_uuid = UUID(str(command_id))
    async with _connection(db) as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                """
                SELECT completion_seq_hwm, status::text AS status
                FROM jobs
                WHERE id = $1::uuid
                FOR UPDATE
                """,
                job_uuid,
            )
            if job is None:
                raise CompletionTeardownAuthorityLost(
                    f"completion teardown job {job_uuid} no longer exists"
                )

            # Lock only the effect row. CompletionEffectRunner._complete writes
            # effect -> command in one CTE, so taking command -> effect here
            # would create a deadlock edge. The marker UPDATE below re-checks
            # the exact live command term in its EXISTS predicate; a takeover
            # between this read and that write therefore fails closed.
            row = await conn.fetchrow(
                """
                SELECT command.report_seq, effect.detail,
                       disposition.detail #>>
                           '{output,new_status}' AS disposition_status,
                       CASE
                           WHEN entry.detail #>> '{output,matched}' = 'true'
                           THEN entry.detail #>> '{output,entry_status}'
                       END AS late_entry_status
                FROM job_completion_commands AS command
                JOIN completion_effects AS effect
                  ON effect.producer_kind = 'job_completion'
                 AND effect.producer_id = command.id
                 AND effect.effect_name = $4::text
                LEFT JOIN completion_effects AS disposition
                  ON disposition.producer_kind = 'job_completion'
                 AND disposition.producer_id = command.id
                 AND disposition.effect_name = 'main_status_write'
                 AND disposition.state = 'done'
                LEFT JOIN completion_effects AS entry
                  ON entry.producer_kind = 'job_completion'
                 AND entry.producer_id = command.id
                 AND entry.effect_name = 'late_callback_guard'
                 AND entry.state = 'done'
                WHERE command.id = $2::uuid
                  AND command.job_id = $1::uuid
                  AND command.state = 'finalizing'
                  AND command.finalizing_by = $3::text
                  AND command.lease_expires_at > now()
                  AND command.deadline_at > now()
                  AND effect.state = 'pending'
                  AND effect.complete_by > now()
                FOR UPDATE OF effect
                """,
                job_uuid,
                command_uuid,
                owner,
                WORKSPACE_TEARDOWN_EFFECT,
            )
            if row is None:
                raise CompletionTeardownAuthorityLost(
                    f"completion teardown {command_uuid} lost its exact effect term"
                )

            report_seq = int(row["report_seq"])
            completion_seq_hwm = int(job["completion_seq_hwm"] or 0)
            observed_status = str(job["status"] or "")
            expected_status = (
                str(row["disposition_status"] or "").strip()
                or str(row["late_entry_status"] or "").strip()
            )
            expected_status = expected_status or None
            detail = _json_object(row["detail"])
            marker = detail.get(_AUTHORIZATION_KEY)
            marker_active = bool(
                isinstance(marker, Mapping) and marker.get("active") is True
            )
            if completion_seq_hwm < report_seq:
                raise CompletionTeardownAuthorityLost(
                    "completion teardown report sequence exceeds the jobs HWM"
                )
            if completion_seq_hwm > report_seq:
                if marker_active:
                    return TeardownAuthorizationDecision(
                        authorized=False,
                        command_id=str(command_uuid),
                        report_seq=report_seq,
                        completion_seq_hwm=completion_seq_hwm,
                        disposition="operator_hold",
                        observed_status=observed_status,
                        expected_status=expected_status,
                    )
                return TeardownAuthorizationDecision(
                    authorized=False,
                    command_id=str(command_uuid),
                    report_seq=report_seq,
                    completion_seq_hwm=completion_seq_hwm,
                    disposition="deferred",
                    observed_status=observed_status,
                    expected_status=expected_status,
                )

            if expected_status is None:
                return TeardownAuthorizationDecision(
                    authorized=False,
                    command_id=str(command_uuid),
                    report_seq=report_seq,
                    completion_seq_hwm=completion_seq_hwm,
                    disposition="operator_hold",
                    observed_status=observed_status,
                    expected_status=None,
                )
            if observed_status != expected_status:
                return TeardownAuthorizationDecision(
                    authorized=False,
                    command_id=str(command_uuid),
                    report_seq=report_seq,
                    completion_seq_hwm=completion_seq_hwm,
                    disposition=(
                        "operator_hold" if marker_active else "world_state_superseded"
                    ),
                    observed_status=observed_status,
                    expected_status=expected_status,
                )

            detail[_AUTHORIZATION_KEY] = {
                "active": True,
                "report_seq": report_seq,
            }
            updated = await conn.fetchval(
                """
                UPDATE completion_effects AS effect
                SET detail = $5::jsonb
                WHERE effect.producer_kind = 'job_completion'
                  AND effect.producer_id = $2::uuid
                  AND effect.effect_name = $4::text
                  AND effect.state = 'pending'
                  AND effect.complete_by > now()
                  AND EXISTS (
                      SELECT 1
                      FROM job_completion_commands AS command
                      WHERE command.id = $2::uuid
                        AND command.job_id = $1::uuid
                        AND command.state = 'finalizing'
                        AND command.finalizing_by = $3::text
                        AND command.lease_expires_at > now()
                        AND command.deadline_at > now()
                  )
                RETURNING 1
                """,
                job_uuid,
                command_uuid,
                owner,
                WORKSPACE_TEARDOWN_EFFECT,
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
            )
            if updated is None:
                raise CompletionTeardownAuthorityLost(
                    f"completion teardown {command_uuid} lost its authorization term"
                )

            return TeardownAuthorizationDecision(
                authorized=True,
                command_id=str(command_uuid),
                report_seq=report_seq,
                completion_seq_hwm=completion_seq_hwm,
                disposition="authorized",
                observed_status=observed_status,
                expected_status=expected_status,
            )


async def workspace_teardown_handoff(
    db: Any,
    *,
    job_id: str,
    before_report_seq: int,
) -> TeardownHandoff:
    """Read the immediate predecessor command's settled S36 disposition.

    Skipping a predecessor with no S36 and rediscovering an older deferred row
    is unsafe: that intervening command may have retained the workspace and
    allowed a new execution generation. Handoffs therefore move one report at
    a time. A missing/non-deferred immediate predecessor closes the old chain;
    every genuinely terminal later command still runs its own normal S36.
    """

    async with _connection(db) as conn:
        row = await conn.fetchrow(
            """
            WITH predecessor AS (
                SELECT command.id, command.report_seq
                FROM job_completion_commands AS command
                WHERE command.job_id = $1::uuid
                  AND command.report_seq < $2::bigint
                ORDER BY command.report_seq DESC, command.id DESC
                LIMIT 1
            )
            SELECT predecessor.id AS command_id, predecessor.report_seq,
                   effect.detail #>>
                       '{output,teardown_disposition}' AS disposition
            FROM predecessor
            LEFT JOIN completion_effects AS effect
              ON effect.producer_kind = 'job_completion'
             AND effect.producer_id = predecessor.id
             AND effect.effect_name = $3::text
             AND effect.state = 'done'
            """,
            UUID(str(job_id)),
            int(before_report_seq),
            WORKSPACE_TEARDOWN_EFFECT,
        )
    if row is None:
        return TeardownHandoff(required=False)
    disposition = str(row["disposition"]) if row["disposition"] is not None else None
    return TeardownHandoff(
        required=disposition == "deferred",
        source_command_id=str(row["command_id"]),
        source_report_seq=int(row["report_seq"]),
        disposition=disposition,
    )


__all__ = [
    "ActiveTeardownAuthorization",
    "CompletionTeardownAuthorityLost",
    "TeardownAuthorizationDecision",
    "TeardownHandoff",
    "WORKSPACE_TEARDOWN_EFFECT",
    "active_workspace_teardown_authorization",
    "authorize_workspace_teardown",
    "workspace_teardown_handoff",
]
