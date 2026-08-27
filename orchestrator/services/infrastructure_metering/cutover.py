"""Crash-resumable legacy workspace -> typed Pod metering cutover.

An explicit, feature-gated fleet-admin operation calls
:meth:`InfrastructureWorkspaceCutover.prepare` with an actor, reason, and UUID
idempotency key.  Merely enabling collection, shadow mode, v2 reads, or
publication can never choose the irreversible barrier.

The app transaction fixes one database-clock barrier, closes every legacy open,
and splits every exactly owner-matched shadow Pod interval at that same instant.
Subsequent bounded passes freeze and strictly deliver the two legacy audit rows
for every interval at or before the barrier.  Cross-database work is performed
through a narrow injected protocol; no app lock is held during audit I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

import asyncpg

from .types import decimal_text

_UTC = timezone.utc
_GIB = 1024**3
_REASON_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_SCHEMA_VERSION = 1
_DEFAULT_SCOPE_FRESHNESS = timedelta(minutes=15)
_LEGACY_EVENT_FIELDS = frozenset(
    {
        "ts",
        "user_id",
        "project_id",
        "ref_kind",
        "ref_id",
        "category",
        "resource",
        "quantity",
        "unit",
        "rate_usd",
        "cost_usd",
        "source",
        "source_id",
        "details",
    }
)


class CutoverError(RuntimeError):
    """Base class for durable workspace cutover failures."""


class CutoverFenceError(CutoverError):
    """The caller does not own the current metering generation."""


class CutoverConflictError(CutoverError):
    """Persisted cutover intent conflicts with the requested operation."""


class CutoverBlocked(CutoverError):
    """A fail-closed readiness or legacy integrity condition is unresolved."""


class CutoverContractError(CutoverError, ValueError):
    """A caller, database row, or injected ledger violates the contract."""


class LegacyWorkspaceLedgerError(RuntimeError):
    """The injected strict legacy ledger could not freeze/deliver a batch."""


class LegacyWorkspaceLedgerConflict(LegacyWorkspaceLedgerError):
    """An existing legacy audit key has a different immutable payload."""


class CutoverPhase(StrEnum):
    DISABLED = "disabled"
    LEGACY_DRAINING = "legacy-draining"
    READY_TO_ACTIVATE = "ready-to-activate"
    ACTIVE = "active"


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CutoverContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(_UTC)


def _timestamp_text(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _uuid(value: Any, field_name: str, *, nullable: bool = False) -> UUID | None:
    if value is None and nullable:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CutoverContractError(f"{field_name} must be a UUID") from exc


def _canonical_hash(domain: str, value: Any) -> str:
    payload = json.dumps(
        [domain, _PAYLOAD_SCHEMA_VERSION, value],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def legacy_workspace_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the versioned hash used by the legacy cutover audit adapter."""

    if set(payload) != _LEGACY_EVENT_FIELDS:
        raise CutoverContractError("legacy frozen event fields differ from contract")
    return _canonical_hash(
        "srw-legacy-workspace-cutover-event",
        [[field, payload[field]] for field in sorted(_LEGACY_EVENT_FIELDS)],
    )


def _event_set_hash(events: Sequence["FrozenLegacyWorkspaceEvent"]) -> str:
    rows = sorted(
        [
            [
                event.payload["source"],
                event.payload["source_id"],
                event.payload["unit"],
                event.payload["ts"],
                event.row_hash,
            ]
            for event in events
        ]
    )
    return _canonical_hash("srw-legacy-workspace-cutover-event-set", rows)


def _legacy_quantities(
    *,
    started_at: datetime,
    ended_at: datetime,
    cpu_millicores: int,
    memory_bytes: int,
) -> dict[str, Decimal]:
    # Preserve the existing workspace_metering.py float->str->numeric contract.
    # The cutover adapter verifies/repairs those historical rows; silently
    # claiming more precision here would conflict with valid pre-cutover data.
    duration_hours = max(0.0, (ended_at - started_at).total_seconds() / 3600.0)
    return {
        "vcpu-hour": Decimal(str((cpu_millicores / 1000.0) * duration_hours)),
        "gib-hour": Decimal(str((memory_bytes / _GIB) * duration_hours)),
    }


@dataclass(frozen=True, slots=True)
class LegacyWorkspaceFreezeRequest:
    workspace_interval_id: int
    owner_kind: str
    owner_id: UUID
    tier: str | None
    cpu_millicores: int
    memory_bytes: int
    started_at: datetime
    ended_at: datetime
    user_id: UUID | None
    project_id: UUID | None

    def __post_init__(self) -> None:
        if self.workspace_interval_id <= 0:
            raise CutoverContractError("workspace interval id must be positive")
        if self.owner_kind not in {"job", "session"}:
            raise CutoverContractError("legacy workspace owner kind is invalid")
        if self.cpu_millicores < 0 or self.memory_bytes < 0:
            raise CutoverContractError("legacy workspace capacity cannot be negative")
        started = _aware_utc(self.started_at, "started_at")
        ended = _aware_utc(self.ended_at, "ended_at")
        if ended < started:
            raise CutoverContractError(
                "legacy workspace interval ends before it starts"
            )

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "LegacyWorkspaceFreezeRequest":
        return cls(
            workspace_interval_id=int(row["id"]),
            owner_kind=str(row["owner_kind"]),
            owner_id=_uuid(row["owner_id"], "owner_id"),  # type: ignore[arg-type]
            tier=None if row["tier"] is None else str(row["tier"]),
            cpu_millicores=int(row["cpu_millicores"]),
            memory_bytes=int(row["mem_bytes"]),
            started_at=_aware_utc(row["started_at"], "started_at"),
            ended_at=_aware_utc(row["ended_at"], "ended_at"),
            user_id=_uuid(row.get("user_id"), "user_id", nullable=True),
            project_id=_uuid(row.get("project_id"), "project_id", nullable=True),
        )

    @property
    def source_id(self) -> str:
        return (
            f"ws:{self.owner_kind}:{self.owner_id}:{int(self.started_at.timestamp())}"
        )

    @property
    def ref_kind(self) -> str:
        return "job" if self.owner_kind == "job" else "thread"

    @property
    def quantities(self) -> dict[str, Decimal]:
        return _legacy_quantities(
            started_at=self.started_at,
            ended_at=self.ended_at,
            cpu_millicores=self.cpu_millicores,
            memory_bytes=self.memory_bytes,
        )

    @property
    def details(self) -> dict[str, Any]:
        duration_hours = max(
            0.0, (self.ended_at - self.started_at).total_seconds() / 3600.0
        )
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "cpu_millicores": self.cpu_millicores,
            "mem_bytes": self.memory_bytes,
            "tier": self.tier,
            "duration_h": round(duration_hours, 6),
        }

    def draft_payloads(self) -> tuple[dict[str, Any], dict[str, Any]]:
        common = {
            "ts": _timestamp_text(self.ended_at),
            "user_id": None if self.user_id is None else str(self.user_id),
            "project_id": None if self.project_id is None else str(self.project_id),
            "ref_kind": self.ref_kind,
            "ref_id": str(self.owner_id),
            "category": "compute",
            "resource": "workspace_pod",
            "rate_usd": None,
            "cost_usd": None,
            "source": "orchestrator",
            "source_id": self.source_id,
            "details": self.details,
        }
        return tuple(
            {
                **common,
                "quantity": decimal_text(self.quantities[unit]),
                "unit": unit,
            }
            for unit in ("vcpu-hour", "gib-hour")
        )  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FrozenLegacyWorkspaceEvent:
    payload: Mapping[str, Any]
    row_hash: str

    def __post_init__(self) -> None:
        payload = dict(self.payload)
        if not _HASH_RE.fullmatch(self.row_hash):
            raise CutoverContractError("legacy frozen row hash is invalid")
        if legacy_workspace_payload_hash(payload) != self.row_hash:
            raise CutoverContractError("legacy frozen row hash does not match payload")
        object.__setattr__(self, "payload", payload)

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "FrozenLegacyWorkspaceEvent":
        payload = row["event_payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise CutoverContractError(
                    "legacy frozen event JSON is invalid"
                ) from exc
        if not isinstance(payload, Mapping):
            raise CutoverContractError("legacy frozen event payload is not an object")
        return cls(payload=dict(payload), row_hash=str(row["row_hash"]))

    def validate_for(self, request: LegacyWorkspaceFreezeRequest) -> None:
        payload = self.payload
        unit = str(payload["unit"])
        if unit not in request.quantities:
            raise CutoverContractError("legacy ledger returned an unsupported unit")
        expected = request.draft_payloads()[0 if unit == "vcpu-hour" else 1]
        for field_name in (
            "ts",
            "ref_kind",
            "ref_id",
            "category",
            "resource",
            "quantity",
            "unit",
            "source",
            "source_id",
            "details",
        ):
            if payload[field_name] != expected[field_name]:
                raise CutoverContractError(
                    f"legacy frozen event {field_name} differs from interval"
                )
        try:
            quantity = Decimal(str(payload["quantity"]))
            rate = (
                None
                if payload["rate_usd"] is None
                else Decimal(str(payload["rate_usd"]))
            )
            cost = (
                None
                if payload["cost_usd"] is None
                else Decimal(str(payload["cost_usd"]))
            )
        except InvalidOperation as exc:
            raise CutoverContractError("legacy frozen pricing is invalid") from exc
        if not quantity.is_finite() or quantity < 0:
            raise CutoverContractError("legacy frozen quantity is invalid")
        if (rate is None) != (cost is None):
            raise CutoverContractError("legacy frozen rate and cost must be atomic")
        if rate is not None and (
            not rate.is_finite()
            or rate < 0
            or cost is None
            or not cost.is_finite()
            or cost != quantity * rate
        ):
            raise CutoverContractError("legacy frozen cost does not match its rate")


