"""Generation-fenced UTC day sealing for infrastructure usage.

This component is deliberately dark and has no runtime loop.  It closes the
app-side correctness boundary that the typed daily rollup already respects: a
post-cutover day may become immutable only after every required inventory epoch
is complete, every enabled interval cursor covers its overlap, and no
unpublished plan remains.

All checks and ``open -> sealing -> sealed`` transitions occur in one short app
database transaction.  The planner takes a shared lock on the same day row, so
ordinary publication intent cannot appear behind a committed seal.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence
from uuid import UUID

import asyncpg

_UTC = timezone.utc
_SAFETY_LAG = timedelta(minutes=15)
_DEFAULT_ENABLED_RESOURCES = ("workspace_pod",)
_RESOURCE_API_RESOURCES = {
    "workspace_pod": frozenset({"core/v1/pods"}),
    "workspace_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "session_workspace_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "session_agent_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "persistent_agent_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "vm_rootdisk_claim": frozenset({"core/v1/persistentvolumeclaims"}),
    "golden_image_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "platform_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "unclassified_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "unmapped_block_volume": frozenset({"core/v1/persistentvolumes"}),
}
_MAPPED_VOLUME_RESOURCE_RE = re.compile(r"^block_volume_[a-z0-9_]+$")


def _resource_api_resources(resource: str) -> frozenset[str] | None:
    mapped = _RESOURCE_API_RESOURCES.get(resource)
    if mapped is not None:
        return mapped
    if _MAPPED_VOLUME_RESOURCE_RE.fullmatch(resource):
        return frozenset({"core/v1/persistentvolumes"})
    return None


_MANIFEST_VERSION = 1


class DaySealingError(RuntimeError):
    """Base class for day-sealing failures."""


class DaySealingDisabled(DaySealingError):
    """The independent day-sealing runtime gate is disabled."""


class DaySealingFenceError(DaySealingError):
    """The caller does not own the active metering generation."""


class DaySealingContractError(DaySealingError, ValueError):
    """A requested day or persisted row violates the sealing contract."""


class DaySealingBlocked(DaySealingError):
    """Coverage/publication state is not yet sufficient to seal a day."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DaySealDisposition(StrEnum):
    SEALED = "sealed"
    ALREADY_SEALED = "already-sealed"


@dataclass(frozen=True, slots=True)
class DaySealResult:
    day: date
    disposition: DaySealDisposition
    coverage_status: str
    coverage_revision: str
    unknown_ranges: tuple[tuple[datetime, datetime], ...]
    required_scopes: int | None


_CONTROL_SQL = """
/* infra-seal:control */
SELECT leader_generation, cutover_state, cutover_at, statement_timestamp() AS now
FROM infra_metering_control
WHERE singleton = TRUE
FOR SHARE
"""

_EPOCHS_SQL = """
/* infra-seal:epochs */
SELECT epoch.id, epoch.scope_id, epoch.required_from, epoch.reliable_from,
       epoch.continuous_since, epoch.complete_through, epoch.retired_at,
       scope.api_resource
FROM resource_inventory_scope_epochs AS epoch
JOIN resource_inventory_scopes AS scope ON scope.id = epoch.scope_id
WHERE epoch.required_for_rollup = TRUE
  AND epoch.required_from < $2
  AND (epoch.retired_at IS NULL OR epoch.retired_at > $1)
ORDER BY epoch.scope_id, epoch.required_from, epoch.id
FOR SHARE OF epoch
"""

_SEALED_DAY_SQL = """
/* infra-seal:sealed-day */
SELECT day, coverage_status, coverage_revision, unknown_ranges
FROM infra_usage_day_state
WHERE day = $1 AND state = 'sealed'
"""

