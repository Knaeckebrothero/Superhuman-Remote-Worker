"""Bounded Kubernetes LIST/WATCH collection engine.

This module deliberately knows neither Kubernetes client model classes nor any
database.  A thin transport adapter supplies bounded pages/events, a typed
normalizer strips the raw object to an allowlisted observation, and injected
callbacks stage or atomically apply that observation.  Consequently a failed
LIST can leave only an incomplete snapshot ID and a WATCH cursor never moves
independently of its object mutation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime, timezone
import hashlib
import inspect
import re
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4

from orchestrator.services.infrastructure_metering.collectors.contracts import (
    CollectorLimits,
    InventoryError,
    InventoryScope,
    InventorySnapshot,
    KubernetesListPage,
    KubernetesWatchEvent,
    RecoverableItemError,
    StagedInventoryItem,
    WatchEventByteLimitExceeded,
    WatchEventType,
    WatchGapReason,
    WatchObservation,
    WatchOutcome,
    WatchProtocolFailure,
    WatchQueueOverflow,
    normalized_payload,
)


_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CLASS = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MANIFEST_PREFIX = b"srw-inventory-manifest-v1\x00"
_MISSING = object()
T = TypeVar("T")


class KubernetesInventoryClient(Protocol):
    """Transport boundary used by the collection engine.

    There is intentionally no selector argument.  The adapter must issue an
    unfiltered exact-scope LIST. A non-null ``resource_version`` requests an
    exact historical relist anchored at the last committed WATCH cursor; the
    continuation token still owns consistency across pages.
    """

    def list_resources(
        self,
        *,
        scope: InventoryScope,
        limit: int,
        continue_token: str | None,
        resource_version: str | None,
    ) -> KubernetesListPage | Awaitable[KubernetesListPage]: ...

    def watch_resources(
        self,
        *,
        scope: InventoryScope,
        resource_version: str,
        allow_bookmarks: bool,
        timeout_seconds: int | None,
    ) -> (
        AsyncIterator[KubernetesWatchEvent]
        | Awaitable[AsyncIterator[KubernetesWatchEvent]]
    ): ...


Normalizer = Callable[[Any], Any | Awaitable[Any]]
StageItem = Callable[[StagedInventoryItem], None | Awaitable[None]]
ApplyWatchObservation = Callable[[WatchObservation], None | Awaitable[None]]
Clock = Callable[[], datetime]
SnapshotIdFactory = Callable[[], UUID]


async def _await_if_needed(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_watch_stream(iterator: Any, stream: Any) -> bool:
    """Close each distinct async WATCH layer that exposes ``aclose``.

    Most async generators are both iterable and iterator, but adapters may
    return a wrapper whose ``__aiter__`` creates a separate iterator.  Closing
    only that iterator can otherwise leave the wrapper's HTTP response open.
    Cleanup failures are reported to the caller without leaking exception text.
    """

    failed = False
    seen: set[int] = set()
    for target in (iterator, stream):
        if target is None or id(target) in seen:
            continue
        seen.add(id(target))
        close = getattr(target, "aclose", None)
        if not callable(close):
            continue
        try:
            await _await_if_needed(close())
        except Exception:
            failed = True
    return failed


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        result = value.get(name, _MISSING)
    else:
        result = getattr(value, name, _MISSING)
    if result is _MISSING:
        if default is _MISSING:
            raise AttributeError(name)
        return default
    return result


def _bounded_now(clock: Clock, floor: datetime) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("collector clock must return timezone-aware timestamps")
    return max(value, floor)


def inventory_item_digest(
    rows: list[tuple[str, str, str]] | tuple[tuple[str, str, str], ...],
) -> str:
    """Return the app-store v1 manifest digest for normalized inventory rows."""

    digest = hashlib.sha256(_MANIFEST_PREFIX)
    for row in sorted(rows):
        for value in row:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _status_code(exc: BaseException) -> int | None:
    for attribute in ("status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _safe_error_class(value: Any, fallback: str) -> str:
    return (
        value
        if isinstance(value, str) and _SAFE_ERROR_CLASS.fullmatch(value)
        else fallback
    )


class KubernetesCollectionEngine:
    """One bounded state machine reusable for Pod, PVC, and PV scopes."""

    def __init__(
        self,
        client: KubernetesInventoryClient,
        normalizer: Normalizer,
        *,
        collector_id: str,
        limits: CollectorLimits | None = None,
        clock: Clock | None = None,
        snapshot_id_factory: SnapshotIdFactory = uuid4,
    ):
        if (
            not isinstance(collector_id, str)
            or not collector_id
            or collector_id != collector_id.strip()
            or len(collector_id) > 128
            or any(character.isspace() for character in collector_id)
        ):
            raise ValueError("collector_id must be a bounded opaque identifier")
        self._client = client
        self._normalizer = normalizer
        self._collector_id = collector_id
        self._limits = limits or CollectorLimits()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._snapshot_id_factory = snapshot_id_factory

    def _error(
        self,
        scope: InventoryScope,
        error_class: str,
        message: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> InventoryError:
        # Every message passed here is a static collector diagnostic.  Raw API
        # exception messages and object representations never enter envelopes.
        return InventoryError(
            error_class=_safe_error_class(error_class, "collector-error"),
            scope=scope,
            message=message,
            kind=kind,
            uid=uid,
        )

    def _api_error(
        self, scope: InventoryScope, exc: BaseException, *, operation: str
    ) -> InventoryError:
        status = _status_code(exc)
        if status == 410:
            return self._error(
                scope,
                "resource-version-expired",
                f"Kubernetes {operation} history expired; a fresh LIST is required",
            )
        suffix = f" (HTTP {status})" if status is not None else ""
        return self._error(
            scope,
            "kubernetes-api",
            f"Kubernetes {operation} failed{suffix}",
        )

    def _namespace_matches(self, scope: InventoryScope, namespace: Any) -> bool:
        if scope.cluster_scoped:
            return namespace in (None, "")
        return isinstance(namespace, str) and namespace == scope.namespace

    async def _normalize(
        self,
        raw: Any,
        *,
        scope: InventoryScope,
        snapshot_id: UUID | None,
    ) -> tuple[
        StagedInventoryItem | None,
        InventoryError | None,
        InventoryError | None,
    ]:
        """Return ``(item, recoverable_error, fatal_error)`` without raw data."""

        try:
            normalized = await _await_if_needed(self._normalizer(raw))
        except RecoverableItemError as exc:
            try:
                if not isinstance(exc.kind, str) or not isinstance(exc.uid, str):
                    raise ValueError("identity type")
                kind = exc.kind
                uid = exc.uid
                if not self._namespace_matches(scope, exc.namespace):
                    raise ValueError("scope mismatch")
                error_class = _safe_error_class(exc.error_class, "item-normalization")
                item = StagedInventoryItem(
                    scope=scope,
                    snapshot_id=snapshot_id,
                    kind=kind,
                    uid=uid,
                    revision_hash=None,
                    valid_for_metering=False,
                    # Preserve only the typed exact-scope identity and stable
                    # error code.  App staging requires a durable normalized
                    # mapping even when capacity could not be projected; no
                    # field from the raw object is copied into this fallback.
                    normalized={
                        "source_kind": kind,
                        "uid": uid,
                        "namespace": exc.namespace,
                        "valid_for_metering": False,
                        "revision_hash": None,
                        "normalization_error": error_class,
                    },
                )
            except (TypeError, ValueError):
                return (
                    None,
                    None,
                    self._error(
                        scope,
                        "fatal-identity",
                        "A normalization failure did not preserve exact-scope identity",
                    ),
                )
            return (
                item,
                self._error(
                    scope,
                    error_class,
                    "An identifiable object is invalid for metering",
                    kind=item.kind,
                    uid=item.uid,
                ),
                None,
            )
        except Exception as exc:
            # Exception text can include the entire Kubernetes object (including
            # env/Secret refs). Persist only the exception class, never str(exc).
            exception_name = type(exc).__name__[:80]
            return (
                None,
                None,
                self._error(
                    scope,
                    "fatal-normalization",
                    f"Typed normalizer raised {exception_name}",
                ),
            )

        try:
            kind = _field(normalized, "kind")
            uid = _field(normalized, "uid")
            namespace = _field(normalized, "namespace")
            valid = _field(normalized, "valid_for_metering")
            revision_hash = _field(normalized, "revision_hash", None)
            if not isinstance(kind, str) or not isinstance(uid, str):
                raise ValueError("identity type")
            if not self._namespace_matches(scope, namespace):
                raise ValueError("scope mismatch")
            if not isinstance(valid, bool):
                raise ValueError("validity type")
            if valid and (
                not isinstance(revision_hash, str)
                or not _HEX_SHA256.fullmatch(revision_hash)
            ):
                raise ValueError("revision hash")
            if (
                not valid
                and revision_hash is not None
                and (
                    not isinstance(revision_hash, str)
                    or not _HEX_SHA256.fullmatch(revision_hash)
                )
            ):
                raise ValueError("revision hash")
        except Exception:
            return (
                None,
                None,
                self._error(
                    scope,
                    "fatal-identity",
                    "Normalized object lacks a valid exact-scope identity or revision",
                ),
            )

        try:
            wire_payload = normalized_payload(normalized)
        except Exception:
            return (
                None,
                None,
                self._error(
                    scope,
                    "fatal-payload",
                    "Normalizer did not provide an explicit safe wire payload",
                    kind=kind,
                    uid=uid,
                ),
            )

        try:
            item = StagedInventoryItem(
                scope=scope,
                snapshot_id=snapshot_id,
                kind=kind,
                uid=uid,
                revision_hash=revision_hash,
                valid_for_metering=valid,
                normalized=wire_payload,
            )
        except Exception:
            return (
                None,
                None,
                self._error(
                    scope,
                    "fatal-payload",
                    "Normalizer did not provide an explicit safe wire payload",
                    kind=kind,
                    uid=uid,
                ),
            )

        if item.valid_for_metering:
            return item, None, None
        return (
            item,
            self._error(
                scope,
                "item-normalization",
                "An identifiable object is invalid for metering",
                kind=item.kind,
                uid=item.uid,
            ),
            None,
        )

    async def collect_list(
        self,
        scope: InventoryScope,
        *,
        leader_generation: int,
        stage_item: StageItem,
        resource_version: str | None = None,
    ) -> InventorySnapshot:
        """Run one unfiltered, paginated LIST for an exact scope.

        ``stage_item`` is awaited once per normalized object.  The returned
        snapshot is complete only after every page and callback succeeds; its
        cursor/digest remain null on every failure path.
        """

        started = self._clock()
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("collector clock must return timezone-aware timestamps")
        snapshot_id = self._snapshot_id_factory()
        if not isinstance(snapshot_id, UUID):
            raise ValueError("snapshot_id_factory must return UUID")
        if (
            isinstance(leader_generation, bool)
            or not isinstance(leader_generation, int)
            or leader_generation < 0
        ):
            raise ValueError("leader_generation must be a non-negative integer")
        if resource_version == "0" or (
            resource_version is not None and not resource_version
        ):
            raise ValueError("LIST resource version must be non-zero opaque text")
        if resource_version is not None and len(resource_version) > 1024:
            raise ValueError("LIST resource version exceeds its protocol bound")

        pages_read = 0
        bytes_read = 0
        raw_items_seen = 0
        staged_items = 0
        continuation: str | None = None
        seen_continuations: set[str] = set()
        list_resource_version: str | None = None
        digest_rows: list[tuple[str, str, str]] = []
        seen_identities: set[tuple[str, str]] = set()
        item_errors: list[InventoryError] = []
        fatal_errors: list[InventoryError] = []

        while True:
            if pages_read >= self._limits.max_pages:
                fatal_errors.append(
                    self._error(
                        scope,
                        "page-limit",
                        "Kubernetes LIST exceeded the configured page limit",
                    )
                )
                break
            try:
                page = await _await_if_needed(
                    self._client.list_resources(
                        scope=scope,
                        limit=self._limits.list_page_size,
                        continue_token=continuation,
                        # Kubernetes continuation tokens already bind every
                        # later page to the first page's consistent snapshot.
                        # Supplying a non-zero resourceVersion together with
                        # continue is rejected by the API server.
                        resource_version=(
                            resource_version if continuation is None else None
                        ),
                    )
                )
            except Exception as exc:
                fatal_errors.append(self._api_error(scope, exc, operation="LIST"))
                break
            if not isinstance(page, KubernetesListPage):
                fatal_errors.append(
                    self._error(
                        scope,
                        "protocol-error",
                        "Kubernetes LIST adapter returned an invalid page envelope",
                    )
                )
                break
            if len(page.items) > self._limits.max_page_items:
                fatal_errors.append(
                    self._error(
                        scope,
                        "page-item-limit",
                        "Kubernetes LIST page exceeded the configured item limit",
                    )
                )
                break
            if page.byte_count > self._limits.max_page_bytes:
                fatal_errors.append(
                    self._error(
                        scope,
                        "page-byte-limit",
                        "Kubernetes LIST page exceeded the configured byte limit",
                    )
                )
                break
            if raw_items_seen + len(page.items) > self._limits.max_snapshot_items:
                fatal_errors.append(
                    self._error(
                        scope,
                        "snapshot-item-limit",
                        "Kubernetes LIST exceeded the configured snapshot item limit",
                    )
                )
                break
            if bytes_read + page.byte_count > self._limits.max_snapshot_bytes:
                fatal_errors.append(
                    self._error(
                        scope,
                        "snapshot-byte-limit",
                        "Kubernetes LIST exceeded the configured snapshot byte limit",
                    )
                )
                break
            if not page.resource_version or page.resource_version == "0":
                fatal_errors.append(
                    self._error(
                        scope,
                        "resource-version",
                        "Kubernetes LIST page omitted a usable resource version",
                    )
                )
                break
            if (
                list_resource_version is None
                and resource_version is not None
                and page.resource_version != resource_version
            ):
                fatal_errors.append(
                    self._error(
                        scope,
                        "resource-version-mismatch",
                        "Kubernetes LIST did not honor the exact resource version",
                    )
                )
                break
            if list_resource_version is None:
                list_resource_version = page.resource_version
            elif page.resource_version != list_resource_version:
                fatal_errors.append(
                    self._error(
                        scope,
                        "resource-version-mismatch",
                        "Kubernetes LIST continuation changed resource version",
                    )
                )
                break

            pages_read += 1
            bytes_read += page.byte_count
            raw_items_seen += len(page.items)

            page_failed = False
            for raw in page.items:
                item, item_error, fatal_error = await self._normalize(
                    raw,
                    scope=scope,
                    snapshot_id=snapshot_id,
                )
                if fatal_error is not None:
                    fatal_errors.append(fatal_error)
                    page_failed = True
                    break
                assert item is not None
                identity = (item.kind, item.uid)
                if identity in seen_identities:
                    fatal_errors.append(
                        self._error(
                            scope,
                            "duplicate-identity",
                            "Kubernetes LIST repeated an object UID in one snapshot",
                            kind=item.kind,
                            uid=item.uid,
                        )
                    )
                    page_failed = True
                    break
                try:
                    await _await_if_needed(stage_item(item))
                except Exception:
                    fatal_errors.append(
                        self._error(
                            scope,
                            "staging-failed",
                            "Normalized inventory staging callback failed",
                            kind=item.kind,
                            uid=item.uid,
                        )
                    )
                    page_failed = True
                    break
                seen_identities.add(identity)
                digest_rows.append((item.kind, item.uid, item.digest_revision))
                staged_items += 1
                if item_error is not None:
                    item_errors.append(item_error)
            if page_failed:
                break

            next_token = page.continue_token
            if next_token is None:
                continuation = None
                break
            if next_token == continuation or next_token in seen_continuations:
                fatal_errors.append(
                    self._error(
                        scope,
                        "continuation-cycle",
                        "Kubernetes LIST repeated a continuation token",
                    )
                )
                break
            seen_continuations.add(next_token)
            continuation = next_token

        completed = _bounded_now(self._clock, started)
        complete = not fatal_errors and continuation is None
        return InventorySnapshot(
            collector_id=self._collector_id,
            scope=scope,
            collection_started_at=started,
            collection_completed_at=completed,
            complete=complete,
            snapshot_id=snapshot_id,
            leader_generation=leader_generation,
            resource_version=list_resource_version if complete else None,
            item_count=staged_items,
            item_digest=inventory_item_digest(digest_rows) if complete else None,
            pages_read=pages_read,
            bytes_read=bytes_read,
            fatal_errors=tuple(fatal_errors),
            item_errors=tuple(item_errors),
        )

    async def watch(
        self,
        scope: InventoryScope,
        *,
        resource_version: str,
        apply_observation: ApplyWatchObservation,
        timeout_seconds: int | None = None,
    ) -> WatchOutcome:
        """Consume one bounded ordered WATCH session.

        ``apply_observation`` receives the normalized mutation and its new cursor
        together and must commit them atomically.  It is awaited serially.  A
        BOOKMARK uses the same callback with ``item=None``: it advances only the
        transport cursor and never counts as object confirmation.
        """

        if not isinstance(resource_version, str) or not resource_version:
            raise ValueError("WATCH requires a non-empty resource version")
        if resource_version == "0":
            raise ValueError("resource version 0 is not a metering cursor")
        if len(resource_version) > 1024:
            raise ValueError("WATCH resource version exceeds its protocol bound")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")

        started = self._clock()
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("collector clock must return timezone-aware timestamps")
        committed_cursor = resource_version
        processed_events = 0
        object_events = 0
        bookmarks = 0
        bytes_read = 0
        item_errors: list[InventoryError] = []
        fatal_errors: list[InventoryError] = []
        relist_required = False
        history_lost = False
        limit_reached = False
        gap_reason: WatchGapReason | None = None
        ambiguous_resource_version: str | None = None
        stream: Any = None
        iterator: Any = None

        try:
            stream = await _await_if_needed(
                self._client.watch_resources(
                    scope=scope,
                    resource_version=resource_version,
                    allow_bookmarks=True,
                    timeout_seconds=timeout_seconds,
                )
            )
            iterator = stream.__aiter__()
            while processed_events < self._limits.max_watch_events:
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    break
                if not isinstance(event, KubernetesWatchEvent):
                    fatal_errors.append(
                        self._error(
                            scope,
                            "protocol-error",
                            "Kubernetes WATCH adapter returned an invalid event",
                        )
                    )
                    relist_required = True
                    history_lost = True
                    gap_reason = WatchGapReason.PROTOCOL_ERROR
                    break
                if event.byte_count > self._limits.max_watch_event_bytes:
                    fatal_errors.append(
                        self._error(
                            scope,
                            "watch-event-byte-limit",
                            "Kubernetes WATCH event exceeded the configured byte limit",
                        )
                    )
                    relist_required = True
                    history_lost = True
                    gap_reason = WatchGapReason.EVENT_BYTE_LIMIT
                    break
                if bytes_read + event.byte_count > self._limits.max_watch_bytes:
                    fatal_errors.append(
                        self._error(
                            scope,
                            "watch-byte-limit",
                            "Kubernetes WATCH exceeded the configured byte limit",
                        )
                    )
                    relist_required = True
                    history_lost = True
                    gap_reason = WatchGapReason.STREAM_BYTE_LIMIT
                    break
                bytes_read += event.byte_count

                if event.event_type == WatchEventType.ERROR:
                    if event.status_code == 410:
                        fatal_errors.append(
                            self._error(
                                scope,
                                "resource-version-expired",
                                "Kubernetes WATCH history expired; a fresh LIST is required",
                            )
                        )
                        relist_required = True
                        history_lost = True
                        gap_reason = WatchGapReason.RESOURCE_VERSION_EXPIRED
                    else:
                        suffix = (
                            f" (HTTP {event.status_code})"
                            if event.status_code is not None
                            else ""
                        )
                        fatal_errors.append(
                            self._error(
                                scope,
                                "watch-error",
                                f"Kubernetes WATCH returned an error event{suffix}",
                            )
                        )
                    break
                if not event.resource_version or event.resource_version == "0":
                    fatal_errors.append(
                        self._error(
                            scope,
                            "resource-version",
                            "Kubernetes WATCH event omitted a usable cursor",
                        )
                    )
                    relist_required = True
                    history_lost = True
                    gap_reason = WatchGapReason.PROTOCOL_ERROR
                    break

                if event.event_type == WatchEventType.BOOKMARK:
                    observation = WatchObservation(
                        scope=scope,
                        event_type=WatchEventType.BOOKMARK,
                        resource_version=event.resource_version,
                        collector_observed_at=_bounded_now(self._clock, started),
                        source_event_bytes=event.byte_count,
                        item=None,
                    )
                else:
                    item, item_error, fatal_error = await self._normalize(
                        event.raw_object,
                        scope=scope,
                        snapshot_id=None,
                    )
                    if fatal_error is not None:
                        fatal_errors.append(fatal_error)
                        relist_required = True
                        history_lost = True
                        gap_reason = WatchGapReason.NORMALIZATION_ERROR
                        break
                    assert item is not None
                    if item_error is not None:
                        item_errors.append(item_error)
                    observation = WatchObservation(
                        scope=scope,
                        event_type=event.event_type,
                        resource_version=event.resource_version,
                        collector_observed_at=_bounded_now(self._clock, started),
                        source_event_bytes=event.byte_count,
                        item=item,
                    )

                try:
                    await _await_if_needed(apply_observation(observation))
                except Exception:
                    fatal_errors.append(
                        self._error(
                            scope,
                            "watch-apply-failed",
                            "Atomic WATCH mutation and cursor callback failed",
                            kind=(observation.item.kind if observation.item else None),
                            uid=(observation.item.uid if observation.item else None),
                        )
                    )
                    # The callback may have committed while its response was
                    # lost. Retain the last acknowledged cursor, but preserve
                    # the attempted cursor so ingestion can classify this as
                    # an ambiguous apply gap. A complete LIST reconciles either
                    # outcome without guessing whether the mutation committed.
                    relist_required = True
                    history_lost = True
                    gap_reason = WatchGapReason.AMBIGUOUS_APPLY
                    ambiguous_resource_version = observation.resource_version
                    break

                committed_cursor = observation.resource_version
                processed_events += 1
                if observation.event_type == WatchEventType.BOOKMARK:
                    bookmarks += 1
                else:
                    object_events += 1
            else:
                limit_reached = True
        except WatchEventByteLimitExceeded:
            fatal_errors.append(
                self._error(
                    scope,
                    "watch-event-byte-limit",
                    "Kubernetes WATCH event exceeded the configured byte limit",
                )
            )
            relist_required = True
            history_lost = True
            gap_reason = WatchGapReason.EVENT_BYTE_LIMIT
        except WatchProtocolFailure:
            fatal_errors.append(
                self._error(
                    scope,
                    "protocol-error",
                    "Kubernetes WATCH adapter returned an invalid event",
                )
            )
            relist_required = True
            history_lost = True
            gap_reason = WatchGapReason.PROTOCOL_ERROR
        except WatchQueueOverflow:
            fatal_errors.append(
                self._error(
                    scope,
                    "watch-queue-overflow",
                    "Kubernetes WATCH queue overflow broke event continuity",
                )
            )
            relist_required = True
            history_lost = True
            gap_reason = WatchGapReason.QUEUE_OVERFLOW
        except Exception as exc:
            fatal_errors.append(self._api_error(scope, exc, operation="WATCH"))
            if _status_code(exc) == 410:
                relist_required = True
                history_lost = True
                gap_reason = WatchGapReason.RESOURCE_VERSION_EXPIRED
        finally:
            if await _close_watch_stream(iterator, stream):
                fatal_errors.append(
                    self._error(
                        scope,
                        "watch-close-failed",
                        "Kubernetes WATCH stream cleanup failed",
                    )
                )

        completed = _bounded_now(self._clock, started)
        return WatchOutcome(
            collector_id=self._collector_id,
            scope=scope,
            started_at=started,
            completed_at=completed,
            starting_resource_version=resource_version,
            committed_resource_version=committed_cursor,
            processed_events=processed_events,
            object_events=object_events,
            bookmarks=bookmarks,
            bytes_read=bytes_read,
            reconnect_required=True,
            relist_required=relist_required,
            history_lost=history_lost,
            limit_reached=limit_reached,
            gap_reason=gap_reason,
            ambiguous_resource_version=ambiguous_resource_version,
            fatal_errors=tuple(fatal_errors),
            item_errors=tuple(item_errors),
        )


__all__ = [
    "ApplyWatchObservation",
    "KubernetesCollectionEngine",
    "KubernetesInventoryClient",
    "Normalizer",
    "StageItem",
    "inventory_item_digest",
]
