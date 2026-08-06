"""Crash-safe publication of typed infrastructure allocation intervals.

This module is intentionally dark until the Slice 1 cutover gate is complete.
It provides the strict mechanics that the eventual leader-owned runtime loop
will call, but construction defaults to ``publication_enabled=False`` and no
lifespan task imports or starts it yet.

The app database freezes a complete publication plan before audit I/O.  Audit
publication inserts and verifies the immutable batch in one transaction; a
second app transaction advances the exact interval cursor and marks the plan
published.  A crash between those commits replays the same plan and verifies
the same audit hashes before advancing the cursor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from ..usage_ledger import (
    StrictUsageConflict,
    StrictUsageEvent,
    StrictUsageLedgerError,
    StrictUsagePartitionMissing,
    StrictUsagePublishResult,
    UsageLedger,
)
from .types import decimal_text

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_SCHEMA_VERSION = 1
_MICROSECONDS_PER_HOUR = 3_600_000_000
_BYTES_PER_GIB = 1024**3

_EVENT_HASH_FIELDS = (
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
    "period_start",
    "period_end",
    "measurement_basis",
    "cost_domain",
    "resource_class",
    "attribution_scope",
    "measurement_algorithm",
    "source_capacity_value",
    "source_capacity_unit",
    "source_cluster",
    "source_kind",
    "source_uid",
    "source_lifecycle_id",
    "source_interval_id",
    "event_kind",
    "corrects_source",
    "corrects_source_id",
    "corrects_unit",
    "corrects_ts",
    "correction_group_id",
    "correction_reason",
    "correction_actor_id",
    "discovered_at",
)


class PublicationError(RuntimeError):
    """Base class for strict infrastructure publication failures."""


class PublicationDisabledError(PublicationError):
    """The independent runtime publication gate is off."""


class PublicationFenceError(PublicationError):
    """The caller does not own the current metering generation."""


class PublicationConflictError(PublicationError):
    """Frozen intent cannot be reconciled with current app state."""


class PublicationContractError(PublicationError, ValueError):
    """An interval, rate, or frozen plan violates the publication contract."""


def _aware_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise PublicationContractError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PublicationContractError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _uuid_text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PublicationContractError(f"{field} must be a UUID") from exc


def _validate_canonical_json(value: Any) -> None:
    """Validate the constrained RFC 8785-compatible hash domain.

    All event numbers are represented by canonical decimal strings.  The only
    native integers are small schema/ordinal values, so Python's sorted compact
    JSON encoding is RFC 8785-compatible for this deliberately narrow domain.
    """
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise PublicationContractError("canonical JSON integer is not IEEE-safe")
        return
    if isinstance(value, float | Decimal):
        raise PublicationContractError(
            "canonical publication JSON represents numbers as decimal text"
        )
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise PublicationContractError("canonical publication JSON has a surrogate")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PublicationContractError("canonical JSON keys must be text")
            _validate_canonical_json(key)
            _validate_canonical_json(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_canonical_json(child)
        return
    raise PublicationContractError(
        f"unsupported canonical JSON value: {type(value).__name__}"
    )


def _canonical_json_bytes(value: Any) -> bytes:
    _validate_canonical_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash_material(domain: str, value: Any) -> str:
    framed = [domain, _PAYLOAD_SCHEMA_VERSION, value]
    return hashlib.sha256(_canonical_json_bytes(framed)).hexdigest()


def event_payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash the versioned, ordered typed audit payload (excluding its hash)."""
    missing = set(_EVENT_HASH_FIELDS) - payload.keys()
    extra = payload.keys() - set(_EVENT_HASH_FIELDS)
    if missing or extra:
        raise PublicationContractError("event hash fields differ from the v1 contract")
    ordered = [[field, payload[field]] for field in _EVENT_HASH_FIELDS]
    return _hash_material("srw-infrastructure-usage-event", ordered)


