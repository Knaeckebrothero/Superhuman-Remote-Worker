"""Fleet-admin coverage-gap waivers and fail-closed sealed-day degradation.

A waiver is an explicit acceptance that one bounded inventory gap cannot be
backfilled.  It never turns missing evidence into complete coverage: every
already-sealed UTC day intersecting the gap is monotonically degraded to
``partial`` in the same app-database transaction.

The 0094 day-state trigger permits only append-only unknown evidence and an
exact ``coverage_sequence + 1`` transition.  This module therefore preserves
existing JSON evidence byte-for-byte, appends one clipped range only when it is
not already covered, and derives a content-addressed replacement revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

import asyncpg

_UTC = timezone.utc
_REVISION_DOMAIN = "coverage-gap-waiver-v1"
_MAX_REASON_LENGTH = 2048
_REASON_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CoverageGapWaiverError(RuntimeError):
    """Base class for coverage-gap waiver failures."""


class CoverageGapNotFound(CoverageGapWaiverError):
    """The requested coverage gap does not exist."""


class CoverageGapConflict(CoverageGapWaiverError):
    """The gap was resolved by a different request or cannot be waived."""


class CoverageGapContractError(CoverageGapWaiverError, ValueError):
    """The request or persisted coverage state violates the waiver contract."""


@dataclass(frozen=True, slots=True)
class CoverageDayDegradation:
    day: date
    coverage_sequence: int
    coverage_revision: str
    added_range: tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class CoverageGapWaiverResult:
    gap_id: UUID
    actor_id: UUID
    idempotency_key: UUID
    reason: str
    resolved_at: datetime
    replayed: bool
    degraded_days: tuple[CoverageDayDegradation, ...]


_LOCK_GAP_SQL = """
/* infra-coverage:lock-gap */
SELECT gap.id, gap.scope_epoch_id, gap.gap_start, gap.gap_end,
       gap.resolution, gap.resolution_details, gap.resolved_at,
       gap.resolved_by, transaction_timestamp() AS transaction_time
FROM resource_inventory_coverage_gaps AS gap
WHERE gap.id = $1
FOR UPDATE
"""

_WAIVE_GAP_SQL = """
/* infra-coverage:waive-gap */
UPDATE resource_inventory_coverage_gaps AS gap
SET resolution = 'waived',
    resolution_details = $2::jsonb,
    resolved_at = $3,
    resolved_by = $4,
    updated_at = statement_timestamp()
WHERE gap.id = $1
  AND gap.resolution = 'unresolved'
RETURNING gap.resolved_at
"""

_SEALED_DAYS_SQL = """
/* infra-coverage:sealed-days */
SELECT day_state.day, day_state.coverage_status,
       day_state.coverage_revision, day_state.coverage_sequence,
       day_state.unknown_ranges, control.cutover_at
FROM infra_usage_day_state AS day_state
JOIN infra_metering_control AS control ON control.singleton = TRUE
WHERE day_state.state = 'sealed'
  AND day_state.day >= $1
  AND day_state.day <= $2
ORDER BY day_state.day
FOR UPDATE OF day_state
"""

_DEGRADE_DAY_SQL = """
/* infra-coverage:degrade-day */
UPDATE infra_usage_day_state AS day_state
SET coverage_status = 'partial',
    coverage_revision = $2,
    coverage_sequence = $3,
    unknown_ranges = $4::jsonb,
    updated_at = statement_timestamp()
WHERE day_state.day = $1
  AND day_state.state = 'sealed'
  AND day_state.coverage_sequence = $5
  AND day_state.coverage_revision = $6