@dataclass(frozen=True, slots=True)
class LegacyWorkspacePublishResult:
    expected: int
    inserted: int
    verified: int


class LegacyWorkspaceCutoverLedger(Protocol):
    """Strict audit transport required by the cutover coordinator.

    ``freeze`` adopts exact existing rows when present and resolves immutable
    rate/attribution fields for missing rows. ``publish`` inserts and verifies
    the complete frozen pair in one audit transaction. Neither operation may
    swallow an error or fall back to per-row best effort.
    """

    async def freeze_legacy_workspace_events(
        self, request: LegacyWorkspaceFreezeRequest
    ) -> Sequence[FrozenLegacyWorkspaceEvent]: ...

    async def publish_frozen_legacy_workspace_events(
        self, events: Sequence[FrozenLegacyWorkspaceEvent]
    ) -> LegacyWorkspacePublishResult: ...


@dataclass(frozen=True, slots=True)
class CutoverStatus:
    state: str
    phase: CutoverPhase
    leader_generation: int
    cutover_at: datetime | None
    request_id: UUID | None
    actor_id: UUID | None
    reason: str | None
    unplanned_intervals: int
    planned: int
    published: int
    conflicts: int
    open_legacy_intervals: int
    cutover_error: Mapping[str, Any] | None

    @property
    def active(self) -> bool:
        return self.state == "active" and self.phase is CutoverPhase.ACTIVE


@dataclass(frozen=True, slots=True)
class CutoverResumeResult:
    status: CutoverStatus
    plans_frozen: int
    plans_published: int

    @property
    def progressed(self) -> bool:
        return self.plans_frozen > 0 or self.plans_published > 0


_CONTROL_LOCK_SQL = """
/* infra-cutover:control-lock */
SELECT control.*, statement_timestamp() AS now,
       rollup.last_closed_day,
       bootstrap.status AS bootstrap_status,
       bootstrap.seeded_through_day,
       bootstrap.reconciled_through_day,
       bootstrap.completed_at AS bootstrap_completed_at
FROM infra_metering_control AS control
LEFT JOIN rollup_state AS rollup ON rollup.name = 'usage_daily_v2'
LEFT JOIN usage_rollup_v2_bootstrap_state AS bootstrap
  ON bootstrap.singleton = TRUE
WHERE control.singleton = TRUE
FOR UPDATE OF control
"""

_CONTROL_SHARE_SQL = """
/* infra-cutover:control-share */
SELECT * FROM infra_metering_control
WHERE singleton = TRUE
FOR SHARE
"""

_CONTROL_READ_SQL = """
/* infra-cutover:control-read */
SELECT * FROM infra_metering_control
WHERE singleton = TRUE
"""

_STATUS_SQL = """
/* infra-cutover:status */
SELECT control.*,
       (SELECT count(*) FROM workspace_intervals AS legacy
        WHERE control.cutover_at IS NOT NULL
          AND legacy.ended_at <= control.cutover_at
          AND NOT EXISTS (
              SELECT 1 FROM legacy_workspace_cutover_plans AS plan
              WHERE plan.workspace_interval_id = legacy.id
          )) AS unplanned_intervals,
       (SELECT count(*) FROM legacy_workspace_cutover_plans
        WHERE state = 'planned') AS planned,
       (SELECT count(*) FROM legacy_workspace_cutover_plans
        WHERE state = 'published') AS published,
       (SELECT count(*) FROM legacy_workspace_cutover_plans
        WHERE state = 'conflict') AS conflicts,
       (SELECT count(*) FROM workspace_intervals
        WHERE ended_at IS NULL) AS open_legacy_intervals
FROM infra_metering_control AS control
WHERE control.singleton = TRUE
"""

_LEGACY_PREFLIGHT_SQL = """
/* infra-cutover:legacy-preflight */
SELECT legacy.*,
       CASE WHEN legacy.owner_kind = 'job'
            THEN job.user_id ELSE thread.user_id END AS user_id,
       CASE WHEN legacy.owner_kind = 'job'
            THEN job.project_id ELSE thread.project_id END AS project_id
FROM workspace_intervals AS legacy
LEFT JOIN jobs AS job
  ON legacy.owner_kind = 'job' AND job.id = legacy.owner_id
LEFT JOIN threads AS thread
  ON legacy.owner_kind = 'session' AND thread.id = legacy.owner_id
WHERE legacy.ended_at IS NOT NULL
ORDER BY legacy.id
LIMIT $1
"""

_LOCK_ALL_LEGACY_SQL = """
/* infra-cutover:lock-all-legacy */
SELECT legacy.*,
       CASE WHEN legacy.owner_kind = 'job'
            THEN job.user_id ELSE thread.user_id END AS user_id,
       CASE WHEN legacy.owner_kind = 'job'
            THEN job.project_id ELSE thread.project_id END AS project_id
FROM workspace_intervals AS legacy
LEFT JOIN jobs AS job
  ON legacy.owner_kind = 'job' AND job.id = legacy.owner_id
LEFT JOIN threads AS thread
  ON legacy.owner_kind = 'session' AND thread.id = legacy.owner_id
ORDER BY legacy.id
LIMIT $1
FOR UPDATE OF legacy
"""