_GAPS_SQL = """
/* infra-seal:gaps */
SELECT id, scope_epoch_id, gap_start, gap_end, resolution
FROM resource_inventory_coverage_gaps
WHERE scope_epoch_id = ANY($1::uuid[])
  AND gap_start < $3
  AND (gap_end IS NULL OR gap_end > $2)
ORDER BY scope_epoch_id, gap_start, id
"""

_STORAGE_GAPS_SQL = """
/* infra-seal:storage-gaps */
SELECT id, scope_epoch_id, gap_start, gap_end, resolution
FROM resource_inventory_coverage_gaps
WHERE scope_epoch_id = ANY($1::uuid[])
  AND gap_start < $3
  AND (gap_end IS NULL OR gap_end > $2)

UNION ALL

SELECT id, scope_epoch_id, gap_start, gap_end,
       CASE WHEN resolution = 'unresolved' THEN 'unresolved'
            ELSE 'waived' END AS resolution
FROM storage_asset_coverage_gaps
WHERE scope_epoch_id = ANY($1::uuid[])
  AND gap_start < $3
  AND (gap_end IS NULL OR gap_end > $2)
ORDER BY scope_epoch_id, gap_start, id
"""

_ENSURE_DAY_SQL = """
/* infra-seal:ensure-day */
INSERT INTO infra_usage_day_state (day)
VALUES ($1)
ON CONFLICT (day) DO NOTHING
"""

_LOCK_DAY_SQL = """
/* infra-seal:lock-day */
SELECT day, state, coverage_status, coverage_revision, unknown_ranges
FROM infra_usage_day_state
WHERE day = $1
FOR UPDATE
"""

_INTERVAL_BLOCKER_SQL = """
/* infra-seal:interval-blocker */
SELECT interval.id
FROM resource_intervals AS interval
WHERE interval.resource = ANY($3::text[])
  AND interval.started_at < $2
  AND COALESCE(interval.ended_at, 'infinity'::timestamptz) > $1
  AND interval.materialized_through < LEAST(
      $2,
      COALESCE(interval.ended_at, $2)
  )
ORDER BY interval.id
LIMIT 1
"""

_PLAN_BLOCKER_SQL = """
/* infra-seal:plan-blocker */
SELECT plan.id
FROM resource_publication_plans AS plan
JOIN resource_intervals AS interval ON interval.id = plan.source_interval_id
WHERE interval.resource = ANY($3::text[])
  AND tstzrange(plan.period_start, plan.period_end, '[)')
      && tstzrange($1, $2, '[)')
  AND plan.state <> 'published'
ORDER BY plan.period_start, plan.id
LIMIT 1
"""