def _duration_microseconds(start: datetime, end: datetime) -> int:
    delta = end - start
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass(frozen=True)
class CanonicalRateVersion:
    id: UUID
    unit: str
    usd_per_unit: Decimal
    effective_from: datetime
    effective_to: datetime | None

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> CanonicalRateVersion:
        try:
            rate_id = row["id"]
            unit = str(row["unit"])
            usd_per_unit = Decimal(str(row["usd_per_unit"]))
            effective_from = _aware_utc(row["effective_from"], "effective_from")
            effective_to = (
                None
                if row["effective_to"] is None
                else _aware_utc(row["effective_to"], "effective_to")
            )
        except (InvalidOperation, KeyError, ValueError) as exc:
            raise PublicationContractError("invalid canonical rate row") from exc
        if not unit:
            raise PublicationContractError("canonical rate unit is empty")
        if not usd_per_unit.is_finite() or usd_per_unit < 0:
            raise PublicationContractError("canonical rate must be finite/nonnegative")
        if effective_to is not None and effective_to <= effective_from:
            raise PublicationContractError("canonical rate range is empty")
        return cls(
            id=UUID(str(rate_id)),
            unit=unit,
            usd_per_unit=Decimal(decimal_text(usd_per_unit)),
            effective_from=effective_from,
            effective_to=effective_to,
        )


@dataclass(frozen=True)
class _CapacityDimension:
    unit: str
    capacity: int
    capacity_unit: str
    quantity_denominator: int


@dataclass(frozen=True)
class PlannedUsageEvent:
    ordinal: int
    event: StrictUsageEvent
    canonical_rate_version_id: UUID | None


