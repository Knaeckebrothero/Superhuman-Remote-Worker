"""Fenced, bounded persistence for authoritative resource inventories.

This module owns only the app-database side of Slice 1.  It stages normalized
items, verifies an ordered manifest digest, seals and safely reconciles one
snapshot, and consumes its one-time ingest ticket in a single transaction.  It
does not publish audit events, change cutover state, or derive Kubernetes
requests from raw objects.

Positive reconciliation is intentionally conservative at this boundary:

* every observed UID prevents complete-list absence closure;
* an invalid item advances only ``last_seen_at`` (never the capacity proof);
* a valid item confirms an existing interval only when its revision hash equals
  the interval's immutable ``source_revision``; and
* new/revision-changed valid items are reported as pending for the typed
  collector-specific interval builder.  They are never silently treated as
  zero and never make an unrelated UID look absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import json
import re
import secrets
from typing import Any
from uuid import UUID, uuid4

import asyncpg


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SOURCE_KINDS = frozenset({"pod", "vmi", "pvc", "volume"})
_MANIFEST_PREFIX = b"srw-inventory-manifest-v1\x00"
_COLLECTOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MIN_SNAPSHOT_ITEM_RETENTION = timedelta(days=7)
_MIN_DIAGNOSTIC_RETENTION = timedelta(days=7)
_MIN_ABANDONED_STAGING_RETENTION = timedelta(hours=24)
_MAX_PURGE_BATCH = 10_000
_ABANDONED_WATCH_GAP_REASON = "watch-session-abandoned"
_CONTROLLER_EPOCH_GAP_REASON = "controller-epoch-changed"
_WATCH_GAP_REASONS = frozenset(
    {
        "resource-version-expired",
        "watch-queue-overflow",
        "watch-protocol-error",
        "watch-event-byte-limit",
        "watch-byte-limit",
        "watch-normalization-error",
        "ambiguous-watch-apply",
    }
)
_RECOVERABLE_COVERAGE_GAP_REASONS = frozenset(
    {
        # Compatibility with shadow rows created before typed WATCH reasons.
        "watch-history-lost",
        "list-resource-version-expired",
        "list-resource-version-mismatch",
        _ABANDONED_WATCH_GAP_REASON,
        _CONTROLLER_EPOCH_GAP_REASON,
        *_WATCH_GAP_REASONS,
    }
)


class InventoryContractError(ValueError):
    """An envelope/item violates the local typed ingestion contract."""


class InventoryFenceError(RuntimeError):
    """The caller no longer owns the active generation/scope epoch."""


class InventoryConflictError(RuntimeError):
    """An idempotency key was replayed with different immutable content."""


class InventoryRecoveryRequired(InventoryConflictError):
    """A durable continuity gap requires a recovery epoch and complete LIST."""


class InventoryTicketError(RuntimeError):
    """An ingest ticket is unknown, expired, consumed, or incorrectly bound."""


class ShadowComparisonStatus(StrEnum):
    MATCHED = "matched"
    CAPACITY_MISMATCH = "capacity-mismatch"
    LIFETIME_MISMATCH = "lifetime-mismatch"
    OWNER_MISMATCH = "owner-mismatch"
    LEGACY_MISSING = "legacy-missing"
    INVALID_OBSERVATION = "invalid-observation"
    NOT_APPLICABLE = "not-applicable"


class WatchEventKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    BOOKMARK = "bookmark"


class WatchMutationAction(StrEnum):
    CONFIRM = "confirm"
    OPEN = "open"
    REVISE = "revise"
    NOT_APPLICABLE = "not-applicable"
    PRESENCE_INVALID = "presence-invalid"
    CLOSE = "close"
    ALREADY_ABSENT = "already-absent"
    BOOKMARK = "bookmark"
    HISTORY_GAP = "history-gap"


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InventoryContractError(f"{field} must be timezone-aware")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InventoryContractError("value is not canonical JSON data") from exc


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise InventoryContractError("database JSON value is not an object")
    return value


def canonical_request_digest(value: bytes | str | Mapping[str, Any]) -> str:
    """Hash a versioned collector request without storing the bearer request."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    elif isinstance(value, Mapping):
        payload = _canonical_json(dict(value)).encode("utf-8")
    else:
        raise InventoryContractError("request digest input must be bytes/text/object")
    return hashlib.sha256(b"srw-inventory-request-v1\x00" + payload).hexdigest()


@dataclass(frozen=True)
class SanitizedInventoryError:
    """Structured error evidence with no free-form message/customer data."""

    code: str
    retryable: bool = False
    component: str | None = None

    def __post_init__(self) -> None:
        if not _CODE_RE.fullmatch(self.code):
            raise InventoryContractError("inventory error code is invalid")
        if self.component is not None and not _CODE_RE.fullmatch(self.component):
            raise InventoryContractError("inventory error component is invalid")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.component is not None:
            result["component"] = self.component
        return result


@dataclass(frozen=True)
class InventoryItem:
    source_kind: str
    source_uid: str
    revision_hash: str | None
    normalized_item: Mapping[str, Any]
    valid_for_metering: bool
    item_error: SanitizedInventoryError | None = None

    def __post_init__(self) -> None:
        if self.source_kind not in _SOURCE_KINDS:
            raise InventoryContractError("unsupported inventory source_kind")
        if not self.source_uid:
            raise InventoryContractError("inventory source_uid is required")
        if not isinstance(self.normalized_item, Mapping):
            raise InventoryContractError("normalized_item must be an object")
        _canonical_json(dict(self.normalized_item))
        if self.valid_for_metering:
            if self.revision_hash is None or not _HASH_RE.fullmatch(self.revision_hash):
                raise InventoryContractError(
                    "valid inventory item requires a SHA-256 revision hash"
                )
            if self.item_error is not None:
                raise InventoryContractError("valid inventory item cannot have error")
        elif self.item_error is None:
            raise InventoryContractError("invalid inventory item requires error code")
        elif self.revision_hash is not None and not _HASH_RE.fullmatch(
            self.revision_hash
        ):
            raise InventoryContractError("revision_hash must be SHA-256 when set")

    @property
    def manifest_revision(self) -> str:
        return self.revision_hash if self.valid_for_metering else "invalid"

    def payload(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_uid": self.source_uid,
            "revision_hash": self.revision_hash,
            "normalized_item": dict(self.normalized_item),
            "valid_for_metering": self.valid_for_metering,
            "item_error": None
            if self.item_error is None
            else self.item_error.as_dict(),
        }


def _hash_manifest_tuple(
    digest: Any,
    source_kind: str,
    source_uid: str,
    revision: str,
) -> None:
    for value in (source_kind, source_uid, revision):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)


def inventory_manifest_digest(items: Iterable[InventoryItem]) -> str:
    """Return the v1 digest over sorted ``(kind, UID, revision-or-invalid)``."""
    keys = sorted(
        (item.source_kind, item.source_uid, item.manifest_revision) for item in items
    )
    if len(keys) != len({(kind, uid) for kind, uid, _ in keys}):
        raise InventoryContractError("manifest contains duplicate kind/UID")
    digest = hashlib.sha256(_MANIFEST_PREFIX)
    for source_kind, source_uid, revision in keys:
        _hash_manifest_tuple(digest, source_kind, source_uid, revision)
    return digest.hexdigest()


@dataclass(frozen=True)
class ShadowComparison:
    source_uid: str
    status: ShadowComparisonStatus | str
    reason_code: str
    explained: bool
    comparison_at: datetime
    owner_trusted: bool = False
    owner_kind: str | None = None
    owner_id: UUID | None = None
    legacy_interval_id: int | None = None
    legacy_cpu_millicores: int | None = None
    legacy_memory_bytes: int | None = None
    legacy_started_at: datetime | None = None
    observed_cpu_millicores: int | None = None
    observed_memory_bytes: int | None = None
    observed_started_at: datetime | None = None
    observed_start_time_source: str | None = None
    observed_start_uncertainty_us: int | None = None
    start_delta_us: int | None = None

    def __post_init__(self) -> None:
        if not self.source_uid:
            raise InventoryContractError("comparison source_uid is required")
        try:
            ShadowComparisonStatus(str(self.status))
        except ValueError as exc:
            raise InventoryContractError("comparison status is invalid") from exc
        if not _CODE_RE.fullmatch(self.reason_code):
            raise InventoryContractError("comparison reason_code is invalid")
        _require_aware(self.comparison_at, "comparison_at")
        if not isinstance(self.explained, bool) or not isinstance(
            self.owner_trusted, bool
        ):
            raise InventoryContractError("comparison trust flags must be boolean")
        if self.owner_trusted:
            if self.owner_kind not in {"job", "thread"} or self.owner_id is None:
                raise InventoryContractError(
                    "trusted owner requires job/thread kind and UUID"
                )
        elif self.owner_kind is not None or self.owner_id is not None:
            raise InventoryContractError("untrusted owner identity must not be stored")
        legacy = (
            self.legacy_interval_id,
            self.legacy_cpu_millicores,
            self.legacy_memory_bytes,
        )
        if any(value is None for value in legacy) and any(
            value is not None for value in legacy
        ):
            raise InventoryContractError("legacy interval/capacities are atomic")
        if self.legacy_started_at is not None:
            _require_aware(self.legacy_started_at, "legacy_started_at")
            if self.legacy_interval_id is None:
                raise InventoryContractError("legacy start requires a legacy interval")
        observed_start = (
            self.observed_started_at,
            self.observed_start_time_source,
            self.observed_start_uncertainty_us,
        )
        if any(value is None for value in observed_start) and any(
            value is not None for value in observed_start
        ):
            raise InventoryContractError(
                "observed start time/source/uncertainty are atomic"
            )
        if self.observed_started_at is not None:
            _require_aware(self.observed_started_at, "observed_started_at")
            assert self.observed_start_time_source is not None
            if not _CODE_RE.fullmatch(self.observed_start_time_source):
                raise InventoryContractError("observed start time source is invalid")
            if (
                isinstance(self.observed_start_uncertainty_us, bool)
                or not isinstance(self.observed_start_uncertainty_us, int)
                or self.observed_start_uncertainty_us < 0
            ):
                raise InventoryContractError(
                    "observed start uncertainty must be nonnegative"
                )
        if self.start_delta_us is not None:
            if isinstance(self.start_delta_us, bool) or not isinstance(
                self.start_delta_us, int
            ):
                raise InventoryContractError("start delta must be integer microseconds")
            if self.legacy_started_at is None or self.observed_started_at is None:
                raise InventoryContractError(
                    "start delta requires legacy and observed starts"
                )
            delta = self.observed_started_at - self.legacy_started_at
            expected_delta = (
                delta.days * 86_400_000_000
                + delta.seconds * 1_000_000
                + delta.microseconds
            )
            if self.start_delta_us != expected_delta:
                raise InventoryContractError(
                    "start delta does not match comparison timestamps"
                )
        status = ShadowComparisonStatus(str(self.status))
        if status is ShadowComparisonStatus.MATCHED and self.start_delta_us not in {
            None,
            0,
        }:
            raise InventoryContractError("matched comparison has a start mismatch")
        if status is ShadowComparisonStatus.LIFETIME_MISMATCH:
            unexplained = not self.explained and self.reason_code in {
                "start-semantics",
                "start-evidence-missing",
            }
            bounded = (
                self.explained
                and self.reason_code == "bounded-start-semantics"
                and self.start_delta_us is not None
                and self.start_delta_us > 0
                and self.observed_start_time_source == "app-db-received"
                and self.observed_start_uncertainty_us is not None
                and self.start_delta_us <= self.observed_start_uncertainty_us
            )
            if self.start_delta_us == 0 or not (unexplained or bounded):
                raise InventoryContractError(
                    "lifetime mismatch requires unresolved or bounded start evidence"
                )
        for value in (
            self.legacy_interval_id,
            self.legacy_cpu_millicores,
            self.legacy_memory_bytes,
            self.observed_cpu_millicores,
            self.observed_memory_bytes,
        ):
            if value is not None and value < 0:
                raise InventoryContractError(
                    "comparison capacities must be nonnegative"
                )

    def payload(self) -> dict[str, Any]:
        return {
            "source_uid": self.source_uid,
            "owner_kind": self.owner_kind,
            "owner_id": None if self.owner_id is None else str(self.owner_id),
            "owner_trusted": self.owner_trusted,
            "legacy_interval_id": self.legacy_interval_id,
            "legacy_cpu_millicores": self.legacy_cpu_millicores,
            "legacy_memory_bytes": self.legacy_memory_bytes,
            "legacy_started_at": (
                None
                if self.legacy_started_at is None
                else self.legacy_started_at.isoformat()
            ),
            "observed_cpu_millicores": self.observed_cpu_millicores,
            "observed_memory_bytes": self.observed_memory_bytes,
            "observed_started_at": (
                None
                if self.observed_started_at is None
                else self.observed_started_at.isoformat()
            ),
            "observed_start_time_source": self.observed_start_time_source,
            "observed_start_uncertainty_us": self.observed_start_uncertainty_us,
            "start_delta_us": self.start_delta_us,
            "status": str(self.status),
            "reason_code": self.reason_code,
            "explained": self.explained,
            "comparison_at": self.comparison_at.isoformat(),
        }


@dataclass(frozen=True)
class InventoryScopeIdentity:
    collector_id: str
    source_cluster: str
    api_resource: str
    namespace: str | None

    def __post_init__(self) -> None:
        if not _COLLECTOR_ID_RE.fullmatch(self.collector_id):
            raise InventoryContractError("scope collector_id is invalid")
        for field, value in (
            ("source_cluster", self.source_cluster),
            ("api_resource", self.api_resource),
        ):
            if not value:
                raise InventoryContractError(f"scope {field} is required")
        if self.namespace == "":
            raise InventoryContractError("scope namespace cannot be empty")


@dataclass(frozen=True)
class TransportNonceClaim:
    """One HMAC-authenticated request nonce claimed inside its DB transaction."""

    collector_id: str
    request_nonce: UUID
    request_kind: str
    request_digest: str

    def __post_init__(self) -> None:
        if not _COLLECTOR_ID_RE.fullmatch(self.collector_id):
            raise InventoryContractError("transport collector_id is invalid")
        if not _CODE_RE.fullmatch(self.request_kind):
            raise InventoryContractError("transport request_kind is invalid")
        if not _HASH_RE.fullmatch(self.request_digest):
            raise InventoryContractError("transport request_digest must be SHA-256")


@dataclass(frozen=True)
class IngestTicket:
    id: UUID
    token: str
    scope_epoch_id: UUID
    leader_generation: int
    request_digest: str
    max_snapshot_items: int
    max_snapshot_bytes: int
    expires_at: datetime


@dataclass(frozen=True)
class SnapshotHandle:
    snapshot_id: UUID
    scope_epoch_id: UUID
    inventory_scope_id: UUID
    leader_generation: int
    manifest_state: str
    replayed: bool = False


@dataclass(frozen=True)
class RecoveryEpochHandle:
    scope_epoch_id: UUID
    recovery_from_epoch_id: UUID
    inventory_scope_id: UUID
    epoch_number: int
    leader_generation: int
    predecessor_retired_at: datetime


@dataclass(frozen=True)
class StageResult:
    inserted: int
    total: int


@dataclass(frozen=True)
class InventoryPurgeResult:
    """One bounded leader cleanup pass over non-authoritative diagnostics."""

    leader_generation: int
    batch_limit: int
    sealed_snapshots_expired: int
    abandoned_snapshots_expired: int
    snapshot_items_deleted: int
    shadow_comparisons_deleted: int
    watch_events_deleted: int
    watch_sessions_deleted: int
    unbound_tickets_deleted: int

    @property
    def made_progress(self) -> bool:
        return any(
            value > 0
            for value in (
                self.sealed_snapshots_expired,
                self.abandoned_snapshots_expired,
                self.snapshot_items_deleted,
                self.shadow_comparisons_deleted,
                self.watch_events_deleted,
                self.watch_sessions_deleted,
                self.unbound_tickets_deleted,
            )
        )

    @property
    def might_have_more(self) -> bool:
        """Whether any category filled its batch and merits an immediate pass."""
        return any(
            value == self.batch_limit
            for value in (
                self.sealed_snapshots_expired,
                self.abandoned_snapshots_expired,
                self.snapshot_items_deleted,
                self.shadow_comparisons_deleted,
                self.watch_events_deleted,
                self.watch_sessions_deleted,
                self.unbound_tickets_deleted,
            )
        )


@dataclass(frozen=True)
class WatchSessionGrant:
    id: UUID
    token: str
    scope_epoch_id: UUID
    leader_generation: int
    request_digest: str
    starting_resource_version: str
    max_events: int
    max_bytes: int
    expires_at: datetime