_ITEM_BLOCKER_SQL = """
/* infra-seal:item-blocker */
WITH required AS (
    SELECT epoch.id AS scope_epoch_id,
           GREATEST($2, epoch.required_from) AS window_start,
           LEAST($3, COALESCE(epoch.retired_at, $3)) AS window_end,
           CASE
               WHEN epoch.complete_through IS NULL THEN NULL
               ELSE LEAST(epoch.complete_through, $4)
           END AS evidence_through
    FROM resource_inventory_scope_epochs AS epoch
    WHERE epoch.id = ANY($1::uuid[])
), bounded AS (
    SELECT required.*,
           CASE
               WHEN evidence_through IS NULL THEN NULL
               ELSE LEAST(window_end, evidence_through)
           END AS known_end
    FROM required
), snapshot_boundaries AS (
    SELECT bounded.*,
           baseline.received_at AS baseline_at,
           boundary.received_at AS boundary_at
    FROM bounded
    LEFT JOIN LATERAL (
        SELECT max(snapshot.received_at) AS received_at
        FROM resource_inventory_snapshots AS snapshot
        WHERE snapshot.scope_epoch_id = bounded.scope_epoch_id
          AND snapshot.complete = TRUE
          AND snapshot.manifest_state IN ('sealed', 'items-expired')
          AND snapshot.received_at < bounded.window_start
    ) AS baseline ON TRUE
    LEFT JOIN LATERAL (
        SELECT min(snapshot.received_at) AS received_at
        FROM resource_inventory_snapshots AS snapshot
        WHERE snapshot.scope_epoch_id = bounded.scope_epoch_id
          AND snapshot.complete = TRUE
          AND snapshot.manifest_state IN ('sealed', 'items-expired')
          AND bounded.known_end > bounded.window_start
          AND snapshot.received_at >= bounded.known_end
          AND snapshot.received_at <= bounded.evidence_through
    ) AS boundary ON TRUE
), relevant_snapshots AS (
    -- Every manifest tied at a boundary is evidence; UUID ordering must not
    -- silently choose a clean sibling at the same server receipt timestamp.
    SELECT snapshot.id, snapshot.item_errors
    FROM snapshot_boundaries AS boundary
    JOIN resource_inventory_snapshots AS snapshot
      ON snapshot.scope_epoch_id = boundary.scope_epoch_id
     AND snapshot.received_at = boundary.baseline_at
    WHERE snapshot.complete = TRUE
      AND snapshot.manifest_state IN ('sealed', 'items-expired')

    UNION ALL

    SELECT snapshot.id, snapshot.item_errors
    FROM snapshot_boundaries AS boundary
    JOIN resource_inventory_snapshots AS snapshot
      ON snapshot.scope_epoch_id = boundary.scope_epoch_id
     AND snapshot.received_at >= boundary.window_start
     AND snapshot.received_at < boundary.known_end
    WHERE snapshot.complete = TRUE
      AND snapshot.manifest_state IN ('sealed', 'items-expired')

    UNION ALL

    SELECT snapshot.id, snapshot.item_errors
    FROM snapshot_boundaries AS boundary
    JOIN resource_inventory_snapshots AS snapshot
      ON snapshot.scope_epoch_id = boundary.scope_epoch_id
     AND snapshot.received_at = boundary.boundary_at
    WHERE snapshot.complete = TRUE
      AND snapshot.manifest_state IN ('sealed', 'items-expired')
), latest_prestart_watch AS (
    SELECT boundary.scope_epoch_id, event.source_kind, event.source_uid,
           max(event.received_at) AS received_at
    FROM snapshot_boundaries AS boundary
    JOIN resource_inventory_watch_events AS event
      ON event.scope_epoch_id = boundary.scope_epoch_id
     AND event.source_uid IS NOT NULL
     AND boundary.baseline_at IS NOT NULL
     AND event.received_at >= boundary.baseline_at
     AND event.received_at < boundary.window_start
    GROUP BY boundary.scope_epoch_id, event.source_kind, event.source_uid
), blockers AS (
    SELECT 1 AS priority, 'invalid-inventory-items'::text AS blocker_kind,
           snapshot.id AS id
    FROM relevant_snapshots AS snapshot
    WHERE jsonb_array_length(snapshot.item_errors) > 0

    UNION ALL

    SELECT 1, 'invalid-inventory-items', event.id
    FROM latest_prestart_watch AS latest
    JOIN resource_inventory_watch_events AS event
      ON event.scope_epoch_id = latest.scope_epoch_id
     AND event.source_kind = latest.source_kind
     AND event.source_uid = latest.source_uid
     AND event.received_at = latest.received_at
    WHERE event.valid_for_metering IS FALSE
      AND event.mutation_action = 'presence-invalid'

    UNION ALL

    SELECT 1, 'invalid-inventory-items', event.id
    FROM bounded
    JOIN resource_inventory_watch_events AS event
      ON event.scope_epoch_id = bounded.scope_epoch_id
     AND event.received_at >= bounded.window_start
     AND event.received_at < bounded.known_end
    WHERE event.valid_for_metering IS FALSE
      AND event.mutation_action = 'presence-invalid'

    UNION ALL

    SELECT 2, 'inventory-boundary-evidence-missing', boundary.scope_epoch_id
    FROM snapshot_boundaries AS boundary
    WHERE boundary.known_end > boundary.window_start
      AND boundary.boundary_at IS NULL
)
SELECT blocker_kind, id
FROM blockers
ORDER BY priority, blocker_kind, id
LIMIT 1
"""

