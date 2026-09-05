"""Persistence-free contracts for bounded infrastructure inventory collection.

The collector process is deliberately not a database client.  It emits only
typed, normalized observations and a small metadata envelope; raw Kubernetes
objects remain confined to the client/normalizer call stack.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence as SequenceABC
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
import math
import re
from typing import Any, Literal, Sequence
from uuid import UUID


_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CLASS = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_FORBIDDEN_NORMALIZED_KEYS = frozenset(
    {
        "raw",
        "raw_object",
        "full_object",
        "spec",
        "metadata",
        "annotations",
        "env",
        "env_from",
        "secret",
        "secrets",
        "secret_ref",
        "secret_key_ref",
        "string_data",
        "storage_class_parameters",
        "csi_volume_attributes",
        "volume_handle",
        "volumehandle",
        "volume_attributes",
        "volumeattributes",
        "controller_publish_secret_ref",
        "controllerpublishsecretref",
        "controller_expand_secret_ref",
        "controllerexpandsecretref",
        "node_publish_secret_ref",
        "nodepublishsecretref",
        "node_stage_secret_ref",
        "nodestagesecretref",
        "node_expand_secret_ref",
        "nodeexpandsecretref",
    }
)


def _required_text(value: str, field_name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty trimmed text")
    if len(value) > maximum or any(character.isspace() for character in value):
        raise ValueError(f"{field_name} is not a bounded opaque identifier")
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _plain_json(value: Any, *, path: str = "normalized") -> Any:
    """Convert explicitly projected normalized data to plain JSON values.

    Dataclasses and arbitrary objects are deliberately unsupported: callers
    must expose an explicit ``to_db_item()`` projection, preventing a future
    ``dataclasses.asdict`` from recursively copying a hidden raw API object.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{path} contains a non-finite decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        return _aware(value, path).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _plain_json(value.value, path=path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError(f"{path} contains an invalid mapping key")
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in _FORBIDDEN_NORMALIZED_KEYS:
                raise ValueError(f"{path} contains forbidden raw/sensitive field")
            result[key] = _plain_json(child, path=f"{path}.{key}")
        return result
    if isinstance(value, SequenceABC) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_plain_json(child, path=f"{path}[]") for child in value]
    raise ValueError(f"{path} contains a non-JSON-safe object")


def normalized_payload(value: Any) -> dict[str, Any]:
    """Return an allowlisted normalized mapping without introspecting objects."""

    projector = getattr(value, "to_db_item", None)
    projected = projector() if callable(projector) else value
    if not isinstance(projected, Mapping):
        raise ValueError("normalizer output must be a mapping or expose to_db_item()")
    plain = _plain_json(projected)
    assert isinstance(plain, dict)
    return plain


@dataclass(frozen=True, slots=True)
class InventoryScope:
    """One exact Kubernetes inventory authority.

    A null namespace requires the caller to opt into a reviewed cluster-scoped
    authority.  This prevents an accidentally omitted namespace from turning a
    namespaced collector into an all-cluster absence proof.
    """

    source_cluster: str
    api_resource: str
    namespace: str | None
    cluster_scoped: bool = False

    def __post_init__(self) -> None:
        if not _CLUSTER_ID.fullmatch(self.source_cluster):
            raise ValueError("source_cluster must be a stable bounded identifier")
        _required_text(self.api_resource, "api_resource")
        if len(self.api_resource.split("/")) != 3:
            raise ValueError("api_resource must be group/version/resource")
        if self.cluster_scoped:
            if self.namespace is not None:
                raise ValueError("cluster-scoped inventory cannot name a namespace")
        else:
            if self.namespace is None:
                raise ValueError(
                    "a null namespace requires explicit cluster_scoped=True"
                )
            _required_text(self.namespace, "namespace", maximum=253)

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.source_cluster, self.api_resource, self.namespace)

    def to_wire(self) -> dict[str, Any]:
        return {
            "source_cluster": self.source_cluster,
            "api_resource": self.api_resource,
            "namespace": self.namespace,
            "cluster_scoped": self.cluster_scoped,
        }