@dataclass(frozen=True)
class WatchObjectEvent:
    """One object or BOOKMARK event; receipt time is stamped by app PostgreSQL."""

    event_type: WatchEventKind | str
    resource_version: str
    collector_observed_at: datetime
    event_bytes: int
    item: InventoryItem | None = None
    source_kind: str | None = None
    source_uid: str | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        try:
            event_type = WatchEventKind(str(self.event_type))
        except ValueError as exc:
            raise InventoryContractError("watch event type is invalid") from exc
        object.__setattr__(self, "event_type", event_type)
        if not self.resource_version or self.resource_version == "0":
            raise InventoryContractError(
                "watch resource_version must be non-zero opaque text"
            )
        if len(self.resource_version) > 1024:
            raise InventoryContractError("watch resource_version exceeds bound")
        _require_aware(self.collector_observed_at, "collector_observed_at")
        if self.event_bytes <= 0:
            raise InventoryContractError("watch event_bytes must be positive")
        if event_type in {WatchEventKind.ADDED, WatchEventKind.MODIFIED}:
            if self.item is None:
                raise InventoryContractError("object watch event requires item")
            if self.source_kind is not None or self.source_uid is not None:
                raise InventoryContractError(
                    "object identity is taken from the normalized item"
                )
        elif event_type is WatchEventKind.DELETED:
            if self.item is not None or self.terminal:
                raise InventoryContractError(
                    "DELETED uses only its trusted kind/UID identity"
                )
            if self.source_kind not in _SOURCE_KINDS or not self.source_uid:
                raise InventoryContractError("DELETED requires trusted kind/UID")
        elif (
            self.item is not None
            or self.source_kind is not None
            or self.source_uid is not None
            or self.terminal
        ):
            raise InventoryContractError("BOOKMARK cannot carry object state")

    @property
    def identity(self) -> tuple[str | None, str | None]:
        if self.item is not None:
            return self.item.source_kind, self.item.source_uid
        return self.source_kind, self.source_uid


@dataclass(frozen=True)
class WatchIntervalMutationContext:
    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    event_type: WatchEventKind
    received_at: datetime
    existing_interval_id: UUID | None
    existing_source_revision: str | None


WatchIntervalMutator = Callable[
    [asyncpg.Connection, WatchIntervalMutationContext, InventoryItem],
    Awaitable[UUID | None],
]


@dataclass(frozen=True)
class SnapshotIntervalMutationContext:
    snapshot_id: UUID
    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    received_at: datetime
    existing_interval_id: UUID | None
    existing_source_revision: str | None


SnapshotIntervalMutator = Callable[
    [asyncpg.Connection, SnapshotIntervalMutationContext, InventoryItem],
    Awaitable[UUID | None],
]


@dataclass(frozen=True)
class SnapshotObservationContext:
    snapshot_id: UUID
    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    received_at: datetime
    current_interval_id: UUID | None
    current_source_revision: str | None


SnapshotObservationHook = Callable[
    [asyncpg.Connection, SnapshotObservationContext, InventoryItem],
    Awaitable[None],
]


@dataclass(frozen=True)
class SnapshotCompletionContext:
    """One complete sealed-manifest candidate before absence reconciliation."""

    snapshot_id: UUID
    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    received_at: datetime


SnapshotCompletionHook = Callable[
    [asyncpg.Connection, SnapshotCompletionContext],
    Awaitable[None],
]


@dataclass(frozen=True)
class SnapshotAbsenceMutationContext:
    """One open interval absent from a complete authoritative LIST.

    Most resource kinds use the generic absence close below. Physical storage
    assets are different: a Kubernetes PV object may disappear while a
    ``Retain`` backend disk still exists. A resource-specific hook can consume
    that absence and persist the conservative backend-unverified transition
    before the generic closer handles the remaining intervals.
    """

    snapshot_id: UUID
    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    received_at: datetime


SnapshotAbsenceMutator = Callable[
    [asyncpg.Connection, SnapshotAbsenceMutationContext, asyncpg.Record],
    Awaitable[bool],
]


@dataclass(frozen=True)
class WatchDeletionMutationContext:
    """Resource-specific context for a trusted Kubernetes DELETED event."""

    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    received_at: datetime
    source_kind: str
    source_uid: str


WatchDeletionMutator = Callable[
    [
        asyncpg.Connection,
        WatchDeletionMutationContext,
        asyncpg.Record | None,
    ],
    Awaitable[tuple[WatchMutationAction, UUID | None] | None],
]


@dataclass(frozen=True)
class WatchTerminalMutationContext:
    """Resource-specific context for a trusted terminal object observation."""

    scope_epoch_id: UUID
    inventory_scope_id: UUID
    source_cluster: str
    namespace: str | None
    received_at: datetime
    source_kind: str
    source_uid: str


WatchTerminalMutator = Callable[
    [
        asyncpg.Connection,
        WatchTerminalMutationContext,
        asyncpg.Record | None,
    ],
    Awaitable[tuple[WatchMutationAction, UUID | None] | None],
]


@dataclass(frozen=True)
class WatchCommitResult:
    watch_session_id: UUID
    event_id: UUID
    event_type: str
    resource_version: str
    mutation_action: WatchMutationAction
    received_at: datetime
    affected_interval_id: UUID | None = None
    coverage_gap_id: UUID | None = None
    session_consumed: bool = False
    replayed: bool = False


@dataclass(frozen=True)
class SnapshotFinalization:
    collection_completed_at: datetime
    complete: bool
    item_count: int
    item_digest: str | None
    source_snapshot_at: datetime | None = None
    resource_version: str | None = None
    controller_epoch: str | None = None
    sequence: int | None = None
    fatal_errors: tuple[SanitizedInventoryError, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.collection_completed_at, "collection_completed_at")
        if self.source_snapshot_at is not None:
            _require_aware(self.source_snapshot_at, "source_snapshot_at")
        if self.item_count < 0:
            raise InventoryContractError("item_count must be nonnegative")
        if self.item_digest is not None and not _HASH_RE.fullmatch(self.item_digest):
            raise InventoryContractError("item_digest must be SHA-256")
        if self.complete and self.item_digest is None:
            raise InventoryContractError("complete snapshot requires item digest")
        if self.complete and self.fatal_errors:
            raise InventoryContractError("complete snapshot cannot have fatal errors")
        if (self.controller_epoch is None) != (self.sequence is None):
            raise InventoryContractError(
                "controller_epoch and sequence must be supplied together"
            )
        if self.controller_epoch == "":
            raise InventoryContractError("controller_epoch cannot be empty")
        if self.sequence is not None and self.sequence < 0:
            raise InventoryContractError("sequence must be nonnegative")


@dataclass(frozen=True)
class ReconciliationResult:
    snapshot_id: UUID
    complete: bool
    present_items: int
    invalid_items: int
    observed_intervals: int
    confirmed_intervals: int
    closed_intervals: int
    pending_valid_items: int
    shadow_comparisons: int
    replayed: bool = False

    @classmethod
    def from_summary(
        cls,
        snapshot_id: UUID,
        complete: bool,
        summary: Mapping[str, Any],
        *,
        replayed: bool,
    ) -> ReconciliationResult:
        return cls(
            snapshot_id=snapshot_id,
            complete=complete,
            present_items=int(summary["present_items"]),
            invalid_items=int(summary["invalid_items"]),
            observed_intervals=int(summary["observed_intervals"]),
            confirmed_intervals=int(summary["confirmed_intervals"]),
            closed_intervals=int(summary["closed_intervals"]),
            pending_valid_items=int(summary["pending_valid_items"]),
            shadow_comparisons=int(summary["shadow_comparisons"]),
            replayed=replayed,
        )


_TICKET_SQL = """
SELECT id, scope_epoch_id, leader_generation, request_digest, expires_at,
       max_snapshot_items, max_snapshot_bytes, staged_bytes,
       bound_snapshot_id, bound_at, consumed_at
FROM resource_inventory_ingest_tickets
WHERE nonce_hash = $1
FOR UPDATE
"""

_WATCH_SESSION_SQL = """
SELECT id, scope_epoch_id, leader_generation, request_digest,
       starting_resource_version, last_resource_version,
       max_events, max_bytes, committed_events, committed_bytes,
       expires_at, termination_reason, consumed_at
FROM resource_inventory_watch_sessions
WHERE nonce_hash = $1
FOR UPDATE
"""

_WATCH_EVENT_SQL = """
SELECT *
FROM resource_inventory_watch_events
WHERE watch_session_id = $1 AND id = $2
"""

_EPOCH_SQL = """
SELECT epoch.id, epoch.scope_id, epoch.retired_at, epoch.last_attempt_at,
       epoch.last_complete_at, epoch.last_complete_snapshot_id,
       epoch.last_resource_version, epoch.leader_generation,
       epoch.controller_epoch, epoch.last_sequence,
       epoch.continuous_since, epoch.complete_through,
       epoch.continuity_health,
       epoch.epoch_number, epoch.coverage_mode, epoch.capture_epoch,
       epoch.required_for_rollup, epoch.recovery_from_epoch_id,
       epoch.require_after_recovery,
       scope.collector_id, scope.source_cluster, scope.api_resource, scope.namespace
FROM resource_inventory_scope_epochs AS epoch
JOIN resource_inventory_scopes AS scope ON scope.id = epoch.scope_id
WHERE epoch.id = $1
FOR UPDATE OF epoch
"""

_SNAPSHOT_SQL = """
SELECT * FROM resource_inventory_snapshots WHERE id = $1 FOR UPDATE
"""

_OPEN_INTERVAL_SQL = """
SELECT *
FROM resource_intervals
WHERE inventory_scope_id = $1
  AND source_kind = $2
  AND source_uid = $3
  AND ended_at IS NULL
FOR UPDATE
"""

_ITEM_CONFLICT_SQL = """
WITH incoming AS (
    SELECT * FROM jsonb_to_recordset($2::jsonb) AS item(
        source_kind text,
        source_uid text,
        revision_hash text,
        normalized_item jsonb,
        valid_for_metering boolean,
        item_error jsonb
    )
)
SELECT count(*)
FROM incoming
JOIN resource_inventory_snapshot_items AS existing
  ON existing.snapshot_id = $1
 AND existing.source_kind = incoming.source_kind
 AND existing.source_uid = incoming.source_uid
WHERE existing.revision_hash IS DISTINCT FROM incoming.revision_hash
   OR existing.normalized_item IS DISTINCT FROM incoming.normalized_item
   OR existing.valid_for_metering IS DISTINCT FROM incoming.valid_for_metering
   OR existing.item_error IS DISTINCT FROM incoming.item_error
"""

_ITEM_INSERT_SQL = """
WITH incoming AS (
    SELECT * FROM jsonb_to_recordset($2::jsonb) AS item(
        source_kind text,
        source_uid text,
        revision_hash text,
        normalized_item jsonb,
        valid_for_metering boolean,
        item_error jsonb
    )
), inserted AS (
    INSERT INTO resource_inventory_snapshot_items (
        snapshot_id, source_kind, source_uid, revision_hash,
        normalized_item, valid_for_metering, item_error
    )
    SELECT $1, source_kind, source_uid, revision_hash,
           normalized_item, valid_for_metering, item_error
    FROM incoming
    ON CONFLICT (snapshot_id, source_kind, source_uid) DO NOTHING
    RETURNING public.resource_inventory_snapshot_item_size_bytes(
        source_kind, source_uid, revision_hash, normalized_item, item_error
    ) AS staged_bytes
)
SELECT count(*) AS inserted_count,
       COALESCE(sum(staged_bytes), 0)::bigint AS inserted_bytes
FROM inserted
"""

_COMPARISON_CONFLICT_SQL = """
WITH incoming AS (
    SELECT * FROM jsonb_to_recordset($2::jsonb) AS comparison(
        source_uid text,
        owner_kind text,
        owner_id uuid,
        owner_trusted boolean,
        legacy_interval_id bigint,
        legacy_cpu_millicores bigint,
        legacy_memory_bytes bigint,
        legacy_started_at timestamptz,
        observed_cpu_millicores bigint,
        observed_memory_bytes bigint,
        observed_started_at timestamptz,
        observed_start_time_source text,
        observed_start_uncertainty_us bigint,
        start_delta_us bigint,
        status text,
        reason_code text,
        explained boolean,
        comparison_at timestamptz
    )
)
SELECT count(*)
FROM incoming
JOIN resource_inventory_shadow_comparisons AS existing
  ON existing.snapshot_id = $1
 AND existing.source_uid = incoming.source_uid
WHERE existing.owner_kind IS DISTINCT FROM incoming.owner_kind
   OR existing.owner_id IS DISTINCT FROM incoming.owner_id
   OR existing.owner_trusted IS DISTINCT FROM incoming.owner_trusted
   OR existing.legacy_interval_id IS DISTINCT FROM incoming.legacy_interval_id
   OR existing.legacy_cpu_millicores
        IS DISTINCT FROM incoming.legacy_cpu_millicores
   OR existing.legacy_memory_bytes
        IS DISTINCT FROM incoming.legacy_memory_bytes
   OR existing.legacy_started_at IS DISTINCT FROM incoming.legacy_started_at
   OR existing.observed_cpu_millicores
        IS DISTINCT FROM incoming.observed_cpu_millicores
   OR existing.observed_memory_bytes
        IS DISTINCT FROM incoming.observed_memory_bytes
   OR existing.observed_started_at IS DISTINCT FROM incoming.observed_started_at
   OR existing.observed_start_time_source
        IS DISTINCT FROM incoming.observed_start_time_source
   OR existing.observed_start_uncertainty_us
        IS DISTINCT FROM incoming.observed_start_uncertainty_us
   OR existing.start_delta_us IS DISTINCT FROM incoming.start_delta_us
   OR existing.status IS DISTINCT FROM incoming.status
   OR existing.reason_code IS DISTINCT FROM incoming.reason_code
   OR existing.explained IS DISTINCT FROM incoming.explained
   OR existing.comparison_at IS DISTINCT FROM incoming.comparison_at
"""

_COMPARISON_INSERT_SQL = """
WITH incoming AS (
    SELECT * FROM jsonb_to_recordset($3::jsonb) AS comparison(
        source_uid text,
        owner_kind text,
        owner_id uuid,
        owner_trusted boolean,
        legacy_interval_id bigint,
        legacy_cpu_millicores bigint,
        legacy_memory_bytes bigint,
        legacy_started_at timestamptz,
        observed_cpu_millicores bigint,
        observed_memory_bytes bigint,
        observed_started_at timestamptz,
        observed_start_time_source text,
        observed_start_uncertainty_us bigint,
        start_delta_us bigint,
        status text,
        reason_code text,
        explained boolean,
        comparison_at timestamptz
    )
), inserted AS (
    INSERT INTO resource_inventory_shadow_comparisons (
        snapshot_id, inventory_scope_id, source_uid,
        owner_kind, owner_id, owner_trusted,
        legacy_interval_id, legacy_cpu_millicores, legacy_memory_bytes,
        legacy_started_at, observed_cpu_millicores, observed_memory_bytes,
        observed_started_at, observed_start_time_source,
        observed_start_uncertainty_us, start_delta_us,
        status, reason_code, explained, comparison_at
    )
    SELECT $1, $2, source_uid, owner_kind, owner_id, owner_trusted,
           legacy_interval_id, legacy_cpu_millicores, legacy_memory_bytes,
           legacy_started_at, observed_cpu_millicores, observed_memory_bytes,
           observed_started_at, observed_start_time_source,
           observed_start_uncertainty_us, start_delta_us,
           status, reason_code, explained, comparison_at
    FROM incoming
    ON CONFLICT (snapshot_id, source_uid) DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""

_RECONCILIATION_COUNTS_SQL = """
WITH item_counts AS (
    SELECT count(*) AS present_items,
           count(*) FILTER (WHERE NOT valid_for_metering) AS invalid_items
    FROM resource_inventory_snapshot_items
    WHERE snapshot_id = $1
), interval_counts AS (
    SELECT
        count(*) FILTER (
            WHERE $4 >= interval.last_seen_at
        ) AS observed_intervals,
        count(*) FILTER (
            WHERE $3
              AND item.valid_for_metering
              AND item.revision_hash = interval.source_revision
              AND $4 >= interval.last_seen_at
        ) AS confirmed_intervals
    FROM resource_inventory_snapshot_items AS item
    JOIN resource_intervals AS interval
      ON interval.inventory_scope_id = $2
     AND interval.source_kind = item.source_kind
     AND interval.source_uid = item.source_uid
     AND interval.ended_at IS NULL
    WHERE item.snapshot_id = $1
), pending AS (
    SELECT count(*) AS pending_valid_items
    FROM resource_inventory_snapshot_items AS item
    LEFT JOIN resource_inventory_shadow_comparisons AS comparison
      ON comparison.snapshot_id = item.snapshot_id
     AND comparison.source_uid = item.source_uid
    WHERE item.snapshot_id = $1
      AND item.valid_for_metering
      AND (comparison.status IS NULL OR comparison.status <> 'not-applicable')
      AND NOT EXISTS (
          SELECT 1 FROM resource_intervals AS interval
          WHERE interval.inventory_scope_id = $2
            AND interval.source_kind = item.source_kind
            AND interval.source_uid = item.source_uid
            AND interval.source_revision = item.revision_hash
            AND interval.ended_at IS NULL
      )
), absent AS (
    SELECT count(*) AS closed_intervals
    FROM resource_intervals AS interval
    WHERE $3
      AND interval.inventory_scope_id = $2
      AND interval.ended_at IS NULL
      AND $4 >= interval.last_confirmed_at
      AND NOT EXISTS (
          SELECT 1 FROM resource_inventory_snapshot_items AS item
          WHERE item.snapshot_id = $1
            AND item.source_kind = interval.source_kind
            AND item.source_uid = interval.source_uid
      )
), shadow AS (
    SELECT count(*) AS shadow_comparisons
    FROM resource_inventory_shadow_comparisons
    WHERE snapshot_id = $1
)
SELECT item_counts.present_items, item_counts.invalid_items,
       interval_counts.observed_intervals, interval_counts.confirmed_intervals,
       absent.closed_intervals, pending.pending_valid_items,
       shadow.shadow_comparisons