_START_SEAL_SQL = """
/* infra-seal:start */
UPDATE infra_usage_day_state
SET state = 'sealing', updated_at = statement_timestamp()
WHERE day = $1 AND state = 'open'
RETURNING state
"""

_FINISH_SEAL_SQL = """
/* infra-seal:finish */
UPDATE infra_usage_day_state AS day_state
SET state = 'sealed',
    coverage_status = $2,
    coverage_revision = $3,
    unknown_ranges = $4::jsonb,
    sealed_at = statement_timestamp(),
    updated_at = statement_timestamp()
WHERE day_state.day = $1
  AND day_state.state = 'sealing'
  AND EXISTS (
      SELECT 1
      FROM infra_metering_control AS control
      WHERE control.singleton = TRUE
        AND control.leader_generation = $5
        AND control.cutover_state = 'active'
  )
RETURNING day_state.coverage_status, day_state.coverage_revision,
          day_state.unknown_ranges
"""


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DaySealingContractError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DaySealingContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(_UTC)


def _midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=_UTC)


def _timestamp_text(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _uuid_text(value: Any, field_name: str) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DaySealingContractError(f"{field_name} must be a UUID") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _timestamp_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise DaySealingContractError(
        f"unsupported coverage manifest value: {type(value).__name__}"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _clip_range(
    start: datetime,
    end: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    clipped_start = max(start, window_start)
    clipped_end = window_end if end is None else min(end, window_end)
    return None if clipped_end <= clipped_start else (clipped_start, clipped_end)


def _merge_ranges(
    values: Sequence[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(values):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _range_is_covered(
    target: tuple[datetime, datetime],
    evidence: Sequence[tuple[datetime, datetime]],
) -> bool:
    cursor = target[0]
    for start, end in sorted(evidence):
        clipped = _clip_range(start, end, target[0], target[1])
        if clipped is None or clipped[1] <= cursor:
            continue
        if clipped[0] > cursor:
            return False
        cursor = max(cursor, clipped[1])
        if cursor >= target[1]:
            return True
    return cursor >= target[1]


def _parse_unknown_ranges(value: Any) -> tuple[tuple[datetime, datetime], ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DaySealingContractError(
                "sealed unknown_ranges is invalid JSON"
            ) from exc
    if not isinstance(value, list):
        raise DaySealingContractError("sealed unknown_ranges must be an array")
    parsed: list[tuple[datetime, datetime]] = []
    for item in value:
        if not isinstance(item, Mapping) or not {"start", "end"} <= item.keys():
            raise DaySealingContractError("sealed unknown range has invalid shape")
        try:
            start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise DaySealingContractError(
                "sealed unknown range timestamp is invalid"
            ) from exc
        clipped = (_aware_utc(start, "unknown start"), _aware_utc(end, "unknown end"))
        if clipped[1] <= clipped[0]:
            raise DaySealingContractError("sealed unknown range is empty")
        parsed.append(clipped)
    return tuple(parsed)


class InfrastructureUsageDaySealer:
    """Seal one UTC day after exact app-side coverage verification."""

    def __init__(
        self,
        app_pool: asyncpg.Pool,
        *,
        sealing_enabled: bool = False,
        enabled_resources: Sequence[str] = _DEFAULT_ENABLED_RESOURCES,
        safety_lag: timedelta = _SAFETY_LAG,
    ) -> None:
        resources = tuple(dict.fromkeys(str(value) for value in enabled_resources))
        if not resources or any(not value for value in resources):
            raise ValueError("day sealer requires a non-empty resource allowlist")
        if safety_lag < timedelta(0):
            raise ValueError("day sealer safety lag cannot be negative")
        unsupported = sorted(
            resource
            for resource in set(resources)
            if _resource_api_resources(resource) is None
        )
        if unsupported:
            raise ValueError(
                "day sealer has no inventory-scope mapping for: "
                + ", ".join(unsupported)
            )
        self._app = app_pool
        self._sealing_enabled = sealing_enabled
        self._enabled_resources = resources
        self._include_storage_asset_gaps = any(
            "core/v1/persistentvolumes" in (_resource_api_resources(resource) or ())
            for resource in resources
        )
        self._safety_lag = safety_lag

    def _require_enabled(self) -> None:
        if not self._sealing_enabled:
            raise DaySealingDisabled("infrastructure day-sealing gate is disabled")

    @staticmethod
    def _validate_control(
        control: Mapping[str, Any] | None,
        generation: int,
    ) -> tuple[datetime, datetime]:
        if control is None:
            raise DaySealingFenceError("metering control row is missing")
        if generation <= 0 or int(control["leader_generation"]) != generation:
            raise DaySealingFenceError("metering leader generation is stale")
        if control["cutover_state"] != "active" or control["cutover_at"] is None:
            raise DaySealingDisabled("infrastructure cutover is not active")
        return (
            _aware_utc(control["cutover_at"], "cutover_at"),
            _aware_utc(control["now"], "statement_timestamp"),
        )

    async def seal_day(self, day: date, generation: int) -> DaySealResult:
        self._require_enabled()
        if not isinstance(day, date) or isinstance(day, datetime):
            raise DaySealingContractError("day must be a date")

        async with self._app.acquire() as conn:
            async with conn.transaction():
                cutover_at, observed_at = self._validate_control(
                    await conn.fetchrow(_CONTROL_SQL),
                    generation,
                )
                day_start = _midnight(day)
                day_end = _midnight(day + timedelta(days=1))
                already_sealed = await conn.fetchrow(_SEALED_DAY_SQL, day)
                if already_sealed is not None:
                    return self._sealed_result(day, already_sealed)
                if day < cutover_at.date():
                    raise DaySealingContractError("cannot seal a pre-cutover day")
                if day_end > observed_at - self._safety_lag:
                    raise DaySealingBlocked("day-not-closeable")
                seal_start = max(day_start, cutover_at)

                epochs = await conn.fetch(_EPOCHS_SQL, seal_start, day_end)
                scope_ids = {str(row["scope_id"]) for row in epochs}
                if not scope_ids:
                    raise DaySealingBlocked("no-required-inventory-source")
                epoch_ids = [row["id"] for row in epochs]
                gaps = await conn.fetch(
                    _STORAGE_GAPS_SQL
                    if self._include_storage_asset_gaps
                    else _GAPS_SQL,
                    epoch_ids,
                    seal_start,
                    day_end,
                )

                await conn.execute(_ENSURE_DAY_SQL, day)
                current = await conn.fetchrow(_LOCK_DAY_SQL, day)
                if current is None:
                    raise DaySealingContractError("usage day state disappeared")
                if current["state"] == "sealed":
                    return self._sealed_result(day, current)
                if current["state"] == "sealing":
                    raise DaySealingContractError(
                        "usage day is stranded in sealing state"
                    )
                if current["state"] != "open":
                    raise DaySealingContractError("usage day has unknown state")

                epoch_manifest, missing_by_epoch = self._validate_epochs(
                    epochs,
                    seal_start=seal_start,
                    day_end=day_end,
                )
                gap_manifest, unknown_ranges, evidence_by_epoch = self._validate_gaps(
                    gaps,
                    seal_start=seal_start,
                    day_end=day_end,
                )
                for epoch_id, missing_ranges in missing_by_epoch.items():
                    evidence = evidence_by_epoch.get(epoch_id, ())
                    if any(
                        not _range_is_covered(missing, evidence)
                        for missing in missing_ranges
                    ):
                        raise DaySealingBlocked("required-source-incomplete")
                item_blocker = await conn.fetchrow(
                    _ITEM_BLOCKER_SQL,
                    epoch_ids,
                    seal_start,
                    day_end,
                    observed_at,
                )
                if item_blocker:
                    # A newly seen invalid object has no interval for the cursor
                    # blocker to find. Keep it visible as a hard seal blocker
                    # until item-specific, explicitly waivable gaps exist.
                    raise DaySealingBlocked(
                        str(item_blocker.get("blocker_kind", "invalid-inventory-items"))
                    )
                if await conn.fetchrow(
                    _INTERVAL_BLOCKER_SQL,
                    seal_start,
                    day_end,
                    list(self._enabled_resources),
                ):
                    raise DaySealingBlocked("interval-materialization-incomplete")
                if await conn.fetchrow(
                    _PLAN_BLOCKER_SQL,
                    seal_start,
                    day_end,
                    list(self._enabled_resources),
                ):
                    raise DaySealingBlocked("publication-plan-unresolved")

                coverage_status = "partial" if unknown_ranges else "complete"
                manifest = {
                    "version": _MANIFEST_VERSION,
                    "day": day,
                    "cutover_at": cutover_at,
                    "seal_start": seal_start,
                    "day_end": day_end,
                    "enabled_resources": list(self._enabled_resources),
                    "epochs": epoch_manifest,
                    "gaps": gap_manifest,
                    "coverage_status": coverage_status,
                    "unknown_ranges": [
                        {"start": start, "end": end} for start, end in unknown_ranges
                    ],
                }
                manifest_json = _canonical_json(manifest)
                revision = (
                    "seal-v1:"
                    + hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
                )
                unknown_json = _canonical_json(
                    [{"start": start, "end": end} for start, end in unknown_ranges]
                )

                started = await conn.fetchval(_START_SEAL_SQL, day)
                if started != "sealing":
                    raise DaySealingFenceError("usage day changed before sealing")
                sealed = await conn.fetchrow(
                    _FINISH_SEAL_SQL,
                    day,
                    coverage_status,
                    revision,
                    unknown_json,
                    generation,
                )
                if sealed is None:
                    raise DaySealingFenceError(
                        "metering generation changed before seal commit"
                    )
                return DaySealResult(
                    day=day,
                    disposition=DaySealDisposition.SEALED,
                    coverage_status=coverage_status,
                    coverage_revision=revision,
                    unknown_ranges=unknown_ranges,
                    required_scopes=len(scope_ids),
                )

    @staticmethod
    def _sealed_result(
        day: date,
        row: Mapping[str, Any],
    ) -> DaySealResult:
        status = str(row["coverage_status"])
        revision = str(row["coverage_revision"])
        if status not in {"complete", "partial"} or not revision:
            raise DaySealingContractError("sealed usage day proof is incomplete")
        return DaySealResult(
            day=day,
            disposition=DaySealDisposition.ALREADY_SEALED,
            coverage_status=status,
            coverage_revision=revision,
            unknown_ranges=_parse_unknown_ranges(row["unknown_ranges"]),
            # The v1 day-state row predates manifest persistence. Returning no
            # count keeps replay independent of mutable inventory configuration.
            required_scopes=None,
        )

    def _validate_epochs(
        self,
        epochs: Sequence[Mapping[str, Any]],
        *,
        seal_start: datetime,
        day_end: datetime,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, list[tuple[datetime, datetime]]],
    ]:
        manifest: list[dict[str, Any]] = []
        missing_by_epoch: dict[str, list[tuple[datetime, datetime]]] = {}
        enabled_api_resources = set().union(
            *(
                _resource_api_resources(resource) or frozenset()
                for resource in self._enabled_resources
            )
        )
        for row in epochs:
            api_resource = str(row.get("api_resource") or "")
            if api_resource not in enabled_api_resources:
                raise DaySealingBlocked("required-source-not-enabled")
            required_from = _aware_utc(row["required_from"], "required_from")
            retired_at = (
                None
                if row.get("retired_at") is None
                else _aware_utc(row["retired_at"], "retired_at")
            )
            effective_start = max(seal_start, required_from)
            effective_end = min(day_end, retired_at or day_end)
            reliable_from = row.get("reliable_from")
            continuous_since = row.get("continuous_since")
            if (
                reliable_from is None
                or _aware_utc(reliable_from, "reliable_from") > effective_start
                or continuous_since is None
                or _aware_utc(continuous_since, "continuous_since") > effective_start
            ):
                raise DaySealingBlocked("required-source-incomplete")
            complete_through = row.get("complete_through")
            complete_end = effective_start
            if complete_through is not None:
                complete_end = max(
                    effective_start,
                    min(
                        effective_end,
                        _aware_utc(complete_through, "complete_through"),
                    ),
                )
            epoch_id = _uuid_text(row["id"], "epoch.id")
            if complete_end < effective_end:
                missing_by_epoch.setdefault(epoch_id, []).append(
                    (complete_end, effective_end)
                )
            manifest.append(
                {
                    "id": epoch_id,
                    "scope_id": _uuid_text(row["scope_id"], "epoch.scope_id"),
                    "api_resource": api_resource,
                    "required_from": required_from,
                    "reliable_from": _aware_utc(reliable_from, "reliable_from"),
                    "continuous_since": _aware_utc(
                        continuous_since,
                        "continuous_since",
                    ),
                    "complete_through": (
                        None
                        if complete_through is None
                        else _aware_utc(complete_through, "complete_through")
                    ),
                    "retired_at": retired_at,
                    "effective_start": effective_start,
                    "effective_end": effective_end,
                }
            )
        return manifest, missing_by_epoch

    @staticmethod
    def _validate_gaps(
        gaps: Sequence[Mapping[str, Any]],
        *,
        seal_start: datetime,
        day_end: datetime,
    ) -> tuple[
        list[dict[str, Any]],
        tuple[tuple[datetime, datetime], ...],
        dict[str, list[tuple[datetime, datetime]]],
    ]:
        manifest: list[dict[str, Any]] = []
        waived: list[tuple[datetime, datetime]] = []
        evidence_by_epoch: dict[str, list[tuple[datetime, datetime]]] = {}
        for row in gaps:
            resolution = str(row["resolution"])
            gap_start = _aware_utc(row["gap_start"], "gap_start")
            gap_end = (
                None
                if row.get("gap_end") is None
                else _aware_utc(row["gap_end"], "gap_end")
            )
            clipped = _clip_range(gap_start, gap_end, seal_start, day_end)
            if clipped is None:
                continue
            if resolution == "unresolved":
                raise DaySealingBlocked("unresolved-coverage-gap")
            if resolution == "waived":
                waived.append(clipped)
            elif resolution != "backfilled":
                raise DaySealingContractError("coverage gap has unknown resolution")
            epoch_id = _uuid_text(row["scope_epoch_id"], "gap.scope_epoch_id")
            evidence_by_epoch.setdefault(epoch_id, []).append(clipped)
            manifest.append(
                {
                    "id": _uuid_text(row["id"], "gap.id"),
                    "scope_epoch_id": epoch_id,
                    "start": gap_start,
                    "end": gap_end,
                    "resolution": resolution,
                }
            )
        return manifest, _merge_ranges(waived), evidence_by_epoch


__all__ = [
    "DaySealDisposition",
    "DaySealResult",
    "DaySealingBlocked",
    "DaySealingContractError",
    "DaySealingDisabled",
    "DaySealingError",
    "DaySealingFenceError",
    "InfrastructureUsageDaySealer",
]