@dataclass(frozen=True)
class FrozenPublicationPlan:
    id: UUID
    source_interval_id: UUID
    source_revision: str
    plan_kind: str
    plan_revision: int
    advances_cursor: bool
    previous_materialized_through: datetime | None
    correction_group_id: UUID | None
    period_start: datetime
    period_end: datetime
    payload_schema_version: int
    event_set_hash: str
    rate_selection_hash: str
    creator_generation: int
    state: str
    events: tuple[PlannedUsageEvent, ...]

    def __post_init__(self) -> None:
        if self.payload_schema_version != _PAYLOAD_SCHEMA_VERSION:
            raise PublicationContractError("unknown publication payload version")
        if self.plan_kind != "usage" or self.plan_revision != 0:
            raise PublicationContractError(
                "this materializer currently supports ordinary usage plans only"
            )
        if not self.advances_cursor or self.previous_materialized_through is None:
            raise PublicationContractError("usage plan must advance an exact cursor")
        if self.correction_group_id is not None:
            raise PublicationContractError("usage plan cannot be a correction group")
        if self.period_end <= self.period_start:
            raise PublicationContractError("publication plan period is empty")
        if self.previous_materialized_through != self.period_start:
            raise PublicationContractError("publication plan cursor/start mismatch")
        if self.creator_generation <= 0:
            raise PublicationContractError("publication generation must be positive")
        if self.state not in {"planned", "published", "conflict"}:
            raise PublicationContractError("unknown publication plan state")
        if [event.ordinal for event in self.events] != list(range(len(self.events))):
            raise PublicationContractError(
                "publication event ordinals are not contiguous"
            )
        if not self.events:
            raise PublicationContractError("publication plan has no events")
        for planned in self.events:
            payload = dict(planned.event.payload)
            claimed_hash = payload.pop("payload_hash", None)
            if (
                claimed_hash != planned.event.row_hash
                or event_payload_hash(payload) != planned.event.row_hash
            ):
                raise PublicationContractError(
                    "publication event payload hash mismatch"
                )
            if payload["source_interval_id"] != str(self.source_interval_id):
                raise PublicationContractError("publication event interval mismatch")
            if payload["period_start"] != _timestamp_text(self.period_start):
                raise PublicationContractError("publication event start mismatch")
            if payload["period_end"] != _timestamp_text(self.period_end):
                raise PublicationContractError("publication event end mismatch")
            details = payload["details"]
            if (
                not isinstance(details, Mapping)
                or details.get("source_revision") != self.source_revision
            ):
                raise PublicationContractError("publication event revision mismatch")
            if planned.canonical_rate_version_id is None:
                if payload["rate_usd"] is not None or payload["cost_usd"] is not None:
                    raise PublicationContractError(
                        "unpriced publication event carries a rate or cost"
                    )
            elif payload["rate_usd"] is None or payload["cost_usd"] is None:
                raise PublicationContractError(
                    "priced publication event is missing its rate or cost"
                )
        if _event_set_hash(self.events) != self.event_set_hash:
            raise PublicationContractError("publication event-set hash mismatch")
        if _rate_selection_hash(self.events) != self.rate_selection_hash:
            raise PublicationContractError("publication rate-selection hash mismatch")

    @classmethod
    def from_records(
        cls,
        plan: Mapping[str, Any],
        event_rows: Sequence[Mapping[str, Any]],
    ) -> FrozenPublicationPlan:
        events: list[PlannedUsageEvent] = []
        for row in event_rows:
            payload = row["event_payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, Mapping):
                raise PublicationContractError("publication payload is not an object")
            events.append(
                PlannedUsageEvent(
                    ordinal=int(row["ordinal"]),
                    event=StrictUsageEvent(
                        payload=dict(payload), row_hash=str(row["row_hash"])
                    ),
                    canonical_rate_version_id=(
                        None
                        if row["canonical_rate_version_id"] is None
                        else UUID(str(row["canonical_rate_version_id"]))
                    ),
                )
            )
        if int(plan["expected_event_count"]) != len(events):
            raise PublicationContractError("publication manifest count mismatch")
        return cls(
            id=UUID(str(plan["id"])),
            source_interval_id=UUID(str(plan["source_interval_id"])),
            source_revision=str(plan["source_revision"]),
            plan_kind=str(plan["plan_kind"]),
            plan_revision=int(plan["plan_revision"]),
            advances_cursor=bool(plan["advances_cursor"]),
            previous_materialized_through=(
                None
                if plan["previous_materialized_through"] is None
                else _aware_utc(
                    plan["previous_materialized_through"],
                    "previous_materialized_through",
                )
            ),
            correction_group_id=(
                None
                if plan["correction_group_id"] is None
                else UUID(str(plan["correction_group_id"]))
            ),
            period_start=_aware_utc(plan["period_start"], "period_start"),
            period_end=_aware_utc(plan["period_end"], "period_end"),
            payload_schema_version=int(plan["payload_schema_version"]),
            event_set_hash=str(plan["event_set_hash"]),
            rate_selection_hash=str(plan["rate_selection_hash"]),
            creator_generation=int(plan["creator_generation"]),
            state=str(plan["state"]),
            events=tuple(events),
        )


@dataclass(frozen=True)
class PublicationResult:
    plan_id: UUID
    audit: StrictUsagePublishResult
    cursor_advanced: bool


def _event_set_hash(events: Sequence[PlannedUsageEvent]) -> str:
    ordered = sorted(
        ([*event.event.dedupe_key, event.event.row_hash] for event in events),
        key=lambda item: tuple(item[:4]),
    )
    return _hash_material("srw-infrastructure-event-set", ordered)


def _rate_selection_hash(events: Sequence[PlannedUsageEvent]) -> str:
    selections = []
    for planned in events:
        payload = planned.event.payload
        selections.append(
            [
                payload["cost_domain"],
                payload["measurement_basis"],
                payload["category"],
                payload["resource_class"],
                payload["resource"],
                payload["unit"],
                (
                    None
                    if planned.canonical_rate_version_id is None
                    else str(planned.canonical_rate_version_id)
                ),
                payload["rate_usd"],
            ]
        )
    return _hash_material("srw-infrastructure-rate-selection", sorted(selections))


def _capacity_dimensions(interval: Mapping[str, Any]) -> tuple[_CapacityDimension, ...]:
    kind = str(interval["source_kind"])
    category = str(interval["category"])
    if category == "compute" and kind in {"pod", "vmi"}:
        values = (
            ("vcpu-hour", interval["cpu_millicores"], "millicore", 1000),
            ("gib-hour", interval["memory_bytes"], "byte", _BYTES_PER_GIB),
        )
    elif category == "storage" and kind in {"pvc", "volume"}:
        instance_unit = "claim-hour" if kind == "pvc" else "volume-hour"
        values = (
            ("gib-hour", interval["storage_bytes"], "byte", _BYTES_PER_GIB),
            (instance_unit, 1, "instance", 1),
        )
    else:
        raise PublicationContractError("unsupported interval capacity shape")

    dimensions: list[_CapacityDimension] = []
    for unit, raw_capacity, capacity_unit, scale in values:
        if isinstance(raw_capacity, bool) or not isinstance(raw_capacity, int):
            raise PublicationContractError(f"{capacity_unit} capacity must be integer")
        if raw_capacity < 0:
            raise PublicationContractError("interval capacity cannot be negative")
        dimensions.append(
            _CapacityDimension(
                unit=unit,
                capacity=raw_capacity,
                capacity_unit=capacity_unit,
                quantity_denominator=scale * _MICROSECONDS_PER_HOUR,
            )
        )
    return tuple(dimensions)


def _rates_by_unit(
    rates: Sequence[CanonicalRateVersion],
) -> dict[str, list[CanonicalRateVersion]]:
    grouped: dict[str, list[CanonicalRateVersion]] = {}
    for rate in rates:
        grouped.setdefault(rate.unit, []).append(rate)
    for unit, versions in grouped.items():
        versions.sort(key=lambda item: item.effective_from)
        previous: CanonicalRateVersion | None = None
        for version in versions:
            if previous is not None and (
                previous.effective_to is None
                or previous.effective_to > version.effective_from
            ):
                raise PublicationContractError(
                    f"overlapping canonical rate versions for {unit}"
                )
            previous = version
    return grouped


def _select_rate_and_boundary(
    versions: Sequence[CanonicalRateVersion],
    start: datetime,
) -> tuple[CanonicalRateVersion | None, datetime | None]:
    selected = next(
        (
            rate
            for rate in versions
            if rate.effective_from <= start
            and (rate.effective_to is None or start < rate.effective_to)
        ),
        None,
    )
    boundaries = [
        rate.effective_from for rate in versions if rate.effective_from > start
    ]
    if selected is not None and selected.effective_to is not None:
        boundaries.append(selected.effective_to)
    return selected, min(boundaries, default=None)


def _interval_details(interval: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "source_revision",
        "namespace",
        "name",
        "revision_no",
        "attribution_source",
        "attribution_quality",
        "backing_resource_uid",
        "lifecycle_confidence",
        "capacity_source",
        "capacity_quality",
        "start_time_source",
        "start_uncertainty_us",
        "end_time_source",
        "end_uncertainty_us",
        "end_reason",
    )
    details: dict[str, Any] = {}
    for field in fields:
        value = interval.get(field)
        details[field] = None if value is None else str(value)
    return details


def build_usage_plan(
    interval: Mapping[str, Any],
    rate_rows: Sequence[Mapping[str, Any] | CanonicalRateVersion],
    *,
    creator_generation: int,
    plan_id: UUID | None = None,
) -> FrozenPublicationPlan | None:
    """Build the next immutable day/rate-boundary segment for one interval."""
    if creator_generation <= 0:
        raise PublicationContractError("creator generation must be positive")
    source_revision = str(interval["source_revision"])
    if not _HASH_RE.fullmatch(source_revision):
        raise PublicationContractError("interval source_revision must be SHA-256")

    segment_start = _aware_utc(interval["materialized_through"], "materialized_through")
    started_at = _aware_utc(interval["started_at"], "started_at")
    ended_at = (
        None
        if interval["ended_at"] is None
        else _aware_utc(interval["ended_at"], "ended_at")
    )
    last_confirmed_at = _aware_utc(interval["last_confirmed_at"], "last_confirmed_at")
    if segment_start < started_at:
        raise PublicationContractError("materialization cursor precedes interval")
    publishable_end = (
        ended_at
        if ended_at is not None
        else datetime.combine(last_confirmed_at.date(), time.min, tzinfo=timezone.utc)
    )
    if segment_start >= publishable_end:
        return None

    dimensions = _capacity_dimensions(interval)
    rate_versions = [
        rate
        if isinstance(rate, CanonicalRateVersion)
        else CanonicalRateVersion.from_record(rate)
        for rate in rate_rows
    ]
    rates_by_unit = _rates_by_unit(rate_versions)
    selected_rates: dict[str, CanonicalRateVersion | None] = {}
    boundaries = [
        publishable_end,
        datetime.combine(
            segment_start.date() + timedelta(days=1),
            time.min,
            tzinfo=timezone.utc,
        ),
    ]
    for dimension in dimensions:
        selected, boundary = _select_rate_and_boundary(
            rates_by_unit.get(dimension.unit, ()), segment_start
        )
        selected_rates[dimension.unit] = selected
        if boundary is not None:
            boundaries.append(boundary)
    segment_end = min(boundary for boundary in boundaries if boundary > segment_start)
    if segment_end > publishable_end:
        raise PublicationContractError("segment extends past confirmed usage")

    duration_us = _duration_microseconds(segment_start, segment_end)
    if duration_us <= 0:
        raise PublicationContractError("publication segment duration is not positive")

    interval_id = UUID(str(interval["id"]))
    lifecycle_id = _uuid_text(interval["source_lifecycle_id"], "source_lifecycle_id")
    source_id = _hash_material(
        "srw-infrastructure-source-id",
        [
            str(interval_id),
            _timestamp_text(segment_start),
            _timestamp_text(segment_end),
        ],
    )
    attribution_scope = str(interval["attribution_scope"])
    customer = attribution_scope == "customer"
    ref_kind = str(interval["owner_kind"]) if customer else None
    ref_id = _uuid_text(interval["owner_id"], "owner_id") if customer else None
    user_id = _uuid_text(interval["user_id"], "user_id") if customer else None
    project_id = (
        _uuid_text(interval.get("project_id"), "project_id", nullable=True)
        if customer
        else None
    )
    if customer and ref_kind not in {"job", "thread"}:
        raise PublicationContractError("customer interval owner kind is invalid")
    if not customer and (
        interval.get("user_id") is not None or interval.get("project_id") is not None
    ):
        raise PublicationContractError(
            "non-customer interval cannot carry customer attribution ids"
        )

    details = _interval_details(interval)
    events: list[PlannedUsageEvent] = []
    for ordinal, dimension in enumerate(dimensions):
        with localcontext() as context:
            context.prec = 80
            quantity = (
                Decimal(dimension.capacity)
                * Decimal(duration_us)
                / Decimal(dimension.quantity_denominator)
            )
        quantity_text = decimal_text(quantity)
        rate = selected_rates[dimension.unit]
        rate_text = None if rate is None else decimal_text(rate.usd_per_unit)
        if rate is None:
            cost_text = None
        else:
            with localcontext() as context:
                context.prec = 80
                raw_cost = Decimal(quantity_text) * rate.usd_per_unit
            cost_text = decimal_text(raw_cost)
        payload: dict[str, Any] = {
            "ts": _timestamp_text(segment_start),
            "user_id": user_id,
            "project_id": project_id,
            "ref_kind": ref_kind,
            "ref_id": ref_id,
            "category": str(interval["category"]),
            "resource": str(interval["resource"]),
            "quantity": quantity_text,
            "unit": dimension.unit,
            "rate_usd": rate_text,
            "cost_usd": cost_text,
            "source": "infra-allocation-v2",
            "source_id": source_id,
            "details": details,
            "period_start": _timestamp_text(segment_start),
            "period_end": _timestamp_text(segment_end),
            "measurement_basis": str(interval["measurement_basis"]),
            "cost_domain": str(interval["cost_domain"]),
            "resource_class": str(interval["resource_class"]),
            "attribution_scope": attribution_scope,
            "measurement_algorithm": str(interval["measurement_algorithm"]),
            "source_capacity_value": str(dimension.capacity),
            "source_capacity_unit": dimension.capacity_unit,
            "source_cluster": str(interval["source_cluster"]),
            "source_kind": str(interval["source_kind"]),
            "source_uid": str(interval["source_uid"]),
            "source_lifecycle_id": lifecycle_id,
            "source_interval_id": str(interval_id),
            "event_kind": "usage",
            "corrects_source": None,
            "corrects_source_id": None,
            "corrects_unit": None,
            "corrects_ts": None,
            "correction_group_id": None,
            "correction_reason": None,
            "correction_actor_id": None,
            "discovered_at": None,
        }
        row_hash = event_payload_hash(payload)
        payload["payload_hash"] = row_hash
        events.append(
            PlannedUsageEvent(
                ordinal=ordinal,
                event=StrictUsageEvent(payload=payload, row_hash=row_hash),
                canonical_rate_version_id=None if rate is None else rate.id,
            )
        )

    event_tuple = tuple(events)
    return FrozenPublicationPlan(
        id=plan_id or uuid4(),
        source_interval_id=interval_id,
        source_revision=source_revision,
        plan_kind="usage",
        plan_revision=0,
        advances_cursor=True,
        previous_materialized_through=segment_start,
        correction_group_id=None,
        period_start=segment_start,
        period_end=segment_end,
        payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
        event_set_hash=_event_set_hash(event_tuple),
        rate_selection_hash=_rate_selection_hash(event_tuple),
        creator_generation=creator_generation,
        state="planned",
        events=event_tuple,
    )


_CONTROL_SQL = """
/* infra-publication:control */
SELECT leader_generation, cutover_state, cutover_at
FROM infra_metering_control
WHERE singleton = TRUE
FOR SHARE
"""

_CANDIDATE_INTERVALS_SQL = """
/* infra-publication:candidates */
SELECT interval.*
FROM resource_intervals AS interval
WHERE interval.resource = ANY($1::text[])
  AND interval.materialized_through >= $2
  AND interval.materialized_through < CASE
        WHEN interval.ended_at IS NOT NULL THEN interval.ended_at
        ELSE date_trunc('day', interval.last_confirmed_at, 'UTC')
      END
  AND NOT EXISTS (
      SELECT 1
      FROM resource_publication_plans AS plan
      WHERE plan.source_interval_id = interval.id
        AND plan.advances_cursor
        AND plan.previous_materialized_through = interval.materialized_through
  )
ORDER BY interval.materialized_through, interval.id
LIMIT $3
FOR UPDATE OF interval SKIP LOCKED
"""

_DAY_STATE_SQL = """
/* infra-publication:day-state */
SELECT state
FROM infra_usage_day_state
WHERE day = $1
FOR SHARE
"""

_RATE_ROWS_SQL = """
/* infra-publication:rates */
WITH requested(unit) AS (
    SELECT unnest($6::text[])
)
SELECT rate.id, rate.unit, rate.usd_per_unit,
       rate.effective_from, rate.effective_to
FROM requested
CROSS JOIN LATERAL (
    SELECT candidate.id, candidate.unit, candidate.usd_per_unit,
           candidate.effective_from, candidate.effective_to
    FROM usage_rates_v2 AS candidate
    WHERE candidate.cost_domain = $1
      AND candidate.measurement_basis = $2
      AND candidate.category = $3
      AND candidate.resource_class = $4
      AND candidate.resource = $5
      AND candidate.unit = requested.unit
      AND candidate.effective_from < $8
      AND (candidate.effective_to IS NULL OR candidate.effective_to > $7)
    ORDER BY candidate.effective_from
    LIMIT 2
) AS rate
ORDER BY rate.unit, rate.effective_from
"""

_INSERT_PLAN_SQL = """
/* infra-publication:insert-plan */
INSERT INTO resource_publication_plans (
    id, source_interval_id, source_revision, plan_kind, plan_revision,
    advances_cursor, previous_materialized_through, correction_group_id,
    period_start, period_end, expected_event_count, payload_schema_version,
    event_set_hash, rate_selection_hash, creator_generation
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
)
"""

_INSERT_PLAN_EVENTS_SQL = """
/* infra-publication:insert-plan-events */
INSERT INTO resource_publication_plan_events (
    plan_id, ordinal, source, source_id, unit, ts, event_kind,
    canonical_rate_version_id, row_hash, event_payload
)
SELECT
    event.plan_id, event.ordinal, event.source, event.source_id, event.unit,
    event.ts, event.event_kind, event.canonical_rate_version_id,
    event.row_hash, event.event_payload
FROM jsonb_to_recordset($1::jsonb) AS event(
    plan_id uuid, ordinal integer, source text, source_id text, unit text,
    ts timestamptz, event_kind text, canonical_rate_version_id uuid,
    row_hash text, event_payload jsonb
)
"""

_PENDING_PLAN_SQL = """
/* infra-publication:pending-plan */
SELECT *
FROM resource_publication_plans
WHERE state = 'planned'
ORDER BY created_at, id
LIMIT 1
"""

_PLAN_EVENTS_SQL = """
/* infra-publication:plan-events */
SELECT ordinal, canonical_rate_version_id, row_hash, event_payload
FROM resource_publication_plan_events
WHERE plan_id = $1
ORDER BY ordinal
"""

_LOCK_PLAN_SQL = """
/* infra-publication:lock-plan */
SELECT state
FROM resource_publication_plans
WHERE id = $1
FOR UPDATE
"""

_ADVANCE_CURSOR_SQL = """
/* infra-publication:advance-cursor */
UPDATE resource_intervals
SET materialized_through = $4, updated_at = statement_timestamp()
WHERE id = $1
  AND source_revision = $2
  AND materialized_through = $3
RETURNING materialized_through
"""

_PUBLISH_PLAN_SQL = """
/* infra-publication:publish-plan */
UPDATE resource_publication_plans
SET state = 'published',
    attempt_count = attempt_count + 1,
    last_attempt_at = statement_timestamp(),
    sanitized_error = NULL,
    published_at = statement_timestamp()
WHERE id = $1 AND state = 'planned'
RETURNING id
"""

_RECORD_FAILURE_SQL = """
/* infra-publication:record-failure */
UPDATE resource_publication_plans AS plan
SET state = $3,
    attempt_count = attempt_count + 1,
    last_attempt_at = statement_timestamp(),
    sanitized_error = $4::jsonb
WHERE plan.id = $1
  AND plan.state = 'planned'
  AND EXISTS (
      SELECT 1 FROM infra_metering_control AS control
      WHERE control.singleton = TRUE
        AND control.leader_generation = $2
        AND control.cutover_state = 'active'
  )
RETURNING plan.state
"""


class InfrastructureUsageMaterializer:
    """Freeze and deliver ordinary workspace-Pod publication plans.

    The initial class allowlist is intentionally just ``workspace_pod``.  Later
    slices add claims, volumes, agents, and VMIs behind independent rollout
    gates rather than broadening this query implicitly.
    """

    def __init__(
        self,
        app_pool: asyncpg.Pool,
        ledger: UsageLedger,
        *,
        publication_enabled: bool = False,
        batch_size: int = 100,
        enabled_resources: Sequence[str] = ("workspace_pod",),
    ) -> None:
        if batch_size <= 0 or batch_size > 1000:
            raise ValueError("materializer batch_size must be between 1 and 1000")
        resources = tuple(dict.fromkeys(str(item) for item in enabled_resources))
        if not resources or any(not item for item in resources):
            raise ValueError("materializer requires a non-empty resource allowlist")
        self._app = app_pool
        self._ledger = ledger
        self._publication_enabled = publication_enabled
        self._batch_size = batch_size
        self._enabled_resources = resources

    def _require_enabled(self) -> None:
        if not self._publication_enabled:
            raise PublicationDisabledError(
                "infrastructure publication runtime gate is disabled"
            )

    @staticmethod
    def _validate_control(
        control: Mapping[str, Any] | None, generation: int
    ) -> datetime:
        if control is None:
            raise PublicationFenceError("metering control row is missing")
        if int(control["leader_generation"]) != generation or generation <= 0:
            raise PublicationFenceError("metering leader generation is stale")
        if control["cutover_state"] != "active" or control["cutover_at"] is None:
            raise PublicationDisabledError("infrastructure cutover is not active")
        return _aware_utc(control["cutover_at"], "cutover_at")

    async def plan_batch(self, generation: int) -> tuple[FrozenPublicationPlan, ...]:
        """Freeze a bounded batch without holding locks across audit I/O."""
        self._require_enabled()
        plans: list[FrozenPublicationPlan] = []
        async with self._app.acquire() as conn:
            async with conn.transaction():
                cutover_at = self._validate_control(
                    await conn.fetchrow(_CONTROL_SQL), generation
                )
                intervals = await conn.fetch(
                    _CANDIDATE_INTERVALS_SQL,
                    list(self._enabled_resources),
                    cutover_at,
                    self._batch_size,
                )
                for interval in intervals:
                    cursor = _aware_utc(
                        interval["materialized_through"], "materialized_through"
                    )
                    day_state = await conn.fetchrow(_DAY_STATE_SQL, cursor.date())
                    if day_state is not None and day_state["state"] == "sealed":
                        raise PublicationConflictError(
                            f"cannot plan ordinary usage for sealed day {cursor.date()}"
                        )
                    dimensions = _capacity_dimensions(interval)
                    ended_at = interval["ended_at"]
                    candidate_end = (
                        _aware_utc(ended_at, "ended_at")
                        if ended_at is not None
                        else datetime.combine(
                            _aware_utc(
                                interval["last_confirmed_at"], "last_confirmed_at"
                            ).date(),
                            time.min,
                            tzinfo=timezone.utc,
                        )
                    )
                    rate_rows = await conn.fetch(
                        _RATE_ROWS_SQL,
                        interval["cost_domain"],
                        interval["measurement_basis"],
                        interval["category"],
                        interval["resource_class"],
                        interval["resource"],
                        [dimension.unit for dimension in dimensions],
                        cursor,
                        candidate_end,
                    )
                    plan = build_usage_plan(
                        interval,
                        rate_rows,
                        creator_generation=generation,
                    )
                    if plan is None:
                        continue
                    await conn.execute(
                        _INSERT_PLAN_SQL,
                        plan.id,
                        plan.source_interval_id,
                        plan.source_revision,
                        plan.plan_kind,
                        plan.plan_revision,
                        plan.advances_cursor,
                        plan.previous_materialized_through,
                        plan.correction_group_id,
                        plan.period_start,
                        plan.period_end,
                        len(plan.events),
                        plan.payload_schema_version,
                        plan.event_set_hash,
                        plan.rate_selection_hash,
                        plan.creator_generation,
                    )
                    event_payload = [
                        {
                            "plan_id": str(plan.id),
                            "ordinal": item.ordinal,
                            "source": item.event.payload["source"],
                            "source_id": item.event.payload["source_id"],
                            "unit": item.event.payload["unit"],
                            "ts": item.event.payload["ts"],
                            "event_kind": item.event.payload["event_kind"],
                            "canonical_rate_version_id": (
                                None
                                if item.canonical_rate_version_id is None
                                else str(item.canonical_rate_version_id)
                            ),
                            "row_hash": item.event.row_hash,
                            "event_payload": dict(item.event.payload),
                        }
                        for item in plan.events
                    ]
                    await conn.execute(
                        _INSERT_PLAN_EVENTS_SQL,
                        json.dumps(
                            event_payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    )
                    plans.append(plan)
        return tuple(plans)

    async def next_pending_plan(self) -> FrozenPublicationPlan | None:
        """Load one immutable manifest in a repeatable-read app snapshot."""
        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                plan = await conn.fetchrow(_PENDING_PLAN_SQL)
                if plan is None:
                    return None
                events = await conn.fetch(_PLAN_EVENTS_SQL, plan["id"])
        return FrozenPublicationPlan.from_records(plan, events)

    async def publish_one(self, generation: int) -> PublicationResult | None:
        """Verify one pending plan in audit, then fenced-CAS its app cursor."""
        self._require_enabled()
        async with self._app.acquire() as conn:
            # FOR SHARE is intentionally a short fencing read, but PostgreSQL
            # classifies row locks as writes and rejects it in a read-only txn.
            async with conn.transaction():
                self._validate_control(await conn.fetchrow(_CONTROL_SQL), generation)
        plan = await self.next_pending_plan()
        if plan is None:
            return None

        try:
            audit_result = await self._ledger.publish_frozen_events(
                [item.event for item in plan.events]
            )
        except StrictUsageConflict:
            await self._record_failure(
                plan.id, generation, code="audit-payload-conflict", conflict=True
            )
            raise
        except StrictUsagePartitionMissing as exc:
            await self._record_failure(
                plan.id,
                generation,
                code="audit-partition-missing",
                details={"partitions": list(exc.partitions)},
            )
            raise
        except StrictUsageLedgerError:
            await self._record_failure(
                plan.id, generation, code="audit-publication-failed"
            )
            raise
        except Exception:
            await self._record_failure(
                plan.id, generation, code="audit-publication-failed"
            )
            raise

        advanced = await self._finalize(plan, generation)
        return PublicationResult(
            plan_id=plan.id,
            audit=audit_result,
            cursor_advanced=advanced,
        )

    async def _record_failure(
        self,
        plan_id: UUID,
        generation: int,
        *,
        code: str,
        details: Mapping[str, Any] | None = None,
        conflict: bool = False,
    ) -> None:
        error = {"code": code, **dict(details or {})}
        async with self._app.acquire() as conn:
            row = await conn.fetchrow(
                _RECORD_FAILURE_SQL,
                plan_id,
                generation,
                "conflict" if conflict else "planned",
                json.dumps(error, separators=(",", ":"), sort_keys=True),
            )
        if row is None:
            logger.warning(
                "infrastructure publication attempt diagnostics were fenced "
                "for plan %s",
                plan_id,
            )

    async def _finalize(self, plan: FrozenPublicationPlan, generation: int) -> bool:
        async with self._app.acquire() as conn:
            async with conn.transaction():
                self._validate_control(await conn.fetchrow(_CONTROL_SQL), generation)
                current = await conn.fetchrow(_LOCK_PLAN_SQL, plan.id)
                if current is None:
                    raise PublicationConflictError("publication plan disappeared")
                if current["state"] == "published":
                    return False
                if current["state"] != "planned":
                    raise PublicationConflictError(
                        f"publication plan is terminal: {current['state']}"
                    )
                if plan.advances_cursor:
                    advanced = await conn.fetchrow(
                        _ADVANCE_CURSOR_SQL,
                        plan.source_interval_id,
                        plan.source_revision,
                        plan.previous_materialized_through,
                        plan.period_end,
                    )
                    if advanced is None:
                        raise PublicationConflictError(
                            "interval revision/cursor changed before finalization"
                        )
                published = await conn.fetchrow(_PUBLISH_PLAN_SQL, plan.id)
                if published is None:
                    raise PublicationConflictError(
                        "publication plan changed before finalization"
                    )
        return plan.advances_cursor


__all__ = [
    "CanonicalRateVersion",
    "FrozenPublicationPlan",
    "InfrastructureUsageMaterializer",
    "PlannedUsageEvent",
    "PublicationConflictError",
    "PublicationContractError",
    "PublicationDisabledError",
    "PublicationError",
    "PublicationFenceError",
    "PublicationResult",
    "build_usage_plan",
    "event_payload_hash",
]