@dataclass(frozen=True, slots=True)
class CollectorLimits:
    """Hard memory/protocol limits for one LIST or bounded WATCH session."""

    list_page_size: int = 500
    max_page_items: int = 500
    max_pages: int = 1_000
    max_page_bytes: int = 8 * 1024 * 1024
    max_snapshot_items: int = 50_000
    max_snapshot_bytes: int = 64 * 1024 * 1024
    max_watch_events: int = 10_000
    max_watch_event_bytes: int = 2 * 1024 * 1024
    max_watch_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "list_page_size",
            "max_page_items",
            "max_pages",
            "max_page_bytes",
            "max_snapshot_items",
            "max_snapshot_bytes",
            "max_watch_events",
            "max_watch_event_bytes",
            "max_watch_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.list_page_size > self.max_page_items:
            raise ValueError("list_page_size cannot exceed max_page_items")
        if self.max_page_bytes > self.max_snapshot_bytes:
            raise ValueError("max_page_bytes cannot exceed max_snapshot_bytes")
        if self.max_watch_event_bytes > self.max_watch_bytes:
            raise ValueError("max_watch_event_bytes cannot exceed max_watch_bytes")


@dataclass(frozen=True, slots=True)
class KubernetesListPage:
    """One already-decoded API page plus its exact encoded response size.

    ``byte_count`` is supplied by the transport adapter from the bounded HTTP
    response.  Re-serializing arbitrary models here would both undercount some
    wire formats and unnecessarily duplicate potentially sensitive raw data.
    """

    items: Sequence[Any] = field(repr=False)
    byte_count: int
    resource_version: str | None = None
    continue_token: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.items, (str, bytes, bytearray)):
            raise ValueError("items must be a sequence of Kubernetes objects")
        if not isinstance(self.items, SequenceABC):
            raise ValueError("items must be a bounded sequence")
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int):
            raise ValueError("byte_count must be an integer")
        if self.byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        for field_name in ("resource_version", "continue_token"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{field_name} must be null or non-empty text")
        if self.resource_version is not None and len(self.resource_version) > 1024:
            raise ValueError("resource_version exceeds its protocol bound")
        if self.continue_token is not None and len(self.continue_token) > 8192:
            raise ValueError("continue_token exceeds its protocol bound")


class WatchEventType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    BOOKMARK = "BOOKMARK"
    ERROR = "ERROR"


class WatchGapReason(StrEnum):
    RESOURCE_VERSION_EXPIRED = "resource-version-expired"
    QUEUE_OVERFLOW = "watch-queue-overflow"
    PROTOCOL_ERROR = "watch-protocol-error"
    EVENT_BYTE_LIMIT = "watch-event-byte-limit"
    STREAM_BYTE_LIMIT = "watch-byte-limit"
    NORMALIZATION_ERROR = "watch-normalization-error"
    AMBIGUOUS_APPLY = "ambiguous-watch-apply"


@dataclass(frozen=True, slots=True)
class KubernetesWatchEvent:
    """Transport-neutral Kubernetes WATCH event."""

    event_type: WatchEventType | str
    resource_version: str | None
    byte_count: int
    raw_object: Any = field(default=None, repr=False)
    status_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", WatchEventType(self.event_type))
        if self.resource_version is not None and (
            not isinstance(self.resource_version, str) or not self.resource_version
        ):
            raise ValueError("resource_version must be null or non-empty text")
        if self.resource_version is not None and len(self.resource_version) > 1024:
            raise ValueError("resource_version exceeds its protocol bound")
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int):
            raise ValueError("byte_count must be an integer")
        if self.byte_count <= 0:
            raise ValueError("WATCH byte_count must be positive")
        if self.status_code is not None and (
            isinstance(self.status_code, bool) or not isinstance(self.status_code, int)
        ):
            raise ValueError("status_code must be an integer")