_OPEN_SHADOW_SQL = """
/* infra-cutover:open-shadow */
SELECT interval.*,
       comparison.id AS comparison_id,
       comparison.snapshot_id AS comparison_snapshot_id,
       comparison.owner_kind AS comparison_owner_kind,
       comparison.owner_id AS comparison_owner_id,
       comparison.owner_trusted AS comparison_owner_trusted,
       comparison.legacy_interval_id AS comparison_legacy_interval_id,
       comparison.legacy_cpu_millicores AS comparison_legacy_cpu_millicores,
       comparison.legacy_memory_bytes AS comparison_legacy_memory_bytes,
       comparison.legacy_started_at AS comparison_legacy_started_at,
       comparison.observed_cpu_millicores AS comparison_cpu_millicores,
       comparison.observed_memory_bytes AS comparison_memory_bytes,
       comparison.observed_started_at AS comparison_started_at,
       comparison.status AS comparison_status,
       comparison.reason_code AS comparison_reason_code,
       comparison.explained AS comparison_explained
FROM resource_intervals AS interval
LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM resource_inventory_shadow_comparisons AS candidate
    WHERE candidate.inventory_scope_id = interval.inventory_scope_id
      AND candidate.source_uid = interval.source_uid
    ORDER BY candidate.comparison_at DESC, candidate.created_at DESC,
             candidate.id DESC
    LIMIT 1
) AS comparison ON TRUE
WHERE interval.resource = 'workspace_pod' AND interval.ended_at IS NULL
ORDER BY interval.owner_kind, interval.owner_id, interval.id
LIMIT $1
FOR UPDATE OF interval
"""

_UNEXPLAINED_LATEST_SHADOW_SQL = """
/* infra-cutover:unexplained-latest-shadow */
WITH configured_scopes AS (
    SELECT scope.id
    FROM resource_inventory_scopes AS scope
    JOIN resource_inventory_scope_epochs AS epoch ON epoch.scope_id = scope.id
    WHERE scope.api_resource = 'core/v1/pods'
      AND scope.source_cluster = $1
      AND scope.namespace = ANY($2::text[])
      AND epoch.retired_at IS NULL
), latest AS (
    SELECT DISTINCT ON (comparison.inventory_scope_id, comparison.source_uid)
           comparison.id, comparison.inventory_scope_id, comparison.source_uid,
           comparison.explained
    FROM resource_inventory_shadow_comparisons AS comparison
    JOIN configured_scopes AS scope
      ON scope.id = comparison.inventory_scope_id
    ORDER BY comparison.inventory_scope_id, comparison.source_uid,
             comparison.comparison_at DESC, comparison.created_at DESC,
             comparison.id DESC
)
SELECT id, inventory_scope_id, source_uid
FROM latest
WHERE explained = FALSE
ORDER BY inventory_scope_id, source_uid
LIMIT 1
"""

_CUTOVER_SCOPE_EPOCHS_SQL = """
/* infra-cutover:scope-epochs */
SELECT epoch.*, scope.api_resource, scope.source_cluster, scope.namespace
FROM resource_inventory_scope_epochs AS epoch
JOIN resource_inventory_scopes AS scope ON scope.id = epoch.scope_id
WHERE scope.api_resource = 'core/v1/pods'
  AND scope.source_cluster = $1
  AND scope.namespace = ANY($2::text[])
  AND epoch.retired_at IS NULL
ORDER BY scope.namespace, scope.id, epoch.epoch_number
FOR UPDATE OF epoch
"""

_PROMOTE_CUTOVER_SCOPE_EPOCH_SQL = """
/* infra-cutover:promote-scope-epoch */
UPDATE resource_inventory_scope_epochs
SET required_for_rollup = TRUE,
    required_from = $2,
    updated_at = statement_timestamp()
WHERE id = $1
  AND retired_at IS NULL
  AND leader_generation = $3
RETURNING TRUE
"""

_CUTOVER_SCOPE_PROOF_SQL = """
/* infra-cutover:scope-proof */
SELECT count(*) AS namespace_count,
       count(*) FILTER (WHERE proof.epoch_count = 1) AS proven_count
FROM unnest($3::text[]) AS configured(namespace)
LEFT JOIN LATERAL (
    SELECT count(*) AS epoch_count
    FROM resource_inventory_scopes AS scope
    JOIN resource_inventory_scope_epochs AS epoch ON epoch.scope_id = scope.id
    WHERE scope.api_resource = 'core/v1/pods'
      AND scope.source_cluster = $2
      AND scope.namespace = configured.namespace
      AND epoch.required_for_rollup = TRUE
      AND epoch.required_from = $1
      AND epoch.reliable_from IS NOT NULL
      AND epoch.reliable_from <= $1
      AND epoch.continuous_since IS NOT NULL
      AND epoch.continuous_since <= $1
      AND (epoch.retired_at IS NULL OR epoch.retired_at > $1)
) AS proof ON TRUE
"""

_CLOSE_LEGACY_SQL = """
/* infra-cutover:close-legacy */
UPDATE workspace_intervals
SET ended_at = $2
WHERE id = $1 AND ended_at IS NULL AND started_at <= $2
RETURNING id
"""

_CLOSE_SHADOW_SQL = """
/* infra-cutover:close-shadow */
UPDATE resource_intervals
SET ended_at = $2,
    end_time_source = 'cutover-barrier',
    end_uncertainty_us = floor(extract(epoch FROM ($2 - last_confirmed_at))
        * 1000000)::bigint,
    end_reason = 'cutover-barrier',
    updated_at = statement_timestamp()
WHERE id = $1 AND ended_at IS NULL AND last_confirmed_at <= $2
RETURNING source_lifecycle_id
"""

_ADVANCE_HEAD_SQL = """
/* infra-cutover:advance-head */
UPDATE resource_lifecycle_heads
SET latest_revision_no = latest_revision_no + 1,
    current_interval_id = NULL,
    updated_at = statement_timestamp()
WHERE source_lifecycle_id = $1 AND current_interval_id = $2
RETURNING latest_revision_no
"""

_CLONE_SHADOW_SQL = """
/* infra-cutover:clone-shadow */
INSERT INTO resource_intervals (
    id, inventory_scope_id, source_cluster, source_kind, source_uid,
    source_api_version, source_resource_version, source_lifecycle_id,
    revision_no, source_revision, namespace, name, category, resource,
    measurement_basis, cost_domain, resource_class, attribution_scope,
    owner_kind, owner_id, user_id, project_id, attribution_source,
    attribution_quality, backing_resource_uid, lifecycle_confidence,
    cpu_millicores, memory_bytes, storage_bytes, capacity_source,
    capacity_quality, measurement_algorithm, started_at, start_time_source,
    start_uncertainty_us, last_seen_at, last_confirmed_at,
    last_seen_snapshot_id, materialized_through, details
)
SELECT
    $2, old.inventory_scope_id, old.source_cluster, old.source_kind,
    old.source_uid, old.source_api_version, old.source_resource_version,
    old.source_lifecycle_id, $4, $5, old.namespace, old.name, old.category,
    old.resource, old.measurement_basis, old.cost_domain, old.resource_class,
    old.attribution_scope, old.owner_kind, old.owner_id, old.user_id,
    old.project_id, old.attribution_source, old.attribution_quality,
    old.backing_resource_uid, old.lifecycle_confidence, old.cpu_millicores,
    old.memory_bytes, old.storage_bytes, old.capacity_source,
    old.capacity_quality, old.measurement_algorithm, $3, 'cutover-barrier', 0,
    $3, $3, NULL, $3,
    old.details || jsonb_build_object(
        'cutover_request_id', $6::uuid::text,
        'canonical_from', to_char($3 AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'shadow_interval_id', old.id::text,
        'publication_enabled', TRUE
    )
FROM resource_intervals AS old
WHERE old.id = $1 AND old.ended_at = $3
RETURNING id
"""

_LINK_HEAD_SQL = """
/* infra-cutover:link-head */
UPDATE resource_lifecycle_heads
SET current_interval_id = $2, updated_at = statement_timestamp()
WHERE source_lifecycle_id = $1 AND current_interval_id IS NULL
RETURNING TRUE
"""