RETURNING day_state.coverage_sequence, day_state.coverage_revision
"""


def _uuid(value: Any, field_name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CoverageGapContractError(f"{field_name} must be a UUID") from exc


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise CoverageGapContractError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CoverageGapContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(_UTC)


def _timestamp_text(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_UTC)


def _reason(value: Any) -> str:
    if not isinstance(value, str):
        raise CoverageGapContractError("waiver reason must be text")
    normalized = value.strip()
    if not normalized:
        raise CoverageGapContractError("waiver reason is required")
    if _REASON_CONTROL.search(normalized):
        raise CoverageGapContractError("waiver reason must be printable")
    if len(normalized) > _MAX_REASON_LENGTH:
        raise CoverageGapContractError(
            f"waiver reason exceeds {_MAX_REASON_LENGTH} characters"
        )
    return normalized


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CoverageGapContractError(f"{field_name} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise CoverageGapContractError(f"{field_name} must be an object")
    result = dict(value)
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CoverageGapContractError(
            f"{field_name} is not canonical JSON data"
        ) from exc
    return result


def _unknown_ranges(
    value: Any,
) -> tuple[list[dict[str, Any]], tuple[tuple[datetime, datetime], ...]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CoverageGapContractError(
                "sealed unknown_ranges is invalid JSON"
            ) from exc
    if not isinstance(value, list):
        raise CoverageGapContractError("sealed unknown_ranges must be an array")

    preserved: list[dict[str, Any]] = []
    parsed: list[tuple[datetime, datetime]] = []
    for item in value:
        if not isinstance(item, Mapping) or not {"start", "end"} <= item.keys():
            raise CoverageGapContractError("sealed unknown range has invalid shape")
        try:
            start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CoverageGapContractError(
                "sealed unknown range timestamp is invalid"
            ) from exc
        interval = (
            _aware_utc(start, "unknown range start"),
            _aware_utc(end, "unknown range end"),
        )
        if interval[1] <= interval[0]:
            raise CoverageGapContractError("sealed unknown range is empty")
        preserved.append(dict(item))
        parsed.append(interval)
    return preserved, tuple(parsed)


def _range_is_covered(
    target: tuple[datetime, datetime],
    evidence: Sequence[tuple[datetime, datetime]],
) -> bool:
    cursor = target[0]
    for start, end in sorted(evidence):
        clipped_start = max(start, target[0])
        clipped_end = min(end, target[1])
        if clipped_end <= cursor:
            continue
        if clipped_start > cursor:
            return False
        cursor = max(cursor, clipped_end)
        if cursor >= target[1]:
            return True
    return cursor >= target[1]


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CoverageGapContractError(
            "coverage degradation manifest is not canonical JSON data"
        ) from exc


def _degraded_revision(
    *,
    day: date,
    previous_revision: str,
    next_sequence: int,
    unknown_ranges: Sequence[Mapping[str, Any]],
    gap_id: UUID,
    idempotency_key: UUID,
    gap_start: datetime,
    gap_end: datetime,
) -> str:
    manifest = {
        "version": 1,
        "day": day.isoformat(),
        "previous_coverage_revision": previous_revision,
        "coverage_sequence": next_sequence,
        "coverage_status": "partial",
        "unknown_ranges": list(unknown_ranges),
        "cause": {
            "kind": "coverage-gap-waiver",
            "gap_id": str(gap_id),
            "idempotency_key": str(idempotency_key),
            "gap_start": _timestamp_text(gap_start),
            "gap_end": _timestamp_text(gap_end),
        },
    }
    digest = hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()
    return f"{_REVISION_DOMAIN}:{digest}"


async def degrade_sealed_days_for_gap(
    conn: asyncpg.Connection,
    *,
    gap_id: UUID,
    idempotency_key: UUID,
    gap_start: datetime,
    gap_end: datetime,
) -> tuple[CoverageDayDegradation, ...]:
    """Append a bounded gap to every intersecting sealed day exactly once.

    The caller must invoke this inside the same transaction that establishes
    the late evidence. Day rows are locked in ascending order. Existing range
    objects are never rewritten, which satisfies the 0094 JSONB containment
    guard and preserves their original evidence.
    """

    gap_id = _uuid(gap_id, "gap_id")
    idempotency_key = _uuid(idempotency_key, "idempotency_key")
    gap_start = _aware_utc(gap_start, "gap_start")
    gap_end = _aware_utc(gap_end, "gap_end")
    if gap_end <= gap_start:
        raise CoverageGapContractError("coverage gap range is empty")

    first_day = gap_start.date()
    last_day = (gap_end - timedelta(microseconds=1)).date()
    rows = await conn.fetch(_SEALED_DAYS_SQL, first_day, last_day)
    degraded: list[CoverageDayDegradation] = []
    previous_day: date | None = None
    for row in rows:
        day = row.get("day")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise CoverageGapContractError("sealed coverage day is invalid")
        if previous_day is not None and day <= previous_day:
            raise CoverageGapContractError(
                "sealed coverage days are not strictly ordered"
            )
        previous_day = day

        day_start = _midnight(day)
        day_end = _midnight(day + timedelta(days=1))
        cutover_at = row.get("cutover_at")
        if cutover_at is not None:
            day_start = max(day_start, _aware_utc(cutover_at, "cutover_at"))
        clipped = (max(gap_start, day_start), min(gap_end, day_end))
        if clipped[1] <= clipped[0]:
            continue

        raw_ranges, parsed_ranges = _unknown_ranges(row.get("unknown_ranges"))
        status = row.get("coverage_status")
        revision = row.get("coverage_revision")
        sequence = row.get("coverage_sequence")
        if status not in {"complete", "partial"}:
            raise CoverageGapContractError("sealed coverage day has an invalid status")
        if not isinstance(revision, str) or not revision:
            raise CoverageGapContractError(
                "sealed coverage day has no coverage revision"
            )
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise CoverageGapContractError(
                "sealed coverage day has an invalid coverage sequence"
            )

        range_is_covered = _range_is_covered(clipped, parsed_ranges)
        if range_is_covered and status == "partial":
            continue

        added = {
            "start": _timestamp_text(clipped[0]),
            "end": _timestamp_text(clipped[1]),
        }
        # A legacy/corrupt ``complete`` row may already contain unknown evidence.
        # 0094 permits its fail-closed status repair without duplicating that
        # evidence, while ordinary additions append an exact object so JSONB
        # containment remains monotonic.
        next_ranges = raw_ranges if range_is_covered else [*raw_ranges, added]
        next_sequence = sequence + 1
        next_revision = _degraded_revision(
            day=day,
            previous_revision=revision,
            next_sequence=next_sequence,
            unknown_ranges=next_ranges,
            gap_id=gap_id,
            idempotency_key=idempotency_key,
            gap_start=gap_start,
            gap_end=gap_end,
        )
        updated = await conn.fetchrow(
            _DEGRADE_DAY_SQL,
            day,
            next_revision,
            next_sequence,
            _canonical_json(next_ranges),
            sequence,
            revision,
        )
        if updated is None:
            raise CoverageGapConflict(
                "sealed coverage day changed during gap degradation"
            )
        if (
            int(updated["coverage_sequence"]) != next_sequence
            or str(updated["coverage_revision"]) != next_revision
        ):
            raise CoverageGapContractError(
                "sealed coverage day returned an unexpected revision"
            )
        degraded.append(
            CoverageDayDegradation(
                day=day,
                coverage_sequence=next_sequence,
                coverage_revision=next_revision,
                added_range=clipped,
            )
        )
    return tuple(degraded)


class CoverageGapWaiverService:
    """Transactionally waive one bounded inventory coverage gap."""

    def __init__(self, app_pool: asyncpg.Pool) -> None:
        self._app = app_pool

    async def waive(
        self,
        gap_id: UUID,
        actor_id: UUID,
        reason: str,
        idempotency_key: UUID,
    ) -> CoverageGapWaiverResult:
        gap_id = _uuid(gap_id, "gap_id")
        actor_id = _uuid(actor_id, "actor_id")
        idempotency_key = _uuid(idempotency_key, "idempotency_key")
        reason = _reason(reason)

        async with self._app.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_LOCK_GAP_SQL, gap_id)
                if row is None:
                    raise CoverageGapNotFound("coverage gap does not exist")

                gap_start = _aware_utc(row["gap_start"], "gap_start")
                if row.get("gap_end") is None:
                    raise CoverageGapContractError(
                        "an open-ended coverage gap cannot be waived"
                    )
                gap_end = _aware_utc(row["gap_end"], "gap_end")
                if gap_end <= gap_start:
                    raise CoverageGapContractError("coverage gap range is empty")
                details = _json_object(
                    row.get("resolution_details"), "resolution_details"
                )
                resolution = str(row.get("resolution"))

                if resolution != "unresolved":
                    if not self._is_exact_replay(
                        row,
                        details,
                        actor_id=actor_id,
                        reason=reason,
                        idempotency_key=idempotency_key,
                    ):
                        raise CoverageGapConflict(
                            "coverage gap was resolved by a different request"
                        )
                    resolved_at = _aware_utc(row["resolved_at"], "resolved_at")
                    degraded = await degrade_sealed_days_for_gap(
                        conn,
                        gap_id=gap_id,
                        idempotency_key=idempotency_key,
                        gap_start=gap_start,
                        gap_end=gap_end,
                    )
                    return CoverageGapWaiverResult(
                        gap_id=gap_id,
                        actor_id=actor_id,
                        idempotency_key=idempotency_key,
                        reason=reason,
                        resolved_at=resolved_at,
                        replayed=True,
                        degraded_days=degraded,
                    )

                if "waiver" in details:
                    raise CoverageGapContractError(
                        "unresolved coverage gap already carries waiver provenance"
                    )
                resolved_at = _aware_utc(row["transaction_time"], "transaction_time")
                details["waiver"] = {
                    "actor_id": str(actor_id),
                    "reason": reason,
                    "idempotency_key": str(idempotency_key),
                    "resolved_at": _timestamp_text(resolved_at),
                }
                updated = await conn.fetchrow(
                    _WAIVE_GAP_SQL,
                    gap_id,
                    _canonical_json(details),
                    resolved_at,
                    actor_id,
                )
                if updated is None:
                    raise CoverageGapConflict(
                        "coverage gap changed before waiver commit"
                    )
                persisted_at = _aware_utc(updated["resolved_at"], "resolved_at")
                if persisted_at != resolved_at:
                    raise CoverageGapContractError(
                        "coverage gap returned an unexpected resolution time"
                    )

                degraded = await degrade_sealed_days_for_gap(
                    conn,
                    gap_id=gap_id,
                    idempotency_key=idempotency_key,
                    gap_start=gap_start,
                    gap_end=gap_end,
                )
                return CoverageGapWaiverResult(
                    gap_id=gap_id,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    reason=reason,
                    resolved_at=resolved_at,
                    replayed=False,
                    degraded_days=degraded,
                )

    @staticmethod
    def _is_exact_replay(
        row: Mapping[str, Any],
        details: Mapping[str, Any],
        *,
        actor_id: UUID,
        reason: str,
        idempotency_key: UUID,
    ) -> bool:
        if row.get("resolution") != "waived" or row.get("resolved_at") is None:
            return False
        try:
            resolved_by = _uuid(row.get("resolved_by"), "resolved_by")
        except CoverageGapContractError:
            return False
        waiver = details.get("waiver")
        if not isinstance(waiver, Mapping):
            return False
        return (
            resolved_by == actor_id
            and waiver.get("actor_id") == str(actor_id)
            and waiver.get("reason") == reason
            and waiver.get("idempotency_key") == str(idempotency_key)
        )


__all__ = [
    "CoverageDayDegradation",
    "CoverageGapConflict",
    "CoverageGapContractError",
    "CoverageGapNotFound",
    "CoverageGapWaiverError",
    "CoverageGapWaiverResult",
    "CoverageGapWaiverService",
    "degrade_sealed_days_for_gap",
]