@dataclass(frozen=True, slots=True)
class InventoryError:
    """Sanitized collector diagnostic safe for durable metadata and logs."""

    error_class: str
    scope: InventoryScope
    message: str
    kind: str | None = None
    uid: str | None = None

    def __post_init__(self) -> None:
        if not _SAFE_ERROR_CLASS.fullmatch(self.error_class):
            raise ValueError("error_class must be a stable kebab-case code")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty text")
        if len(self.message) > 512 or "\n" in self.message or "\r" in self.message:
            raise ValueError("message must be a bounded single-line diagnostic")
        if self.kind is not None:
            _required_text(self.kind, "kind", maximum=128)
        if self.uid is not None:
            _required_text(self.uid, "uid")

    def to_wire(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class,
            "scope": self.scope.to_wire(),
            "message": self.message,
            "kind": self.kind,
            "uid": self.uid,
        }


@dataclass(frozen=True, slots=True)
class StagedInventoryItem:
    """One normalized item emitted to an injected staging callback."""

    scope: InventoryScope
    snapshot_id: UUID | None
    kind: str
    uid: str
    revision_hash: str | None
    valid_for_metering: bool
    normalized: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        _required_text(self.kind, "kind", maximum=128)
        _required_text(self.uid, "uid")
        if not isinstance(self.valid_for_metering, bool):
            raise ValueError("valid_for_metering must be boolean")
        if self.valid_for_metering:
            if self.revision_hash is None or not _HEX_SHA256.fullmatch(
                self.revision_hash
            ):
                raise ValueError("a valid item requires a SHA-256 revision_hash")
        elif self.revision_hash is not None and not _HEX_SHA256.fullmatch(
            self.revision_hash
        ):
            raise ValueError("revision_hash must be null or a SHA-256 digest")
        object.__setattr__(self, "normalized", normalized_payload(self.normalized))

    @property
    def digest_revision(self) -> str:
        return self.revision_hash if self.valid_for_metering else "invalid"

    def to_wire(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_wire(),
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "kind": self.kind,
            "uid": self.uid,
            "revision_hash": self.revision_hash,
            "valid_for_metering": self.valid_for_metering,
            "normalized": normalized_payload(self.normalized),
        }


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """Final metadata for one exact-scope LIST attempt.

    Items are streamed to the staging callback and intentionally are not held in
    this object.  Only a complete envelope can authorize absence reconciliation.
    """

    collector_id: str
    scope: InventoryScope
    collection_started_at: datetime
    collection_completed_at: datetime
    complete: bool
    snapshot_id: UUID
    leader_generation: int
    resource_version: str | None
    item_count: int
    item_digest: str | None
    pages_read: int
    bytes_read: int
    fatal_errors: tuple[InventoryError, ...] = ()
    item_errors: tuple[InventoryError, ...] = ()
    source_snapshot_at: datetime | None = None
    controller_epoch: str | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        _required_text(self.collector_id, "collector_id", maximum=128)
        start = _aware(self.collection_started_at, "collection_started_at")
        completed = _aware(self.collection_completed_at, "collection_completed_at")
        if completed < start:
            raise ValueError("collection timestamps must be monotonic")
        if (
            isinstance(self.leader_generation, bool)
            or not isinstance(self.leader_generation, int)
            or self.leader_generation < 0
        ):
            raise ValueError("leader_generation must be a non-negative integer")
        for name in ("item_count", "pages_read", "bytes_read"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (self.controller_epoch is None) != (self.sequence is None):
            raise ValueError("controller_epoch and sequence must be supplied together")
        if self.controller_epoch is not None:
            _required_text(
                self.controller_epoch,
                "controller_epoch",
                maximum=256,
            )
            if (
                isinstance(self.sequence, bool)
                or not isinstance(self.sequence, int)
                or self.sequence < 0
            ):
                raise ValueError("sequence must be a non-negative integer")
        object.__setattr__(self, "fatal_errors", tuple(self.fatal_errors))
        object.__setattr__(self, "item_errors", tuple(self.item_errors))
        if self.complete:
            if self.fatal_errors:
                raise ValueError("a complete snapshot cannot contain fatal errors")
            if not self.resource_version or self.resource_version == "0":
                raise ValueError("a complete snapshot requires an opaque LIST cursor")
            if self.item_digest is None or not _HEX_SHA256.fullmatch(self.item_digest):
                raise ValueError("a complete snapshot requires a SHA-256 item digest")
        elif self.resource_version is not None or self.item_digest is not None:
            raise ValueError("an incomplete snapshot cannot advance cursor or digest")

    @property
    def absence_authoritative(self) -> bool:
        return self.complete

    @property
    def source_cluster(self) -> str:
        return self.scope.source_cluster

    @property
    def api_resource(self) -> str:
        return self.scope.api_resource

    @property
    def namespace(self) -> str | None:
        return self.scope.namespace

    def to_wire(self) -> dict[str, Any]:
        """Serialize final metadata only; item frames are sent separately."""

        return {
            "collector_id": self.collector_id,
            **self.scope.to_wire(),
            "collection_started_at": self.collection_started_at.isoformat(),
            "collection_completed_at": self.collection_completed_at.isoformat(),
            # Canonical received_at is intentionally absent. The authenticated
            # ingestion service stamps its own app-DB receipt clock; a remote
            # collector cannot authoritatively supply accounting time.
            "source_snapshot_at": (
                self.source_snapshot_at.isoformat()
                if self.source_snapshot_at is not None
                else None
            ),
            "complete": self.complete,
            "snapshot_id": str(self.snapshot_id),
            "leader_generation": self.leader_generation,
            "controller_epoch": self.controller_epoch,
            "sequence": self.sequence,
            "resource_version": self.resource_version,
            "item_count": self.item_count,
            "item_digest": self.item_digest,
            "pages_read": self.pages_read,
            "bytes_read": self.bytes_read,
            "items_streamed": True,
            "fatal_errors": [error.to_wire() for error in self.fatal_errors],
            "item_errors": [error.to_wire() for error in self.item_errors],
        }


@dataclass(frozen=True, slots=True)
class WatchObservation:
    """One cursor+mutation unit for an atomic persistence callback."""

    scope: InventoryScope
    event_type: WatchEventType
    resource_version: str
    collector_observed_at: datetime
    source_event_bytes: int
    item: StagedInventoryItem | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", WatchEventType(self.event_type))
        if not self.resource_version or self.resource_version == "0":
            raise ValueError("a watch observation requires a non-zero cursor")
        _aware(self.collector_observed_at, "collector_observed_at")
        if (
            isinstance(self.source_event_bytes, bool)
            or not isinstance(self.source_event_bytes, int)
            or self.source_event_bytes <= 0
        ):
            raise ValueError("source_event_bytes must be a positive integer")
        if self.event_type == WatchEventType.BOOKMARK:
            if self.item is not None:
                raise ValueError("BOOKMARK cannot carry a normalized item")
        elif self.item is None:
            raise ValueError("object WATCH events require a normalized item")

    @property
    def confirms_presence(self) -> bool:
        return self.event_type in {WatchEventType.ADDED, WatchEventType.MODIFIED}

    @property
    def confirms_object(self) -> bool:
        return self.event_type != WatchEventType.BOOKMARK

    def to_wire(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_wire(),
            "event_type": self.event_type.value,
            "resource_version": self.resource_version,
            "collector_observed_at": self.collector_observed_at.isoformat(),
            "source_event_bytes": self.source_event_bytes,
            "confirms_presence": self.confirms_presence,
            "item": self.item.to_wire() if self.item is not None else None,
        }


@dataclass(frozen=True, slots=True)
class WatchOutcome:
    collector_id: str
    scope: InventoryScope
    started_at: datetime
    completed_at: datetime
    starting_resource_version: str
    committed_resource_version: str
    processed_events: int
    object_events: int
    bookmarks: int
    bytes_read: int
    reconnect_required: bool
    relist_required: bool
    history_lost: bool
    limit_reached: bool
    gap_reason: WatchGapReason | str | None = None
    ambiguous_resource_version: str | None = None
    fatal_errors: tuple[InventoryError, ...] = ()
    item_errors: tuple[InventoryError, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.collector_id, "collector_id", maximum=128)
        start = _aware(self.started_at, "started_at")
        completed = _aware(self.completed_at, "completed_at")
        if completed < start:
            raise ValueError("watch timestamps must be monotonic")
        for cursor_name in (
            "starting_resource_version",
            "committed_resource_version",
        ):
            cursor = getattr(self, cursor_name)
            _required_text(cursor, cursor_name, maximum=1024)
            if cursor == "0":
                raise ValueError(f"{cursor_name} must be a non-zero opaque cursor")
        for name in (
            "processed_events",
            "object_events",
            "bookmarks",
            "bytes_read",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.object_events + self.bookmarks != self.processed_events:
            raise ValueError("watch event counters are inconsistent")
        if self.history_lost and not self.relist_required:
            raise ValueError("lost history must force a relist")
        if self.history_lost:
            if self.gap_reason is None:
                raise ValueError("lost history requires a typed gap reason")
            object.__setattr__(self, "gap_reason", WatchGapReason(self.gap_reason))
        elif self.gap_reason is not None:
            raise ValueError("gap reason requires lost history")
        if self.ambiguous_resource_version is not None:
            _required_text(
                self.ambiguous_resource_version,
                "ambiguous_resource_version",
                maximum=1024,
            )
            if self.ambiguous_resource_version == "0":
                raise ValueError(
                    "ambiguous_resource_version must be a non-zero opaque cursor"
                )
            if self.gap_reason is not WatchGapReason.AMBIGUOUS_APPLY:
                raise ValueError(
                    "ambiguous resource version requires ambiguous apply gap"
                )
        elif self.gap_reason is WatchGapReason.AMBIGUOUS_APPLY:
            raise ValueError("ambiguous apply gap requires attempted resource version")
        object.__setattr__(self, "fatal_errors", tuple(self.fatal_errors))
        object.__setattr__(self, "item_errors", tuple(self.item_errors))

    def to_wire(self) -> dict[str, Any]:
        return {
            "collector_id": self.collector_id,
            "scope": self.scope.to_wire(),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "starting_resource_version": self.starting_resource_version,
            "committed_resource_version": self.committed_resource_version,
            "processed_events": self.processed_events,
            "object_events": self.object_events,
            "bookmarks": self.bookmarks,
            "bytes_read": self.bytes_read,
            "reconnect_required": self.reconnect_required,
            "relist_required": self.relist_required,
            "history_lost": self.history_lost,
            "limit_reached": self.limit_reached,
            "gap_reason": (None if self.gap_reason is None else self.gap_reason.value),
            "ambiguous_resource_version": self.ambiguous_resource_version,
            "fatal_errors": [error.to_wire() for error in self.fatal_errors],
            "item_errors": [error.to_wire() for error in self.item_errors],
        }


class KubernetesApiFailure(Exception):
    """Safe transport exception; response bodies are intentionally absent."""

    def __init__(self, status_code: int | None = None):
        super().__init__("Kubernetes API request failed")
        self.status_code = status_code
        # Match the synchronous Kubernetes client's conventional attribute.
        self.status = status_code


class WatchEventByteLimitExceeded(KubernetesApiFailure):
    """A WATCH frame crossed the adapter's bound before it could be decoded."""

    def __init__(self) -> None:
        super().__init__(413)


class WatchProtocolFailure(KubernetesApiFailure):
    """A consumed WATCH frame could not satisfy the typed event protocol."""

    def __init__(self) -> None:
        super().__init__(None)


class WatchQueueOverflow(Exception):
    """The bounded transport queue lost continuity and must be relisted."""


class RecoverableItemError(Exception):
    """Normalization failure with enough identity to preserve presence."""

    def __init__(
        self,
        *,
        kind: str,
        uid: str,
        namespace: str | None,
        error_class: str = "item-normalization",
    ):
        super().__init__("normalized item is invalid for metering")
        self.kind = kind
        self.uid = uid
        self.namespace = namespace
        self.error_class = error_class


ObjectWatchType = Literal[
    WatchEventType.ADDED, WatchEventType.MODIFIED, WatchEventType.DELETED
]


__all__ = [
    "CollectorLimits",
    "InventoryError",
    "InventoryScope",
    "InventorySnapshot",
    "KubernetesApiFailure",
    "KubernetesListPage",
    "KubernetesWatchEvent",
    "ObjectWatchType",
    "RecoverableItemError",
    "StagedInventoryItem",
    "WatchEventType",
    "WatchEventByteLimitExceeded",
    "WatchGapReason",
    "WatchObservation",
    "WatchOutcome",
    "WatchProtocolFailure",
    "WatchQueueOverflow",
    "normalized_payload",
]