_ENTER_PREPARING_SQL = """
/* infra-cutover:enter-preparing */
UPDATE infra_metering_control
SET cutover_state = 'preparing',
    cutover_phase = 'legacy-draining',
    cutover_at = $2,
    cutover_request_id = $3,
    cutover_actor_id = $4,
    cutover_reason = $5,
    cutover_requested_at = $2,
    barrier_committed_at = $2,
    cutover_error = NULL,
    updated_at = statement_timestamp()
WHERE singleton = TRUE
  AND leader_generation = $1
  AND cutover_state = 'disabled'
RETURNING cutover_request_id
"""

_FREEZE_CANDIDATES_SQL = """
/* infra-cutover:freeze-candidates */
SELECT legacy.*,
       CASE WHEN legacy.owner_kind = 'job'
            THEN job.user_id ELSE thread.user_id END AS user_id,
       CASE WHEN legacy.owner_kind = 'job'
            THEN job.project_id ELSE thread.project_id END AS project_id
FROM workspace_intervals AS legacy
LEFT JOIN jobs AS job
  ON legacy.owner_kind = 'job' AND job.id = legacy.owner_id
LEFT JOIN threads AS thread
  ON legacy.owner_kind = 'session' AND thread.id = legacy.owner_id
WHERE legacy.ended_at <= $1
  AND NOT EXISTS (
      SELECT 1 FROM legacy_workspace_cutover_plans AS plan
      WHERE plan.workspace_interval_id = legacy.id
  )
ORDER BY legacy.id
LIMIT $2
"""

_INSERT_PLAN_SQL = """
/* infra-cutover:insert-plan */
INSERT INTO legacy_workspace_cutover_plans (
    id, workspace_interval_id, cutover_request_id, event_set_hash,
    creator_generation
)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (workspace_interval_id) DO NOTHING
RETURNING id
"""

_INSERT_PLAN_EVENT_SQL = """
/* infra-cutover:insert-plan-event */
INSERT INTO legacy_workspace_cutover_plan_events (
    plan_id, ordinal, source, source_id, unit, ts, row_hash, event_payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
"""

_EXISTING_PLAN_SQL = """
/* infra-cutover:existing-plan */
SELECT id, event_set_hash FROM legacy_workspace_cutover_plans
WHERE workspace_interval_id = $1
FOR SHARE
"""

_PENDING_PLANS_SQL = """
/* infra-cutover:pending-plans */
SELECT * FROM legacy_workspace_cutover_plans
WHERE state = 'planned'
ORDER BY created_at, id
LIMIT $1
"""

_PLAN_EVENTS_SQL = """
/* infra-cutover:plan-events */
SELECT * FROM legacy_workspace_cutover_plan_events
WHERE plan_id = $1
ORDER BY ordinal
"""

_LOCK_PLAN_SQL = """
/* infra-cutover:lock-plan */
SELECT * FROM legacy_workspace_cutover_plans
WHERE id = $1
FOR UPDATE
"""

_PUBLISH_PLAN_SQL = """
/* infra-cutover:publish-plan */
UPDATE legacy_workspace_cutover_plans AS plan
SET state = 'published',
    attempt_count = attempt_count + 1,
    last_attempt_generation = $2,
    last_attempt_at = statement_timestamp(),
    sanitized_error = NULL,
    published_at = statement_timestamp()
WHERE plan.id = $1 AND plan.state = 'planned'
  AND EXISTS (
      SELECT 1 FROM infra_metering_control AS control
      WHERE control.singleton = TRUE
        AND control.leader_generation = $2
        AND control.cutover_state = 'preparing'
        AND control.cutover_request_id = $3
  )
RETURNING plan.id
"""

_FAIL_PLAN_SQL = """
/* infra-cutover:fail-plan */
UPDATE legacy_workspace_cutover_plans AS plan
SET state = $4,
    attempt_count = attempt_count + 1,
    last_attempt_generation = $2,
    last_attempt_at = statement_timestamp(),
    sanitized_error = $5::jsonb
WHERE plan.id = $1 AND plan.state = 'planned'
  AND EXISTS (
      SELECT 1 FROM infra_metering_control AS control
      WHERE control.singleton = TRUE
        AND control.leader_generation = $2
        AND control.cutover_state = 'preparing'
        AND control.cutover_request_id = $3
  )
RETURNING plan.id
"""

_DRAIN_COUNTS_SQL = """
/* infra-cutover:drain-counts */
SELECT
    (SELECT count(*) FROM workspace_intervals AS legacy
     WHERE legacy.ended_at <= $1
       AND NOT EXISTS (
           SELECT 1 FROM legacy_workspace_cutover_plans AS plan
           WHERE plan.workspace_interval_id = legacy.id
       )) AS unplanned,
    (SELECT count(*) FROM legacy_workspace_cutover_plans
     WHERE state = 'planned') AS planned,
    (SELECT count(*) FROM legacy_workspace_cutover_plans
     WHERE state = 'conflict') AS conflicts,
    (SELECT count(*) FROM workspace_intervals
     WHERE ended_at IS NULL) AS open_legacy
"""

_MARK_READY_SQL = """
/* infra-cutover:mark-ready */
WITH stamped AS (
    UPDATE workspace_intervals
    SET materialized_at = COALESCE(materialized_at, statement_timestamp())
    WHERE ended_at <= $2
)
UPDATE infra_metering_control
SET cutover_phase = 'ready-to-activate',
    legacy_drained_at = statement_timestamp(),
    cutover_error = NULL,
    updated_at = statement_timestamp()
WHERE singleton = TRUE
  AND leader_generation = $1
  AND cutover_state = 'preparing'
  AND cutover_phase = 'legacy-draining'
  AND cutover_request_id = $3
RETURNING cutover_phase
"""

_ACTIVATE_SQL = """
/* infra-cutover:activate */
UPDATE infra_metering_control
SET cutover_state = 'active',
    cutover_phase = 'active',
    activated_at = statement_timestamp(),
    cutover_error = NULL,
    updated_at = statement_timestamp()
WHERE singleton = TRUE
  AND leader_generation = $1
  AND cutover_state = 'preparing'
  AND cutover_phase = 'ready-to-activate'
  AND cutover_request_id = $2
RETURNING cutover_state
"""

_CONTROL_ERROR_SQL = """
/* infra-cutover:control-error */
UPDATE infra_metering_control
SET cutover_error = $4::jsonb, updated_at = statement_timestamp()
WHERE singleton = TRUE
  AND leader_generation = $1
  AND cutover_state = 'preparing'
  AND cutover_request_id = $2
  AND cutover_phase = $3
RETURNING TRUE
"""