FROM item_counts, interval_counts, pending, absent, shadow
"""

_INVENTORY_ONLY_COUNTS_SQL = """
SELECT count(*) AS present_items,
       count(*) FILTER (WHERE NOT valid_for_metering) AS invalid_items,
       0::bigint AS observed_intervals,
       0::bigint AS confirmed_intervals,
       0::bigint AS closed_intervals,
       0::bigint AS pending_valid_items,
       0::bigint AS shadow_comparisons
FROM resource_inventory_snapshot_items
WHERE snapshot_id = $1
"""

_OBSERVE_PRESENT_SQL = """
WITH updated AS (
    UPDATE resource_intervals AS interval
    SET last_seen_at = GREATEST(interval.last_seen_at, $4),
        last_confirmed_at = CASE
            WHEN $3 AND item.valid_for_metering
                 AND item.revision_hash = interval.source_revision
            THEN GREATEST(interval.last_confirmed_at, $4)
            ELSE interval.last_confirmed_at
        END,
        last_seen_snapshot_id = CASE
            WHEN $3 THEN $1 ELSE interval.last_seen_snapshot_id
        END,
        updated_at = statement_timestamp()
    FROM resource_inventory_snapshot_items AS item
    WHERE item.snapshot_id = $1
      AND interval.inventory_scope_id = $2
      AND interval.source_kind = item.source_kind
      AND interval.source_uid = item.source_uid
      AND interval.ended_at IS NULL
      AND $4 >= interval.last_seen_at
    RETURNING ($3 AND item.valid_for_metering
               AND item.revision_hash = interval.source_revision) AS confirmed
)
SELECT count(*) AS observed_intervals,
       count(*) FILTER (WHERE confirmed) AS confirmed_intervals
FROM updated
"""

_LINK_PRESENT_CLOSURES_SQL = """
/* infra-inventory:link-present-closures */
WITH linked AS (
    UPDATE resource_intervals AS interval
    SET last_seen_at = GREATEST(interval.last_seen_at, $3),
        last_seen_snapshot_id = $1,
        updated_at = statement_timestamp()
    FROM resource_inventory_snapshot_items AS item
    WHERE item.snapshot_id = $1
      AND item.valid_for_metering IS TRUE
      AND interval.inventory_scope_id = $2
      AND interval.source_kind = item.source_kind
      AND interval.source_uid = item.source_uid
      AND interval.ended_at = $3
      AND interval.end_time_source = 'app-db-received'
      AND interval.end_reason IN ('not-applicable', 'terminal-or-unscheduled')
      AND interval.last_seen_at <= $3
      AND interval.last_seen_snapshot_id IS DISTINCT FROM $1
    RETURNING interval.id
)
SELECT count(*) FROM linked
"""

_CLOSE_ABSENT_SQL = """
WITH closed AS (
    UPDATE resource_intervals AS interval
    SET ended_at = $3,
        end_time_source = 'complete-inventory-absence',
        end_uncertainty_us = floor(
            extract(epoch FROM ($3 - interval.last_confirmed_at)) * 1000000
        )::bigint,
        end_reason = 'absent-from-complete-snapshot',
        updated_at = statement_timestamp()
    WHERE $4
      AND interval.inventory_scope_id = $2
      AND interval.ended_at IS NULL
      AND $3 >= interval.last_confirmed_at
      AND NOT EXISTS (
          SELECT 1 FROM resource_inventory_snapshot_items AS item
          WHERE item.snapshot_id = $1
            AND item.source_kind = interval.source_kind
            AND item.source_uid = interval.source_uid
      )
    RETURNING interval.id, interval.source_lifecycle_id
), cleared_heads AS (
    UPDATE resource_lifecycle_heads AS head
    SET current_interval_id = NULL,
        updated_at = statement_timestamp()
    FROM closed
    WHERE head.source_lifecycle_id = closed.source_lifecycle_id
      AND head.current_interval_id = closed.id
    RETURNING 1
)
SELECT (SELECT count(*) FROM closed) AS closed_count,
       (SELECT count(*) FROM cleared_heads) AS cleared_head_count
