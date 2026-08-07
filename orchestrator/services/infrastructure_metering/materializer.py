"""Crash-safe publication of typed infrastructure allocation intervals.

This module stays dark until the Slice 1 publication and cutover gates are
explicitly enabled.  The leader-owned runtime loop calls these strict mechanics,
but construction defaults to ``publication_enabled=False`` and collection or v2
reads alone cannot start publication.

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
from dataclasses import dataclass, field
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

_CURSOR_ADVANCING_PLAN_KINDS = frozenset({"usage", "late-usage"})
_PLAN_KINDS = _CURSOR_ADVANCING_PLAN_KINDS | {"correction"}
_CORRECTION_OVERRIDE_FIELDS = frozenset(
    {
        "user_id",
        "project_id",
        "ref_kind",
        "ref_id",
        "category",
        "resource",
        "rate_usd",
        "details",
        "measurement_basis",
        "cost_domain",
        "resource_class",
        "attribution_scope",
        "measurement_algorithm",
        "source_capacity_value",
        "source_capacity_unit",
    }
)
_CANONICAL_RATE_SELECTOR_FIELDS = (
    "cost_domain",
    "measurement_basis",
    "category",
    "resource_class",
    "resource",
    "unit",
)
_CORRECTION_DIMENSION_FIELDS = (
    "user_id",
    "project_id",
    "ref_kind",
    "ref_id",
    "category",
    "resource",
    "measurement_basis",
    "cost_domain",
    "resource_class",
    "attribution_scope",
    "measurement_algorithm",
    "source_capacity_value",
    "source_capacity_unit",
)
_AUDIT_NUMERIC_LIMIT = 10**20
_MAX_CORRECTION_EVENTS = 100
_COMPUTE_RESOURCE_ACTIVATIONS = {
    "agent_pod": "agent_pod",
    "workspace_vm": "workspace_vm",
}
_IDE_WORKSPACE_PRODUCT_CLASS = "ide-session"
_STORAGE_MEASUREMENT_BASES = frozenset({"claim-requested", "volume-provisioned"})
_STORAGE_REQUIREMENT_API_RESOURCES = {
    ("claim-requested", "quantity"): "core/v1/persistentvolumeclaims",
    ("volume-provisioned", "quantity"): "core/v1/persistentvolumes",
    ("volume-provisioned", "attribution"): "core/v1/persistentvolumeclaims",
}
_COLLECTOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SOURCE_CLUSTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


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


@dataclass(frozen=True, order=True, slots=True)
class StoragePublicationAuthority:
    """One process-approved storage quantity authority.

    Namespace/scope membership remains database-owned by the immutable 0105
    requirement set.  This process policy deliberately stops at the stable
    source identity so one primary-cluster publication gate cannot authorize a
    distinct ``kubevirt-storage`` source that happens to emit the same resource
    name.
    """

    measurement_basis: str
    collector_id: str
    source_cluster: str

    def __post_init__(self) -> None:
        if self.measurement_basis not in _STORAGE_MEASUREMENT_BASES:
            raise PublicationContractError(
                "storage publication authority measurement_basis is unsupported"
            )
        if not _COLLECTOR_ID_RE.fullmatch(self.collector_id):
            raise PublicationContractError(
                "storage publication authority collector_id is invalid"
            )
        if not _SOURCE_CLUSTER_RE.fullmatch(self.source_cluster):
            raise PublicationContractError(
                "storage publication authority source_cluster is invalid"
            )


@dataclass(frozen=True, slots=True)
class StoragePublicationPolicy:
    """Immutable per-process allowlist for independently gated storage sources."""

    authorities: tuple[StoragePublicationAuthority, ...] = ()

    def __post_init__(self) -> None:
        try:
            normalized = tuple(sorted(set(self.authorities)))
        except TypeError as exc:
            raise PublicationContractError(
                "storage publication policy authorities are invalid"
            ) from exc
        if any(
            not isinstance(authority, StoragePublicationAuthority)
            for authority in normalized
        ):
            raise PublicationContractError(
                "storage publication policy requires typed authorities"
            )
        object.__setattr__(self, "authorities", normalized)

    def allows(
        self,
        *,
        measurement_basis: str,
        collector_id: str,
        source_cluster: str,
    ) -> bool:
        return (
            StoragePublicationAuthority(
                measurement_basis=measurement_basis,
                collector_id=collector_id,
                source_cluster=source_cluster,
            )
            in self.authorities
        )

    def sql_columns(self) -> tuple[list[str], list[str], list[str]]:
        return (
            [authority.measurement_basis for authority in self.authorities],
            [authority.collector_id for authority in self.authorities],
            [authority.source_cluster for authority in self.authorities],
        )


def _aware_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise PublicationContractError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PublicationContractError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, order=True, slots=True)
class StorageCoverageRequirement:
    """One immutable 0105 scope requirement for an active storage source.

    Quantity requirements authorize publication. Attribution requirements are
    coverage-only dependencies: a PV can be billed without publishing PVC
    quantity, but its PVC evidence must still be complete so ownership remains
    trustworthy. Keeping the role on the identity prevents a PVC attribution
    scope from being mistaken for a claim publication authority.
    """

    measurement_basis: str
    collector_id: str
    source_cluster: str
    inventory_scope_id: UUID
    requirement_role: str
    effective_from: datetime

    def __post_init__(self) -> None:
        # Reuse the exact stable-source validation used by the process policy.
        StoragePublicationAuthority(
            measurement_basis=self.measurement_basis,
            collector_id=self.collector_id,
            source_cluster=self.source_cluster,
        )
        try:
            scope_id = (
                self.inventory_scope_id
                if isinstance(self.inventory_scope_id, UUID)
                else UUID(str(self.inventory_scope_id))
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise PublicationContractError(
                "storage coverage requirement inventory_scope_id is invalid"
            ) from exc
        if (
            self.measurement_basis,
            self.requirement_role,
        ) not in _STORAGE_REQUIREMENT_API_RESOURCES:
            raise PublicationContractError(
                "storage coverage requirement basis/role is invalid"
            )
        object.__setattr__(self, "inventory_scope_id", scope_id)
        object.__setattr__(
            self,
            "effective_from",
            _aware_utc(self.effective_from, "storage requirement effective_from"),
        )

    @property
    def authority(self) -> StoragePublicationAuthority:
        return StoragePublicationAuthority(
            measurement_basis=self.measurement_basis,
            collector_id=self.collector_id,
            source_cluster=self.source_cluster,
        )

    @property
    def expected_api_resource(self) -> str:
        return _STORAGE_REQUIREMENT_API_RESOURCES[
            (self.measurement_basis, self.requirement_role)
        ]

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> StorageCoverageRequirement:
        return cls(
            measurement_basis=str(row.get("measurement_basis") or ""),
            collector_id=str(row.get("collector_id") or ""),
            source_cluster=str(row.get("source_cluster") or ""),
            inventory_scope_id=row.get("inventory_scope_id"),
            requirement_role=str(row.get("requirement_role") or ""),
            effective_from=row.get("effective_from"),
        )


def _timestamp_text(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _payload_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PublicationContractError(f"{field} must be canonical timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationContractError(f"{field} is invalid") from exc
    canonical = _timestamp_text(parsed)
    if canonical != value:
        raise PublicationContractError(f"{field} is not canonical UTC microseconds")
    return parsed.astimezone(timezone.utc)


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


def _correction_source_id(group_id: UUID, ordinal: int) -> str:
    return _hash_material(
        "srw-infrastructure-correction-source-id",
        [str(group_id), ordinal],
    )


def event_payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash the versioned, ordered typed audit payload (excluding its hash)."""
    missing = set(_EVENT_HASH_FIELDS) - payload.keys()
    extra = payload.keys() - set(_EVENT_HASH_FIELDS)
    if missing or extra:
        raise PublicationContractError("event hash fields differ from the v1 contract")
    ordered = [[field, payload[field]] for field in _EVENT_HASH_FIELDS]
    return _hash_material("srw-infrastructure-usage-event", ordered)


def duration_microseconds(start: datetime, end: datetime) -> int:
    """Return an exact integer duration for a timezone-aware interval."""

    start = _aware_utc(start, "start")
    end = _aware_utc(end, "end")
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
class CapacityDimension:
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
class CorrectionDelta:
    """One signed correction derived from an already-frozen audit event.

    ``original`` is the exact app-side event whose audit key must be verified
    before the resulting correction plan is frozen.  By default pricing is
    inherited byte-for-byte from that immutable event.  A reviewed rate change
    must opt out explicitly and provide both the replacement rate (through
    ``payload_overrides``) and its immutable rate-version identity.

    Overrides deliberately cannot replace source identity, time, unit, or
    correction provenance.  Those fields are derived by the builder so a
    caller cannot accidentally create an untraceable delta.
    """

    original: PlannedUsageEvent
    quantity: Decimal | int | str
    payload_overrides: Mapping[str, Any] = field(default_factory=dict)
    inherit_rate: bool = True
    canonical_rate_version_id: UUID | None = None