class InfrastructureWorkspaceCutover:
    """Bounded, generation-fenced workspace metering handoff coordinator."""

    def __init__(
        self,
        app_pool: asyncpg.Pool,
        ledger: LegacyWorkspaceCutoverLedger,
        *,
        source_cluster: str,
        namespace_allowlist: Sequence[str],
        barrier_limit: int = 1000,
        preflight_limit: int = 10_000,
        freeze_batch_size: int = 100,
        publish_batch_size: int = 20,
        max_scope_age: timedelta = _DEFAULT_SCOPE_FRESHNESS,
    ) -> None:
        for name, value, maximum in (
            ("barrier_limit", barrier_limit, 10_000),
            ("preflight_limit", preflight_limit, 100_000),
            ("freeze_batch_size", freeze_batch_size, 1000),
            ("publish_batch_size", publish_batch_size, 1000),
        ):
            if value <= 0 or value > maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if isinstance(namespace_allowlist, (str, bytes)):
            raise ValueError("namespace_allowlist must be a sequence of namespaces")
        normalized_cluster = str(source_cluster).strip()
        requested_namespaces = tuple(
            str(value).strip() for value in namespace_allowlist
        )
        normalized_namespaces = tuple(dict.fromkeys(requested_namespaces))
        if not normalized_cluster:
            raise ValueError("source_cluster must be non-empty")
        if (
            not normalized_namespaces
            or any(not value for value in normalized_namespaces)
            or len(normalized_namespaces) != len(requested_namespaces)
        ):
            raise ValueError(
                "namespace_allowlist must contain unique non-empty namespaces"
            )
        if max_scope_age <= timedelta(0) or max_scope_age > timedelta(days=7):
            raise ValueError("max_scope_age must be between 0 and 7 days")
        self._app = app_pool
        self._ledger = ledger
        self._source_cluster = normalized_cluster
        self._namespace_allowlist = normalized_namespaces
        self._barrier_limit = barrier_limit
        self._preflight_limit = preflight_limit
        self._freeze_batch_size = freeze_batch_size
        self._publish_batch_size = publish_batch_size
        self._max_scope_age = max_scope_age

    @staticmethod
    def _validate_generation(
        control: Mapping[str, Any] | None, generation: int
    ) -> None:
        if control is None:
            raise CutoverFenceError("metering control row is missing")
        if generation <= 0 or int(control["leader_generation"]) != generation:
            raise CutoverFenceError("metering leader generation is stale")

    @staticmethod
    def _validate_request(actor_id: UUID, reason: str, request_id: UUID) -> str:
        _uuid(actor_id, "actor_id")
        _uuid(request_id, "idempotency_key")
        normalized = str(reason).strip()
        if (
            not normalized
            or len(normalized) > 1024
            or _REASON_CONTROL.search(normalized)
        ):
            raise CutoverContractError(
                "cutover reason must be 1-1024 printable characters"
            )
        return normalized

    @staticmethod
    def _validate_bootstrap_and_watermark(
        control: Mapping[str, Any], barrier: datetime
    ) -> None:
        if (
            control.get("bootstrap_status") != "complete"
            or control.get("bootstrap_completed_at") is None
            or control.get("seeded_through_day") is None
            or control.get("reconciled_through_day")
            != control.get("seeded_through_day")
        ):
            raise CutoverBlocked("typed usage rollup bootstrap is incomplete")
        last_closed = control.get("last_closed_day")
        if last_closed is not None:
            if not isinstance(last_closed, date) or isinstance(last_closed, datetime):
                raise CutoverContractError("usage rollup watermark is invalid")
            if barrier.date() <= last_closed:
                raise CutoverBlocked(
                    "cutover barrier must be after the typed rollup watermark"
                )

    @staticmethod
    def _validate_prepare_replay(
        status: CutoverStatus,
        *,
        actor_id: UUID,
        reason: str,
        request_id: UUID,
    ) -> CutoverStatus:
        if status.request_id == request_id:
            if status.actor_id == actor_id and status.reason == reason:
                return status
            raise CutoverConflictError(
                "cutover idempotency replay changed actor or reason"
            )
        raise CutoverConflictError("a different infrastructure cutover already exists")

    async def _preflight_legacy_integrity(
        self, generation: int
    ) -> tuple[LegacyWorkspaceFreezeRequest, ...]:
        """Repair and strictly verify the bounded closed legacy snapshot.

        Audit I/O deliberately happens without an app-DB lock. ``prepare``
        subsequently locks and compares the entire legacy set before committing
        the irreversible barrier, so a concurrent close can only abort the
        attempt while cutover remains disabled.
        """

        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                control = await conn.fetchrow(_CONTROL_READ_SQL)
                self._validate_generation(control, generation)
                assert control is not None
                if str(control["cutover_state"]) != "disabled":
                    raise CutoverConflictError(
                        "an infrastructure cutover started during preflight"
                    )
                rows = await conn.fetch(
                    _LEGACY_PREFLIGHT_SQL, self._preflight_limit + 1
                )
        if len(rows) > self._preflight_limit:
            raise CutoverBlocked(
                "legacy audit integrity preflight exceeds its configured bound"
            )

        requests = tuple(LegacyWorkspaceFreezeRequest.from_record(row) for row in rows)
        for request in requests:
            try:
                events = tuple(
                    await self._ledger.freeze_legacy_workspace_events(request)
                )
                self._validate_frozen_pair(request, events)
                result = await self._ledger.publish_frozen_legacy_workspace_events(
                    events
                )
                if (
                    result.expected != len(events)
                    or result.verified != len(events)
                    or result.inserted < 0
                    or result.inserted > len(events)
                ):
                    raise LegacyWorkspaceLedgerError(
                        "legacy ledger returned an incomplete verification"
                    )
            except LegacyWorkspaceLedgerConflict as exc:
                raise CutoverBlocked(
                    "legacy audit integrity preflight found an immutable conflict"
                ) from exc
            except Exception as exc:
                raise CutoverBlocked(
                    "legacy audit integrity preflight could not verify every row"
                ) from exc
        return requests

    async def status(self) -> CutoverStatus:
        row = await self._app.fetchrow(_STATUS_SQL)
        if row is None:
            raise CutoverFenceError("metering control row is missing")
        return self._status_from(row)

    @staticmethod
    def _status_from(row: Mapping[str, Any]) -> CutoverStatus:
        error = row.get("cutover_error")
        if isinstance(error, str):
            try:
                error = json.loads(error)
            except json.JSONDecodeError as exc:
                raise CutoverContractError(
                    "cutover diagnostic JSON is invalid"
                ) from exc
        if error is not None and not isinstance(error, Mapping):
            raise CutoverContractError("cutover diagnostic is not an object")
        try:
            phase = CutoverPhase(str(row["cutover_phase"]))
        except ValueError as exc:
            raise CutoverContractError("cutover phase is invalid") from exc
        return CutoverStatus(
            state=str(row["cutover_state"]),
            phase=phase,
            leader_generation=int(row["leader_generation"]),
            cutover_at=(
                None
                if row["cutover_at"] is None
                else _aware_utc(row["cutover_at"], "cutover_at")
            ),
            request_id=_uuid(
                row.get("cutover_request_id"), "cutover_request_id", nullable=True
            ),
            actor_id=_uuid(
                row.get("cutover_actor_id"), "cutover_actor_id", nullable=True
            ),
            reason=row.get("cutover_reason"),
            unplanned_intervals=int(row.get("unplanned_intervals", 0)),
            planned=int(row.get("planned", 0)),
            published=int(row.get("published", 0)),
            conflicts=int(row.get("conflicts", 0)),
            open_legacy_intervals=int(row.get("open_legacy_intervals", 0)),
            cutover_error=None if error is None else dict(error),
        )

    async def prepare(
        self,
        generation: int,
        *,
        actor_id: UUID,
        reason: str,
        idempotency_key: UUID,
    ) -> CutoverStatus:
        """Atomically choose T, stop legacy opens, and split matched shadows."""

        normalized_reason = self._validate_request(actor_id, reason, idempotency_key)
        initial_control = await self._app.fetchrow(_CONTROL_READ_SQL)
        self._validate_generation(initial_control, generation)
        assert initial_control is not None
        if str(initial_control["cutover_state"]) != "disabled":
            return self._validate_prepare_replay(
                await self.status(),
                actor_id=actor_id,
                reason=normalized_reason,
                request_id=idempotency_key,
            )

        preflight = await self._preflight_legacy_integrity(generation)
        async with self._app.acquire() as conn:
            async with conn.transaction():
                control = await conn.fetchrow(_CONTROL_LOCK_SQL)
                self._validate_generation(control, generation)
                assert control is not None
                state = str(control["cutover_state"])
                if state != "disabled":
                    return self._validate_prepare_replay(
                        await self._status_on_connection(conn),
                        actor_id=actor_id,
                        reason=normalized_reason,
                        request_id=idempotency_key,
                    )

                barrier = _aware_utc(control["now"], "statement_timestamp")
                self._validate_bootstrap_and_watermark(control, barrier)
                all_legacy_rows = await conn.fetch(
                    _LOCK_ALL_LEGACY_SQL,
                    self._preflight_limit + self._barrier_limit + 1,
                )
                if len(all_legacy_rows) > self._preflight_limit + self._barrier_limit:
                    raise CutoverBlocked(
                        "legacy cutover set exceeds its configured bound"
                    )
                closed_rows = tuple(
                    row for row in all_legacy_rows if row["ended_at"] is not None
                )
                legacy_rows = tuple(
                    row for row in all_legacy_rows if row["ended_at"] is None
                )
                if (
                    len(closed_rows) > self._preflight_limit
                    or len(legacy_rows) > self._barrier_limit
                ):
                    raise CutoverBlocked(
                        "legacy cutover set exceeds its configured bound"
                    )
                locked_preflight = tuple(
                    LegacyWorkspaceFreezeRequest.from_record(row) for row in closed_rows
                )
                if locked_preflight != preflight:
                    raise CutoverBlocked(
                        "legacy workspace set changed during integrity preflight"
                    )
                if any(row["user_id"] is None for row in legacy_rows):
                    raise CutoverBlocked("legacy workspace attribution is unavailable")

                scope_epochs = await conn.fetch(
                    _CUTOVER_SCOPE_EPOCHS_SQL,
                    self._source_cluster,
                    list(self._namespace_allowlist),
                )
                self._validate_scope_epochs(
                    scope_epochs,
                    generation,
                    barrier,
                    source_cluster=self._source_cluster,
                    namespace_allowlist=self._namespace_allowlist,
                    max_scope_age=self._max_scope_age,
                )
                shadow_rows = await conn.fetch(
                    _OPEN_SHADOW_SQL, self._barrier_limit + 1
                )
                if len(shadow_rows) > self._barrier_limit:
                    raise CutoverBlocked("cutover barrier exceeds its configured bound")
                unexplained = await conn.fetchrow(
                    _UNEXPLAINED_LATEST_SHADOW_SQL,
                    self._source_cluster,
                    list(self._namespace_allowlist),
                )
                if unexplained is not None:
                    raise CutoverBlocked(
                        "latest workspace shadow comparisons remain unexplained"
                    )

                matches = self._match_open_intervals(legacy_rows, shadow_rows, barrier)

                entered = await conn.fetchval(
                    _ENTER_PREPARING_SQL,
                    generation,
                    barrier,
                    idempotency_key,
                    actor_id,
                    normalized_reason,
                )
                if entered is None:
                    raise CutoverFenceError("cutover barrier lost its generation fence")

                for epoch in scope_epochs:
                    promoted = await conn.fetchval(
                        _PROMOTE_CUTOVER_SCOPE_EPOCH_SQL,
                        epoch["id"],
                        barrier,
                        generation,
                    )
                    if promoted is not True:
                        raise CutoverFenceError(
                            "inventory source epoch changed at cutover barrier"
                        )

                for legacy, shadow in matches:
                    closed = await conn.fetchval(
                        _CLOSE_LEGACY_SQL, legacy["id"], barrier
                    )
                    if closed is None:
                        raise CutoverConflictError(
                            "legacy workspace changed during cutover barrier"
                        )
                    await self._split_shadow(
                        conn,
                        shadow,
                        barrier=barrier,
                        request_id=idempotency_key,
                    )
        return await self.status()

    @staticmethod
    def _validate_scope_epochs(
        rows: Sequence[Mapping[str, Any]],
        generation: int,
        barrier: datetime,
        *,
        source_cluster: str,
        namespace_allowlist: Sequence[str],
        max_scope_age: timedelta = _DEFAULT_SCOPE_FRESHNESS,
    ) -> None:
        if max_scope_age <= timedelta(0):
            raise CutoverContractError("Pod inventory freshness bound is invalid")
        if not rows:
            raise CutoverBlocked("no active Pod inventory source is available")
        expected_namespaces = set(namespace_allowlist)
        seen_namespaces: set[str] = set()
        for row in rows:
            _uuid(row["scope_id"], "scope_epoch.scope_id")
            namespace = str(row.get("namespace") or "")
            if (
                row.get("source_cluster") != source_cluster
                or namespace not in expected_namespaces
                or namespace in seen_namespaces
            ):
                raise CutoverBlocked(
                    "Pod inventory source is not unique for its configured namespace"
                )
            seen_namespaces.add(namespace)
            reliable_from = row.get("reliable_from")
            continuous_since = row.get("continuous_since")
            complete_through = row.get("complete_through")
            if (
                int(row["leader_generation"]) != generation
                or reliable_from is None
                or _aware_utc(reliable_from, "scope_epoch.reliable_from") > barrier
                or continuous_since is None
                or _aware_utc(continuous_since, "scope_epoch.continuous_since")
                > barrier
                or row.get("last_complete_snapshot_id") is None
                or complete_through is None
                or _aware_utc(complete_through, "scope_epoch.complete_through")
                < barrier - max_scope_age
                or _aware_utc(complete_through, "scope_epoch.complete_through")
                > barrier
                or row.get("snapshot_health") != "healthy"
                or row.get("continuity_health") != "healthy"
                or row.get("item_health") != "healthy"
                or row.get("backend_health") != "healthy"
            ):
                raise CutoverBlocked(
                    "Pod inventory source is not continuously proven at cutover"
                )
        if seen_namespaces != expected_namespaces:
            raise CutoverBlocked(
                "Pod inventory source is missing for a configured namespace"
            )

    @staticmethod
    async def _validate_scope_proof(
        conn: asyncpg.Connection,
        barrier: datetime,
        *,
        source_cluster: str,
        namespace_allowlist: Sequence[str],
    ) -> None:
        proof = await conn.fetchrow(
            _CUTOVER_SCOPE_PROOF_SQL,
            barrier,
            source_cluster,
            list(namespace_allowlist),
        )
        if (
            proof is None
            or int(proof["namespace_count"]) <= 0
            or int(proof["namespace_count"]) != int(proof["proven_count"])
        ):
            raise CutoverBlocked(
                "cutover-day Pod inventory requirement proof is incomplete"
            )

    @staticmethod
    def _owner_key(row: Mapping[str, Any], *, legacy: bool) -> tuple[str, str]:
        owner_kind = str(row["owner_kind"])
        if legacy:
            if owner_kind == "session":
                owner_kind = "thread"
            elif owner_kind != "job":
                raise CutoverBlocked("legacy workspace has an unsupported owner kind")
        elif (
            owner_kind not in {"job", "thread"}
            or row["attribution_scope"] != "customer"
            or row["attribution_quality"] not in {"exact", "derived"}
            or row["user_id"] is None
        ):
            raise CutoverBlocked("open workspace Pod lacks trusted attribution")
        try:
            owner_id = str(UUID(str(row["owner_id"])))
        except (TypeError, ValueError, AttributeError) as exc:
            raise CutoverBlocked("open workspace owner identity is invalid") from exc
        return owner_kind, owner_id

    @staticmethod
    def _validate_current_shadow_comparison(
        legacy: Mapping[str, Any], shadow: Mapping[str, Any]
    ) -> None:
        if (
            shadow.get("comparison_id") is None
            or shadow.get("last_seen_snapshot_id") is None
            or shadow.get("comparison_snapshot_id")
            != shadow.get("last_seen_snapshot_id")
            or shadow.get("comparison_explained") is not True
            or shadow.get("comparison_owner_trusted") is not True
            or shadow.get("comparison_status")
            not in {"matched", "capacity-mismatch", "lifetime-mismatch"}
        ):
            raise CutoverBlocked(
                "open workspace Pod lacks current explained shadow evidence"
            )
        try:
            identities_match = (
                str(shadow["comparison_owner_kind"]) == str(shadow["owner_kind"])
                and _uuid(shadow["comparison_owner_id"], "comparison owner_id")
                == _uuid(shadow["owner_id"], "shadow owner_id")
                and int(shadow["comparison_legacy_interval_id"]) == int(legacy["id"])
                and int(shadow["comparison_legacy_cpu_millicores"])
                == int(legacy["cpu_millicores"])
                and int(shadow["comparison_legacy_memory_bytes"])
                == int(legacy["mem_bytes"])
                and int(shadow["comparison_cpu_millicores"])
                == int(shadow["cpu_millicores"])
                and int(shadow["comparison_memory_bytes"])
                == int(shadow["memory_bytes"])
                and _aware_utc(
                    shadow["comparison_legacy_started_at"],
                    "comparison legacy_started_at",
                )
                == _aware_utc(legacy["started_at"], "legacy started_at")
                and _aware_utc(shadow["comparison_started_at"], "comparison started_at")
                == _aware_utc(shadow["started_at"], "shadow started_at")
                and _uuid(shadow["user_id"], "shadow user_id")
                == _uuid(legacy["user_id"], "legacy user_id")
                and _uuid(shadow.get("project_id"), "shadow project_id", nullable=True)
                == _uuid(legacy.get("project_id"), "legacy project_id", nullable=True)
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise CutoverBlocked(
                "open workspace Pod shadow evidence is incomplete"
            ) from exc
        if not identities_match:
            raise CutoverBlocked(
                "open workspace Pod shadow evidence differs from current state"
            )

    def _match_open_intervals(
        self,
        legacy_rows: Sequence[Mapping[str, Any]],
        shadow_rows: Sequence[Mapping[str, Any]],
        barrier: datetime,
    ) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
        legacy_by_owner: dict[tuple[str, str], Mapping[str, Any]] = {}
        for row in legacy_rows:
            if _aware_utc(row["started_at"], "legacy started_at") > barrier:
                raise CutoverBlocked("legacy workspace starts after cutover barrier")
            key = self._owner_key(row, legacy=True)
            if key in legacy_by_owner:
                raise CutoverBlocked("legacy workspace owner is not unique")
            legacy_by_owner[key] = row

        shadow_by_owner: dict[tuple[str, str], Mapping[str, Any]] = {}
        for row in shadow_rows:
            if _aware_utc(row["started_at"], "shadow started_at") > barrier:
                raise CutoverBlocked("shadow workspace starts after cutover barrier")
            if _aware_utc(row["last_confirmed_at"], "last_confirmed_at") > barrier:
                raise CutoverBlocked("shadow confirmation is after cutover barrier")
            key = self._owner_key(row, legacy=False)
            if key in shadow_by_owner:
                raise CutoverBlocked("workspace Pod owner match is ambiguous")
            shadow_by_owner[key] = row

        if legacy_by_owner.keys() != shadow_by_owner.keys():
            raise CutoverBlocked(
                "legacy and trusted shadow workspace owners do not match exactly"
            )
        matches = tuple(
            (legacy_by_owner[key], shadow_by_owner[key])
            for key in sorted(legacy_by_owner)
        )
        for legacy, shadow in matches:
            self._validate_current_shadow_comparison(legacy, shadow)
        return matches

    @staticmethod
    async def _split_shadow(
        conn: asyncpg.Connection,
        shadow: Mapping[str, Any],
        *,
        barrier: datetime,
        request_id: UUID,
    ) -> UUID:
        old_id = UUID(str(shadow["id"]))
        lifecycle_id = await conn.fetchval(_CLOSE_SHADOW_SQL, old_id, barrier)
        if lifecycle_id is None:
            raise CutoverConflictError("shadow workspace changed at cutover barrier")
        revision_no = await conn.fetchval(_ADVANCE_HEAD_SQL, lifecycle_id, old_id)
        if revision_no is None:
            raise CutoverConflictError("shadow lifecycle head lost cutover barrier")
        new_id = uuid4()
        new_revision = hashlib.sha256(
            (
                "srw-cutover-barrier-v1\0"
                + str(shadow["source_revision"])
                + "\0"
                + _timestamp_text(barrier)
            ).encode()
        ).hexdigest()
        inserted = await conn.fetchval(
            _CLONE_SHADOW_SQL,
            old_id,
            new_id,
            barrier,
            revision_no,
            new_revision,
            request_id,
        )
        if inserted is None:
            raise CutoverConflictError("canonical cutover revision was not created")
        linked = await conn.fetchval(_LINK_HEAD_SQL, lifecycle_id, new_id)
        if not linked:
            raise CutoverConflictError("canonical cutover lifecycle link failed")
        return new_id

    async def resume(
        self, generation: int, *, idempotency_key: UUID
    ) -> CutoverResumeResult:
        """Run one bounded freeze/delivery/phase-advance pass."""

        _uuid(idempotency_key, "idempotency_key")
        status = await self.status()
        if status.request_id != idempotency_key:
            raise CutoverConflictError("cutover idempotency key does not match")
        if status.active:
            return CutoverResumeResult(status=status, plans_frozen=0, plans_published=0)
        if status.state != "preparing":
            raise CutoverConflictError("infrastructure cutover is not preparing")
        if status.leader_generation != generation or generation <= 0:
            raise CutoverFenceError("metering leader generation is stale")

        frozen = await self._freeze_batch(generation, idempotency_key, status)
        published = await self._publish_batch(generation, idempotency_key)
        await self._advance_phase(generation, idempotency_key)
        return CutoverResumeResult(
            status=await self.status(),
            plans_frozen=frozen,
            plans_published=published,
        )

    async def _freeze_batch(
        self, generation: int, request_id: UUID, status: CutoverStatus
    ) -> int:
        if status.phase is not CutoverPhase.LEGACY_DRAINING:
            return 0
        assert status.cutover_at is not None
        candidates = await self._app.fetch(
            _FREEZE_CANDIDATES_SQL,
            status.cutover_at,
            self._freeze_batch_size,
        )
        frozen = 0
        for row in candidates:
            request = LegacyWorkspaceFreezeRequest.from_record(row)
            try:
                events = tuple(
                    await self._ledger.freeze_legacy_workspace_events(request)
                )
                self._validate_frozen_pair(request, events)
                if await self._persist_plan(generation, request_id, request, events):
                    frozen += 1
            except (CutoverError, LegacyWorkspaceLedgerError):
                await self._record_control_error(
                    generation,
                    request_id,
                    CutoverPhase.LEGACY_DRAINING,
                    code="legacy-freeze-failed",
                    workspace_interval_id=request.workspace_interval_id,
                )
                raise
        return frozen

    @staticmethod
    def _validate_frozen_pair(
        request: LegacyWorkspaceFreezeRequest,
        events: Sequence[FrozenLegacyWorkspaceEvent],
    ) -> None:
        if len(events) != 2 or {str(event.payload["unit"]) for event in events} != {
            "vcpu-hour",
            "gib-hour",
        }:
            raise CutoverContractError(
                "legacy ledger must freeze exactly one CPU and one RAM row"
            )
        for event in events:
            event.validate_for(request)
        for field_name in ("user_id", "project_id", "ref_kind", "ref_id"):
            if events[0].payload[field_name] != events[1].payload[field_name]:
                raise CutoverContractError(
                    "legacy frozen pair has inconsistent attribution"
                )
        if events[0].payload["user_id"] is None:
            raise CutoverBlocked("legacy workspace attribution is unavailable")

    async def _persist_plan(
        self,
        generation: int,
        request_id: UUID,
        request: LegacyWorkspaceFreezeRequest,
        events: Sequence[FrozenLegacyWorkspaceEvent],
    ) -> bool:
        event_hash = _event_set_hash(events)
        plan_id = uuid4()
        async with self._app.acquire() as conn:
            async with conn.transaction():
                control = await conn.fetchrow(_CONTROL_SHARE_SQL)
                self._validate_preparing(control, generation, request_id)
                existing = await conn.fetchrow(
                    _EXISTING_PLAN_SQL, request.workspace_interval_id
                )
                if existing is not None:
                    if str(existing["event_set_hash"]) != event_hash:
                        raise CutoverConflictError(
                            "legacy workspace plan changed during replay"
                        )
                    return False
                inserted = await conn.fetchval(
                    _INSERT_PLAN_SQL,
                    plan_id,
                    request.workspace_interval_id,
                    request_id,
                    event_hash,
                    generation,
                )
                if inserted is None:
                    existing = await conn.fetchrow(
                        _EXISTING_PLAN_SQL, request.workspace_interval_id
                    )
                    if (
                        existing is None
                        or str(existing["event_set_hash"]) != event_hash
                    ):
                        raise CutoverConflictError(
                            "legacy workspace plan insert conflicted"
                        )
                    return False
                for ordinal, event in enumerate(
                    sorted(
                        events, key=lambda item: str(item.payload["unit"]), reverse=True
                    )
                ):
                    await conn.execute(
                        _INSERT_PLAN_EVENT_SQL,
                        plan_id,
                        ordinal,
                        event.payload["source"],
                        event.payload["source_id"],
                        event.payload["unit"],
                        _aware_utc(request.ended_at, "ended_at"),
                        event.row_hash,
                        json.dumps(
                            dict(event.payload),
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    )
        return True

    async def _publish_batch(self, generation: int, request_id: UUID) -> int:
        headers = await self._app.fetch(_PENDING_PLANS_SQL, self._publish_batch_size)
        published = 0
        for header in headers:
            event_rows = await self._app.fetch(_PLAN_EVENTS_SQL, header["id"])
            events = tuple(
                FrozenLegacyWorkspaceEvent.from_record(row) for row in event_rows
            )
            if len(events) != int(header["expected_event_count"]):
                raise CutoverConflictError(
                    "legacy workspace plan manifest is incomplete"
                )
            if _event_set_hash(events) != str(header["event_set_hash"]):
                raise CutoverConflictError("legacy workspace plan hash changed")
            try:
                result = await self._ledger.publish_frozen_legacy_workspace_events(
                    events
                )
                if (
                    result.expected != len(events)
                    or result.verified != len(events)
                    or result.inserted < 0
                    or result.inserted > len(events)
                ):
                    raise LegacyWorkspaceLedgerError(
                        "legacy ledger returned an incomplete verification"
                    )
            except LegacyWorkspaceLedgerConflict:
                await self._record_plan_failure(
                    header["id"],
                    generation,
                    request_id,
                    code="legacy-audit-conflict",
                    conflict=True,
                )
                raise
            except Exception:
                await self._record_plan_failure(
                    header["id"],
                    generation,
                    request_id,
                    code="legacy-audit-publication-failed",
                )
                raise
            if await self._finalize_plan(header["id"], generation, request_id):
                published += 1
        return published

    @staticmethod
    def _validate_preparing(
        control: Mapping[str, Any] | None, generation: int, request_id: UUID
    ) -> None:
        InfrastructureWorkspaceCutover._validate_generation(control, generation)
        assert control is not None
        if (
            control["cutover_state"] != "preparing"
            or control["cutover_request_id"] is None
            or UUID(str(control["cutover_request_id"])) != request_id
        ):
            raise CutoverConflictError("durable cutover request is not preparing")

    async def _finalize_plan(
        self, plan_id: UUID, generation: int, request_id: UUID
    ) -> bool:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                control = await conn.fetchrow(_CONTROL_SHARE_SQL)
                self._validate_preparing(control, generation, request_id)
                plan = await conn.fetchrow(_LOCK_PLAN_SQL, plan_id)
                if plan is None:
                    raise CutoverConflictError("legacy workspace plan disappeared")
                if plan["state"] == "published":
                    return False
                if plan["state"] != "planned":
                    raise CutoverConflictError("legacy workspace plan is terminal")
                finalized = await conn.fetchval(
                    _PUBLISH_PLAN_SQL, plan_id, generation, request_id
                )
                if finalized is None:
                    raise CutoverFenceError("legacy plan finalization was fenced")
        return True

    async def _record_plan_failure(
        self,
        plan_id: UUID,
        generation: int,
        request_id: UUID,
        *,
        code: str,
        conflict: bool = False,
    ) -> None:
        await self._app.fetchval(
            _FAIL_PLAN_SQL,
            plan_id,
            generation,
            request_id,
            "conflict" if conflict else "planned",
            json.dumps({"code": code}, separators=(",", ":"), sort_keys=True),
        )

    async def _advance_phase(self, generation: int, request_id: UUID) -> None:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                control = await conn.fetchrow(_CONTROL_LOCK_SQL)
                self._validate_preparing(control, generation, request_id)
                assert control is not None
                cutover_at = _aware_utc(control["cutover_at"], "cutover_at")
                counts = await conn.fetchrow(_DRAIN_COUNTS_SQL, cutover_at)
                if counts is None:
                    raise CutoverConflictError("legacy drain counts are unavailable")
                if int(counts["conflicts"]):
                    raise CutoverBlocked("legacy audit conflict requires resolution")
                if int(counts["open_legacy"]):
                    raise CutoverBlocked("legacy workspace reopened after barrier")
                if int(counts["unplanned"]) or int(counts["planned"]):
                    return

                phase = CutoverPhase(str(control["cutover_phase"]))
                if phase is CutoverPhase.LEGACY_DRAINING:
                    marked = await conn.fetchval(
                        _MARK_READY_SQL, generation, cutover_at, request_id
                    )
                    if marked is None:
                        raise CutoverFenceError("legacy drain completion was fenced")
                    return
                if phase is CutoverPhase.READY_TO_ACTIVATE:
                    self._validate_bootstrap_and_watermark(control, cutover_at)
                    await self._validate_scope_proof(
                        conn,
                        cutover_at,
                        source_cluster=self._source_cluster,
                        namespace_allowlist=self._namespace_allowlist,
                    )
                    activated = await conn.fetchval(
                        _ACTIVATE_SQL, generation, request_id
                    )
                    if activated != "active":
                        raise CutoverFenceError("cutover activation was fenced")

    async def _record_control_error(
        self,
        generation: int,
        request_id: UUID,
        phase: CutoverPhase,
        *,
        code: str,
        workspace_interval_id: int,
    ) -> None:
        await self._app.fetchval(
            _CONTROL_ERROR_SQL,
            generation,
            request_id,
            str(phase),
            json.dumps(
                {
                    "code": code,
                    "workspace_interval_id": workspace_interval_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    @staticmethod
    async def _status_on_connection(conn: asyncpg.Connection) -> CutoverStatus:
        row = await conn.fetchrow(_STATUS_SQL)
        if row is None:
            raise CutoverFenceError("metering control row is missing")
        return InfrastructureWorkspaceCutover._status_from(row)


__all__ = [
    "CutoverBlocked",
    "CutoverConflictError",
    "CutoverContractError",
    "CutoverError",
    "CutoverFenceError",
    "CutoverPhase",
    "CutoverResumeResult",
    "CutoverStatus",
    "FrozenLegacyWorkspaceEvent",
    "InfrastructureWorkspaceCutover",
    "LegacyWorkspaceCutoverLedger",
    "LegacyWorkspaceFreezeRequest",
    "LegacyWorkspaceLedgerConflict",
    "LegacyWorkspaceLedgerError",
    "LegacyWorkspacePublishResult",
    "legacy_workspace_payload_hash",
]