"""


class InventoryStore:
    """Transactional inventory persistence and conservative reconciliation."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        max_batch_items: int = 500,
        max_batch_bytes: int = 2 * 1024 * 1024,
        max_snapshot_items: int = 100_000,
        max_snapshot_bytes: int = 64 * 1024 * 1024,
        max_error_items: int = 2_000,
        digest_prefetch: int = 500,
        ticket_ttl: timedelta = timedelta(minutes=15),
        max_ticket_ttl: timedelta = timedelta(hours=1),
        watch_session_ttl: timedelta = timedelta(minutes=15),
        max_watch_session_ttl: timedelta = timedelta(hours=1),
        max_watch_events: int = 10_000,
        max_watch_event_bytes: int = 2 * 1024 * 1024,
        max_watch_bytes: int = 64 * 1024 * 1024,
        transport_nonce_retention: timedelta = timedelta(hours=24),
        max_transport_nonce_retention: timedelta = timedelta(days=7),
        max_collector_clock_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        for name, value in (
            ("max_batch_items", max_batch_items),
            ("max_batch_bytes", max_batch_bytes),
            ("max_snapshot_items", max_snapshot_items),
            ("max_snapshot_bytes", max_snapshot_bytes),
            ("max_error_items", max_error_items),
            ("digest_prefetch", digest_prefetch),
            ("max_watch_events", max_watch_events),
            ("max_watch_event_bytes", max_watch_event_bytes),
            ("max_watch_bytes", max_watch_bytes),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if ticket_ttl <= timedelta(0) or ticket_ttl > max_ticket_ttl:
            raise ValueError("ticket_ttl must be positive and within maximum")
        if max_batch_bytes > max_snapshot_bytes:
            raise ValueError("max_batch_bytes cannot exceed max_snapshot_bytes")
        if (
            watch_session_ttl <= timedelta(0)
            or watch_session_ttl > max_watch_session_ttl
        ):
            raise ValueError("watch session TTL is outside configured bounds")
        if max_watch_event_bytes > max_watch_bytes:
            raise ValueError("max_watch_event_bytes cannot exceed max_watch_bytes")
        if (
            transport_nonce_retention <= timedelta(0)
            or transport_nonce_retention > max_transport_nonce_retention
        ):
            raise ValueError("transport nonce retention is outside configured bounds")
        if max_collector_clock_skew < timedelta(0):
            raise ValueError("max_collector_clock_skew must be nonnegative")
        self._pool = pool
        self.max_batch_items = max_batch_items
        self.max_batch_bytes = max_batch_bytes
        self.max_snapshot_items = max_snapshot_items
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_error_items = max_error_items
        self.digest_prefetch = digest_prefetch
        self.ticket_ttl = ticket_ttl
        self.max_ticket_ttl = max_ticket_ttl
        self.watch_session_ttl = watch_session_ttl
        self.max_watch_session_ttl = max_watch_session_ttl
        self.max_watch_events = max_watch_events
        self.max_watch_event_bytes = max_watch_event_bytes
        self.max_watch_bytes = max_watch_bytes
        self.transport_nonce_retention = transport_nonce_retention
        self.max_transport_nonce_retention = max_transport_nonce_retention
        self.max_collector_clock_skew = max_collector_clock_skew
        self._active_generation: int | None = None
        self._generation_lock = asyncio.Lock()

    @property
    def active_generation(self) -> int | None:
        return self._active_generation

    async def activate_generation(self, expected_generation: int | None = None) -> int:
        """Activate this store for one leader-tenure generation.

        ``expected_generation`` is supplied by the advisory-lock acquisition
        callback in production, so every singleton metering component adopts
        the same token.  The no-argument form remains useful for isolated store
        tests and explicitly increments the durable fence itself.
        """
        async with self._generation_lock:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    if expected_generation is None:
                        generation = await conn.fetchval(
                            "UPDATE infra_metering_control "
                            "SET leader_generation = leader_generation + 1, "
                            "updated_at = statement_timestamp() "
                            "WHERE singleton = TRUE RETURNING leader_generation"
                        )
                    else:
                        if (
                            isinstance(expected_generation, bool)
                            or not isinstance(expected_generation, int)
                            or expected_generation <= 0
                        ):
                            raise InventoryFenceError(
                                "metering generation must be a positive integer"
                            )
                        generation = await conn.fetchval(
                            "SELECT leader_generation FROM infra_metering_control "
                            "WHERE singleton = TRUE FOR SHARE"
                        )
                        if int(generation or -1) != expected_generation:
                            raise InventoryFenceError(
                                "metering leader generation changed before adoption"
                            )
                    if generation is None:
                        raise InventoryFenceError("infra metering control row missing")
            self._active_generation = int(generation)
            return self._active_generation

    async def deactivate_generation(self, generation: int) -> bool:
        """Clear only this instance's lease state; DB generation never regresses."""
        async with self._generation_lock:
            if self._active_generation != generation:
                return False
            self._active_generation = None
            return True

    def _require_local_generation(self, generation: int) -> None:
        if self._active_generation != generation:
            raise InventoryFenceError("metering generation is not active locally")

    @staticmethod
    def _assert_scope(
        epoch: asyncpg.Record,
        expected: InventoryScopeIdentity,
    ) -> None:
        if (
            epoch["collector_id"] != expected.collector_id
            or epoch["source_cluster"] != expected.source_cluster
            or epoch["api_resource"] != expected.api_resource
            or epoch["namespace"] != expected.namespace
        ):
            raise InventoryFenceError(
                "ingestion grant does not match the requested inventory scope"
            )

    @staticmethod
    def _assert_items_in_scope(
        items: Sequence[InventoryItem],
        expected: InventoryScopeIdentity,
    ) -> None:
        if any(
            item.normalized_item.get("source_kind") != item.source_kind
            or item.normalized_item.get("uid") != item.source_uid
            or (
                expected.namespace is not None
                and item.normalized_item.get("namespace") != expected.namespace
            )
            for item in items
        ):
            raise InventoryFenceError(
                "normalized item identity is outside the ticket scope"
            )

    async def _claim_transport_nonce(
        self,
        conn: asyncpg.Connection,
        claim: TransportNonceClaim,
        *,
        scope_epoch_id: UUID,
        leader_generation: int,
        request_kind: str,
        collector_id: str,
    ) -> None:
        """Claim a signed request nonce in the caller's side-effect transaction."""
        if claim.request_kind != request_kind:
            raise InventoryContractError("transport request_kind does not match route")
        if claim.collector_id != collector_id:
            raise InventoryFenceError("transport collector does not own scope")
        try:
            await conn.execute(
                "INSERT INTO resource_inventory_transport_nonces ("
                "collector_id, request_nonce, request_kind, request_digest, "
                "scope_epoch_id, leader_generation, expires_at) VALUES ("
                "$1, $2, $3, $4, $5, $6, "
                "statement_timestamp() + $7::interval)",
                claim.collector_id,
                claim.request_nonce,
                claim.request_kind,
                claim.request_digest,
                scope_epoch_id,
                leader_generation,
                self.transport_nonce_retention,
            )
        except asyncpg.UniqueViolationError as exc:
            raise InventoryConflictError(
                "authenticated transport request nonce was already claimed"
            ) from exc

    async def purge_expired_transport_nonces(self, *, limit: int = 1_000) -> int:
        """Delete an expiry-ordered, skip-locked batch outside request paths."""
        if limit <= 0 or limit > _MAX_PURGE_BATCH:
            raise InventoryContractError("transport nonce cleanup limit is invalid")
        async with self._pool.acquire() as conn:
            deleted = await conn.fetchval(
                "WITH expired AS ("
                "SELECT collector_id, request_nonce "
                "FROM resource_inventory_transport_nonces "
                "WHERE expires_at <= statement_timestamp() "
                "ORDER BY expires_at, collector_id, request_nonce "
                "FOR UPDATE SKIP LOCKED LIMIT $1"
                "), deleted AS ("
                "DELETE FROM resource_inventory_transport_nonces nonce "
                "USING expired "
                "WHERE nonce.collector_id=expired.collector_id "
                "AND nonce.request_nonce=expired.request_nonce RETURNING 1"
                ") SELECT count(*) FROM deleted",
                limit,
            )
        return int(deleted)

    async def purge_diagnostics(
        self,
        generation: int,
        *,
        snapshot_item_retention: timedelta = _MIN_SNAPSHOT_ITEM_RETENTION,
        diagnostic_retention: timedelta = timedelta(days=35),
        abandoned_staging_retention: timedelta = (_MIN_ABANDONED_STAGING_RETENTION),
        limit: int = 1_000,
    ) -> InventoryPurgeResult:
        """Run one bounded, DB-clock retention pass for the active leader.

        Snapshot metadata and all accounting authority remain. Sealed and
        abandoned manifests first enter immutable expiry terminals; only then
        may their normalized payloads and aged shadow diagnostics be removed.
        WATCH children are removed before their terminal session parent, and
        only expired tickets which never bound a snapshot are eligible.
        """
        if limit <= 0 or limit > _MAX_PURGE_BATCH:
            raise InventoryContractError("inventory cleanup limit is invalid")
        if snapshot_item_retention < _MIN_SNAPSHOT_ITEM_RETENTION:
            raise InventoryContractError(
                "snapshot item retention cannot be shorter than seven days"
            )
        if diagnostic_retention < _MIN_DIAGNOSTIC_RETENTION:
            raise InventoryContractError(
                "diagnostic retention cannot be shorter than seven days"
            )
        if diagnostic_retention < snapshot_item_retention:
            raise InventoryContractError(
                "diagnostic retention cannot be shorter than snapshot retention"
            )
        if abandoned_staging_retention < _MIN_ABANDONED_STAGING_RETENTION:
            raise InventoryContractError(
                "abandoned staging retention cannot be shorter than 24 hours"
            )
        self._require_local_generation(generation)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT leader_generation FROM infra_metering_control "
                    "WHERE singleton = TRUE FOR SHARE"
                )
                if int(current or -1) != generation:
                    raise InventoryFenceError("inventory cleanup generation is stale")
                self._require_local_generation(generation)

                sealed_snapshots_expired = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT snapshot.id "
                        "FROM resource_inventory_snapshots snapshot "
                        "WHERE snapshot.manifest_state='sealed' "
                        "AND snapshot.sealed_at <= "
                        "statement_timestamp() - $1::interval "
                        "ORDER BY snapshot.sealed_at, snapshot.id "
                        "LIMIT $2 FOR UPDATE OF snapshot SKIP LOCKED"
                        "), expired AS ("
                        "UPDATE resource_inventory_snapshots snapshot SET "
                        "manifest_state='items-expired', "
                        "items_expired_at=statement_timestamp() "
                        "FROM candidates WHERE snapshot.id=candidates.id "
                        "RETURNING 1"
                        ") SELECT count(*) FROM expired",
                        snapshot_item_retention,
                        limit,
                    )
                )
                abandoned_snapshots_expired = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT snapshot.id "
                        "FROM resource_inventory_snapshots snapshot "
                        "WHERE snapshot.manifest_state='staging' "
                        "AND NOT snapshot.complete "
                        "AND snapshot.created_at <= "
                        "statement_timestamp() - $1::interval "
                        "AND (snapshot.ingest_ticket_id IS NULL OR EXISTS ("
                        "SELECT 1 FROM resource_inventory_ingest_tickets ticket "
                        "WHERE ticket.id=snapshot.ingest_ticket_id "
                        "AND ticket.scope_epoch_id=snapshot.scope_epoch_id "
                        "AND ticket.bound_snapshot_id=snapshot.id "
                        "AND ticket.expires_at <= statement_timestamp()"
                        ")) "
                        "ORDER BY snapshot.created_at, snapshot.id "
                        "LIMIT $2 FOR UPDATE OF snapshot SKIP LOCKED"
                        "), expired AS ("
                        "UPDATE resource_inventory_snapshots snapshot SET "
                        "manifest_state='staging-expired', "
                        "items_expired_at=statement_timestamp() "
                        "FROM candidates WHERE snapshot.id=candidates.id "
                        "RETURNING 1"
                        ") SELECT count(*) FROM expired",
                        abandoned_staging_retention,
                        limit,
                    )
                )
                snapshot_items_deleted = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT item.snapshot_id, item.source_kind, "
                        "item.source_uid "
                        "FROM resource_inventory_snapshot_items item "
                        "JOIN resource_inventory_snapshots snapshot "
                        "ON snapshot.id=item.snapshot_id "
                        "WHERE snapshot.manifest_state IN "
                        "('items-expired', 'staging-expired') "
                        "ORDER BY snapshot.items_expired_at, item.snapshot_id, "
                        "item.source_kind, item.source_uid "
                        "LIMIT $1 FOR UPDATE OF item SKIP LOCKED"
                        "), deleted AS ("
                        "DELETE FROM resource_inventory_snapshot_items item "
                        "USING candidates WHERE "
                        "item.snapshot_id=candidates.snapshot_id "
                        "AND item.source_kind=candidates.source_kind "
                        "AND item.source_uid=candidates.source_uid RETURNING 1"
                        ") SELECT count(*) FROM deleted",
                        limit,
                    )
                )
                shadow_comparisons_deleted = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT comparison.id "
                        "FROM resource_inventory_shadow_comparisons comparison "
                        "JOIN resource_inventory_snapshots snapshot "
                        "ON snapshot.id=comparison.snapshot_id "
                        "WHERE snapshot.manifest_state IN "
                        "('items-expired', 'staging-expired') "
                        "AND comparison.comparison_at <= "
                        "statement_timestamp() - $1::interval "
                        "AND comparison.created_at <= "
                        "statement_timestamp() - $1::interval "
                        "ORDER BY comparison.comparison_at, "
                        "comparison.created_at, comparison.id "
                        "LIMIT $2 FOR UPDATE OF comparison SKIP LOCKED"
                        "), deleted AS ("
                        "DELETE FROM resource_inventory_shadow_comparisons "
                        "comparison USING candidates "
                        "WHERE comparison.id=candidates.id RETURNING 1"
                        ") SELECT count(*) FROM deleted",
                        diagnostic_retention,
                        limit,
                    )
                )
                watch_events_deleted = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT event.watch_session_id, event.id "
                        "FROM resource_inventory_watch_events event "
                        "JOIN resource_inventory_watch_sessions session "
                        "ON session.id=event.watch_session_id "
                        "AND session.scope_epoch_id=event.scope_epoch_id "
                        "WHERE (session.consumed_at IS NOT NULL "
                        "OR session.expires_at <= statement_timestamp()) "
                        "AND COALESCE(session.consumed_at, session.expires_at) "
                        "<= statement_timestamp() - $1::interval "
                        "ORDER BY COALESCE(session.consumed_at, "
                        "session.expires_at), event.received_at, "
                        "event.watch_session_id, event.id "
                        "LIMIT $2 FOR UPDATE OF event SKIP LOCKED"
                        "), deleted AS ("
                        "DELETE FROM resource_inventory_watch_events event "
                        "USING candidates WHERE "
                        "event.watch_session_id=candidates.watch_session_id "
                        "AND event.id=candidates.id RETURNING 1"
                        ") SELECT count(*) FROM deleted",
                        diagnostic_retention,
                        limit,
                    )
                )
                watch_sessions_deleted = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT session.id "
                        "FROM resource_inventory_watch_sessions session "
                        "WHERE (session.consumed_at IS NOT NULL "
                        "OR session.expires_at <= statement_timestamp()) "
                        "AND COALESCE(session.consumed_at, session.expires_at) "
                        "<= statement_timestamp() - $1::interval "
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM resource_inventory_watch_events event "
                        "WHERE event.watch_session_id=session.id"
                        ") ORDER BY COALESCE(session.consumed_at, "
                        "session.expires_at), session.id "
                        "LIMIT $2 FOR UPDATE OF session SKIP LOCKED"
                        "), deleted AS ("
                        "DELETE FROM resource_inventory_watch_sessions session "
                        "USING candidates WHERE session.id=candidates.id "
                        "RETURNING 1"
                        ") SELECT count(*) FROM deleted",
                        diagnostic_retention,
                        limit,
                    )
                )
                unbound_tickets_deleted = int(
                    await conn.fetchval(
                        "WITH candidates AS ("
                        "SELECT ticket.id "
                        "FROM resource_inventory_ingest_tickets ticket "
                        "WHERE ticket.bound_snapshot_id IS NULL "
                        "AND ticket.bound_at IS NULL "
                        "AND ticket.consumed_at IS NULL "
                        "AND ticket.expires_at <= statement_timestamp() "
                        "ORDER BY ticket.expires_at, ticket.id "
                        "LIMIT $1 FOR UPDATE OF ticket SKIP LOCKED"
                        "), deleted AS ("
                        "DELETE FROM resource_inventory_ingest_tickets ticket "
                        "USING candidates WHERE ticket.id=candidates.id "
                        "RETURNING 1"
                        ") SELECT count(*) FROM deleted",
                        limit,
                    )
                )

        return InventoryPurgeResult(
            leader_generation=generation,
            batch_limit=limit,
            sealed_snapshots_expired=sealed_snapshots_expired,
            abandoned_snapshots_expired=abandoned_snapshots_expired,
            snapshot_items_deleted=snapshot_items_deleted,
            shadow_comparisons_deleted=shadow_comparisons_deleted,
            watch_events_deleted=watch_events_deleted,
            watch_sessions_deleted=watch_sessions_deleted,
            unbound_tickets_deleted=unbound_tickets_deleted,
        )

    async def issue_ingest_ticket(
        self,
        scope_epoch_id: UUID,
        request_digest: str,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        require_healthy_continuity: bool = False,
        ttl: timedelta | None = None,
        max_snapshot_items: int | None = None,
        max_snapshot_bytes: int | None = None,
    ) -> IngestTicket:
        if not _HASH_RE.fullmatch(request_digest):
            raise InventoryContractError("request_digest must be SHA-256")
        ttl = self.ticket_ttl if ttl is None else ttl
        if ttl <= timedelta(0) or ttl > self.max_ticket_ttl:
            raise InventoryContractError("ticket TTL is outside configured bounds")
        ticket_max_items = (
            self.max_snapshot_items
            if max_snapshot_items is None
            else max_snapshot_items
        )
        ticket_max_bytes = (
            self.max_snapshot_bytes
            if max_snapshot_bytes is None
            else max_snapshot_bytes
        )
        if ticket_max_items <= 0 or ticket_max_items > self.max_snapshot_items:
            raise InventoryContractError("snapshot item limit is outside bounds")
        if ticket_max_bytes <= 0 or ticket_max_bytes > self.max_snapshot_bytes:
            raise InventoryContractError("snapshot byte limit is outside bounds")

        ticket_id = uuid4()
        token = secrets.token_urlsafe(32)
        nonce_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        generation = 0
        row: asyncpg.Record | None = None
        recovery_required = False
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT leader_generation FROM infra_metering_control "
                    "WHERE singleton = TRUE FOR SHARE"
                )
                generation = int(current or 0)
                if generation <= 0:
                    raise InventoryFenceError("no active metering generation")
                epoch = await conn.fetchrow(_EPOCH_SQL, scope_epoch_id)
                if epoch is None or epoch["retired_at"] is not None:
                    raise InventoryFenceError("scope epoch is missing or retired")
                self._assert_scope(epoch, scope)
                if require_healthy_continuity and epoch["continuity_health"] == "gap":
                    # This check must remain under the epoch row lock. The HTTP
                    # adapter's earlier health check is only an optimization;
                    # a WATCH request may record a gap before ticket admission.
                    # "initializing" is valid here: its first LIST is what
                    # establishes healthy continuity.
                    raise InventoryRecoveryRequired(
                        "snapshot admission requires healthy continuity"
                    )
                # The epoch row lock serializes this admission decision with
                # WATCH-session issuance. Do not lock the session row here:
                # event/finish paths lock session then epoch, and reversing
                # that order would deadlock. A concurrently finishing session
                # is conservatively classified as abandoned, which is safe.
                abandoned = await conn.fetchrow(
                    "SELECT id, starting_resource_version, "
                    "last_resource_version, committed_events, committed_bytes, "
                    "created_at, expires_at "
                    "FROM resource_inventory_watch_sessions "
                    "WHERE scope_epoch_id=$1 AND consumed_at IS NULL "
                    "ORDER BY created_at, id LIMIT 1",
                    scope_epoch_id,
                )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=scope_epoch_id,
                    leader_generation=generation,
                    request_kind="snapshot-ticket",
                    collector_id=str(epoch["collector_id"]),
                )
                if abandoned is not None:
                    received_at = await conn.fetchval("SELECT statement_timestamp()")
                    gap_start = next(
                        (
                            value
                            for value in (
                                epoch["complete_through"],
                                epoch["last_complete_at"],
                                epoch["continuous_since"],
                            )
                            if value is not None
                        ),
                        received_at,
                    )
                    if gap_start > received_at:
                        gap_start = received_at
                    gap_id = uuid4()
                    await conn.execute(
                        "INSERT INTO resource_inventory_coverage_gaps ("
                        "id, scope_epoch_id, gap_start, reason, "
                        "resolution_details) VALUES ($1, $2, $3, $4, $5::jsonb)",
                        gap_id,
                        scope_epoch_id,
                        gap_start,
                        _ABANDONED_WATCH_GAP_REASON,
                        _canonical_json(
                            {
                                "code": _ABANDONED_WATCH_GAP_REASON,
                                "watch_session_id": str(abandoned["id"]),
                                "starting_resource_version": str(
                                    abandoned["starting_resource_version"]
                                ),
                                "server_committed_resource_version": str(
                                    abandoned["last_resource_version"]
                                ),
                                "committed_events": int(abandoned["committed_events"]),
                                "committed_bytes": int(abandoned["committed_bytes"]),
                            }
                        ),
                    )
                    changed = await conn.fetchval(
                        "UPDATE resource_inventory_scope_epochs SET "
                        "last_attempt_at=$2, leader_generation=$3, "
                        "continuity_health='gap', backend_health='degraded', "
                        "consecutive_failures=consecutive_failures+1, "
                        "sanitized_error=$4::jsonb, "
                        "updated_at=statement_timestamp() "
                        "WHERE id=$1 AND retired_at IS NULL RETURNING TRUE",
                        scope_epoch_id,
                        received_at,
                        generation,
                        _canonical_json(
                            {
                                "code": _ABANDONED_WATCH_GAP_REASON,
                                "coverage_gap_id": str(gap_id),
                            }
                        ),
                    )
                    if not changed:
                        raise InventoryFenceError(
                            "scope epoch retired during snapshot admission"
                        )
                    recovery_required = True
                else:
                    row = await conn.fetchrow(
                        "INSERT INTO resource_inventory_ingest_tickets ("
                        "id, nonce_hash, scope_epoch_id, leader_generation, "
                        "request_digest, max_snapshot_items, max_snapshot_bytes, "
                        "expires_at) VALUES ("
                        "$1, $2, $3, $4, $5, $6, $7, "
                        "statement_timestamp() + $8::interval) "
                        "RETURNING expires_at",
                        ticket_id,
                        nonce_hash,
                        scope_epoch_id,
                        generation,
                        request_digest,
                        ticket_max_items,
                        ticket_max_bytes,
                        ttl,
                    )
        if recovery_required:
            raise InventoryRecoveryRequired(
                "snapshot admission found broken WATCH continuity"
            )
        if row is None:
            raise InventoryConflictError("snapshot ticket issuance produced no row")
        return IngestTicket(
            id=ticket_id,
            token=token,
            scope_epoch_id=scope_epoch_id,
            leader_generation=generation,
            request_digest=request_digest,
            max_snapshot_items=ticket_max_items,
            max_snapshot_bytes=ticket_max_bytes,
            expires_at=row["expires_at"],
        )

    async def _lock_ticket(
        self,
        conn: asyncpg.Connection,
        token: str,
        ticket_id: UUID,
        expected_scope: InventoryScopeIdentity,
        *,
        allow_consumed: bool = False,
    ) -> tuple[asyncpg.Record, asyncpg.Record]:
        nonce_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ticket = await conn.fetchrow(_TICKET_SQL, nonce_hash)
        if ticket is None:
            raise InventoryTicketError("inventory ingest ticket is unknown")
        if ticket["id"] != ticket_id:
            raise InventoryTicketError("inventory ingest ticket id/token mismatch")
        current = await conn.fetchval(
            "SELECT leader_generation FROM infra_metering_control "
            "WHERE singleton = TRUE FOR SHARE"
        )
        if int(current or -1) != int(ticket["leader_generation"]):
            raise InventoryFenceError("inventory ingest ticket generation is stale")
        epoch = await conn.fetchrow(_EPOCH_SQL, ticket["scope_epoch_id"])
        if epoch is None or epoch["retired_at"] is not None:
            raise InventoryFenceError("scope epoch is missing or retired")
        self._assert_scope(epoch, expected_scope)
        if ticket["consumed_at"] is not None and not allow_consumed:
            raise InventoryTicketError("inventory ingest ticket is already consumed")
        live = await conn.fetchval(
            "SELECT $1::timestamptz > statement_timestamp()",
            ticket["expires_at"],
        )
        if not live and ticket["consumed_at"] is None:
            raise InventoryTicketError("inventory ingest ticket has expired")
        return ticket, epoch

    async def issue_watch_session(
        self,
        scope_epoch_id: UUID,
        request_digest: str,
        starting_resource_version: str,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        max_events: int | None = None,
        max_bytes: int | None = None,
        ttl: timedelta | None = None,
    ) -> WatchSessionGrant:
        """Issue one current-generation, cursor-bound, bounded WATCH grant."""
        if not _HASH_RE.fullmatch(request_digest):
            raise InventoryContractError("request_digest must be SHA-256")
        if (
            not starting_resource_version
            or starting_resource_version == "0"
            or len(starting_resource_version) > 1024
        ):
            raise InventoryContractError("starting watch cursor is invalid")
        session_max_events = self.max_watch_events if max_events is None else max_events
        session_max_bytes = self.max_watch_bytes if max_bytes is None else max_bytes
        if session_max_events <= 0 or session_max_events > self.max_watch_events:
            raise InventoryContractError("watch event limit is outside bounds")
        if session_max_bytes <= 0 or session_max_bytes > self.max_watch_bytes:
            raise InventoryContractError("watch byte limit is outside bounds")
        session_ttl = self.watch_session_ttl if ttl is None else ttl
        if session_ttl <= timedelta(0) or session_ttl > self.max_watch_session_ttl:
            raise InventoryContractError("watch session TTL is outside bounds")

        session_id = uuid4()
        token = secrets.token_urlsafe(32)
        nonce_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT leader_generation FROM infra_metering_control "
                    "WHERE singleton = TRUE FOR SHARE"
                )
                generation = int(current or 0)
                if generation <= 0:
                    raise InventoryFenceError("no active metering generation")
                epoch = await conn.fetchrow(_EPOCH_SQL, scope_epoch_id)
                if epoch is None or epoch["retired_at"] is not None:
                    raise InventoryFenceError("scope epoch is missing or retired")
                self._assert_scope(epoch, scope)
                if epoch["continuity_health"] == "gap":
                    raise InventoryFenceError(
                        "watch history is broken; a recovery epoch/LIST is required"
                    )
                if (
                    int(epoch["leader_generation"]) != generation
                    or epoch["last_complete_snapshot_id"] is None
                    or not await conn.fetchval(
                        "SELECT TRUE FROM resource_inventory_snapshots "
                        "WHERE id=$1 AND scope_epoch_id=$2 "
                        "AND leader_generation=$3 AND complete "
                        "AND manifest_state='sealed'",
                        epoch["last_complete_snapshot_id"],
                        scope_epoch_id,
                        generation,
                    )
                ):
                    raise InventoryFenceError(
                        "current generation requires a fresh complete LIST"
                    )
                if epoch["last_resource_version"] != starting_resource_version:
                    raise InventoryConflictError(
                        "starting watch cursor does not match committed cursor"
                    )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=scope_epoch_id,
                    leader_generation=generation,
                    request_kind="watch-session",
                    collector_id=str(epoch["collector_id"]),
                )
                row = await conn.fetchrow(
                    "INSERT INTO resource_inventory_watch_sessions ("
                    "id, nonce_hash, scope_epoch_id, leader_generation, "
                    "request_digest, starting_resource_version, "
                    "last_resource_version, max_events, max_bytes, expires_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8, "
                    "statement_timestamp() + $9::interval) RETURNING expires_at",
                    session_id,
                    nonce_hash,
                    scope_epoch_id,
                    generation,
                    request_digest,
                    starting_resource_version,
                    session_max_events,
                    session_max_bytes,
                    session_ttl,
                )
        return WatchSessionGrant(
            id=session_id,
            token=token,
            scope_epoch_id=scope_epoch_id,
            leader_generation=generation,
            request_digest=request_digest,
            starting_resource_version=starting_resource_version,
            max_events=session_max_events,
            max_bytes=session_max_bytes,
            expires_at=row["expires_at"],
        )

    async def _lock_watch_session(
        self,
        conn: asyncpg.Connection,
        token: str,
        watch_session_id: UUID,
        expected_scope: InventoryScopeIdentity,
        *,
        allow_consumed: bool = False,
    ) -> tuple[asyncpg.Record, asyncpg.Record]:
        nonce_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        session = await conn.fetchrow(_WATCH_SESSION_SQL, nonce_hash)
        if session is None:
            raise InventoryTicketError("inventory watch session is unknown")
        if session["id"] != watch_session_id:
            raise InventoryTicketError("watch session id/token mismatch")
        current = await conn.fetchval(
            "SELECT leader_generation FROM infra_metering_control "
            "WHERE singleton = TRUE FOR SHARE"
        )
        if int(current or -1) != int(session["leader_generation"]):
            raise InventoryFenceError("inventory watch session generation is stale")
        epoch = await conn.fetchrow(_EPOCH_SQL, session["scope_epoch_id"])
        if epoch is None or epoch["retired_at"] is not None:
            raise InventoryFenceError("scope epoch is missing or retired")
        self._assert_scope(epoch, expected_scope)
        if session["consumed_at"] is not None and not allow_consumed:
            raise InventoryTicketError("inventory watch session is consumed")
        live = await conn.fetchval(
            "SELECT $1::timestamptz > statement_timestamp()",
            session["expires_at"],
        )
        if not live and session["consumed_at"] is None:
            raise InventoryTicketError("inventory watch session has expired")
        return session, epoch

    async def start_watch_recovery_epoch(
        self,
        broken_scope_epoch_id: UUID,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
    ) -> RecoveryEpochHandle:
        """Retire a 410-broken epoch and create its non-authoritative successor."""
        new_epoch_id = uuid4()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT leader_generation FROM infra_metering_control "
                    "WHERE singleton = TRUE FOR SHARE"
                )
                generation = int(current or 0)
                if generation <= 0:
                    raise InventoryFenceError("no active metering generation")
                old = await conn.fetchrow(_EPOCH_SQL, broken_scope_epoch_id)
                if old is None or old["retired_at"] is not None:
                    raise InventoryFenceError("broken scope epoch is not active")
                self._assert_scope(old, scope)
                if old["continuity_health"] != "gap":
                    raise InventoryContractError(
                        "recovery epoch requires a durable continuity gap"
                    )
                unresolved = await conn.fetchval(
                    "SELECT TRUE FROM resource_inventory_coverage_gaps "
                    "WHERE scope_epoch_id=$1 AND resolution='unresolved' "
                    "LIMIT 1",
                    broken_scope_epoch_id,
                )
                if not unresolved:
                    raise InventoryContractError(
                        "recovery epoch requires an unresolved predecessor gap"
                    )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=broken_scope_epoch_id,
                    leader_generation=generation,
                    request_kind="scope-recovery",
                    collector_id=str(old["collector_id"]),
                )
                retired_at = await conn.fetchval("SELECT statement_timestamp()")
                retired = await conn.fetchval(
                    "UPDATE resource_inventory_scope_epochs SET "
                    "retired_at=$2, leader_generation=$3, "
                    "updated_at=statement_timestamp() "
                    "WHERE id=$1 AND retired_at IS NULL "
                    "AND continuity_health='gap' RETURNING TRUE",
                    broken_scope_epoch_id,
                    retired_at,
                    generation,
                )
                if not retired:
                    raise InventoryConflictError(
                        "broken scope epoch lost its active transition"
                    )
                await conn.execute(
                    "INSERT INTO resource_inventory_scope_epochs ("
                    "id, scope_id, epoch_number, coverage_mode, capture_epoch, "
                    "leader_generation, recovery_from_epoch_id, "
                    "require_after_recovery) VALUES ("
                    "$1, $2, $3, $4, $5, $6, $7, $8)",
                    new_epoch_id,
                    old["scope_id"],
                    int(old["epoch_number"]) + 1,
                    old["coverage_mode"],
                    old["capture_epoch"],
                    generation,
                    broken_scope_epoch_id,
                    bool(old["required_for_rollup"]),
                )
                return RecoveryEpochHandle(
                    scope_epoch_id=new_epoch_id,
                    recovery_from_epoch_id=broken_scope_epoch_id,
                    inventory_scope_id=old["scope_id"],
                    epoch_number=int(old["epoch_number"]) + 1,
                    leader_generation=generation,
                    predecessor_retired_at=retired_at,
                )

    async def start_controller_epoch_recovery(
        self,
        broken_scope_epoch_id: UUID,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
    ) -> RecoveryEpochHandle:
        """Fence a restarted remote controller behind a fresh recovery LIST.

        A new collector process cannot prove what happened between the old
        process's last committed cursor and its first observation.  Record that
        uncertainty on the predecessor and create a non-authoritative successor
        atomically; only the successor's complete LIST can restore coverage.
        """

        new_epoch_id = uuid4()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchval(
                    "SELECT leader_generation FROM infra_metering_control "
                    "WHERE singleton = TRUE FOR SHARE"
                )
                generation = int(current or 0)
                if generation <= 0:
                    raise InventoryFenceError("no active metering generation")
                old = await conn.fetchrow(_EPOCH_SQL, broken_scope_epoch_id)
                if old is None or old["retired_at"] is not None:
                    raise InventoryFenceError("controller scope epoch is not active")
                self._assert_scope(old, scope)
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=broken_scope_epoch_id,
                    leader_generation=generation,
                    request_kind="controller-epoch-change",
                    collector_id=str(old["collector_id"]),
                )
                received_at = await conn.fetchval("SELECT statement_timestamp()")
                gap_start = next(
                    (
                        value
                        for value in (
                            old["complete_through"],
                            old["last_complete_at"],
                            old["continuous_since"],
                        )
                        if value is not None
                    ),
                    received_at,
                )
                if gap_start > received_at:
                    gap_start = received_at
                await conn.execute(
                    "INSERT INTO resource_inventory_coverage_gaps ("
                    "scope_epoch_id, gap_start, reason, resolution_details) "
                    "VALUES ($1, $2, $3, $4::jsonb)",
                    broken_scope_epoch_id,
                    gap_start,
                    _CONTROLLER_EPOCH_GAP_REASON,
                    _canonical_json({"code": _CONTROLLER_EPOCH_GAP_REASON}),
                )
                retired = await conn.fetchval(
                    "UPDATE resource_inventory_scope_epochs SET "
                    "retired_at=$2, leader_generation=$3, "
                    "continuity_health='gap', backend_health='degraded', "
                    "sanitized_error=$4::jsonb, "
                    "updated_at=statement_timestamp() "
                    "WHERE id=$1 AND retired_at IS NULL RETURNING TRUE",
                    broken_scope_epoch_id,
                    received_at,
                    generation,
                    _canonical_json({"code": _CONTROLLER_EPOCH_GAP_REASON}),
                )
                if not retired:
                    raise InventoryConflictError(
                        "controller scope epoch lost its active transition"
                    )
                await conn.execute(
                    "INSERT INTO resource_inventory_scope_epochs ("
                    "id, scope_id, epoch_number, coverage_mode, capture_epoch, "
                    "leader_generation, recovery_from_epoch_id, "
                    "require_after_recovery) VALUES ("
                    "$1, $2, $3, $4, $5, $6, $7, $8)",
                    new_epoch_id,
                    old["scope_id"],
                    int(old["epoch_number"]) + 1,
                    old["coverage_mode"],
                    old["capture_epoch"],
                    generation,
                    broken_scope_epoch_id,
                    bool(old["required_for_rollup"]),
                )
                return RecoveryEpochHandle(
                    scope_epoch_id=new_epoch_id,
                    recovery_from_epoch_id=broken_scope_epoch_id,
                    inventory_scope_id=old["scope_id"],
                    epoch_number=int(old["epoch_number"]) + 1,
                    leader_generation=generation,
                    predecessor_retired_at=received_at,
                )

    async def begin_snapshot(
        self,
        token: str,
        ticket_id: UUID,
        snapshot_id: UUID,
        collection_started_at: datetime,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        controller_epoch: str | None = None,
        sequence: int | None = None,
    ) -> SnapshotHandle:
        _require_aware(collection_started_at, "collection_started_at")
        if (controller_epoch is None) != (sequence is None):
            raise InventoryContractError(
                "controller_epoch and sequence must be supplied together"
            )
        if controller_epoch == "" or (sequence is not None and sequence < 0):
            raise InventoryContractError("controller sequence identity is invalid")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ticket, epoch = await self._lock_ticket(conn, token, ticket_id, scope)
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=ticket["scope_epoch_id"],
                    leader_generation=int(ticket["leader_generation"]),
                    request_kind="snapshot-begin",
                    collector_id=str(epoch["collector_id"]),
                )
                bound = ticket["bound_snapshot_id"]
                if bound is not None and bound != snapshot_id:
                    raise InventoryTicketError(
                        "inventory ingest ticket is bound to another snapshot"
                    )
                inserted = await conn.fetchval(
                    "INSERT INTO resource_inventory_snapshots ("
                    "id, scope_epoch_id, inventory_scope_id, "
                    "collection_started_at, collection_completed_at, received_at, "
                    "complete, leader_generation, controller_epoch, sequence, "
                    "item_count, ingest_ticket_id) VALUES ("
                    "$1, $2, $3, $4, $4, $4, FALSE, $5, $6, $7, 0, $8) "
                    "ON CONFLICT DO NOTHING RETURNING TRUE",
                    snapshot_id,
                    ticket["scope_epoch_id"],
                    epoch["scope_id"],
                    collection_started_at,
                    ticket["leader_generation"],
                    controller_epoch,
                    sequence,
                    ticket["id"],
                )
                snapshot = await conn.fetchrow(_SNAPSHOT_SQL, snapshot_id)
                if snapshot is None:
                    raise InventoryConflictError(
                        "snapshot sequence or ticket is already bound differently"
                    )
                if (
                    snapshot["scope_epoch_id"] != ticket["scope_epoch_id"]
                    or snapshot["inventory_scope_id"] != epoch["scope_id"]
                    or snapshot["leader_generation"] != ticket["leader_generation"]
                    or snapshot["ingest_ticket_id"] != ticket["id"]
                    or snapshot["collection_started_at"] != collection_started_at
                    or snapshot["controller_epoch"] != controller_epoch
                    or snapshot["sequence"] != sequence
                ):
                    raise InventoryConflictError(
                        "snapshot id was replayed with different immutable metadata"
                    )
                if bound is None:
                    await conn.execute(
                        "UPDATE resource_inventory_ingest_tickets "
                        "SET bound_snapshot_id=$2, bound_at=statement_timestamp() "
                        "WHERE id=$1",
                        ticket["id"],
                        snapshot_id,
                    )
                return SnapshotHandle(
                    snapshot_id=snapshot_id,
                    scope_epoch_id=ticket["scope_epoch_id"],
                    inventory_scope_id=epoch["scope_id"],
                    leader_generation=int(ticket["leader_generation"]),
                    manifest_state=str(snapshot["manifest_state"]),
                    replayed=not bool(inserted),
                )

    def _bounded_payload(
        self,
        values: Sequence[Any],
        *,
        label: str,
    ) -> str:
        if len(values) > self.max_batch_items:
            raise InventoryContractError(
                f"{label} batch exceeds {self.max_batch_items} rows"
            )
        payload = _canonical_json(values)
        if len(payload.encode("utf-8")) > self.max_batch_bytes:
            raise InventoryContractError(
                f"{label} batch exceeds {self.max_batch_bytes} bytes"
            )
        return payload

    async def stage_items(
        self,
        token: str,
        ticket_id: UUID,
        snapshot_id: UUID,
        items: Sequence[InventoryItem],
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
    ) -> StageResult:
        self._assert_items_in_scope(items, scope)
        keys = [(item.source_kind, item.source_uid) for item in items]
        if len(keys) != len(set(keys)):
            raise InventoryContractError("item batch contains duplicate kind/UID")
        payload = self._bounded_payload(
            [item.payload() for item in items], label="inventory item"
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ticket, epoch = await self._lock_ticket(conn, token, ticket_id, scope)
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=ticket["scope_epoch_id"],
                    leader_generation=int(ticket["leader_generation"]),
                    request_kind="snapshot-items",
                    collector_id=str(epoch["collector_id"]),
                )
                snapshot = await conn.fetchrow(_SNAPSHOT_SQL, snapshot_id)
                self._check_bound_staging(ticket, snapshot, snapshot_id)
                conflicts = await conn.fetchval(
                    _ITEM_CONFLICT_SQL, snapshot_id, payload
                )
                if conflicts:
                    raise InventoryConflictError(
                        "inventory item key replayed with different content"
                    )
                inserted_row = await conn.fetchrow(
                    _ITEM_INSERT_SQL, snapshot_id, payload
                )
                inserted = int(inserted_row["inserted_count"])
                inserted_bytes = int(inserted_row["inserted_bytes"])
                total = int(
                    await conn.fetchval(
                        "SELECT count(*) FROM resource_inventory_snapshot_items "
                        "WHERE snapshot_id=$1",
                        snapshot_id,
                    )
                )
                if total > int(ticket["max_snapshot_items"]):
                    raise InventoryContractError(
                        "snapshot exceeds its ticket item bound"
                    )
                if inserted_bytes:
                    staged_bytes = await conn.fetchval(
                        "UPDATE resource_inventory_ingest_tickets SET "
                        "staged_bytes=staged_bytes+$2 WHERE id=$1 "
                        "AND staged_bytes+$2 <= max_snapshot_bytes "
                        "RETURNING staged_bytes",
                        ticket["id"],
                        inserted_bytes,
                    )
                    if staged_bytes is None:
                        raise InventoryContractError(
                            "snapshot exceeds its cumulative byte bound"
                        )
                return StageResult(inserted=inserted, total=total)

    async def stage_shadow_comparisons(
        self,
        token: str,
        ticket_id: UUID,
        snapshot_id: UUID,
        comparisons: Sequence[ShadowComparison],
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
    ) -> StageResult:
        keys = [comparison.source_uid for comparison in comparisons]
        if len(keys) != len(set(keys)):
            raise InventoryContractError(
                "comparison batch contains duplicate source_uid"
            )
        payload = self._bounded_payload(
            [comparison.payload() for comparison in comparisons],
            label="shadow comparison",
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ticket, epoch = await self._lock_ticket(conn, token, ticket_id, scope)
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=ticket["scope_epoch_id"],
                    leader_generation=int(ticket["leader_generation"]),
                    request_kind="snapshot-shadow",
                    collector_id=str(epoch["collector_id"]),
                )
                snapshot = await conn.fetchrow(_SNAPSHOT_SQL, snapshot_id)
                self._check_bound_staging(ticket, snapshot, snapshot_id)
                conflicts = await conn.fetchval(
                    _COMPARISON_CONFLICT_SQL, snapshot_id, payload
                )
                if conflicts:
                    raise InventoryConflictError(
                        "shadow comparison replayed with different content"
                    )
                inserted = int(
                    await conn.fetchval(
                        _COMPARISON_INSERT_SQL,
                        snapshot_id,
                        epoch["scope_id"],
                        payload,
                    )
                )
                total = int(
                    await conn.fetchval(
                        "SELECT count(*) "
                        "FROM resource_inventory_shadow_comparisons "
                        "WHERE snapshot_id=$1",
                        snapshot_id,
                    )
                )
                if total > self.max_snapshot_items:
                    raise InventoryContractError(
                        "snapshot comparison total exceeds configured bound"
                    )
                return StageResult(inserted=inserted, total=total)

    @staticmethod
    def _check_bound_staging(
        ticket: asyncpg.Record,
        snapshot: asyncpg.Record | None,
        snapshot_id: UUID,
    ) -> None:
        if snapshot is None or ticket["bound_snapshot_id"] != snapshot_id:
            raise InventoryTicketError("ticket is not bound to this snapshot")
        if snapshot["ingest_ticket_id"] != ticket["id"]:
            raise InventoryTicketError("snapshot carries a different ingest ticket")
        if snapshot["manifest_state"] != "staging":
            raise InventoryConflictError("snapshot manifest is already sealed")

    async def _manifest_from_database(
        self,
        conn: asyncpg.Connection,
        snapshot_id: UUID,
    ) -> tuple[int, str, list[dict[str, Any]]]:
        digest = hashlib.sha256(_MANIFEST_PREFIX)
        count = 0
        item_errors: list[dict[str, Any]] = []
        omitted_error_count = 0
        cursor = conn.cursor(
            "SELECT source_kind, source_uid, revision_hash, "
            "valid_for_metering, item_error "
            "FROM resource_inventory_snapshot_items "
            "WHERE snapshot_id=$1 ORDER BY source_kind, source_uid",
            snapshot_id,
            prefetch=self.digest_prefetch,
        )
        async for row in cursor:
            count += 1
            valid = bool(row["valid_for_metering"])
            revision = str(row["revision_hash"]) if valid else "invalid"
            _hash_manifest_tuple(
                digest,
                str(row["source_kind"]),
                str(row["source_uid"]),
                revision,
            )
            if not valid:
                error = _json_object(row["item_error"])
                if len(item_errors) < self.max_error_items - 1:
                    item_errors.append(
                        {
                            "source_kind": str(row["source_kind"]),
                            "source_uid": str(row["source_uid"]),
                            "code": str(error.get("code", "invalid-item")),
                        }
                    )
                else:
                    omitted_error_count += 1
        if omitted_error_count:
            item_errors.append(
                {
                    "code": "diagnostics-truncated",
                    "omitted_count": omitted_error_count,
                }
            )
        return count, digest.hexdigest(), item_errors

    @staticmethod
    def _assert_finalization_replay(
        snapshot: asyncpg.Record,
        final: SnapshotFinalization,
        computed_digest: str,
    ) -> None:
        if (
            bool(snapshot["complete"]) != final.complete
            or snapshot["collection_completed_at"] != final.collection_completed_at
            or snapshot["source_snapshot_at"] != final.source_snapshot_at
            or snapshot["resource_version"] != final.resource_version
            or snapshot["controller_epoch"] != final.controller_epoch
            or snapshot["sequence"] != final.sequence
            or int(snapshot["item_count"]) != final.item_count
            or str(snapshot["item_digest"]) != computed_digest
        ):
            raise InventoryConflictError(
                "sealed snapshot was replayed with different final metadata"
            )

    @staticmethod
    def _validate_complete_order(
        snapshot: asyncpg.Record,
        epoch: asyncpg.Record,
        final: SnapshotFinalization,
        received_at: datetime,
    ) -> None:
        if not final.complete:
            return
        if (
            epoch["last_complete_at"] is not None
            and snapshot["collection_started_at"] < epoch["last_complete_at"]
        ):
            raise InventoryFenceError(
                "complete snapshot began before the last committed proof"
            )
        if (
            epoch["last_attempt_at"] is not None
            and received_at <= epoch["last_attempt_at"]
        ):
            raise InventoryFenceError("complete snapshot receipt is stale")
        if epoch["controller_epoch"] is not None:
            if final.controller_epoch != epoch["controller_epoch"]:
                raise InventoryFenceError(
                    "controller epoch changed; open a new scope epoch"
                )
            if final.sequence is None or final.sequence <= epoch["last_sequence"]:
                raise InventoryFenceError("controller sequence is stale")

    async def _apply_snapshot_interval_mutations(
        self,
        conn: asyncpg.Connection,
        snapshot_id: UUID,
        epoch: asyncpg.Record,
        received_at: datetime,
        interval_mutator: SnapshotIntervalMutator | None,
    ) -> None:
        """Resolve every valid LIST item before sealing or cursor commit.

        The keyset scan remains bounded and permits the hook to issue SQL on the
        same connection. Revalidating unchanged source revisions is intentional:
        attribution can change in the app database without changing the
        Kubernetes object hash. ``None`` is an explicit not-applicable result
        and is accepted only when no open metered interval remains for the UID.
        """
        if interval_mutator is None:
            return
        last_kind: str | None = None
        last_uid: str | None = None
        while True:
            rows = await conn.fetch(
                "SELECT item.source_kind, item.source_uid, item.revision_hash, "
                "item.normalized_item, item.valid_for_metering, item.item_error, "
                "interval.id AS existing_interval_id, "
                "interval.source_revision AS existing_source_revision "
                "FROM resource_inventory_snapshot_items item "
                "LEFT JOIN resource_intervals interval "
                "ON interval.inventory_scope_id=$2 "
                "AND interval.source_kind=item.source_kind "
                "AND interval.source_uid=item.source_uid "
                "AND interval.ended_at IS NULL "
                "WHERE item.snapshot_id=$1 AND item.valid_for_metering "
                "AND ($3::text IS NULL OR (item.source_kind, item.source_uid) "
                "> ($3::text, $4::text)) "
                "ORDER BY item.source_kind, item.source_uid LIMIT $5",
                snapshot_id,
                epoch["scope_id"],
                last_kind,
                last_uid,
                self.max_batch_items,
            )
            if not rows:
                return
            for row in rows:
                item = InventoryItem(
                    source_kind=str(row["source_kind"]),
                    source_uid=str(row["source_uid"]),
                    revision_hash=str(row["revision_hash"]),
                    normalized_item=_json_object(row["normalized_item"]),
                    valid_for_metering=True,
                )
                context = SnapshotIntervalMutationContext(
                    snapshot_id=snapshot_id,
                    scope_epoch_id=epoch["id"],
                    inventory_scope_id=epoch["scope_id"],
                    source_cluster=str(epoch["source_cluster"]),
                    namespace=epoch["namespace"],
                    received_at=received_at,
                    existing_interval_id=row["existing_interval_id"],
                    existing_source_revision=(
                        None
                        if row["existing_source_revision"] is None
                        else str(row["existing_source_revision"])
                    ),
                )
                interval_id = await interval_mutator(conn, context, item)
                if interval_id is None:
                    still_open = await conn.fetchval(
                        "SELECT TRUE FROM resource_intervals "
                        "WHERE inventory_scope_id=$1 AND source_kind=$2 "
                        "AND source_uid=$3 AND ended_at IS NULL",
                        epoch["scope_id"],
                        item.source_kind,
                        item.source_uid,
                    )
                    if still_open:
                        raise InventoryConflictError(
                            "not-applicable snapshot item retained an open interval"
                        )
                else:
                    if not isinstance(interval_id, UUID):
                        raise InventoryContractError(
                            "snapshot interval mutator must return UUID or None"
                        )
                    postcondition = await conn.fetchval(
                        "SELECT TRUE FROM resource_intervals "
                        "WHERE id=$1 AND inventory_scope_id=$2 "
                        "AND source_kind=$3 AND source_uid=$4 "
                        "AND source_revision=$5 AND ended_at IS NULL "
                        "AND last_seen_at >= $6 AND last_confirmed_at >= $6",
                        interval_id,
                        epoch["scope_id"],
                        item.source_kind,
                        item.source_uid,
                        item.revision_hash,
                        received_at,
                    )
                    if not postcondition:
                        raise InventoryConflictError(
                            "snapshot mutator did not establish interval postcondition"
                        )
                last_kind = item.source_kind
                last_uid = item.source_uid

    async def _apply_snapshot_observations(
        self,
        conn: asyncpg.Connection,
        snapshot_id: UUID,
        epoch: asyncpg.Record,
        received_at: datetime,
        observation_hook: SnapshotObservationHook | None,
        *,
        require_shadow_comparison: bool,
    ) -> None:
        """Run optional object-by-object shadow diagnostics for every staged UID."""
        if observation_hook is None:
            return
        last_kind: str | None = None
        last_uid: str | None = None
        while True:
            rows = await conn.fetch(
                "SELECT item.source_kind, item.source_uid, item.revision_hash, "
                "item.normalized_item, item.valid_for_metering, item.item_error, "
                "interval.id AS current_interval_id, "
                "interval.source_revision AS current_source_revision "
                "FROM resource_inventory_snapshot_items item "
                "LEFT JOIN resource_intervals interval "
                "ON interval.inventory_scope_id=$2 "
                "AND interval.source_kind=item.source_kind "
                "AND interval.source_uid=item.source_uid "
                "AND interval.ended_at IS NULL "
                "WHERE item.snapshot_id=$1 "
                "AND ($3::text IS NULL OR (item.source_kind, item.source_uid) "
                "> ($3::text, $4::text)) "
                "ORDER BY item.source_kind, item.source_uid LIMIT $5",
                snapshot_id,
                epoch["scope_id"],
                last_kind,
                last_uid,
                self.max_batch_items,
            )
            if not rows:
                return
            for row in rows:
                valid = bool(row["valid_for_metering"])
                item_error = None
                if not valid:
                    error_data = _json_object(row["item_error"])
                    item_error = SanitizedInventoryError(
                        code=str(error_data["code"]),
                        retryable=bool(error_data.get("retryable", False)),
                        component=(
                            None
                            if error_data.get("component") is None
                            else str(error_data["component"])
                        ),
                    )
                item = InventoryItem(
                    source_kind=str(row["source_kind"]),
                    source_uid=str(row["source_uid"]),
                    revision_hash=(
                        None
                        if row["revision_hash"] is None
                        else str(row["revision_hash"])
                    ),
                    normalized_item=_json_object(row["normalized_item"]),
                    valid_for_metering=valid,
                    item_error=item_error,
                )
                context = SnapshotObservationContext(
                    snapshot_id=snapshot_id,
                    scope_epoch_id=epoch["id"],
                    inventory_scope_id=epoch["scope_id"],
                    source_cluster=str(epoch["source_cluster"]),
                    namespace=epoch["namespace"],
                    received_at=received_at,
                    current_interval_id=row["current_interval_id"],
                    current_source_revision=(
                        None
                        if row["current_source_revision"] is None
                        else str(row["current_source_revision"])
                    ),
                )
                await observation_hook(conn, context, item)
                if require_shadow_comparison:
                    comparison_exists = await conn.fetchval(
                        "SELECT TRUE FROM resource_inventory_shadow_comparisons "
                        "WHERE snapshot_id=$1 AND source_uid=$2",
                        snapshot_id,
                        item.source_uid,
                    )
                    if not comparison_exists:
                        raise InventoryConflictError(
                            "snapshot observation hook omitted its comparison row"
                        )
                last_kind = item.source_kind
                last_uid = item.source_uid

    async def _apply_snapshot_absence_mutations(
        self,
        conn: asyncpg.Connection,
        snapshot_id: UUID,
        epoch: asyncpg.Record,
        received_at: datetime,
        absence_mutator: SnapshotAbsenceMutator | None,
    ) -> int:
        """Let a resource-specific policy consume complete-LIST absences.

        The generic absence UPDATE remains the default authority. A consumed
        row must be durably closed and relinquish its lifecycle head inside the
        hook transaction; this prevents a buggy storage policy from silently
        converting an absent object into an immortal open interval.
        """

        if absence_mutator is None:
            return 0
        consumed = 0
        last_id: UUID | None = None
        while True:
            rows = await conn.fetch(
                "SELECT interval.* FROM resource_intervals AS interval "
                "WHERE interval.inventory_scope_id=$1 "
                "AND interval.ended_at IS NULL "
                "AND $3 >= interval.last_confirmed_at "
                "AND ($4::uuid IS NULL OR interval.id > $4) "
                "AND NOT EXISTS (SELECT 1 "
                "FROM resource_inventory_snapshot_items AS item "
                "WHERE item.snapshot_id=$2 "
                "AND item.source_kind=interval.source_kind "
                "AND item.source_uid=interval.source_uid) "
                "ORDER BY interval.id LIMIT $5 FOR UPDATE",
                epoch["scope_id"],
                snapshot_id,
                received_at,
                last_id,
                self.max_batch_items,
            )
            if not rows:
                return consumed
            for row in rows:
                context = SnapshotAbsenceMutationContext(
                    snapshot_id=snapshot_id,
                    scope_epoch_id=epoch["id"],
                    inventory_scope_id=epoch["scope_id"],
                    source_cluster=str(epoch["source_cluster"]),
                    namespace=epoch["namespace"],
                    received_at=received_at,
                )
                if await absence_mutator(conn, context, row):
                    postcondition = await conn.fetchval(
                        "SELECT TRUE FROM resource_intervals AS interval "
                        "WHERE interval.id=$1 AND interval.ended_at IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 "
                        "FROM resource_lifecycle_heads AS head "
                        "WHERE head.source_lifecycle_id="
                        "interval.source_lifecycle_id "
                        "AND head.current_interval_id=interval.id)",
                        row["id"],
                    )
                    if not postcondition:
                        raise InventoryConflictError(
                            "absence mutator did not close interval and clear head"
                        )
                    consumed += 1
                last_id = row["id"]

    async def finalize_snapshot(
        self,
        token: str,
        ticket_id: UUID,
        snapshot_id: UUID,
        final: SnapshotFinalization,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        interval_mutator: SnapshotIntervalMutator | None = None,
        observation_hook: SnapshotObservationHook | None = None,
        completion_hook: SnapshotCompletionHook | None = None,
        absence_mutator: SnapshotAbsenceMutator | None = None,
        require_shadow_comparison: bool = True,
        reconcile_intervals: bool = True,
    ) -> ReconciliationResult:
        if final.item_count > self.max_snapshot_items:
            raise InventoryContractError("declared snapshot item count exceeds bound")
        fatal_errors_json = _canonical_json(
            [error.as_dict() for error in final.fatal_errors]
        )
        if not reconcile_intervals and (
            interval_mutator is not None or absence_mutator is not None
        ):
            raise InventoryContractError(
                "inventory-only finalization cannot mutate resource intervals"
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ticket, epoch = await self._lock_ticket(
                    conn, token, ticket_id, scope, allow_consumed=True
                )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=ticket["scope_epoch_id"],
                    leader_generation=int(ticket["leader_generation"]),
                    request_kind="snapshot-finalize",
                    collector_id=str(epoch["collector_id"]),
                )
                snapshot = await conn.fetchrow(_SNAPSHOT_SQL, snapshot_id)
                if snapshot is None or ticket["bound_snapshot_id"] != snapshot_id:
                    raise InventoryTicketError("ticket is not bound to this snapshot")
                if snapshot["ingest_ticket_id"] != ticket["id"]:
                    raise InventoryTicketError(
                        "snapshot carries a different ingest ticket"
                    )

                (
                    count,
                    computed_digest,
                    item_errors,
                ) = await self._manifest_from_database(conn, snapshot_id)
                if count != final.item_count:
                    raise InventoryConflictError(
                        f"snapshot declares {final.item_count} items but staged {count}"
                    )
                if (
                    final.item_digest is not None
                    and final.item_digest != computed_digest
                ):
                    raise InventoryConflictError("snapshot manifest digest mismatch")

                if snapshot["manifest_state"] in {"sealed", "items-expired"}:
                    if ticket["consumed_at"] is None:
                        raise InventoryConflictError(
                            "sealed snapshot has an unconsumed ingest ticket"
                        )
                    self._assert_finalization_replay(snapshot, final, computed_digest)
                    summary = _json_object(snapshot["reconciliation_summary"])
                    if bool(summary.get("reconcile_intervals", True)) is not bool(
                        reconcile_intervals
                    ):
                        raise InventoryConflictError(
                            "sealed snapshot reconciliation mode changed on replay"
                        )
                    return ReconciliationResult.from_summary(
                        snapshot_id,
                        final.complete,
                        summary,
                        replayed=True,
                    )
                self._check_bound_staging(ticket, snapshot, snapshot_id)
                received_at = await conn.fetchval("SELECT statement_timestamp()")
                if (
                    final.collection_completed_at
                    > received_at + self.max_collector_clock_skew
                ):
                    raise InventoryContractError(
                        "collector completion exceeds the permitted clock skew"
                    )
                self._validate_complete_order(snapshot, epoch, final, received_at)
                if final.collection_completed_at < snapshot["collection_started_at"]:
                    raise InventoryContractError(
                        "collection completion precedes collection start"
                    )

                if reconcile_intervals:
                    # Freeze the open set used by hooks/count/absence closure.
                    await conn.execute(
                        "SELECT id FROM resource_intervals "
                        "WHERE inventory_scope_id=$1 AND ended_at IS NULL "
                        "ORDER BY id FOR UPDATE",
                        epoch["scope_id"],
                    )
                    if final.complete:
                        await self._apply_snapshot_interval_mutations(
                            conn,
                            snapshot_id,
                            epoch,
                            received_at,
                            interval_mutator,
                        )
                await self._apply_snapshot_observations(
                    conn,
                    snapshot_id,
                    epoch,
                    received_at,
                    observation_hook,
                    require_shadow_comparison=require_shadow_comparison,
                )
                if final.complete and completion_hook is not None:
                    await completion_hook(
                        conn,
                        SnapshotCompletionContext(
                            snapshot_id=snapshot_id,
                            scope_epoch_id=epoch["id"],
                            inventory_scope_id=epoch["scope_id"],
                            source_cluster=str(epoch["source_cluster"]),
                            namespace=epoch["namespace"],
                            received_at=received_at,
                        ),
                    )
                if reconcile_intervals:
                    counts = await conn.fetchrow(
                        _RECONCILIATION_COUNTS_SQL,
                        snapshot_id,
                        epoch["scope_id"],
                        final.complete,
                        received_at,
                    )
                else:
                    counts = await conn.fetchrow(
                        _INVENTORY_ONLY_COUNTS_SQL, snapshot_id
                    )
                summary = {key: int(counts[key]) for key in counts.keys()}
                summary["reconcile_intervals"] = reconcile_intervals

                await conn.execute(
                    "UPDATE resource_inventory_snapshots SET "
                    "collection_completed_at=$2, received_at=$3, "
                    "source_snapshot_at=$4, complete=$5, resource_version=$6, "
                    "controller_epoch=$7, sequence=$8, item_count=$9, "
                    "item_digest=$10, fatal_errors=$11::jsonb, "
                    "item_errors=$12::jsonb, reconciliation_summary=$13::jsonb, "
                    "manifest_state='sealed', sealed_at=statement_timestamp() "
                    "WHERE id=$1",
                    snapshot_id,
                    final.collection_completed_at,
                    received_at,
                    final.source_snapshot_at,
                    final.complete,
                    final.resource_version,
                    final.controller_epoch,
                    final.sequence,
                    count,
                    computed_digest,
                    fatal_errors_json,
                    _canonical_json(item_errors),
                    _canonical_json(summary),
                )
                if reconcile_intervals:
                    if final.complete:
                        await conn.fetchval(
                            _LINK_PRESENT_CLOSURES_SQL,
                            snapshot_id,
                            epoch["scope_id"],
                            received_at,
                        )
                    observed = await conn.fetchrow(
                        _OBSERVE_PRESENT_SQL,
                        snapshot_id,
                        epoch["scope_id"],
                        final.complete,
                        received_at,
                    )
                    custom_closed = await self._apply_snapshot_absence_mutations(
                        conn,
                        snapshot_id,
                        epoch,
                        received_at,
                        absence_mutator,
                    )
                    closed_row = await conn.fetchrow(
                        _CLOSE_ABSENT_SQL,
                        snapshot_id,
                        epoch["scope_id"],
                        received_at,
                        final.complete,
                    )
                    generic_closed = int(closed_row["closed_count"])
                    closed = custom_closed + generic_closed
                    if int(closed_row["cleared_head_count"]) != generic_closed:
                        raise InventoryConflictError(
                            "closed interval did not own its lifecycle head"
                        )
                    if (
                        int(observed["observed_intervals"])
                        != summary["observed_intervals"]
                        or int(observed["confirmed_intervals"])
                        != summary["confirmed_intervals"]
                        or closed != summary["closed_intervals"]
                    ):
                        raise InventoryConflictError(
                            "interval set changed during snapshot reconciliation"
                        )

                item_health = (
                    "partial"
                    if summary["invalid_items"] or summary["pending_valid_items"]
                    else "healthy"
                )
                continuity_gap_code = next(
                    (
                        error.code
                        for error in final.fatal_errors
                        if error.code
                        in {
                            "resource-version-expired",
                            "resource-version-mismatch",
                        }
                    ),
                    None,
                )
                coverage_gap_id: UUID | None = None
                if continuity_gap_code is not None:
                    gap_start = next(
                        (
                            value
                            for value in (
                                epoch["complete_through"],
                                epoch["last_complete_at"],
                                epoch["continuous_since"],
                            )
                            if value is not None
                        ),
                        received_at,
                    )
                    if gap_start > received_at:
                        gap_start = received_at
                    coverage_gap_id = uuid4()
                    await conn.execute(
                        "INSERT INTO resource_inventory_coverage_gaps ("
                        "id, scope_epoch_id, gap_start, reason, "
                        "resolution_details) VALUES ("
                        "$1, $2, $3, $4, $5::jsonb)",
                        coverage_gap_id,
                        ticket["scope_epoch_id"],
                        gap_start,
                        f"list-{continuity_gap_code}",
                        _canonical_json(
                            {
                                "code": continuity_gap_code,
                                "snapshot_id": str(snapshot_id),
                            }
                        ),
                    )
                if final.complete:
                    await conn.execute(
                        "UPDATE resource_inventory_scope_epochs SET "
                        "last_attempt_at=$2, last_complete_at=$2, "
                        "last_complete_snapshot_id=$1, last_resource_version=$3, "
                        "controller_epoch=$4, last_sequence=$5, "
                        "leader_generation=$6, "
                        "reliable_from=COALESCE(reliable_from, $2), "
                        "required_for_rollup=(required_for_rollup "
                        "OR require_after_recovery), "
                        "required_from=CASE WHEN required_from IS NOT NULL "
                        "THEN required_from WHEN require_after_recovery THEN "
                        "date_trunc('day', $2, 'UTC') + INTERVAL '1 day' "
                        "ELSE NULL END, "
                        "continuous_since=COALESCE(continuous_since, $2), "
                        "complete_through=GREATEST(COALESCE(complete_through, $2), $2), "
                        "snapshot_health='healthy', "
                        "continuity_health=CASE WHEN EXISTS ("
                        "SELECT 1 FROM resource_inventory_coverage_gaps gap "
                        "WHERE gap.scope_epoch_id=$9 "
                        "AND gap.resolution='unresolved' "
                        "AND gap.reason NOT LIKE "
                        "'compute-authority-awaiting-confirmation:%') "
                        "THEN 'gap' "
                        "ELSE 'healthy' END, item_health=$7, "
                        "backend_health='healthy', consecutive_failures=0, "
                        "last_item_count=$8, sanitized_error=NULL, "
                        "updated_at=statement_timestamp() WHERE id=$9",
                        snapshot_id,
                        received_at,
                        final.resource_version,
                        final.controller_epoch,
                        final.sequence,
                        ticket["leader_generation"],
                        item_health,
                        count,
                        ticket["scope_epoch_id"],
                    )
                    if epoch["recovery_from_epoch_id"] is not None:
                        await conn.execute(
                            "UPDATE resource_inventory_coverage_gaps SET "
                            "gap_end=$2, updated_at=statement_timestamp() "
                            "WHERE scope_epoch_id=$1 "
                            "AND reason=ANY($3::text[]) "
                            "AND resolution='unresolved' AND gap_end IS NULL "
                            "AND gap_start < $2",
                            epoch["recovery_from_epoch_id"],
                            received_at,
                            sorted(_RECOVERABLE_COVERAGE_GAP_REASONS),
                        )
                else:
                    error_summary = {
                        "codes": [error.code for error in final.fatal_errors],
                        "snapshot_id": str(snapshot_id),
                    }
                    await conn.execute(
                        "UPDATE resource_inventory_scope_epochs SET "
                        "last_attempt_at=GREATEST(COALESCE(last_attempt_at, $1), $1), "
                        "leader_generation=$2, snapshot_health='incomplete', "
                        "continuity_health=CASE WHEN $7::uuid IS NULL "
                        "THEN continuity_health ELSE 'gap' END, "
                        "item_health=$3, backend_health=$4, "
                        "consecutive_failures=consecutive_failures + 1, "
                        "last_item_count=$5, sanitized_error=$6::jsonb, "
                        "updated_at=statement_timestamp() WHERE id=$8",
                        received_at,
                        ticket["leader_generation"],
                        item_health,
                        "degraded" if final.fatal_errors else "healthy",
                        count,
                        _canonical_json(error_summary),
                        coverage_gap_id,
                        ticket["scope_epoch_id"],
                    )

                # Last mutation before commit: the same transaction that made
                # the sealed snapshot/interval changes visible consumes nonce.
                await conn.execute(
                    "UPDATE resource_inventory_ingest_tickets "
                    "SET consumed_at=statement_timestamp() "
                    "WHERE id=$1 AND bound_snapshot_id=$2 AND consumed_at IS NULL",
                    ticket["id"],
                    snapshot_id,
                )
                return ReconciliationResult.from_summary(
                    snapshot_id,
                    final.complete,
                    summary,
                    replayed=False,
                )

    @staticmethod
    def _watch_result(
        session: asyncpg.Record,
        event: asyncpg.Record,
        *,
        replayed: bool,
    ) -> WatchCommitResult:
        resource_version = (
            event["resource_version"] or event["expected_resource_version"]
        )
        return WatchCommitResult(
            watch_session_id=session["id"],
            event_id=event["id"],
            event_type=str(event["event_type"]),
            resource_version=str(resource_version),
            mutation_action=WatchMutationAction(str(event["mutation_action"])),
            received_at=event["received_at"],
            affected_interval_id=event["affected_interval_id"],
            coverage_gap_id=event["coverage_gap_id"],
            session_consumed=session["consumed_at"] is not None,
            replayed=replayed,
        )

    @staticmethod
    def _assert_watch_event_replay(
        stored: asyncpg.Record,
        *,
        request_digest: str,
        expected_resource_version: str,
        event: WatchObjectEvent,
    ) -> None:
        source_kind, source_uid = event.identity
        item = event.item
        expected_normalized = None if item is None else dict(item.normalized_item)
        expected_error = (
            None
            if item is None or item.item_error is None
            else item.item_error.as_dict()
        )
        stored_normalized = (
            None
            if stored["normalized_item"] is None
            else _json_object(stored["normalized_item"])
        )
        stored_error = (
            None if stored["item_error"] is None else _json_object(stored["item_error"])
        )
        action = WatchMutationAction(str(stored["mutation_action"]))
        if event.event_type is WatchEventKind.BOOKMARK:
            allowed_actions = {WatchMutationAction.BOOKMARK}
        elif event.event_type is WatchEventKind.DELETED or event.terminal:
            allowed_actions = {
                WatchMutationAction.CLOSE,
                WatchMutationAction.ALREADY_ABSENT,
            }
        elif item is not None and item.valid_for_metering:
            allowed_actions = {
                WatchMutationAction.CONFIRM,
                WatchMutationAction.OPEN,
                WatchMutationAction.REVISE,
                WatchMutationAction.NOT_APPLICABLE,
            }
        else:
            allowed_actions = {WatchMutationAction.PRESENCE_INVALID}
        if (
            stored["request_digest"] != request_digest
            or stored["expected_resource_version"] != expected_resource_version
            or stored["event_type"] != str(event.event_type)
            or stored["resource_version"] != event.resource_version
            or stored["source_kind"] != source_kind
            or stored["source_uid"] != source_uid
            or stored["revision_hash"] != (None if item is None else item.revision_hash)
            or stored_normalized != expected_normalized
            or stored["valid_for_metering"]
            != (None if item is None else item.valid_for_metering)
            or stored_error != expected_error
            or int(stored["event_bytes"]) != event.event_bytes
            or stored["collector_observed_at"] != event.collector_observed_at
            or action not in allowed_actions
        ):
            raise InventoryConflictError(
                "watch event id was replayed with different immutable content"
            )

    @staticmethod
    async def _close_watch_interval(
        conn: asyncpg.Connection,
        interval: asyncpg.Record | None,
        received_at: datetime,
        *,
        terminal: bool,
    ) -> tuple[WatchMutationAction, UUID | None]:
        if interval is None:
            return WatchMutationAction.ALREADY_ABSENT, None
        if received_at < interval["last_confirmed_at"]:
            raise InventoryFenceError("watch receipt precedes interval proof")
        closed = await conn.fetchrow(
            "UPDATE resource_intervals SET ended_at=$2, "
            "end_time_source=$3, "
            "end_uncertainty_us=floor(extract(epoch FROM "
            "($2-last_confirmed_at))*1000000)::bigint, "
            "end_reason=$4, updated_at=statement_timestamp() "
            "WHERE id=$1 AND ended_at IS NULL RETURNING id, source_lifecycle_id",
            interval["id"],
            received_at,
            "watch-terminal" if terminal else "watch-deleted",
            "terminal-object-event" if terminal else "watch-deleted",
        )
        if closed is None:
            raise InventoryConflictError("watch interval closure lost its lock")
        cleared = await conn.fetchval(
            "UPDATE resource_lifecycle_heads SET current_interval_id=NULL, "
            "updated_at=statement_timestamp() "
            "WHERE source_lifecycle_id=$1 AND current_interval_id=$2 "
            "RETURNING TRUE",
            closed["source_lifecycle_id"],
            closed["id"],
        )
        if not cleared:
            raise InventoryConflictError(
                "watch interval did not own its lifecycle head"
            )
        return WatchMutationAction.CLOSE, closed["id"]

    async def apply_watch_event(
        self,
        token: str,
        watch_session_id: UUID,
        event_id: UUID,
        request_digest: str,
        expected_resource_version: str,
        event: WatchObjectEvent,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        interval_mutator: WatchIntervalMutator | None = None,
        deletion_mutator: WatchDeletionMutator | None = None,
        terminal_mutator: WatchTerminalMutator | None = None,
        reconcile_intervals: bool = True,
    ) -> WatchCommitResult:
        """Atomically apply one object/BOOKMARK and CAS the opaque cursor."""
        if not _HASH_RE.fullmatch(request_digest):
            raise InventoryContractError("watch request_digest must be SHA-256")
        if (
            not expected_resource_version
            or expected_resource_version == "0"
            or len(expected_resource_version) > 1024
        ):
            raise InventoryContractError("expected watch cursor is invalid")
        if event.event_bytes > self.max_watch_event_bytes:
            raise InventoryContractError("watch event exceeds configured byte bound")
        if event.item is not None:
            self._assert_items_in_scope((event.item,), scope)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                session, epoch = await self._lock_watch_session(
                    conn,
                    token,
                    watch_session_id,
                    scope,
                    allow_consumed=True,
                )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=session["scope_epoch_id"],
                    leader_generation=int(session["leader_generation"]),
                    request_kind="watch-event",
                    collector_id=str(epoch["collector_id"]),
                )
                stored = await conn.fetchrow(
                    _WATCH_EVENT_SQL, watch_session_id, event_id
                )
                if stored is not None:
                    self._assert_watch_event_replay(
                        stored,
                        request_digest=request_digest,
                        expected_resource_version=expected_resource_version,
                        event=event,
                    )
                    if not reconcile_intervals and (
                        stored["affected_interval_id"] is not None
                        or WatchMutationAction(str(stored["mutation_action"]))
                        in {
                            WatchMutationAction.OPEN,
                            WatchMutationAction.CONFIRM,
                            WatchMutationAction.REVISE,
                            WatchMutationAction.CLOSE,
                        }
                    ):
                        raise InventoryConflictError(
                            "inventory-only watch replay contains an interval mutation"
                        )
                    return self._watch_result(session, stored, replayed=True)
                if session["consumed_at"] is not None:
                    raise InventoryTicketError("inventory watch session is consumed")
                if (
                    session["last_resource_version"] != expected_resource_version
                    or epoch["last_resource_version"] != expected_resource_version
                ):
                    raise InventoryConflictError(
                        "watch cursor compare-and-swap precondition failed"
                    )
                if int(session["committed_events"]) >= int(
                    session["max_events"]
                ) or event.event_bytes > int(session["max_bytes"]) - int(
                    session["committed_bytes"]
                ):
                    raise InventoryContractError("watch session bound is exhausted")

                received_at = await conn.fetchval("SELECT statement_timestamp()")
                source_kind, source_uid = event.identity
                interval = None
                if source_kind is not None and source_uid is not None:
                    interval = await conn.fetchrow(
                        _OPEN_INTERVAL_SQL,
                        epoch["scope_id"],
                        source_kind,
                        source_uid,
                    )
                    if not reconcile_intervals and interval is not None:
                        raise InventoryConflictError(
                            "inventory-only watch scope has an open interval"
                        )

                affected_interval_id: UUID | None = None
                if event.event_type is WatchEventKind.BOOKMARK:
                    action = WatchMutationAction.BOOKMARK
                elif event.event_type is WatchEventKind.DELETED or event.terminal:
                    if (
                        event.event_type is WatchEventKind.DELETED
                        and deletion_mutator is not None
                    ):
                        deletion_context = WatchDeletionMutationContext(
                            scope_epoch_id=session["scope_epoch_id"],
                            inventory_scope_id=epoch["scope_id"],
                            source_cluster=str(epoch["source_cluster"]),
                            namespace=epoch["namespace"],
                            received_at=received_at,
                            source_kind=str(source_kind),
                            source_uid=str(source_uid),
                        )
                        deletion_result = await deletion_mutator(
                            conn,
                            deletion_context,
                            interval,
                        )
                        if deletion_result is None:
                            (
                                action,
                                affected_interval_id,
                            ) = await self._close_watch_interval(
                                conn,
                                interval,
                                received_at,
                                terminal=False,
                            )
                        else:
                            action, affected_interval_id = deletion_result
                            if action not in {
                                WatchMutationAction.CLOSE,
                                WatchMutationAction.ALREADY_ABSENT,
                            }:
                                raise InventoryContractError(
                                    "watch deletion mutator returned an invalid action"
                                )
                            if affected_interval_id is not None and not isinstance(
                                affected_interval_id, UUID
                            ):
                                raise InventoryContractError(
                                    "watch deletion mutator returned an invalid interval"
                                )
                    elif event.terminal and terminal_mutator is not None:
                        terminal_result = await terminal_mutator(
                            conn,
                            WatchTerminalMutationContext(
                                scope_epoch_id=session["scope_epoch_id"],
                                inventory_scope_id=epoch["scope_id"],
                                source_cluster=str(epoch["source_cluster"]),
                                namespace=epoch["namespace"],
                                received_at=received_at,
                                source_kind=str(source_kind),
                                source_uid=str(source_uid),
                            ),
                            interval,
                        )
                        if terminal_result is None:
                            (
                                action,
                                affected_interval_id,
                            ) = await self._close_watch_interval(
                                conn,
                                interval,
                                received_at,
                                terminal=True,
                            )
                        else:
                            action, affected_interval_id = terminal_result
                            if action not in {
                                WatchMutationAction.CLOSE,
                                WatchMutationAction.ALREADY_ABSENT,
                            }:
                                raise InventoryContractError(
                                    "watch terminal mutator returned an invalid action"
                                )
                            if affected_interval_id is not None and not isinstance(
                                affected_interval_id, UUID
                            ):
                                raise InventoryContractError(
                                    "watch terminal mutator returned an invalid interval"
                                )
                    else:
                        action, affected_interval_id = await self._close_watch_interval(
                            conn,
                            interval,
                            received_at,
                            terminal=event.terminal,
                        )
                else:
                    assert event.item is not None
                    if not event.item.valid_for_metering:
                        action = WatchMutationAction.PRESENCE_INVALID
                        if interval is not None:
                            affected_interval_id = await conn.fetchval(
                                "UPDATE resource_intervals SET "
                                "last_seen_at=GREATEST(last_seen_at, $2), "
                                "updated_at=statement_timestamp() "
                                "WHERE id=$1 RETURNING id",
                                interval["id"],
                                received_at,
                            )
                    elif interval_mutator is None and (
                        interval is not None
                        and interval["source_revision"] == event.item.revision_hash
                    ):
                        action = WatchMutationAction.CONFIRM
                        affected_interval_id = await conn.fetchval(
                            "UPDATE resource_intervals SET "
                            "last_seen_at=GREATEST(last_seen_at, $2), "
                            "last_confirmed_at=GREATEST(last_confirmed_at, $2), "
                            "updated_at=statement_timestamp() "
                            "WHERE id=$1 RETURNING id",
                            interval["id"],
                            received_at,
                        )
                    else:
                        if interval_mutator is None:
                            raise InventoryContractError(
                                "new/revised watch item requires interval mutator"
                            )
                        context = WatchIntervalMutationContext(
                            scope_epoch_id=session["scope_epoch_id"],
                            inventory_scope_id=epoch["scope_id"],
                            source_cluster=str(epoch["source_cluster"]),
                            namespace=epoch["namespace"],
                            event_type=event.event_type,
                            received_at=received_at,
                            existing_interval_id=(
                                None if interval is None else interval["id"]
                            ),
                            existing_source_revision=(
                                None
                                if interval is None
                                else str(interval["source_revision"])
                            ),
                        )
                        affected_interval_id = await interval_mutator(
                            conn, context, event.item
                        )
                        if not reconcile_intervals and affected_interval_id is not None:
                            raise InventoryConflictError(
                                "inventory-only watch mutator returned an interval"
                            )
                        if affected_interval_id is None:
                            still_open = await conn.fetchval(
                                "SELECT TRUE FROM resource_intervals "
                                "WHERE inventory_scope_id=$1 AND source_kind=$2 "
                                "AND source_uid=$3 AND ended_at IS NULL",
                                epoch["scope_id"],
                                event.item.source_kind,
                                event.item.source_uid,
                            )
                            if still_open:
                                raise InventoryConflictError(
                                    "not-applicable watch item retained an open interval"
                                )
                            if interval is not None:
                                affected_interval_id = interval["id"]
                            action = WatchMutationAction.NOT_APPLICABLE
                        else:
                            if not isinstance(affected_interval_id, UUID):
                                raise InventoryContractError(
                                    "interval mutator must return UUID or None"
                                )
                            postcondition = await conn.fetchrow(
                                "SELECT id FROM resource_intervals "
                                "WHERE id=$1 AND inventory_scope_id=$2 "
                                "AND source_kind=$3 AND source_uid=$4 "
                                "AND source_revision=$5 AND ended_at IS NULL "
                                "AND last_seen_at >= $6 "
                                "AND last_confirmed_at >= $6 FOR UPDATE",
                                affected_interval_id,
                                epoch["scope_id"],
                                event.item.source_kind,
                                event.item.source_uid,
                                event.item.revision_hash,
                                received_at,
                            )
                            if postcondition is None:
                                raise InventoryConflictError(
                                    "interval mutator did not establish watch postcondition"
                                )
                            if interval is None:
                                action = WatchMutationAction.OPEN
                            elif affected_interval_id == interval["id"]:
                                action = WatchMutationAction.CONFIRM
                            else:
                                action = WatchMutationAction.REVISE

                if not reconcile_intervals:
                    if affected_interval_id is not None or action in {
                        WatchMutationAction.OPEN,
                        WatchMutationAction.CONFIRM,
                        WatchMutationAction.REVISE,
                        WatchMutationAction.CLOSE,
                    }:
                        raise InventoryConflictError(
                            "inventory-only watch attempted an interval mutation"
                        )
                    if source_kind is not None and source_uid is not None:
                        leaked_interval = await conn.fetchval(
                            "SELECT TRUE FROM resource_intervals "
                            "WHERE inventory_scope_id=$1 AND source_kind=$2 "
                            "AND source_uid=$3 AND ended_at IS NULL",
                            epoch["scope_id"],
                            source_kind,
                            source_uid,
                        )
                        if leaked_interval:
                            raise InventoryConflictError(
                                "inventory-only watch created an interval"
                            )

                item = event.item
                ordinal = int(session["committed_events"]) + 1
                stored = await conn.fetchrow(
                    "INSERT INTO resource_inventory_watch_events ("
                    "watch_session_id, id, scope_epoch_id, ordinal, "
                    "request_digest, event_type, expected_resource_version, "
                    "resource_version, source_kind, source_uid, revision_hash, "
                    "normalized_item, valid_for_metering, item_error, "
                    "mutation_action, affected_interval_id, event_bytes, "
                    "collector_observed_at, received_at) VALUES ("
                    "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, "
                    "$12::jsonb, $13, $14::jsonb, $15, $16, $17, $18, $19) "
                    "RETURNING *",
                    watch_session_id,
                    event_id,
                    session["scope_epoch_id"],
                    ordinal,
                    request_digest,
                    str(event.event_type),
                    expected_resource_version,
                    event.resource_version,
                    source_kind,
                    source_uid,
                    None if item is None else item.revision_hash,
                    None
                    if item is None
                    else _canonical_json(dict(item.normalized_item)),
                    None if item is None else item.valid_for_metering,
                    None
                    if item is None or item.item_error is None
                    else _canonical_json(item.item_error.as_dict()),
                    str(action),
                    affected_interval_id,
                    event.event_bytes,
                    event.collector_observed_at,
                    received_at,
                )

                advanced = await conn.fetchval(
                    "UPDATE resource_inventory_scope_epochs SET "
                    "last_resource_version=$2, last_attempt_at=$3, "
                    "leader_generation=$4, backend_health='healthy', "
                    "updated_at=statement_timestamp() "
                    "WHERE id=$1 AND last_resource_version IS NOT DISTINCT FROM $5 "
                    "AND retired_at IS NULL RETURNING TRUE",
                    session["scope_epoch_id"],
                    event.resource_version,
                    received_at,
                    session["leader_generation"],
                    expected_resource_version,
                )
                if not advanced:
                    raise InventoryConflictError(
                        "watch cursor compare-and-swap lost a concurrent commit"
                    )

                next_events = ordinal
                next_bytes = int(session["committed_bytes"]) + event.event_bytes
                hit_limit = next_events == int(
                    session["max_events"]
                ) or next_bytes == int(session["max_bytes"])
                await conn.execute(
                    "UPDATE resource_inventory_watch_sessions SET "
                    "last_resource_version=$2, committed_events=$3, "
                    "committed_bytes=$4, termination_reason=$5, consumed_at=$6, "
                    "updated_at=statement_timestamp() WHERE id=$1",
                    watch_session_id,
                    event.resource_version,
                    next_events,
                    next_bytes,
                    "limit-reached" if hit_limit else None,
                    received_at if hit_limit else None,
                )
                session = await conn.fetchrow(
                    _WATCH_SESSION_SQL,
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                )
                return self._watch_result(session, stored, replayed=False)

    async def record_watch_gap(
        self,
        token: str,
        watch_session_id: UUID,
        event_id: UUID,
        request_digest: str,
        expected_resource_version: str,
        *,
        gap_reason: str,
        alternate_expected_resource_version: str | None = None,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        event_bytes: int = 0,
        collector_observed_at: datetime | None = None,
    ) -> WatchCommitResult:
        """Persist a typed continuity gap, including an uncertain apply ACK.

        For an ambiguous apply, the server cursor may still be the collector's
        last acknowledged value or may already equal the attempted event's
        value. Both are safe only because this transaction records a gap and
        forces a fresh recovery LIST instead of guessing which mutation won.
        """
        if not _HASH_RE.fullmatch(request_digest):
            raise InventoryContractError("watch request_digest must be SHA-256")
        if (
            not expected_resource_version
            or expected_resource_version == "0"
            or len(expected_resource_version) > 1024
        ):
            raise InventoryContractError("expected watch cursor is invalid")
        if gap_reason not in _WATCH_GAP_REASONS:
            raise InventoryContractError("watch gap reason is invalid")
        ambiguous = gap_reason == "ambiguous-watch-apply"
        if ambiguous != (alternate_expected_resource_version is not None):
            raise InventoryContractError(
                "ambiguous watch gap requires exactly one attempted cursor"
            )
        if alternate_expected_resource_version is not None and (
            not alternate_expected_resource_version
            or alternate_expected_resource_version == "0"
            or len(alternate_expected_resource_version) > 1024
        ):
            raise InventoryContractError("alternate watch cursor is invalid")
        if event_bytes < 0 or event_bytes > self.max_watch_event_bytes:
            raise InventoryContractError("watch gap event byte count is invalid")
        if collector_observed_at is not None:
            _require_aware(collector_observed_at, "collector_observed_at")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                session, epoch = await self._lock_watch_session(
                    conn,
                    token,
                    watch_session_id,
                    scope,
                    allow_consumed=True,
                )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=session["scope_epoch_id"],
                    leader_generation=int(session["leader_generation"]),
                    request_kind="watch-history-lost",
                    collector_id=str(epoch["collector_id"]),
                )
                session_cursor = str(session["last_resource_version"])
                epoch_cursor = str(epoch["last_resource_version"])
                if session_cursor != epoch_cursor:
                    raise InventoryConflictError(
                        "watch history-loss cursor state is inconsistent"
                    )
                accepted_cursors = {expected_resource_version}
                if alternate_expected_resource_version is not None:
                    accepted_cursors.add(alternate_expected_resource_version)
                if session_cursor not in accepted_cursors:
                    raise InventoryConflictError(
                        "watch history-loss cursor precondition failed"
                    )
                effective_expected_resource_version = session_cursor
                stored = await conn.fetchrow(
                    _WATCH_EVENT_SQL, watch_session_id, event_id
                )
                if stored is not None:
                    if (
                        stored["request_digest"] != request_digest
                        or stored["expected_resource_version"]
                        != effective_expected_resource_version
                        or stored["event_type"] != "history-lost"
                        or int(stored["event_bytes"]) != event_bytes
                        or stored["collector_observed_at"] != collector_observed_at
                    ):
                        raise InventoryConflictError(
                            "watch history event replay changed immutable content"
                        )
                    return self._watch_result(session, stored, replayed=True)
                if session["consumed_at"] is not None:
                    raise InventoryTicketError("inventory watch session is consumed")
                if int(session["committed_events"]) >= int(
                    session["max_events"]
                ) or event_bytes > int(session["max_bytes"]) - int(
                    session["committed_bytes"]
                ):
                    raise InventoryContractError("watch session bound is exhausted")

                received_at = await conn.fetchval("SELECT statement_timestamp()")
                gap_start = next(
                    (
                        value
                        for value in (
                            epoch["complete_through"],
                            epoch["last_complete_at"],
                            epoch["continuous_since"],
                        )
                        if value is not None
                    ),
                    received_at,
                )
                if gap_start > received_at:
                    gap_start = received_at
                gap_id = uuid4()
                gap_details: dict[str, Any] = {
                    "code": gap_reason,
                    "watch_session_id": str(watch_session_id),
                    "watch_event_id": str(event_id),
                }
                if alternate_expected_resource_version is not None:
                    gap_details.update(
                        {
                            "collector_committed_resource_version": (
                                expected_resource_version
                            ),
                            "attempted_resource_version": (
                                alternate_expected_resource_version
                            ),
                            "server_committed_resource_version": (
                                effective_expected_resource_version
                            ),
                        }
                    )
                await conn.execute(
                    "INSERT INTO resource_inventory_coverage_gaps ("
                    "id, scope_epoch_id, gap_start, reason, resolution_details) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb)",
                    gap_id,
                    session["scope_epoch_id"],
                    gap_start,
                    gap_reason,
                    _canonical_json(gap_details),
                )
                ordinal = int(session["committed_events"]) + 1
                stored = await conn.fetchrow(
                    "INSERT INTO resource_inventory_watch_events ("
                    "watch_session_id, id, scope_epoch_id, ordinal, "
                    "request_digest, event_type, expected_resource_version, "
                    "resource_version, mutation_action, coverage_gap_id, "
                    "event_bytes, collector_observed_at, received_at) VALUES ("
                    "$1, $2, $3, $4, $5, 'history-lost', $6, NULL, "
                    "'history-gap', $7, $8, $9, $10) RETURNING *",
                    watch_session_id,
                    event_id,
                    session["scope_epoch_id"],
                    ordinal,
                    request_digest,
                    effective_expected_resource_version,
                    gap_id,
                    event_bytes,
                    collector_observed_at,
                    received_at,
                )
                changed = await conn.fetchval(
                    "UPDATE resource_inventory_scope_epochs SET "
                    "last_attempt_at=$2, leader_generation=$3, "
                    "continuity_health='gap', backend_health='degraded', "
                    "consecutive_failures=consecutive_failures+1, "
                    "sanitized_error=$4::jsonb, updated_at=statement_timestamp() "
                    "WHERE id=$1 AND last_resource_version IS NOT DISTINCT FROM $5 "
                    "AND retired_at IS NULL RETURNING TRUE",
                    session["scope_epoch_id"],
                    received_at,
                    session["leader_generation"],
                    _canonical_json(
                        {
                            "code": gap_reason,
                            "coverage_gap_id": str(gap_id),
                        }
                    ),
                    effective_expected_resource_version,
                )
                if not changed:
                    raise InventoryConflictError(
                        "watch history-loss cursor precondition was lost"
                    )
                await conn.execute(
                    "UPDATE resource_inventory_watch_sessions SET "
                    "committed_events=$2, committed_bytes=$3, "
                    "termination_reason='history-lost', consumed_at=$4, "
                    "updated_at=statement_timestamp() WHERE id=$1",
                    watch_session_id,
                    ordinal,
                    int(session["committed_bytes"]) + event_bytes,
                    received_at,
                )
                session = await conn.fetchrow(
                    _WATCH_SESSION_SQL,
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                )
                return self._watch_result(session, stored, replayed=False)

    async def record_watch_410(
        self,
        token: str,
        watch_session_id: UUID,
        event_id: UUID,
        request_digest: str,
        expected_resource_version: str,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
        event_bytes: int = 0,
        collector_observed_at: datetime | None = None,
    ) -> WatchCommitResult:
        """Compatibility wrapper for the typed resource-version gap."""
        return await self.record_watch_gap(
            token,
            watch_session_id,
            event_id,
            request_digest,
            expected_resource_version,
            gap_reason="resource-version-expired",
            scope=scope,
            transport=transport,
            event_bytes=event_bytes,
            collector_observed_at=collector_observed_at,
        )

    async def finish_watch_session(
        self,
        token: str,
        watch_session_id: UUID,
        *,
        scope: InventoryScopeIdentity,
        transport: TransportNonceClaim,
    ) -> bool:
        """Consume a live bounded grant without changing its committed cursor."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                session, epoch = await self._lock_watch_session(
                    conn,
                    token,
                    watch_session_id,
                    scope,
                    allow_consumed=True,
                )
                await self._claim_transport_nonce(
                    conn,
                    transport,
                    scope_epoch_id=session["scope_epoch_id"],
                    leader_generation=int(session["leader_generation"]),
                    request_kind="watch-finish",
                    collector_id=str(epoch["collector_id"]),
                )
                if session["consumed_at"] is not None:
                    return False
                await conn.execute(
                    "UPDATE resource_inventory_watch_sessions SET "
                    "termination_reason='completed', "
                    "consumed_at=statement_timestamp(), "
                    "updated_at=statement_timestamp() WHERE id=$1",
                    watch_session_id,
                )
                return True


__all__ = [
    "IngestTicket",
    "InventoryConflictError",
    "InventoryContractError",
    "InventoryFenceError",
    "InventoryItem",
    "InventoryPurgeResult",
    "InventoryRecoveryRequired",
    "InventoryScopeIdentity",
    "InventoryStore",
    "InventoryTicketError",
    "ReconciliationResult",
    "RecoveryEpochHandle",
    "SanitizedInventoryError",
    "ShadowComparison",
    "ShadowComparisonStatus",
    "SnapshotIntervalMutationContext",
    "SnapshotIntervalMutator",
    "SnapshotObservationContext",
    "SnapshotObservationHook",
    "SnapshotFinalization",
    "SnapshotCompletionContext",
    "SnapshotCompletionHook",
    "SnapshotAbsenceMutationContext",
    "SnapshotAbsenceMutator",
    "SnapshotHandle",
    "StageResult",
    "TransportNonceClaim",
    "WatchCommitResult",
    "WatchDeletionMutationContext",
    "WatchDeletionMutator",
    "WatchEventKind",
    "WatchIntervalMutationContext",
    "WatchIntervalMutator",
    "WatchMutationAction",
    "WatchObjectEvent",
    "WatchSessionGrant",
    "WatchTerminalMutationContext",
    "WatchTerminalMutator",
    "canonical_request_digest",
    "inventory_manifest_digest",
]