@dataclass(frozen=True)
class CorrectionRequestDelta:
    """Reviewed correction input keyed to one immutable original audit row."""

    source: str
    source_id: str
    unit: str
    ts: datetime
    expected_payload_hash: str
    quantity: Decimal | int | str
    payload_overrides: Mapping[str, Any] = field(default_factory=dict)
    inherit_rate: bool = True
    canonical_rate_version_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.source != "infra-allocation-v2":
            raise PublicationContractError(
                "correction original source must be infra-allocation-v2"
            )
        if not self.source_id or not self.unit:
            raise PublicationContractError(
                "correction request requires a full original key"
            )
        object.__setattr__(self, "ts", _aware_utc(self.ts, "correction original ts"))
        if not _HASH_RE.fullmatch(self.expected_payload_hash):
            raise PublicationContractError(
                "correction original payload hash must be SHA-256"
            )
        object.__setattr__(self, "payload_overrides", dict(self.payload_overrides))


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
        if self.plan_kind not in _PLAN_KINDS:
            raise PublicationContractError("unknown publication plan kind")
        if self.plan_kind in _CURSOR_ADVANCING_PLAN_KINDS:
            if self.plan_revision != 0:
                raise PublicationContractError(
                    "usage/late-usage plan revision must be zero"
                )
            if not self.advances_cursor or self.previous_materialized_through is None:
                raise PublicationContractError(
                    "usage/late-usage plan must advance an exact cursor"
                )
            if self.correction_group_id is not None:
                raise PublicationContractError(
                    "usage/late-usage plan cannot be a correction group"
                )
        else:
            if self.plan_revision <= 0:
                raise PublicationContractError(
                    "correction plan revision must be positive"
                )
            if self.advances_cursor or self.previous_materialized_through is not None:
                raise PublicationContractError(
                    "correction plan cannot advance an interval cursor"
                )
            if self.correction_group_id != self.id:
                raise PublicationContractError(
                    "correction plan id must be its correction group"
                )
        if self.period_end <= self.period_start:
            raise PublicationContractError("publication plan period is empty")
        if (
            self.advances_cursor
            and self.previous_materialized_through != self.period_start
        ):
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
            if payload["event_kind"] != self.plan_kind:
                raise PublicationContractError("publication event kind mismatch")
            details = payload["details"]
            if (
                not isinstance(details, Mapping)
                or details.get("source_revision") != self.source_revision
            ):
                raise PublicationContractError("publication event revision mismatch")
            try:
                quantity = Decimal(str(payload["quantity"]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise PublicationContractError(
                    "publication event quantity is invalid"
                ) from exc
            if not quantity.is_finite():
                raise PublicationContractError(
                    "publication event quantity must be finite"
                )
            expected_quantity = _payload_capacity_quantity(
                payload,
                self.period_start,
                self.period_end,
            )
            if self.plan_kind == "correction" and quantity < 0:
                if abs(quantity) > expected_quantity:
                    raise PublicationContractError(
                        "negative correction exceeds original capacity-time integral"
                    )
            elif abs(quantity) != expected_quantity:
                raise PublicationContractError(
                    "publication quantity differs from capacity-time integral"
                )
            if self.plan_kind in _CURSOR_ADVANCING_PLAN_KINDS:
                if payload["source"] != "infra-allocation-v2" or quantity < 0:
                    raise PublicationContractError(
                        "usage/late-usage event source or quantity is invalid"
                    )
                if any(
                    payload[field] is not None
                    for field in (
                        "corrects_source",
                        "corrects_source_id",
                        "corrects_unit",
                        "corrects_ts",
                        "correction_group_id",
                        "correction_reason",
                        "correction_actor_id",
                    )
                ):
                    raise PublicationContractError(
                        "usage/late-usage event carries correction provenance"
                    )
                discovered_at = payload["discovered_at"]
                if self.plan_kind == "usage":
                    if discovered_at is not None:
                        raise PublicationContractError(
                            "ordinary usage event cannot carry discovery time"
                        )
                else:
                    discovered = _payload_timestamp(
                        discovered_at, "late-usage discovered_at"
                    )
                    if discovered < self.period_end:
                        raise PublicationContractError(
                            "late-usage discovery precedes its period end"
                        )
            else:
                if payload["source"] != "infra-allocation-correction-v2":
                    raise PublicationContractError("correction event source is invalid")
                if quantity == 0:
                    raise PublicationContractError(
                        "correction event quantity cannot be zero"
                    )
                if payload["correction_group_id"] != str(self.id):
                    raise PublicationContractError(
                        "correction event group does not match its plan"
                    )
                if (
                    payload["corrects_source"] != "infra-allocation-v2"
                    or not payload["corrects_source_id"]
                    or payload["corrects_unit"] != payload["unit"]
                    or payload["corrects_ts"] != payload["period_start"]
                    or not payload["correction_reason"]
                    or not payload["correction_actor_id"]
                ):
                    raise PublicationContractError(
                        "correction event provenance is incomplete"
                    )
                if payload["source_id"] != _correction_source_id(
                    self.id, planned.ordinal
                ):
                    raise PublicationContractError(
                        "correction event source id is not deterministic"
                    )
                discovered_at = payload["discovered_at"]
                if (
                    discovered_at is not None
                    and _payload_timestamp(discovered_at, "correction discovered_at")
                    < self.period_end
                ):
                    raise PublicationContractError(
                        "correction discovery precedes its period end"
                    )
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
    return _hash_material(
        "srw-infrastructure-rate-selection",
        sorted(selections, key=_canonical_json_bytes),
    )


def capacity_dimensions(interval: Mapping[str, Any]) -> tuple[CapacityDimension, ...]:
    """Normalize one immutable interval into independently additive measures.

    Publication and interval-tail reads intentionally share this function so
    CPU, memory, storage-capacity, and occurrence-hour denominators cannot
    drift between the write and read sides of the handoff.
    """

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

    dimensions: list[CapacityDimension] = []
    for unit, raw_capacity, capacity_unit, scale in values:
        if isinstance(raw_capacity, bool) or not isinstance(raw_capacity, int):
            raise PublicationContractError(f"{capacity_unit} capacity must be integer")
        if raw_capacity < 0:
            raise PublicationContractError("interval capacity cannot be negative")
        dimensions.append(
            CapacityDimension(
                unit=unit,
                capacity=raw_capacity,
                capacity_unit=capacity_unit,
                quantity_denominator=scale * _MICROSECONDS_PER_HOUR,
            )
        )
    return tuple(dimensions)


def capacity_quantity(
    dimension: CapacityDimension,
    start: datetime,
    end: datetime,
) -> Decimal:
    """Integrate a capacity dimension over ``[start, end)`` exactly.

    Integer microseconds are used before the NUMERIC(38,18) half-even storage
    boundary.  Returning the stored-precision Decimal keeps daily publication,
    partial-window reads, and crash-window reconciliation byte-for-byte
    comparable.
    """

    duration_us = duration_microseconds(start, end)
    if duration_us < 0:
        raise PublicationContractError("capacity interval end precedes start")
    with localcontext() as context:
        context.prec = 80
        quantity = (
            Decimal(dimension.capacity)
            * Decimal(duration_us)
            / Decimal(dimension.quantity_denominator)
        )
    return Decimal(decimal_text(quantity))


def _payload_capacity_quantity(
    payload: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> Decimal:
    try:
        capacity = int(str(payload["source_capacity_value"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationContractError(
            "publication source capacity must be an integer"
        ) from exc
    if capacity < 0 or str(capacity) != str(payload["source_capacity_value"]):
        raise PublicationContractError(
            "publication source capacity must be canonical/nonnegative"
        )
    unit = str(payload["unit"])
    capacity_unit = str(payload["source_capacity_unit"])
    if unit == "vcpu-hour" and capacity_unit == "millicore":
        denominator = 1000 * _MICROSECONDS_PER_HOUR
    elif unit == "gib-hour" and capacity_unit == "byte":
        denominator = _BYTES_PER_GIB * _MICROSECONDS_PER_HOUR
    elif unit in {"claim-hour", "volume-hour"} and capacity_unit == "instance":
        denominator = _MICROSECONDS_PER_HOUR
    else:
        raise PublicationContractError(
            "publication unit/source-capacity pair is unsupported"
        )
    return capacity_quantity(
        CapacityDimension(
            unit=unit,
            capacity=capacity,
            capacity_unit=capacity_unit,
            quantity_denominator=denominator,
        ),
        start,
        end,
    )


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
    for field_name in fields:
        value = interval.get(field_name)
        details[field_name] = None if value is None else str(value)
    raw = interval.get("details")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicationContractError("interval details are invalid JSON") from exc
    if raw is not None and not isinstance(raw, Mapping):
        raise PublicationContractError("interval details must be an object")
    for field_name in (
        "storage_asset_id",
        "identity_scheme",
        "identity_key_version",
        "mapping_version",
        "mapping_fingerprint",
    ):
        if raw is None or field_name not in raw:
            continue
        value = raw.get(field_name)
        if value is not None and not isinstance(value, str):
            raise PublicationContractError(
                f"interval storage provenance {field_name} must be text"
            )
        details[field_name] = value
    return details


def _details_object(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PublicationContractError(f"{field} are invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise PublicationContractError(f"{field} must be an object")
    return value


def _compute_activation_key(
    resource: Any,
    details: Any,
    *,
    details_field: str,
) -> str | None:
    resource_name = str(resource)
    activation_key = _COMPUTE_RESOURCE_ACTIVATIONS.get(resource_name)
    if activation_key is not None:
        return activation_key
    if resource_name != "workspace_pod":
        return None
    product_class = _details_object(details, details_field).get("product_class")
    if product_class == _IDE_WORKSPACE_PRODUCT_CLASS:
        return "ide_workspace_pod"
    return None


def _build_cursor_plan(
    interval: Mapping[str, Any],
    rate_rows: Sequence[Mapping[str, Any] | CanonicalRateVersion],
    *,
    creator_generation: int,
    plan_kind: str,
    discovered_at: datetime | None,
    discovery_evidence: Mapping[str, Any] | None,
    plan_id: UUID | None = None,
) -> FrozenPublicationPlan | None:
    """Build one ordinary or late cursor-advancing immutable segment."""
    if creator_generation <= 0:
        raise PublicationContractError("creator generation must be positive")
    if plan_kind not in _CURSOR_ADVANCING_PLAN_KINDS:
        raise PublicationContractError("cursor plan kind is invalid")
    if plan_kind == "usage":
        if discovered_at is not None or discovery_evidence is not None:
            raise PublicationContractError(
                "ordinary usage cannot carry discovery evidence"
            )
        discovered_text = None
    else:
        if discovered_at is None:
            raise PublicationContractError("late usage requires discovered_at")
        discovered_at = _aware_utc(discovered_at, "discovered_at")
        if not isinstance(discovery_evidence, Mapping) or not discovery_evidence:
            raise PublicationContractError(
                "late usage requires durable discovery evidence"
            )
        _validate_canonical_json(discovery_evidence)
        discovered_text = _timestamp_text(discovered_at)
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

    dimensions = capacity_dimensions(interval)
    rate_versions = [
        rate
        if isinstance(rate, CanonicalRateVersion)
        else CanonicalRateVersion.from_record(rate)
        for rate in rate_rows
    ]
    rates_by_unit = _rates_by_unit(rate_versions)
    force_unpriced = str(interval["resource"]) == "unmapped_block_volume"
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
        if force_unpriced:
            selected, boundary = None, None
        else:
            selected, boundary = _select_rate_and_boundary(
                rates_by_unit.get(dimension.unit, ()), segment_start
            )
        selected_rates[dimension.unit] = selected
        if boundary is not None:
            boundaries.append(boundary)
    segment_end = min(boundary for boundary in boundaries if boundary > segment_start)
    if segment_end > publishable_end:
        raise PublicationContractError("segment extends past confirmed usage")
    if discovered_at is not None and discovered_at < segment_end:
        raise PublicationContractError("late usage was discovered before period end")

    duration_us = duration_microseconds(segment_start, segment_end)
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
    resource = str(interval["resource"])
    mapping_version = details.get("mapping_version")
    mapping_fingerprint = details.get("mapping_fingerprint")
    if resource.startswith("block_volume_"):
        if (
            not isinstance(mapping_version, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", mapping_version)
            is None
            or not isinstance(mapping_fingerprint, str)
            or _HASH_RE.fullmatch(mapping_fingerprint) is None
        ):
            raise PublicationContractError(
                "mapped storage interval lacks immutable mapping provenance"
            )
    elif resource == "unmapped_block_volume" and (
        mapping_version is not None or mapping_fingerprint is not None
    ):
        raise PublicationContractError(
            "unmapped storage interval carries mapping provenance"
        )
    if discovery_evidence is not None:
        details["discovery_evidence"] = dict(discovery_evidence)
    events: list[PlannedUsageEvent] = []
    for ordinal, dimension in enumerate(dimensions):
        quantity_text = decimal_text(
            capacity_quantity(dimension, segment_start, segment_end)
        )
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
            "event_kind": plan_kind,
            "corrects_source": None,
            "corrects_source_id": None,
            "corrects_unit": None,
            "corrects_ts": None,
            "correction_group_id": None,
            "correction_reason": None,
            "correction_actor_id": None,
            "discovered_at": discovered_text,
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
        plan_kind=plan_kind,
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


def build_usage_plan(
    interval: Mapping[str, Any],
    rate_rows: Sequence[Mapping[str, Any] | CanonicalRateVersion],
    *,
    creator_generation: int,
    plan_id: UUID | None = None,
) -> FrozenPublicationPlan | None:
    """Build the next ordinary day/rate-boundary segment for one interval."""

    return _build_cursor_plan(
        interval,
        rate_rows,
        creator_generation=creator_generation,
        plan_kind="usage",
        discovered_at=None,
        discovery_evidence=None,
        plan_id=plan_id,
    )


def build_late_usage_plan(
    interval: Mapping[str, Any],
    rate_rows: Sequence[Mapping[str, Any] | CanonicalRateVersion],
    *,
    creator_generation: int,
    discovered_at: datetime,
    discovery_evidence: Mapping[str, Any],
    plan_id: UUID | None = None,
) -> FrozenPublicationPlan | None:
    """Build a nonnegative historical segment with durable discovery proof."""

    return _build_cursor_plan(
        interval,
        rate_rows,
        creator_generation=creator_generation,
        plan_kind="late-usage",
        discovered_at=discovered_at,
        discovery_evidence=discovery_evidence,
        plan_id=plan_id,
    )


def _correction_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool | float):
        raise PublicationContractError(f"{field_name} must be an exact decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PublicationContractError(f"{field_name} is invalid") from exc
    if not parsed.is_finite():
        raise PublicationContractError(f"{field_name} must be finite")
    return Decimal(decimal_text(parsed))


def _required_correction_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise PublicationContractError(
            f"correction {field_name} must be non-empty text"
        )
    return value


def _validate_correction_audit_contract(payload: Mapping[str, Any]) -> None:
    """Reject final correction payloads that the audit DB would reject.

    Publication plans are immutable once frozen.  This app-side mirror of the
    audit v2 shape checks therefore runs before plan insertion, so a reviewed
    override cannot create a permanently pending, unpublishable manifest.
    """

    for field_name in (
        "category",
        "resource",
        "measurement_basis",
        "cost_domain",
        "resource_class",
        "attribution_scope",
        "measurement_algorithm",
        "source_capacity_unit",
        "source_cluster",
        "source_kind",
        "source_uid",
        "unit",
    ):
        _required_correction_text(payload, field_name)
    for field_name in ("source_lifecycle_id", "source_interval_id"):
        _uuid_text(payload.get(field_name), field_name)

    capacity = int(str(payload["source_capacity_value"]))
    if capacity >= _AUDIT_NUMERIC_LIMIT:
        raise PublicationContractError(
            "correction source capacity exceeds the audit numeric contract"
        )

    cost_domain = str(payload["cost_domain"])
    if cost_domain not in {
        "workload-allocation",
        "physical-asset",
        "idle",
        "overhead",
    }:
        raise PublicationContractError("correction cost domain is invalid")

    attribution_scope = str(payload["attribution_scope"])
    if attribution_scope == "customer":
        if payload.get("ref_kind") not in {"job", "thread"}:
            raise PublicationContractError(
                "customer correction requires a job/thread reference"
            )
        _uuid_text(payload.get("ref_id"), "ref_id")
        _uuid_text(payload.get("user_id"), "user_id")
        if payload.get("project_id") is not None:
            _uuid_text(payload["project_id"], "project_id")
    elif attribution_scope in {"shared-platform", "unknown"}:
        if any(
            payload.get(field_name) is not None
            for field_name in ("user_id", "project_id", "ref_kind", "ref_id")
        ):
            raise PublicationContractError(
                "non-customer correction cannot carry customer attribution"
            )
    else:
        raise PublicationContractError("correction attribution scope is invalid")

    kind = str(payload["source_kind"])
    shape = (
        str(payload["category"]),
        str(payload["measurement_basis"]),
        str(payload["resource_class"]),
        cost_domain,
        str(payload["unit"]),
        str(payload["source_capacity_unit"]),
    )
    allowed_shapes = {
        "pod": {
            (
                "compute",
                "scheduler-request",
                "kubernetes-pod",
                "workload-allocation",
                "vcpu-hour",
                "millicore",
            ),
            (
                "compute",
                "scheduler-request",
                "kubernetes-pod",
                "workload-allocation",
                "gib-hour",
                "byte",
            ),
        },
        "vmi": {
            (
                "compute",
                "guest-provisioned",
                "virtual-machine",
                "workload-allocation",
                "vcpu-hour",
                "millicore",
            ),
            (
                "compute",
                "guest-provisioned",
                "virtual-machine",
                "workload-allocation",
                "gib-hour",
                "byte",
            ),
        },
        "pvc": {
            (
                "storage",
                "claim-requested",
                "persistent-volume-claim",
                "workload-allocation",
                "gib-hour",
                "byte",
            ),
            (
                "storage",
                "claim-requested",
                "persistent-volume-claim",
                "workload-allocation",
                "claim-hour",
                "instance",
            ),
        },
        "volume": {
            (
                "storage",
                "volume-provisioned",
                "persistent-volume",
                "physical-asset",
                "gib-hour",
                "byte",
            ),
            (
                "storage",
                "volume-provisioned",
                "persistent-volume",
                "physical-asset",
                "volume-hour",
                "instance",
            ),
        },
    }
    if shape not in allowed_shapes.get(kind, set()):
        raise PublicationContractError(
            "correction dimensions violate the typed audit resource contract"
        )
    if shape[-1] == "instance" and capacity != 1:
        raise PublicationContractError(
            "correction occurrence capacity must be exactly one instance"
        )


def build_correction_plan(
    interval: Mapping[str, Any],
    deltas: Sequence[CorrectionDelta],
    *,
    correction_reason: str,
    correction_actor_id: UUID | str,
    creator_generation: int,
    plan_revision: int,
    discovered_at: datetime | None = None,
    plan_id: UUID | None = None,
) -> FrozenPublicationPlan:
    """Freeze one reviewed, append-only signed correction group.

    The caller must strictly verify every ``delta.original`` against the audit
    ledger before committing this app-side intent.  All deltas in a plan refer
    to one immutable interval segment.  Reversal and positive replacement rows
    therefore publish atomically in one audit transaction.
    """

    if creator_generation <= 0:
        raise PublicationContractError("creator generation must be positive")
    if isinstance(plan_revision, bool) or plan_revision <= 0:
        raise PublicationContractError("correction plan revision must be positive")
    reason = str(correction_reason).strip()
    if (
        not reason
        or len(reason) > 2048
        or any(not character.isprintable() for character in reason)
    ):
        raise PublicationContractError(
            "correction reason must be 1-2048 printable characters"
        )
    actor_text = _uuid_text(correction_actor_id, "correction_actor_id")
    correction_id = plan_id or uuid4()
    if not deltas:
        raise PublicationContractError("correction plan requires at least one delta")
    if len(deltas) > _MAX_CORRECTION_EVENTS:
        raise PublicationContractError(
            f"correction plan exceeds {_MAX_CORRECTION_EVENTS} events"
        )
    discovered_text = None
    if discovered_at is not None:
        discovered_at = _aware_utc(discovered_at, "discovered_at")
        discovered_text = _timestamp_text(discovered_at)

    interval_id = UUID(str(interval["id"]))
    source_revision = str(interval["source_revision"])
    if not _HASH_RE.fullmatch(source_revision):
        raise PublicationContractError("interval source_revision must be SHA-256")

    period_start: datetime | None = None
    period_end: datetime | None = None
    events: list[PlannedUsageEvent] = []
    for ordinal, delta in enumerate(deltas):
        original = delta.original
        original_payload = dict(original.event.payload)
        claimed_hash = original_payload.pop("payload_hash", None)
        if (
            claimed_hash != original.event.row_hash
            or event_payload_hash(original_payload) != original.event.row_hash
        ):
            raise PublicationContractError("correction original payload hash mismatch")
        if (
            original_payload["source"] != "infra-allocation-v2"
            or original_payload["event_kind"] not in _CURSOR_ADVANCING_PLAN_KINDS
        ):
            raise PublicationContractError(
                "correction original must be ordinary or late infrastructure usage"
            )
        if original_payload["source_interval_id"] != str(interval_id):
            raise PublicationContractError(
                "correction original belongs to a different interval"
            )
        details = original_payload.get("details")
        if (
            not isinstance(details, Mapping)
            or details.get("source_revision") != source_revision
        ):
            raise PublicationContractError(
                "correction original revision differs from its interval"
            )

        original_start = _payload_timestamp(
            original_payload["period_start"], "original period_start"
        )
        original_end = _payload_timestamp(
            original_payload["period_end"], "original period_end"
        )
        if period_start is None:
            period_start, period_end = original_start, original_end
        elif (period_start, period_end) != (original_start, original_end):
            raise PublicationContractError(
                "one correction plan cannot span multiple original periods"
            )

        quantity = _correction_decimal(delta.quantity, "correction quantity")
        if quantity == 0:
            raise PublicationContractError("correction quantity cannot be zero")
        overrides = dict(delta.payload_overrides)
        unknown = overrides.keys() - _CORRECTION_OVERRIDE_FIELDS
        if unknown:
            raise PublicationContractError(
                "correction overrides protected field(s): " + ", ".join(sorted(unknown))
            )
        payload = dict(original_payload)
        details_override = overrides.pop("details", None)
        if details_override is not None:
            if not isinstance(details_override, Mapping):
                raise PublicationContractError(
                    "correction details override must be an object"
                )
            merged_details = dict(details)
            merged_details.update(details_override)
            payload["details"] = merged_details
        else:
            payload["details"] = dict(details)
        payload["details"]["source_revision"] = source_revision
        payload["details"]["corrects_payload_hash"] = original.event.row_hash
        original_quantity = _correction_decimal(
            original_payload["quantity"], "correction original quantity"
        )
        if original_quantity <= 0:
            raise PublicationContractError(
                "correction original quantity must be positive"
            )
        payload["details"]["corrects_quantity"] = decimal_text(original_quantity)

        if delta.inherit_rate:
            if "rate_usd" in overrides or delta.canonical_rate_version_id is not None:
                raise PublicationContractError(
                    "inherited correction rate cannot be overridden"
                )
            selected_rate_id = original.canonical_rate_version_id
            rate_value = payload["rate_usd"]
        else:
            if "rate_usd" not in overrides:
                raise PublicationContractError(
                    "replacement correction rate must be explicit"
                )
            rate_value = overrides.pop("rate_usd")
            selected_rate_id = delta.canonical_rate_version_id

        payload.update(overrides)
        for field_name in ("user_id", "project_id", "ref_id"):
            if payload[field_name] is not None:
                payload[field_name] = _uuid_text(payload[field_name], field_name)
        capacity_value = payload["source_capacity_value"]
        if isinstance(capacity_value, bool):
            raise PublicationContractError(
                "correction source capacity must be an integer"
            )
        try:
            capacity_integer = int(str(capacity_value))
        except (TypeError, ValueError) as exc:
            raise PublicationContractError(
                "correction source capacity must be an integer"
            ) from exc
        if capacity_integer < 0 or str(capacity_integer) != str(capacity_value):
            raise PublicationContractError(
                "correction source capacity must be canonical/nonnegative"
            )
        payload["source_capacity_value"] = str(capacity_integer)

        if delta.inherit_rate:
            changed_selectors = [
                field_name
                for field_name in _CANONICAL_RATE_SELECTOR_FIELDS
                if payload[field_name] != original_payload[field_name]
            ]
            if changed_selectors:
                raise PublicationContractError(
                    "inherited correction rate selector changed: "
                    + ", ".join(changed_selectors)
                )
        if quantity < 0:
            if not delta.inherit_rate:
                raise PublicationContractError(
                    "negative correction must inherit the original rate"
                )
            changed_dimensions = [
                field_name
                for field_name in _CORRECTION_DIMENSION_FIELDS
                if payload[field_name] != original_payload[field_name]
            ]
            if changed_dimensions:
                raise PublicationContractError(
                    "negative correction changed original dimensions: "
                    + ", ".join(changed_dimensions)
                )

        payload["quantity"] = decimal_text(quantity)
        if rate_value is None:
            if selected_rate_id is not None:
                raise PublicationContractError(
                    "unpriced correction cannot reference a rate version"
                )
            payload["rate_usd"] = None
            payload["cost_usd"] = None
        else:
            if selected_rate_id is None:
                raise PublicationContractError(
                    "priced correction requires an immutable rate version"
                )
            rate = _correction_decimal(rate_value, "correction rate")
            if rate < 0:
                raise PublicationContractError("correction rate cannot be negative")
            payload["rate_usd"] = decimal_text(rate)
            with localcontext() as context:
                context.prec = 80
                cost = quantity * rate
            payload["cost_usd"] = decimal_text(cost)

        payload.update(
            {
                "ts": original_payload["period_start"],
                "source": "infra-allocation-correction-v2",
                "source_id": _correction_source_id(correction_id, ordinal),
                "source_interval_id": str(interval_id),
                "event_kind": "correction",
                "corrects_source": original_payload["source"],
                "corrects_source_id": original_payload["source_id"],
                "corrects_unit": original_payload["unit"],
                "corrects_ts": original_payload["ts"],
                "correction_group_id": str(correction_id),
                "correction_reason": reason,
                "correction_actor_id": actor_text,
                "discovered_at": discovered_text,
            }
        )
        _validate_correction_audit_contract(payload)
        row_hash = event_payload_hash(payload)
        payload["payload_hash"] = row_hash
        events.append(
            PlannedUsageEvent(
                ordinal=ordinal,
                event=StrictUsageEvent(payload=payload, row_hash=row_hash),
                canonical_rate_version_id=selected_rate_id,
            )
        )

    assert period_start is not None and period_end is not None
    if discovered_at is not None and discovered_at < period_end:
        raise PublicationContractError("correction discovery precedes period end")
    event_tuple = tuple(events)
    return FrozenPublicationPlan(
        id=correction_id,
        source_interval_id=interval_id,
        source_revision=source_revision,
        plan_kind="correction",
        plan_revision=plan_revision,
        advances_cursor=False,
        previous_materialized_through=None,
        correction_group_id=correction_id,
        period_start=period_start,
        period_end=period_end,
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

_STORAGE_CANDIDATE_FENCE_MARKER = (
    "/* infra-publication:storage-source-candidate-fence */"
)
_COMPUTE_CANDIDATE_FENCE_MARKER = "/* infra-publication:compute-candidate-fence */"
_COMPUTE_CANDIDATE_DISABLED_SQL = """
  AND NOT (
      (interval.source_kind = 'pod'
       AND interval.category = 'compute'
       AND interval.resource = 'agent_pod')
      OR (interval.source_kind = 'pod'
          AND interval.category = 'compute'
          AND interval.resource = 'workspace_pod'
          AND COALESCE(interval.details->>'product_class', '') = 'ide-session')
      OR (interval.source_kind = 'vmi'
          AND interval.category = 'compute'
          AND interval.resource = 'workspace_vm')
  )
"""
_COMPUTE_CANDIDATE_FENCE_SQL = """
  AND (
      CASE
          WHEN interval.source_kind = 'pod'
               AND interval.category = 'compute'
               AND interval.resource = 'agent_pod'
              THEN 'agent_pod'
          WHEN interval.source_kind = 'pod'
               AND interval.category = 'compute'
               AND interval.resource = 'workspace_pod'
               AND interval.details->>'product_class' = 'ide-session'
              THEN 'ide_workspace_pod'
          WHEN interval.source_kind = 'vmi'
               AND interval.category = 'compute'
               AND interval.resource = 'workspace_vm'
              THEN 'workspace_vm'
          ELSE NULL
      END IS NULL
      OR EXISTS (
          SELECT 1
          FROM compute_metering_epoch_authorities AS compute_authority
          JOIN resource_inventory_scope_epochs AS compute_epoch
            ON compute_epoch.id = compute_authority.inventory_scope_epoch_id
           AND compute_epoch.scope_id =
               compute_authority.inventory_scope_id
          JOIN compute_metering_activation AS compute_activation
            ON compute_activation.activation_key =
               compute_authority.activation_key
          WHERE compute_authority.activation_key = CASE
                    WHEN interval.resource = 'agent_pod' THEN 'agent_pod'
                    WHEN interval.resource = 'workspace_pod'
                        THEN 'ide_workspace_pod'
                    ELSE 'workspace_vm'
                END
            AND compute_authority.inventory_scope_id =
                interval.inventory_scope_id
            AND compute_authority.inventory_scope_epoch_id =
                interval.compute_scope_epoch_id
            AND compute_activation.state = 'active'
            AND compute_activation.activated_at IS NOT NULL
            AND statement_timestamp() >= GREATEST(
                compute_activation.activated_at,
                compute_authority.effective_from
            )
            AND interval.started_at >= GREATEST(
                compute_activation.activated_at,
                compute_authority.effective_from
            )
            AND interval.materialized_through >= GREATEST(
                compute_activation.activated_at,
                compute_authority.effective_from
            )
            AND interval.materialized_through <
                COALESCE(compute_epoch.retired_at, 'infinity'::timestamptz)
            AND COALESCE(interval.ended_at, interval.last_confirmed_at) <=
                COALESCE(compute_epoch.retired_at, 'infinity'::timestamptz)
      )
  )
"""
_STORAGE_CANDIDATE_DISABLED_SQL = """
  AND interval.measurement_basis NOT IN (
      'claim-requested', 'volume-provisioned'
  )
"""
_STORAGE_CANDIDATE_FENCE_SQL = """
  AND (
      interval.measurement_basis NOT IN (
          'claim-requested', 'volume-provisioned'
      )
      OR EXISTS (
          SELECT 1
          FROM resource_inventory_scopes AS storage_scope
          JOIN storage_metering_source_requirements AS storage_requirement
            ON storage_requirement.inventory_scope_id = storage_scope.id
           AND storage_requirement.measurement_basis = interval.measurement_basis
           AND storage_requirement.collector_id = storage_scope.collector_id
           AND storage_requirement.source_cluster = storage_scope.source_cluster
           AND storage_requirement.requirement_role = 'quantity'
          JOIN storage_metering_source_activations AS storage_source_activation
            ON storage_source_activation.measurement_basis =
               storage_requirement.measurement_basis
           AND storage_source_activation.collector_id =
               storage_requirement.collector_id
           AND storage_source_activation.source_cluster =
               storage_requirement.source_cluster
          JOIN storage_metering_activation AS storage_global_activation
            ON storage_global_activation.measurement_basis =
               storage_requirement.measurement_basis
          JOIN unnest($5::text[], $6::text[], $7::text[])
               AS storage_policy(
                   measurement_basis, collector_id, source_cluster
               )
            ON storage_policy.measurement_basis =
               storage_requirement.measurement_basis
           AND storage_policy.collector_id = storage_requirement.collector_id
           AND storage_policy.source_cluster = storage_requirement.source_cluster
          WHERE storage_scope.id = interval.inventory_scope_id
            AND storage_scope.source_cluster = interval.source_cluster
            AND storage_source_activation.state = 'active'
            AND storage_source_activation.activated_at IS NOT NULL
            AND storage_global_activation.state = 'active'
            AND storage_global_activation.activated_at IS NOT NULL
            AND statement_timestamp() >= GREATEST(
                storage_source_activation.activated_at,
                storage_global_activation.activated_at
            )
            AND interval.materialized_through >= GREATEST(
                storage_source_activation.activated_at,
                storage_global_activation.activated_at
            )
      )
  )
"""


def _candidate_intervals_sql(
    *,
    compute_policy_enabled: bool = False,
    storage_policy_enabled: bool,
) -> str:
    sql = _CANDIDATE_INTERVALS_SQL.replace(
        _COMPUTE_CANDIDATE_FENCE_MARKER,
        (
            _COMPUTE_CANDIDATE_FENCE_SQL
            if compute_policy_enabled
            else _COMPUTE_CANDIDATE_DISABLED_SQL
        ),
    )
    return sql.replace(
        _STORAGE_CANDIDATE_FENCE_MARKER,
        (
            _STORAGE_CANDIDATE_FENCE_SQL
            if storage_policy_enabled
            else _STORAGE_CANDIDATE_DISABLED_SQL
        ),
    )


_CANDIDATE_INTERVALS_SQL = """
/* infra-publication:candidates */
SELECT interval.*,
       evidence.id AS discovery_snapshot_id,
       evidence.received_at AS discovery_received_at,
       evidence.complete AS discovery_snapshot_complete,
       evidence.manifest_state AS discovery_manifest_state,
       absence.id AS absence_snapshot_id,
       absence.received_at AS absence_received_at,
       absence.manifest_state AS absence_manifest_state,
       watch_evidence.id AS discovery_watch_event_id,
       watch_evidence.watch_session_id AS discovery_watch_session_id,
       watch_evidence.received_at AS discovery_watch_received_at,
       watch_evidence.event_type AS discovery_watch_event_type,
       watch_evidence.mutation_action AS discovery_watch_action,
       watch_evidence.resource_version AS discovery_watch_resource_version,
       successor.id AS successor_interval_id,
       successor.inventory_scope_id AS successor_inventory_scope_id,
       successor.source_lifecycle_id AS successor_lifecycle_id,
       successor.revision_no AS successor_revision_no,
       successor.source_kind AS successor_source_kind,
       successor.source_uid AS successor_source_uid,
       successor.started_at AS successor_started_at,
       successor.start_time_source AS successor_start_time_source,
       successor.details->>'start_evidence_source'
            AS successor_start_evidence_source,
       successor_snapshot.id AS successor_snapshot_id,
       successor_snapshot.received_at AS successor_snapshot_received_at,
       successor_snapshot.complete AS successor_snapshot_complete,
       successor_snapshot.manifest_state AS successor_snapshot_manifest_state,
       successor_watch.id AS successor_watch_event_id,
       successor_watch.watch_session_id AS successor_watch_session_id,
       successor_watch.received_at AS successor_watch_received_at,
       successor_watch.event_type AS successor_watch_event_type,
       successor_watch.mutation_action AS successor_watch_action,
       successor_watch.resource_version AS successor_watch_resource_version
FROM resource_intervals AS interval
LEFT JOIN resource_inventory_snapshots AS evidence
  ON evidence.id = interval.last_seen_snapshot_id
 AND evidence.inventory_scope_id = interval.inventory_scope_id
LEFT JOIN LATERAL (
    SELECT snapshot.id, snapshot.received_at, snapshot.manifest_state
    FROM resource_inventory_scope_epochs AS epoch
    JOIN resource_inventory_snapshots AS snapshot
      ON snapshot.scope_epoch_id = epoch.id
    WHERE interval.end_time_source = 'complete-inventory-absence'
      AND interval.ended_at IS NOT NULL
      AND epoch.scope_id = interval.inventory_scope_id
      AND snapshot.received_at = interval.ended_at
      AND snapshot.complete IS TRUE
      AND snapshot.manifest_state IN ('sealed', 'items-expired')
    ORDER BY snapshot.id
    LIMIT 1
) AS absence ON TRUE
LEFT JOIN LATERAL (
    SELECT event.id, event.watch_session_id, event.received_at,
           event.event_type, event.mutation_action, event.resource_version
    FROM resource_inventory_scope_epochs AS epoch
    JOIN resource_inventory_watch_events AS event
      ON event.scope_epoch_id = epoch.id
     AND event.source_kind = interval.source_kind
     AND event.source_uid = interval.source_uid
    WHERE epoch.scope_id = interval.inventory_scope_id
      AND event.affected_interval_id = interval.id
    ORDER BY event.received_at DESC, event.watch_session_id, event.ordinal DESC
    LIMIT 1
) AS watch_evidence ON TRUE
LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM resource_intervals AS candidate
    WHERE interval.ended_at IS NOT NULL
      AND candidate.source_lifecycle_id = interval.source_lifecycle_id
      AND candidate.inventory_scope_id = interval.inventory_scope_id
      AND candidate.revision_no = interval.revision_no + 1
      AND candidate.source_kind = interval.source_kind
      AND candidate.source_uid = interval.source_uid
      AND candidate.started_at = interval.ended_at
      AND candidate.start_time_source = 'app-db-received'
      AND candidate.details->>'start_evidence_source'
            = 'observed-revision-boundary'
    ORDER BY candidate.id
    LIMIT 1
) AS successor ON TRUE
LEFT JOIN resource_inventory_snapshots AS successor_snapshot
  ON successor_snapshot.id = successor.last_seen_snapshot_id
 AND successor_snapshot.inventory_scope_id = successor.inventory_scope_id
 AND successor_snapshot.complete IS TRUE
 AND successor_snapshot.manifest_state IN ('sealed', 'items-expired')
LEFT JOIN LATERAL (
    SELECT event.id, event.watch_session_id, event.received_at,
           event.event_type, event.mutation_action, event.resource_version
    FROM resource_inventory_scope_epochs AS epoch
    JOIN resource_inventory_watch_events AS event
      ON event.scope_epoch_id = epoch.id
     AND event.source_kind = successor.source_kind
     AND event.source_uid = successor.source_uid
    WHERE epoch.scope_id = successor.inventory_scope_id
      AND event.affected_interval_id = successor.id
      AND event.received_at = successor.started_at
      AND event.mutation_action = 'revise'
    ORDER BY event.watch_session_id, event.ordinal DESC
    LIMIT 1
) AS successor_watch ON TRUE
WHERE interval.resource = ANY($1::text[])
  AND ($4::boolean OR interval.resource <> 'workspace_pod'
       OR COALESCE(interval.details->>'product_class', '') <> 'ide-session')
/* infra-publication:compute-candidate-fence */
/* infra-publication:storage-source-candidate-fence */
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
SELECT day_state.state, day_state.coverage_status,
       day_state.coverage_revision,
       (to_jsonb(day_state)->>'coverage_sequence')::bigint AS coverage_sequence,
       day_state.unknown_ranges
FROM infra_usage_day_state AS day_state
WHERE day_state.day = $1
FOR UPDATE
"""

_LOCK_INTERVAL_SQL = """
/* infra-publication:lock-interval */
SELECT *
FROM resource_intervals
WHERE id = $1 AND source_revision = $2
FOR UPDATE
"""

_PUBLICATION_INTERVAL_FENCE_SQL = """
/* infra-publication:publication-interval-fence */
SELECT *
FROM resource_intervals
WHERE id = $1 AND source_revision = $2
FOR SHARE
"""

_COMPUTE_EPOCH_SET_LOCK_SQL = """
/* infra-publication:compute-epoch-set-lock */
SELECT requirement.activation_key, epoch.id
FROM compute_metering_epoch_authorities AS requirement
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.id = requirement.inventory_scope_epoch_id
 AND epoch.scope_id = requirement.inventory_scope_id
WHERE requirement.activation_key = ANY($1::text[])
ORDER BY epoch.id, requirement.activation_key
FOR SHARE OF epoch
"""

_COMPUTE_EXACT_EPOCH_FENCE_SQL = """
/* infra-publication:compute-exact-epoch-fence */
SELECT authority.effective_from,
       epoch.id AS inventory_scope_epoch_id,
       epoch.retired_at
FROM compute_metering_epoch_authorities AS authority
JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.id = authority.inventory_scope_epoch_id
 AND epoch.scope_id = authority.inventory_scope_id
WHERE authority.activation_key = $1
  AND authority.inventory_scope_id = $2
  AND authority.inventory_scope_epoch_id = $3
FOR SHARE OF epoch
"""

_COMPUTE_ACTIVATION_FENCE_SQL = """
/* infra-publication:compute-activation-fence */
SELECT activation_key, state, activated_at,
       statement_timestamp() AS database_time
FROM compute_metering_activation
WHERE activation_key = $1
FOR SHARE
"""

_STORAGE_PUBLICATION_FENCE_SQL = """
/* infra-publication:storage-source-fence */
SELECT scope.collector_id, scope.source_cluster,
       requirement.requirement_role,
       source_activation.state AS source_state,
       source_activation.activated_at AS source_activated_at,
       global_activation.state AS global_state,
       global_activation.activated_at AS global_activated_at,
       statement_timestamp() AS database_time
FROM resource_inventory_scopes AS scope
JOIN storage_metering_source_requirements AS requirement
  ON requirement.inventory_scope_id = scope.id
 AND requirement.collector_id = scope.collector_id
 AND requirement.source_cluster = scope.source_cluster
 AND requirement.measurement_basis = $2
 AND requirement.requirement_role = 'quantity'
JOIN storage_metering_source_activations AS source_activation
  ON source_activation.measurement_basis = requirement.measurement_basis
 AND source_activation.collector_id = requirement.collector_id
 AND source_activation.source_cluster = requirement.source_cluster
JOIN storage_metering_activation AS global_activation
  ON global_activation.measurement_basis = requirement.measurement_basis
WHERE scope.id = $1
FOR SHARE OF scope, requirement, source_activation, global_activation
"""

_NEXT_CORRECTION_REVISION_SQL = """
/* infra-publication:next-correction-revision */
SELECT COALESCE(MAX(plan_revision), 0) + 1
FROM resource_publication_plans
WHERE source_interval_id = $1
  AND period_start = $2
  AND period_end = $3
  AND plan_kind = 'correction'
"""

_ENSURE_DAY_STATE_SQL = """
/* infra-publication:ensure-day-state */
INSERT INTO infra_usage_day_state (day)
VALUES ($1)
ON CONFLICT (day) DO NOTHING
"""

_DEGRADE_SEALED_DAY_SQL = """
/* infra-publication:degrade-sealed-day */
UPDATE infra_usage_day_state
SET coverage_status = 'partial',
    coverage_revision = $4,
    coverage_sequence = coverage_sequence + 1,
    unknown_ranges = unknown_ranges || $5::jsonb,
    updated_at = statement_timestamp()
WHERE day = $1
  AND state = 'sealed'
  AND coverage_sequence = $2
  AND coverage_revision = $3
RETURNING coverage_sequence, coverage_revision
"""


def _successor_revision_boundary(
    interval: Mapping[str, Any],
) -> tuple[str, datetime] | None:
    """Validate the exact immutable successor selected by the candidate SQL."""

    successor_id = interval.get("successor_interval_id")
    if successor_id is None:
        return None
    try:
        successor_text = _uuid_text(successor_id, "successor interval_id")
        old_id = _uuid_text(interval["id"], "source interval_id")
        old_scope = _uuid_text(interval["inventory_scope_id"], "inventory_scope_id")
        successor_scope = _uuid_text(
            interval["successor_inventory_scope_id"],
            "successor inventory_scope_id",
        )
        old_lifecycle = _uuid_text(
            interval["source_lifecycle_id"], "source_lifecycle_id"
        )
        successor_lifecycle = _uuid_text(
            interval["successor_lifecycle_id"], "successor source_lifecycle_id"
        )
        old_revision = int(interval["revision_no"])
        successor_revision = int(interval["successor_revision_no"])
        ended_at = _aware_utc(interval["ended_at"], "successor boundary ended_at")
        started_at = _aware_utc(
            interval["successor_started_at"], "successor started_at"
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationConflictError(
            "late discovery successor evidence is incomplete"
        ) from exc
    if (
        successor_text == old_id
        or successor_scope != old_scope
        or successor_lifecycle != old_lifecycle
        or isinstance(interval.get("revision_no"), bool)
        or isinstance(interval.get("successor_revision_no"), bool)
        or successor_revision != old_revision + 1
        or str(interval.get("successor_source_kind"))
        != str(interval.get("source_kind"))
        or str(interval.get("successor_source_uid")) != str(interval.get("source_uid"))
        or started_at != ended_at
        or interval.get("successor_start_time_source") != "app-db-received"
        or interval.get("successor_start_evidence_source")
        != "observed-revision-boundary"
    ):
        raise PublicationConflictError(
            "late discovery successor does not match the immutable revision boundary"
        )
    assert successor_text is not None
    return successor_text, started_at


def _late_discovery_evidence(
    interval: Mapping[str, Any],
    period_end: datetime,
) -> tuple[datetime, dict[str, Any]]:
    """Choose durable evidence proving a segment was discovered after its end."""

    period_end = _aware_utc(period_end, "late period_end")
    candidates: list[tuple[datetime, dict[str, Any]]] = []

    snapshot_id = interval.get("discovery_snapshot_id")
    snapshot_at = interval.get("discovery_received_at")
    snapshot_state = interval.get("discovery_manifest_state")
    if (
        snapshot_id is not None
        and snapshot_at is not None
        and bool(interval.get("discovery_snapshot_complete"))
        and snapshot_state in {"sealed", "items-expired"}
    ):
        received_at = _aware_utc(snapshot_at, "discovery_received_at")
        candidates.append(
            (
                received_at,
                {
                    "kind": "complete-inventory-sighting",
                    "snapshot_id": str(snapshot_id),
                    "received_at": _timestamp_text(received_at),
                    "manifest_state": str(snapshot_state),
                },
            )
        )

    absence_id = interval.get("absence_snapshot_id")
    absence_at = interval.get("absence_received_at")
    absence_state = interval.get("absence_manifest_state")
    if (
        absence_id is not None
        and absence_at is not None
        and absence_state in {"sealed", "items-expired"}
    ):
        received_at = _aware_utc(absence_at, "absence_received_at")
        candidates.append(
            (
                received_at,
                {
                    "kind": "complete-inventory-absence",
                    "snapshot_id": str(absence_id),
                    "received_at": _timestamp_text(received_at),
                    "manifest_state": str(absence_state),
                },
            )
        )

    watch_event_id = interval.get("discovery_watch_event_id")
    watch_session_id = interval.get("discovery_watch_session_id")
    watch_at = interval.get("discovery_watch_received_at")
    if (
        watch_event_id is not None
        and watch_session_id is not None
        and watch_at is not None
    ):
        received_at = _aware_utc(watch_at, "discovery_watch_received_at")
        candidates.append(
            (
                received_at,
                {
                    "kind": "authenticated-watch-receipt",
                    "watch_session_id": str(watch_session_id),
                    "event_id": str(watch_event_id),
                    "received_at": _timestamp_text(received_at),
                    "event_type": str(interval.get("discovery_watch_event_type")),
                    "mutation_action": str(interval.get("discovery_watch_action")),
                    "resource_version": (
                        None
                        if interval.get("discovery_watch_resource_version") is None
                        else str(interval["discovery_watch_resource_version"])
                    ),
                },
            )
        )

    successor = _successor_revision_boundary(interval)
    if successor is not None:
        successor_id, successor_at = successor
        successor_snapshot_id = interval.get("successor_snapshot_id")
        successor_snapshot_at = interval.get("successor_snapshot_received_at")
        successor_snapshot_state = interval.get("successor_snapshot_manifest_state")
        if (
            successor_snapshot_id is not None
            and successor_snapshot_at is not None
            and interval.get("successor_snapshot_complete") is True
            and successor_snapshot_state in {"sealed", "items-expired"}
        ):
            received_at = _aware_utc(
                successor_snapshot_at, "successor snapshot received_at"
            )
            if received_at < successor_at:
                raise PublicationConflictError(
                    "successor snapshot predates its immutable revision boundary"
                )
            candidates.append(
                (
                    received_at,
                    {
                        "kind": "successor-complete-inventory-sighting",
                        "successor_interval_id": successor_id,
                        "revision_boundary": _timestamp_text(successor_at),
                        "snapshot_id": str(successor_snapshot_id),
                        "received_at": _timestamp_text(received_at),
                        "manifest_state": str(successor_snapshot_state),
                    },
                )
            )

        successor_watch_event_id = interval.get("successor_watch_event_id")
        successor_watch_session_id = interval.get("successor_watch_session_id")
        successor_watch_at = interval.get("successor_watch_received_at")
        if (
            successor_watch_event_id is not None
            and successor_watch_session_id is not None
            and successor_watch_at is not None
        ):
            received_at = _aware_utc(successor_watch_at, "successor watch received_at")
            if (
                received_at != successor_at
                or interval.get("successor_watch_action") != "revise"
                or interval.get("successor_watch_event_type")
                not in {"added", "modified"}
            ):
                raise PublicationConflictError(
                    "successor WATCH receipt does not match its revision boundary"
                )
            candidates.append(
                (
                    received_at,
                    {
                        "kind": "successor-authenticated-watch-receipt",
                        "successor_interval_id": successor_id,
                        "revision_boundary": _timestamp_text(successor_at),
                        "watch_session_id": str(successor_watch_session_id),
                        "event_id": str(successor_watch_event_id),
                        "received_at": _timestamp_text(received_at),
                        "event_type": str(interval.get("successor_watch_event_type")),
                        "mutation_action": "revise",
                        "resource_version": (
                            None
                            if interval.get("successor_watch_resource_version") is None
                            else str(interval["successor_watch_resource_version"])
                        ),
                    },
                )
            )

    eligible = [candidate for candidate in candidates if candidate[0] >= period_end]
    if not eligible:
        raise PublicationConflictError(
            "sealed-day interval lacks authoritative late discovery evidence"
        )
    return min(
        eligible,
        key=lambda candidate: (
            candidate[0],
            json.dumps(candidate[1], separators=(",", ":"), sort_keys=True),
        ),
    )


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
ORDER BY last_attempt_at ASC NULLS FIRST, attempt_count, created_at, id
LIMIT 1
"""

_PLAN_EVENTS_SQL = """
/* infra-publication:plan-events */
SELECT ordinal, canonical_rate_version_id, row_hash, event_payload
FROM resource_publication_plan_events
WHERE plan_id = $1
ORDER BY ordinal
"""

_CORRECTION_ORIGINAL_SQL = """
/* infra-publication:correction-original */
SELECT plan.id AS original_plan_id,
       plan.source_interval_id, plan.source_revision,
       plan.plan_kind, plan.state,
       event.ordinal, event.canonical_rate_version_id,
       event.row_hash, event.event_payload
FROM resource_publication_plan_events AS event
JOIN resource_publication_plans AS plan ON plan.id = event.plan_id
WHERE event.source = $1
  AND event.source_id = $2
  AND event.unit = $3
  AND event.ts = $4
  AND event.row_hash = $5
"""

_CORRECTION_INTERVAL_SQL = """
/* infra-publication:correction-interval */
SELECT * FROM resource_intervals
WHERE id = $1 AND source_revision = $2
"""

_CORRECTION_PLAN_BY_ID_SQL = """
/* infra-publication:correction-plan-by-id */
SELECT * FROM resource_publication_plans
WHERE id = $1
"""

_CORRECTION_RATE_SQL = """
/* infra-publication:correction-rate */
SELECT TRUE
FROM usage_rates_v2
WHERE id = $1
  AND unit = $2
  AND usd_per_unit = $3
  AND cost_domain = $4
  AND measurement_basis = $5
  AND category = $6
  AND resource_class = $7
  AND resource = $8
  AND effective_from <= $9
  AND (effective_to IS NULL OR effective_to >= $10)
FOR SHARE
"""

_LOCK_CORRECTION_ORIGINALS_SQL = """
/* infra-publication:lock-correction-originals */
WITH requested AS (
    SELECT source, source_id, unit, corrects_ts
    FROM unnest($1::text[], $2::text[], $3::text[], $4::text[])
         WITH ORDINALITY AS item(
             source, source_id, unit, corrects_ts, ordinality
         )
)
SELECT requested.source, requested.source_id, requested.unit,
       requested.corrects_ts,
       (original.event_payload->>'quantity')::numeric AS original_quantity,
       original.row_hash AS original_row_hash
FROM requested
JOIN resource_publication_plan_events AS original
  ON original.source = requested.source
 AND original.source_id = requested.source_id
 AND original.unit = requested.unit
 AND original.ts = requested.corrects_ts::timestamptz
JOIN resource_publication_plans AS original_plan
  ON original_plan.id = original.plan_id
 AND original_plan.plan_kind IN ('usage', 'late-usage')
 AND original_plan.state = 'published'
 AND original_plan.source_interval_id = $5
ORDER BY original.plan_id, original.ordinal
FOR UPDATE OF original_plan, original
"""

_CORRECTION_NEGATIVE_TOTALS_SQL = """
/* infra-publication:correction-negative-totals */
WITH requested AS (
    SELECT source, source_id, unit, corrects_ts, ordinality
    FROM unnest($1::text[], $2::text[], $3::text[], $4::text[])
         WITH ORDINALITY AS item(
             source, source_id, unit, corrects_ts, ordinality
         )
)
SELECT requested.ordinality,
       COALESCE((
           SELECT SUM(-(event.event_payload->>'quantity')::numeric)
           FROM resource_publication_plan_events AS event
           JOIN resource_publication_plans AS plan ON plan.id = event.plan_id
           WHERE plan.plan_kind = 'correction'
             AND plan.state IN ('planned', 'published')
             AND plan.id <> $5
             AND event.event_payload->>'corrects_source' = requested.source
             AND event.event_payload->>'corrects_source_id' = requested.source_id
             AND event.event_payload->>'corrects_unit' = requested.unit
             AND event.event_payload->>'corrects_ts' = requested.corrects_ts
             AND (event.event_payload->>'quantity')::numeric < 0
       ), 0::numeric) AS negative_quantity
FROM requested
ORDER BY requested.ordinality
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

_FINALIZE_CONFLICT_SQL = """
/* infra-publication:finalize-conflict */
UPDATE resource_publication_plans
SET state = 'conflict',
    attempt_count = attempt_count + 1,
    last_attempt_at = statement_timestamp(),
    sanitized_error = $2::jsonb
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
        ide_workspace_pod_enabled: bool = False,
        storage_publication_policy: StoragePublicationPolicy | None = None,
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
        self._ide_workspace_pod_enabled = bool(ide_workspace_pod_enabled)
        if storage_publication_policy is not None and not isinstance(
            storage_publication_policy, StoragePublicationPolicy
        ):
            raise ValueError("materializer storage_publication_policy must be typed")
        self._storage_publication_policy = (
            storage_publication_policy or StoragePublicationPolicy()
        )

    def _require_enabled(self) -> None:
        if not self._publication_enabled:
            raise PublicationDisabledError(
                "infrastructure publication runtime gate is disabled"
            )

    def _require_class_enabled(
        self,
        *,
        resource: Any,
        activation_key: str | None,
        context: str,
    ) -> None:
        resource_name = str(resource)
        if resource_name not in self._enabled_resources:
            raise PublicationDisabledError(
                f"{context} resource {resource_name!r} publication gate is disabled"
            )
        if (
            activation_key == "ide_workspace_pod"
            and not self._ide_workspace_pod_enabled
        ):
            raise PublicationDisabledError(
                f"{context} IDE workspace Pod publication gate is disabled"
            )

    def _enabled_compute_activation_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        if "agent_pod" in self._enabled_resources:
            keys.append("agent_pod")
        if (
            "workspace_pod" in self._enabled_resources
            and self._ide_workspace_pod_enabled
        ):
            keys.append("ide_workspace_pod")
        if "workspace_vm" in self._enabled_resources:
            keys.append("workspace_vm")
        return tuple(keys)

    async def _lock_compute_publication_epochs(
        self,
        conn: asyncpg.Connection,
    ) -> None:
        """Pre-lock exact epochs before any interval row in a write path."""

        keys = self._enabled_compute_activation_keys()
        if keys:
            await conn.fetch(_COMPUTE_EPOCH_SET_LOCK_SQL, list(keys))

    async def _enforce_plan_publication_fence(
        self,
        conn: asyncpg.Connection,
        interval: Mapping[str, Any],
        plan: FrozenPublicationPlan,
    ) -> None:
        """Recheck source/event class gates and compute activation in-app DB.

        Candidate selection is not a publication boundary: a pending plan may
        survive a rollout that disables its class, and reviewed corrections
        may intentionally change resource dimensions.  Every freeze and every
        audit attempt therefore validates both the immutable source interval
        and all frozen event targets under the current process gates.  Compute
        activation is re-read under a row lock so a historical correction
        cannot manufacture pre-activation usage.
        """

        required_activations: set[str] = set()
        interval_key = _compute_activation_key(
            interval["resource"],
            interval.get("details"),
            details_field="interval details",
        )
        self._require_class_enabled(
            resource=interval["resource"],
            activation_key=interval_key,
            context="source interval",
        )
        if interval_key is not None:
            required_activations.add(interval_key)

        measurement_basis = str(interval["measurement_basis"])
        storage_activation: Mapping[str, Any] | None = None
        if measurement_basis in _STORAGE_MEASUREMENT_BASES:
            if not self._storage_publication_policy.authorities:
                raise PublicationDisabledError(
                    "storage source publication policy is disabled"
                )
            storage_activation = await conn.fetchrow(
                _STORAGE_PUBLICATION_FENCE_SQL,
                interval["inventory_scope_id"],
                measurement_basis,
            )
            if storage_activation is None:
                raise PublicationDisabledError(
                    "storage source lacks an exact quantity activation requirement"
                )
            collector_id = str(storage_activation["collector_id"])
            source_cluster = str(storage_activation["source_cluster"])
            if str(interval["source_cluster"]) != source_cluster:
                raise PublicationDisabledError(
                    "storage interval source does not match its inventory authority"
                )
            if not self._storage_publication_policy.allows(
                measurement_basis=measurement_basis,
                collector_id=collector_id,
                source_cluster=source_cluster,
            ):
                raise PublicationDisabledError(
                    "storage source publication gate is disabled for "
                    f"{measurement_basis}/{collector_id}/{source_cluster}"
                )
            if (
                storage_activation.get("requirement_role") != "quantity"
                or storage_activation.get("source_state") != "active"
                or storage_activation.get("global_state") != "active"
                or storage_activation.get("source_activated_at") is None
                or storage_activation.get("global_activated_at") is None
                or storage_activation.get("database_time") is None
            ):
                raise PublicationDisabledError(
                    "storage source activation is not active"
                )
            source_boundary = _aware_utc(
                storage_activation["source_activated_at"],
                "storage source activated_at",
            )
            global_boundary = _aware_utc(
                storage_activation["global_activated_at"],
                "storage global activated_at",
            )
            database_time = _aware_utc(
                storage_activation["database_time"],
                "storage activation database_time",
            )
            effective_boundary = max(source_boundary, global_boundary)
            if database_time < effective_boundary:
                raise PublicationDisabledError(
                    "storage source activation is not effective"
                )
            if plan.period_start < effective_boundary:
                raise PublicationDisabledError(
                    "storage publication plan predates its source activation"
                )

        for planned in plan.events:
            payload = planned.event.payload
            if storage_activation is not None and (
                str(payload.get("measurement_basis")) != measurement_basis
                or str(payload.get("source_cluster"))
                != str(storage_activation["source_cluster"])
            ):
                raise PublicationDisabledError(
                    "storage publication event changed its activated source authority"
                )
            event_key = _compute_activation_key(
                payload["resource"],
                payload.get("details"),
                details_field="publication event details",
            )
            self._require_class_enabled(
                resource=payload["resource"],
                activation_key=event_key,
                context="publication event",
            )
            if event_key is not None:
                required_activations.add(event_key)

        for activation_key in sorted(required_activations):
            exact_epoch = await conn.fetchrow(
                _COMPUTE_EXACT_EPOCH_FENCE_SQL,
                activation_key,
                interval["inventory_scope_id"],
                interval["compute_scope_epoch_id"],
            )
            activation = await conn.fetchrow(
                _COMPUTE_ACTIVATION_FENCE_SQL,
                activation_key,
            )
            if exact_epoch is None or exact_epoch["effective_from"] is None:
                raise PublicationDisabledError(
                    f"compute class {activation_key!r} exact epoch is not active"
                )
            if (
                activation is None
                or activation["state"] != "active"
                or activation["activated_at"] is None
                or activation["database_time"] is None
            ):
                raise PublicationDisabledError(
                    f"compute class {activation_key!r} activation is not active"
                )
            activated_at = _aware_utc(
                activation["activated_at"],
                f"{activation_key} activated_at",
            )
            authority_boundary = _aware_utc(
                exact_epoch["effective_from"],
                f"{activation_key} epoch authority effective_from",
            )
            database_time = _aware_utc(
                activation["database_time"],
                f"{activation_key} database_time",
            )
            effective_boundary = max(
                activated_at,
                authority_boundary,
            )
            if database_time < effective_boundary:
                raise PublicationDisabledError(
                    f"compute class {activation_key!r} activation is not effective"
                )
            if (
                _aware_utc(interval["started_at"], "interval started_at")
                < effective_boundary
                or plan.period_start < effective_boundary
            ):
                raise PublicationDisabledError(
                    f"compute class {activation_key!r} plan predates activation "
                    "or exact epoch authority"
                )
            retired_at = exact_epoch["retired_at"]
            if retired_at is not None:
                authority_end = _aware_utc(
                    retired_at,
                    f"{activation_key} epoch retired_at",
                )
                if plan.period_end > authority_end:
                    raise PublicationDisabledError(
                        f"compute class {activation_key!r} plan exceeds exact epoch"
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

    @staticmethod
    async def _insert_frozen_plan(
        conn: asyncpg.Connection,
        plan: FrozenPublicationPlan,
    ) -> None:
        if plan.plan_kind == "correction":
            await InfrastructureUsageMaterializer._validate_correction_negative_bounds(
                conn, plan
            )
        await InfrastructureUsageMaterializer._lock_plan_rates(conn, plan)
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

    @staticmethod
    async def _degrade_sealed_day_for_plan(
        conn: asyncpg.Connection,
        day_state: Mapping[str, Any],
        plan: FrozenPublicationPlan,
    ) -> None:
        old_revision = day_state.get("coverage_revision")
        old_sequence = day_state.get("coverage_sequence")
        if (
            not isinstance(old_revision, str)
            or not old_revision
            or isinstance(old_sequence, bool)
            or not isinstance(old_sequence, int)
            or old_sequence <= 0
        ):
            raise PublicationConflictError(
                "sealed-day coverage revision is unavailable"
            )
        reason = (
            "late-usage-discovery"
            if plan.plan_kind == "late-usage"
            else "reviewed-correction"
        )
        unknown_entry = [
            {
                "start": _timestamp_text(plan.period_start),
                "end": _timestamp_text(plan.period_end),
                "reason": reason,
                "source_interval_id": str(plan.source_interval_id),
                "publication_plan_id": str(plan.id),
            }
        ]
        revised = (
            reason
            + "-v1:"
            + _hash_material(
                "srw-infrastructure-coverage-degradation",
                [old_revision, old_sequence + 1, plan.event_set_hash, unknown_entry],
            )
        )
        degraded = await conn.fetchrow(
            _DEGRADE_SEALED_DAY_SQL,
            plan.period_start.date(),
            old_sequence,
            old_revision,
            revised,
            json.dumps(
                unknown_entry,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        if degraded is None:
            raise PublicationConflictError(
                "sealed-day coverage changed before publication intent"
            )

    async def freeze_plan(
        self,
        plan: FrozenPublicationPlan,
        generation: int,
    ) -> None:
        """Commit a prebuilt late/correction intent without audit I/O.

        A future cutover/review coordinator owns discovery verification and any
        fail-closed sealed-day coverage revision before calling this method.
        This boundary rechecks the interval cursor/revision, day state, and
        monotonic correction revision under app-DB locks.
        """

        self._require_enabled()
        if plan.creator_generation != generation:
            raise PublicationFenceError(
                "publication plan was built for a different generation"
            )
        async with self._app.acquire() as conn:
            async with conn.transaction():
                self._validate_control(await conn.fetchrow(_CONTROL_SQL), generation)
                await self._lock_compute_publication_epochs(conn)
                interval = await conn.fetchrow(
                    _LOCK_INTERVAL_SQL,
                    plan.source_interval_id,
                    plan.source_revision,
                )
                if interval is None:
                    raise PublicationConflictError(
                        "publication interval revision disappeared"
                    )
                await self._enforce_plan_publication_fence(conn, interval, plan)
                await conn.execute(_ENSURE_DAY_STATE_SQL, plan.period_start.date())
                day_state = await conn.fetchrow(
                    _DAY_STATE_SQL,
                    plan.period_start.date(),
                )
                if day_state is None:
                    raise PublicationConflictError(
                        "publication usage day state disappeared"
                    )
                state = str(day_state["state"])
                if plan.plan_kind == "usage" and state != "open":
                    raise PublicationConflictError(
                        "ordinary usage can only be planned for an open day"
                    )
                if plan.plan_kind == "late-usage" and state != "sealed":
                    raise PublicationConflictError(
                        "late usage requires a sealed historical day"
                    )
                if plan.plan_kind == "correction" and state == "sealing":
                    raise PublicationConflictError(
                        "correction cannot race a day sealer"
                    )
                if plan.advances_cursor:
                    cursor = _aware_utc(
                        interval["materialized_through"],
                        "materialized_through",
                    )
                    if cursor != plan.previous_materialized_through:
                        raise PublicationConflictError(
                            "publication cursor changed before intent was frozen"
                        )
                    publishable_end = (
                        _aware_utc(interval["ended_at"], "ended_at")
                        if interval["ended_at"] is not None
                        else _aware_utc(
                            interval["last_confirmed_at"], "last_confirmed_at"
                        )
                    )
                    if plan.period_end > publishable_end:
                        raise PublicationConflictError(
                            "publication period exceeds confirmed interval evidence"
                        )
                else:
                    next_revision = await conn.fetchval(
                        _NEXT_CORRECTION_REVISION_SQL,
                        plan.source_interval_id,
                        plan.period_start,
                        plan.period_end,
                    )
                    if int(next_revision) != plan.plan_revision:
                        raise PublicationConflictError(
                            "correction plan revision is no longer next"
                        )
                if state == "sealed" and plan.plan_kind in {
                    "late-usage",
                    "correction",
                }:
                    await self._degrade_sealed_day_for_plan(conn, day_state, plan)
                await self._insert_frozen_plan(conn, plan)

    async def create_correction(
        self,
        generation: int,
        requests: Sequence[CorrectionRequestDelta],
        *,
        correction_reason: str,
        correction_actor_id: UUID | str,
        correction_id: UUID | None = None,
        discovered_at: datetime | None = None,
        max_revision_retries: int = 3,
    ) -> FrozenPublicationPlan:
        """Verify originals, then atomically freeze one reviewed correction.

        Audit verification occurs before the app mutation and outside app row
        locks.  The original plan/event rows are immutable once published, so
        re-locking the interval and checking the next correction revision in
        :meth:`freeze_plan` closes the only mutable race.  A concurrent
        correction allocation is retried with a fresh deterministic group.
        """

        self._require_enabled()
        if max_revision_retries <= 0 or max_revision_retries > 10:
            raise ValueError("max_revision_retries must be between 1 and 10")
        if correction_id is not None:
            try:
                correction_id = UUID(str(correction_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise PublicationContractError(
                    "correction idempotency key must be a UUID"
                ) from exc
        request_rows = tuple(requests)
        if not request_rows:
            raise PublicationContractError("correction requires request rows")
        if len(request_rows) > _MAX_CORRECTION_EVENTS:
            raise PublicationContractError(
                f"correction request exceeds {_MAX_CORRECTION_EVENTS} events"
            )
        existing = (
            None
            if correction_id is None
            else await self._load_plan_by_id(correction_id)
        )
        loaded: list[PlannedUsageEvent] = []
        interval_id: UUID | None = None
        source_revision: str | None = None
        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read"):
                self._validate_control(await conn.fetchrow(_CONTROL_SQL), generation)
                for request in request_rows:
                    row = await conn.fetchrow(
                        _CORRECTION_ORIGINAL_SQL,
                        request.source,
                        request.source_id,
                        request.unit,
                        request.ts,
                        request.expected_payload_hash,
                    )
                    if row is None:
                        raise PublicationConflictError(
                            "correction original key/hash is not a frozen plan event"
                        )
                    if row["state"] != "published" or row["plan_kind"] not in {
                        "usage",
                        "late-usage",
                    }:
                        raise PublicationConflictError(
                            "correction original plan is not published usage"
                        )
                    row_interval_id = UUID(str(row["source_interval_id"]))
                    row_revision = str(row["source_revision"])
                    if interval_id is None:
                        interval_id, source_revision = row_interval_id, row_revision
                    elif (interval_id, source_revision) != (
                        row_interval_id,
                        row_revision,
                    ):
                        raise PublicationContractError(
                            "one correction group cannot cross interval revisions"
                        )
                    payload = row["event_payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    if not isinstance(payload, Mapping):
                        raise PublicationContractError(
                            "correction original payload is not an object"
                        )
                    loaded.append(
                        PlannedUsageEvent(
                            ordinal=int(row["ordinal"]),
                            event=StrictUsageEvent(
                                payload=dict(payload),
                                row_hash=str(row["row_hash"]),
                            ),
                            canonical_rate_version_id=(
                                None
                                if row["canonical_rate_version_id"] is None
                                else UUID(str(row["canonical_rate_version_id"]))
                            ),
                        )
                    )
                assert interval_id is not None and source_revision is not None
                interval = await conn.fetchrow(
                    _CORRECTION_INTERVAL_SQL,
                    interval_id,
                    source_revision,
                )
                if interval is None:
                    raise PublicationConflictError(
                        "correction source interval revision disappeared"
                    )
                interval_row = dict(interval)

        unique_originals: dict[tuple[str, str, str, str], StrictUsageEvent] = {}
        for original in loaded:
            unique_originals.setdefault(original.event.dedupe_key, original.event)
        await self._ledger.verify_frozen_events(tuple(unique_originals.values()))

        deltas = tuple(
            CorrectionDelta(
                original=original,
                quantity=request.quantity,
                payload_overrides=request.payload_overrides,
                inherit_rate=request.inherit_rate,
                canonical_rate_version_id=request.canonical_rate_version_id,
            )
            for request, original in zip(request_rows, loaded)
        )
        if existing is not None:
            candidate = build_correction_plan(
                interval_row,
                deltas,
                correction_reason=correction_reason,
                correction_actor_id=correction_actor_id,
                creator_generation=generation,
                plan_revision=existing.plan_revision,
                discovered_at=discovered_at,
                plan_id=correction_id,
            )
            self._validate_correction_replay(existing, candidate)
            return existing

        last_conflict: PublicationConflictError | None = None
        for _attempt in range(max_revision_retries):
            async with self._app.acquire() as conn:
                next_revision = await conn.fetchval(
                    _NEXT_CORRECTION_REVISION_SQL,
                    interval_id,
                    _payload_timestamp(
                        loaded[0].event.payload["period_start"],
                        "correction period_start",
                    ),
                    _payload_timestamp(
                        loaded[0].event.payload["period_end"],
                        "correction period_end",
                    ),
                )
            plan = build_correction_plan(
                interval_row,
                deltas,
                correction_reason=correction_reason,
                correction_actor_id=correction_actor_id,
                creator_generation=generation,
                plan_revision=int(next_revision),
                discovered_at=discovered_at,
                plan_id=correction_id,
            )
            try:
                await self.freeze_plan(plan, generation)
            except asyncpg.UniqueViolationError:
                replay = (
                    None
                    if correction_id is None
                    else await self._load_plan_by_id(correction_id)
                )
                if replay is not None:
                    self._validate_correction_replay(replay, plan)
                    return replay
                last_conflict = PublicationConflictError(
                    "correction revision is no longer next"
                )
                continue
            except PublicationConflictError as exc:
                if "revision is no longer next" not in str(exc):
                    raise
                last_conflict = exc
                continue
            return plan
        raise PublicationConflictError(
            "correction revision allocation remained contended"
        ) from last_conflict

    async def _load_plan_by_id(self, plan_id: UUID) -> FrozenPublicationPlan | None:
        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                plan = await conn.fetchrow(_CORRECTION_PLAN_BY_ID_SQL, plan_id)
                if plan is None:
                    return None
                events = await conn.fetch(_PLAN_EVENTS_SQL, plan_id)
        return FrozenPublicationPlan.from_records(plan, events)

    @staticmethod
    def _validate_correction_replay(
        existing: FrozenPublicationPlan,
        candidate: FrozenPublicationPlan,
    ) -> None:
        if existing.plan_kind != "correction":
            raise PublicationConflictError(
                "correction idempotency key belongs to another plan kind"
            )
        if existing.state == "conflict":
            raise PublicationConflictError(
                "correction idempotency key belongs to a terminal conflict"
            )
        if (
            existing.id != candidate.id
            or existing.source_interval_id != candidate.source_interval_id
            or existing.source_revision != candidate.source_revision
            or existing.period_start != candidate.period_start
            or existing.period_end != candidate.period_end
            or existing.event_set_hash != candidate.event_set_hash
            or existing.rate_selection_hash != candidate.rate_selection_hash
        ):
            raise PublicationConflictError(
                "correction idempotency replay changed immutable intent"
            )

    @staticmethod
    async def _lock_plan_rates(
        conn: asyncpg.Connection,
        plan: FrozenPublicationPlan,
    ) -> None:
        """Verify and row-lock every priced selection until plan insertion.

        This closes both replacement-rate and inherited-selector races.  The
        appended database guard prevents a later range close from invalidating
        a committed ordinary or correction plan reference.
        """

        for planned in plan.events:
            payload = planned.event.payload
            rate_id = planned.canonical_rate_version_id
            if rate_id is None:
                continue
            if payload["rate_usd"] is None:
                raise PublicationContractError(
                    "priced publication event is missing its frozen rate"
                )
            matched = await conn.fetchval(
                _CORRECTION_RATE_SQL,
                rate_id,
                payload["unit"],
                Decimal(str(payload["rate_usd"])),
                payload["cost_domain"],
                payload["measurement_basis"],
                payload["category"],
                payload["resource_class"],
                payload["resource"],
                plan.period_start,
                plan.period_end,
            )
            if matched is not True:
                raise PublicationConflictError(
                    "publication rate does not match its immutable rate version"
                )

    @staticmethod
    async def _validate_correction_negative_bounds(
        conn: asyncpg.Connection,
        plan: FrozenPublicationPlan,
    ) -> None:
        requested: dict[tuple[str, str, str, str], tuple[Decimal, Decimal, str]] = {}
        for planned in plan.events:
            payload = planned.event.payload
            quantity = Decimal(str(payload["quantity"]))
            if quantity >= 0:
                continue
            key = (
                str(payload["corrects_source"]),
                str(payload["corrects_source_id"]),
                str(payload["corrects_unit"]),
                str(payload["corrects_ts"]),
            )
            negative = -quantity
            details = payload.get("details")
            if not isinstance(details, Mapping):
                raise PublicationContractError(
                    "correction negative bound requires immutable original details"
                )
            original_quantity = _correction_decimal(
                details.get("corrects_quantity"),
                "correction immutable original quantity",
            )
            original_row_hash = str(details.get("corrects_payload_hash"))
            if not _HASH_RE.fullmatch(original_row_hash):
                raise PublicationContractError(
                    "correction immutable original hash must be SHA-256"
                )
            candidate_total, frozen_quantity, frozen_hash = requested.get(
                key, (Decimal(0), original_quantity, original_row_hash)
            )
            if (
                original_quantity <= 0
                or frozen_quantity != original_quantity
                or frozen_hash != original_row_hash
            ):
                raise PublicationContractError(
                    "negative corrections disagree on their immutable original"
                )
            requested[key] = (
                candidate_total + negative,
                frozen_quantity,
                frozen_hash,
            )
        if not requested:
            return

        keys = list(requested)
        # This is intentionally a separate first statement.  Every correction
        # for one immutable original takes the same sorted row lock before the
        # aggregate below.  After a waiter acquires the lock, READ COMMITTED
        # gives the second statement a snapshot that includes the winner's
        # newly frozen plan rows.
        locked_rows = await conn.fetch(
            _LOCK_CORRECTION_ORIGINALS_SQL,
            [key[0] for key in keys],
            [key[1] for key in keys],
            [key[2] for key in keys],
            [key[3] for key in keys],
            plan.source_interval_id,
        )
        locked_originals = {
            (
                str(row["source"]),
                str(row["source_id"]),
                str(row["unit"]),
                str(row["corrects_ts"]),
            ): row
            for row in locked_rows
        }
        if len(locked_rows) != len(keys) or set(locked_originals) != set(keys):
            raise PublicationConflictError(
                "correction immutable original is no longer published"
            )
        for key, (_candidate, frozen_quantity, frozen_hash) in requested.items():
            row = locked_originals[key]
            original = Decimal(str(row["original_quantity"]))
            if (
                original != frozen_quantity
                or str(row["original_row_hash"]) != frozen_hash
            ):
                raise PublicationConflictError(
                    "correction immutable original changed before plan freeze"
                )

        rows = await conn.fetch(
            _CORRECTION_NEGATIVE_TOTALS_SQL,
            [key[0] for key in keys],
            [key[1] for key in keys],
            [key[2] for key in keys],
            [key[3] for key in keys],
            plan.id,
        )
        if len(rows) != len(keys):
            raise PublicationConflictError(
                "correction negative-bound query returned an incomplete key set"
            )
        for ordinal, (key, row) in enumerate(zip(keys, rows), start=1):
            if int(row["ordinality"]) != ordinal:
                raise PublicationConflictError(
                    "correction negative-bound query returned out-of-order keys"
                )
            prior = Decimal(str(row["negative_quantity"]))
            candidate, original, _frozen_hash = requested[key]
            if prior < 0 or prior + candidate > original:
                raise PublicationConflictError(
                    "cumulative negative corrections exceed original quantity"
                )

    async def plan_batch(self, generation: int) -> tuple[FrozenPublicationPlan, ...]:
        """Freeze a bounded batch without holding locks across audit I/O."""
        self._require_enabled()
        plans: list[FrozenPublicationPlan] = []
        async with self._app.acquire() as conn:
            async with conn.transaction():
                cutover_at = self._validate_control(
                    await conn.fetchrow(_CONTROL_SQL), generation
                )
                await self._lock_compute_publication_epochs(conn)
                candidate_args: list[Any] = [
                    list(self._enabled_resources),
                    cutover_at,
                    self._batch_size,
                    self._ide_workspace_pod_enabled,
                ]
                storage_policy_enabled = bool(
                    self._storage_publication_policy.authorities
                )
                if storage_policy_enabled:
                    candidate_args.extend(
                        self._storage_publication_policy.sql_columns()
                    )
                intervals = await conn.fetch(
                    _candidate_intervals_sql(
                        compute_policy_enabled=bool(
                            self._enabled_compute_activation_keys()
                        ),
                        storage_policy_enabled=storage_policy_enabled,
                    ),
                    *candidate_args,
                )
                for interval in intervals:
                    # The SQL subtype predicate is an efficient selector, not
                    # the safety boundary. Recheck the frozen source/events so
                    # alternate review/freeze paths share the same contract.
                    cursor = _aware_utc(
                        interval["materialized_through"], "materialized_through"
                    )
                    await conn.execute(_ENSURE_DAY_STATE_SQL, cursor.date())
                    day_state = await conn.fetchrow(_DAY_STATE_SQL, cursor.date())
                    if day_state is None:
                        raise PublicationConflictError(
                            f"usage day state disappeared for {cursor.date()}"
                        )
                    day_state_name = str(day_state["state"])
                    if day_state_name not in {"open", "sealed"}:
                        # A sealer owns the day. Do not freeze intent behind its
                        # coverage decision; a later bounded pass may retry if
                        # an operator resumes a pre-existing sealing row.
                        continue
                    dimensions = capacity_dimensions(interval)
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
                    if day_state_name == "open":
                        plan = build_usage_plan(
                            interval,
                            rate_rows,
                            creator_generation=generation,
                        )
                    else:
                        segment = build_usage_plan(
                            interval,
                            rate_rows,
                            creator_generation=generation,
                        )
                        if segment is None:
                            continue
                        evidence_at, evidence = _late_discovery_evidence(
                            interval, segment.period_end
                        )
                        plan = build_late_usage_plan(
                            interval,
                            rate_rows,
                            creator_generation=generation,
                            discovered_at=evidence_at,
                            discovery_evidence=evidence,
                            plan_id=segment.id,
                        )
                    if plan is None:
                        continue
                    await self._enforce_plan_publication_fence(conn, interval, plan)
                    if plan.plan_kind == "late-usage":
                        old_revision = day_state.get("coverage_revision")
                        old_sequence = day_state.get("coverage_sequence")
                        if (
                            not isinstance(old_revision, str)
                            or not old_revision
                            or isinstance(old_sequence, bool)
                            or not isinstance(old_sequence, int)
                            or old_sequence <= 0
                        ):
                            raise PublicationConflictError(
                                "sealed-day coverage revision is unavailable"
                            )
                        unknown_entry = [
                            {
                                "start": _timestamp_text(plan.period_start),
                                "end": _timestamp_text(plan.period_end),
                                "reason": "late-usage-discovery",
                                "source_interval_id": str(plan.source_interval_id),
                                "discovery_evidence": evidence,
                            }
                        ]
                        revised = "late-v1:" + _hash_material(
                            "srw-infrastructure-late-coverage-revision",
                            [
                                old_revision,
                                old_sequence + 1,
                                plan.event_set_hash,
                                unknown_entry,
                            ],
                        )
                        degraded = await conn.fetchrow(
                            _DEGRADE_SEALED_DAY_SQL,
                            cursor.date(),
                            old_sequence,
                            old_revision,
                            revised,
                            json.dumps(
                                unknown_entry,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                        if degraded is None:
                            raise PublicationConflictError(
                                "sealed-day coverage changed before late intent"
                            )
                    await self._insert_frozen_plan(conn, plan)
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

        # Pending intent can outlive a rollout/configuration change.  Fence it
        # again immediately before audit I/O instead of assuming selection-time
        # gates still describe the running process.
        async with self._app.acquire() as conn:
            async with conn.transaction():
                await self._lock_compute_publication_epochs(conn)
                interval = await conn.fetchrow(
                    _PUBLICATION_INTERVAL_FENCE_SQL,
                    plan.source_interval_id,
                    plan.source_revision,
                )
                if interval is None:
                    raise PublicationConflictError(
                        "pending publication interval revision disappeared"
                    )
                await self._enforce_plan_publication_fence(conn, interval, plan)

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
        cursor_conflict = False
        async with self._app.acquire() as conn:
            async with conn.transaction():
                self._validate_control(await conn.fetchrow(_CONTROL_SQL), generation)
                await self._lock_compute_publication_epochs(conn)
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
                        error = json.dumps(
                            {"code": "interval-cursor-conflict"},
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        marked = await conn.fetchrow(
                            _FINALIZE_CONFLICT_SQL,
                            plan.id,
                            error,
                        )
                        if marked is None:
                            raise PublicationConflictError(
                                "publication plan changed before conflict recording"
                            )
                        cursor_conflict = True
                if not cursor_conflict:
                    published = await conn.fetchrow(_PUBLISH_PLAN_SQL, plan.id)
                    if published is None:
                        raise PublicationConflictError(
                            "publication plan changed before finalization"
                        )
        if cursor_conflict:
            raise PublicationConflictError(
                "interval revision/cursor changed before finalization"
            )
        return plan.advances_cursor


__all__ = [
    "CapacityDimension",
    "CanonicalRateVersion",
    "CorrectionDelta",
    "CorrectionRequestDelta",
    "FrozenPublicationPlan",
    "InfrastructureUsageMaterializer",
    "PlannedUsageEvent",
    "PublicationConflictError",
    "PublicationContractError",
    "PublicationDisabledError",
    "PublicationError",
    "PublicationFenceError",
    "PublicationResult",
    "StorageCoverageRequirement",
    "StoragePublicationAuthority",
    "StoragePublicationPolicy",
    "build_correction_plan",
    "build_late_usage_plan",
    "build_usage_plan",
    "capacity_dimensions",
    "capacity_quantity",
    "duration_microseconds",
    "event_payload_hash",
]
