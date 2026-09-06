"""Dedicated, database-free Kubernetes Pod/PVC/PV/VMI inventory runtime.

The runtime deliberately has only two authorities: namespace-scoped read access
to the Kubernetes API and an HMAC-authenticated internal HTTP client.  Raw API
objects are normalized before they reach the bounded spool, logs, or wire
transport; the collector never receives application database credentials.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import heapq
import logging
import math
import os
import random
import signal
import tempfile
import time
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ValidationError

from orchestrator.services.infrastructure_metering.collectors import (
    CollectorLimits,
    InventoryError,
    InventoryScope,
    InventorySnapshot,
    KubernetesCollectionEngine,
    StagedInventoryItem,
    WatchEventType,
    WatchObservation,
    WatchOutcome,
)
from orchestrator.services.infrastructure_metering.collectors.kubernetes_client import (
    RawKubernetesClient,
)
from orchestrator.services.infrastructure_metering.collectors.pod_normalization import (
    PodEffectiveRequest,
    normalize_pod,
)
from orchestrator.services.infrastructure_metering.collectors.storage_normalization import (
    normalize_pv,
    normalize_pvc,
)
from orchestrator.services.infrastructure_metering.collectors.vmi_normalization import (
    normalize_vmi,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventoryItemWire,
    InventorySnapshotBegin,
    InventorySnapshotFinalize,
    InventorySnapshotItemBatch,
    InventoryTicketRequest,
    InventoryTicketResponse,
    InventoryWatchApply,
    InventoryWatchFinish,
    SNAPSHOT_BATCH_BODY_LIMIT,
    SNAPSHOT_METADATA_BODY_LIMIT,
    TICKET_BODY_LIMIT,
    WATCH_BODY_LIMIT,
)
from orchestrator.services.infrastructure_metering.transport import (
    canonical_json_bytes,
    sign_transport_request,
)


logger = logging.getLogger(__name__)

API_PREFIX = "/api/internal/infrastructure-metering/v1"
TICKETS_PATH = f"{API_PREFIX}/tickets"
SNAPSHOT_BEGIN_PATH = f"{API_PREFIX}/snapshots/begin"
SNAPSHOT_ITEMS_PATH = f"{API_PREFIX}/snapshots/items"
SNAPSHOT_FINALIZE_PATH = f"{API_PREFIX}/snapshots/finalize"
WATCH_APPLY_PATH = f"{API_PREFIX}/watch/apply"
WATCH_FINISH_PATH = f"{API_PREFIX}/watch/finish"

POD_API_RESOURCE = "core/v1/pods"
PVC_API_RESOURCE = "core/v1/persistentvolumeclaims"
PV_API_RESOURCE = "core/v1/persistentvolumes"
VMI_API_RESOURCE = "kubevirt.io/v1/virtualmachineinstances"
PRIMARY_COLLECTOR_ID = "kubernetes-pods"
VMI_COLLECTOR_ID = "kubevirt-vmis"
VM_STORAGE_COLLECTOR_ID = "kubevirt-storage"
_COLLECTOR_IDS = frozenset(
    {PRIMARY_COLLECTOR_ID, VMI_COLLECTOR_ID, VM_STORAGE_COLLECTOR_ID}
)
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_BATCH_ITEMS = 500
MAX_FATAL_ERRORS = 1_000
MAX_ITEM_ERRORS = 2_000
TARGET_BATCH_BYTES = 1024 * 1024
SPOOL_MEMORY_BYTES = 1024 * 1024
# Keep sessions bounded while avoiding a new immutable session row every ten
# seconds. Cancellation closes an established raw response, and the adapter's
# separate connect/read timeouts still bound shutdown while the worker thread
# is opening a request.
MAX_WATCH_SESSION_SECONDS = 60
MAX_HTTP_ATTEMPTS = 4
_RETRYABLE_HTTP_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_REQUEST_BODY_LIMITS = {
    TICKETS_PATH: TICKET_BODY_LIMIT,
    SNAPSHOT_BEGIN_PATH: SNAPSHOT_METADATA_BODY_LIMIT,
    SNAPSHOT_ITEMS_PATH: SNAPSHOT_BATCH_BODY_LIMIT,
    SNAPSHOT_FINALIZE_PATH: SNAPSHOT_METADATA_BODY_LIMIT,
    WATCH_APPLY_PATH: WATCH_BODY_LIMIT,
    WATCH_FINISH_PATH: WATCH_BODY_LIMIT,
}

T = TypeVar("T", bound=BaseModel)


class CollectorRuntimeError(RuntimeError):
    """A sanitized runtime error that is safe to classify in logs."""


class CollectorConfigurationError(CollectorRuntimeError):
    """The dedicated collector is enabled but cannot start safely."""


class IngestionTransportError(CollectorRuntimeError):
    """A bounded internal ingestion request failed without exposing its body."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class IngestionTransport(Protocol):
    """Small injectable boundary used by the collector state machine."""

    async def post(self, path: str, payload: Mapping[str, Any]) -> bytes: ...


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


class SignedIngestionHttpClient:
    """Bounded HMAC HTTP transport with retries and no response-body logging."""

    def __init__(
        self,
        *,
        base_url: str,
        collector_id: str,
        ingestion_key: str | bytes,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        random_value: Callable[[], float] = random.random,
        max_attempts: int = MAX_HTTP_ATTEMPTS,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        self._collector_id = collector_id
        key_bytes = (
            ingestion_key.encode("utf-8")
            if isinstance(ingestion_key, str)
            else bytes(ingestion_key)
        )
        if len(key_bytes) < 32:
            raise CollectorConfigurationError(
                "infrastructure metering ingestion key must be 32+ bytes"
            )
        self._ingestion_key = key_bytes
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._random_value = random_value
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=15.0, write=15.0, pool=3.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def __aenter__(self) -> "SignedIngestionHttpClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _read_response(self, response: httpx.Response) -> bytes:
        content = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise IngestionTransportError("response-too-large")
                content.extend(chunk)
        finally:
            await response.aclose()
        return bytes(content)

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                requested = float(retry_after)
            except ValueError:
                requested = -1
            if 0 <= requested <= 5:
                return requested
        base = min(0.25 * (2**attempt), 5.0)
        return base * (0.75 + 0.5 * self._random_value())

    async def post(self, path: str, payload: Mapping[str, Any]) -> bytes:
        maximum_body_bytes = _REQUEST_BODY_LIMITS.get(path)
        if maximum_body_bytes is None:
            raise ValueError("unsupported infrastructure metering endpoint")
        body = canonical_json_bytes(payload)
        if len(body) > maximum_body_bytes:
            raise IngestionTransportError("request-too-large")

        for attempt in range(self._max_attempts):
            headers = sign_transport_request(
                method="POST",
                path=path,
                collector_id=self._collector_id,
                body=body,
                key=self._ingestion_key,
            )
            request = self._client.build_request(
                "POST",
                f"{self._base_url}{path}",
                headers=headers,
                content=body,
            )
            try:
                response = await self._client.send(request, stream=True)
                status_code = response.status_code
                retry_after = response.headers.get("Retry-After")
                response_body = await self._read_response(response)
            except IngestionTransportError:
                raise
            except httpx.HTTPError as exc:
                if attempt + 1 >= self._max_attempts:
                    raise IngestionTransportError("network-failure") from exc
                await self._sleep(self._retry_delay(attempt, None))
                continue

            if 200 <= status_code < 300:
                return response_body
            if (
                status_code not in _RETRYABLE_HTTP_STATUS
                or attempt + 1 >= self._max_attempts
            ):
                raise IngestionTransportError(
                    "ingestion-rejected", status_code=status_code
                )
            await self._sleep(self._retry_delay(attempt, retry_after))

        raise AssertionError("bounded retry loop did not terminate")


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CollectorConfigurationError("metering orchestrator URL is required")
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` performs urllib's numeric/range validation.
        parsed.port
    except ValueError as exc:
        raise CollectorConfigurationError(
            "metering orchestrator URL is invalid"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any(character.isspace() for character in value)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CollectorConfigurationError("metering orchestrator URL is invalid")
    return value.rstrip("/")


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=False)


def _model_from_json(model_type: type[T], value: Mapping[str, Any]) -> T:
    return model_type.model_validate_json(canonical_json_bytes(value))


def _error_sort_key(error: InventoryError) -> tuple[str, str, str, str]:
    return (
        error.kind or "",
        error.uid or "",
        error.error_class,
        error.message,
    )


def _with_bounded_diagnostics(
    payload: Mapping[str, Any],
    *,
    error_groups: Sequence[tuple[str, Sequence[InventoryError], int]],
    maximum_bytes: int,
) -> dict[str, Any]:
    """Attach deterministic error prefixes that fit the full encoded body.

    Fatal diagnostics are passed first by callers, so they consume the budget
    before per-item diagnostics. The accounting is exact for canonical JSON:
    adding an element to an empty array costs its encoded size, and every later
    element costs one additional comma byte.
    """

    result = dict(payload)
    for field_name, _errors, _limit in error_groups:
        if field_name in result:
            raise ValueError("diagnostic field must not be pre-populated")
        result[field_name] = []

    encoded_size = len(canonical_json_bytes(result))
    if encoded_size > maximum_bytes:
        raise CollectorRuntimeError("diagnostic-envelope-too-large")
    remaining = maximum_bytes - encoded_size

    for field_name, errors, limit in error_groups:
        selected = heapq.nsmallest(limit, errors, key=_error_sort_key)
        output = result[field_name]
        assert isinstance(output, list)
        for error in selected:
            wire = error.to_wire()
            additional_bytes = len(canonical_json_bytes(wire)) + bool(output)
            if additional_bytes > remaining:
                break
            output.append(wire)
            remaining -= additional_bytes

    if len(canonical_json_bytes(result)) > maximum_bytes:
        raise AssertionError("canonical diagnostic byte accounting drifted")
    return result


def _snapshot_begin_wire(
    snapshot: InventorySnapshot,
    *,
    ticket: InventoryTicketResponse,
) -> dict[str, Any]:
    # Invalid objects remain staged with safe per-item state.  The envelope is
    # diagnostic only, so select a deterministic bounded subset rather than
    # constructing a potentially 50k-row JSON error array.
    payload = {
        "ticket_id": str(ticket.ticket_id),
        "ticket_token": ticket.ticket_token,
        "collector_id": snapshot.collector_id,
        **snapshot.scope.to_wire(),
        "collection_started_at": snapshot.collection_started_at.isoformat(),
        "collection_completed_at": snapshot.collection_completed_at.isoformat(),
        "source_snapshot_at": (
            snapshot.source_snapshot_at.isoformat()
            if snapshot.source_snapshot_at is not None
            else None
        ),
        "complete": snapshot.complete,
        "snapshot_id": str(snapshot.snapshot_id),
        "leader_generation": snapshot.leader_generation,
        "controller_epoch": snapshot.controller_epoch,
        "sequence": snapshot.sequence,
        "resource_version": snapshot.resource_version,
        "item_count": snapshot.item_count if snapshot.complete else 0,
        "item_digest": snapshot.item_digest,
        "pages_read": snapshot.pages_read,
        "bytes_read": snapshot.bytes_read,
        "items_streamed": True,
    }
    return _with_bounded_diagnostics(
        payload,
        error_groups=(
            ("fatal_errors", snapshot.fatal_errors, MAX_FATAL_ERRORS),
            ("item_errors", snapshot.item_errors, MAX_ITEM_ERRORS),
        ),
        maximum_bytes=SNAPSHOT_METADATA_BODY_LIMIT,
    )


def _snapshot_finalize_wire(
    snapshot: InventorySnapshot,
    *,
    ticket: InventoryTicketResponse,
    shadow_enabled: bool,
) -> dict[str, Any]:
    payload = {
        "ticket_id": str(ticket.ticket_id),
        "ticket_token": ticket.ticket_token,
        "snapshot_id": str(snapshot.snapshot_id),
        "scope": snapshot.scope.to_wire(),
        "shadow_enabled": shadow_enabled,
        "collection_completed_at": snapshot.collection_completed_at.isoformat(),
        "source_snapshot_at": (
            snapshot.source_snapshot_at.isoformat()
            if snapshot.source_snapshot_at is not None
            else None
        ),
        "complete": snapshot.complete,
        "resource_version": snapshot.resource_version,
        "item_count": snapshot.item_count if snapshot.complete else 0,
        "item_digest": snapshot.item_digest,
        "controller_epoch": snapshot.controller_epoch,
        "sequence": snapshot.sequence,
    }
    return _with_bounded_diagnostics(
        payload,
        error_groups=(("fatal_errors", snapshot.fatal_errors, MAX_FATAL_ERRORS),),
        maximum_bytes=SNAPSHOT_METADATA_BODY_LIMIT,
    )


def _watch_finish_wire(
    outcome: WatchOutcome,
    *,
    ticket: InventoryTicketResponse,
) -> dict[str, Any]:
    payload = {
        "ticket_id": str(ticket.ticket_id),
        "ticket_token": ticket.ticket_token,
        "leader_generation": ticket.leader_generation,
        "scope": outcome.scope.to_wire(),
        "started_at": outcome.started_at.isoformat(),
        "completed_at": outcome.completed_at.isoformat(),
        "starting_resource_version": outcome.starting_resource_version,
        "committed_resource_version": outcome.committed_resource_version,
        "processed_events": outcome.processed_events,
        "object_events": outcome.object_events,
        "bookmarks": outcome.bookmarks,
        "bytes_read": outcome.bytes_read,
        "reconnect_required": outcome.reconnect_required,
        "relist_required": outcome.relist_required,
        "history_lost": outcome.history_lost,
        "limit_reached": outcome.limit_reached,
        "gap_reason": (
            None if outcome.gap_reason is None else outcome.gap_reason.value
        ),
        "ambiguous_resource_version": outcome.ambiguous_resource_version,
        "history_event_id": str(uuid4()) if outcome.history_lost else None,
    }
    return _with_bounded_diagnostics(
        payload,
        error_groups=(
            ("fatal_errors", outcome.fatal_errors, MAX_FATAL_ERRORS),
            ("item_errors", outcome.item_errors, MAX_ITEM_ERRORS),
        ),
        maximum_bytes=WATCH_BODY_LIMIT,
    )


class _SnapshotSpool(AbstractContextManager["_SnapshotSpool"]):
    """Newline-framed safe items that spill to an anonymous file under /tmp."""

    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._bytes_written = 0
        self._item_count = 0
        self._file = tempfile.SpooledTemporaryFile(
            max_size=min(SPOOL_MEMORY_BYTES, maximum_bytes),
            mode="w+b",
            prefix="srw-inventory-",
            dir="/tmp",
        )

    @property
    def item_count(self) -> int:
        return self._item_count

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def append(self, item: StagedInventoryItem) -> None:
        frame = canonical_json_bytes(item.to_wire())
        # Keep a single item comfortably below the HTTP request bound; a Pod
        # with excessive admitted metadata becomes a non-authoritative LIST.
        if len(frame) > TARGET_BATCH_BYTES:
            raise CollectorRuntimeError("normalized-item-too-large")
        framed_size = len(frame) + 1
        if self._bytes_written + framed_size > self._maximum_bytes:
            raise CollectorRuntimeError("normalized-snapshot-too-large")
        self._file.write(frame)
        self._file.write(b"\n")
        self._bytes_written += framed_size
        self._item_count += 1

    def batches(self) -> Iterator[list[InventoryItemWire]]:
        self._file.seek(0)
        batch: list[InventoryItemWire] = []
        batch_bytes = 0
        for line in self._file:
            if not line.endswith(b"\n"):
                raise CollectorRuntimeError("invalid-spool-frame")
            frame = line[:-1]
            if batch and (
                len(batch) >= MAX_BATCH_ITEMS
                or batch_bytes + len(frame) > TARGET_BATCH_BYTES
            ):
                yield batch
                batch = []
                batch_bytes = 0
            try:
                item = InventoryItemWire.model_validate_json(frame)
            except ValidationError as exc:
                raise CollectorRuntimeError("invalid-spool-item") from exc
            batch.append(item)
            batch_bytes += len(frame)
        if batch:
            yield batch

    def close(self) -> None:
        self._file.close()

    def __exit__(self, *_args: object) -> None:
        self.close()


class _EffectiveRequestCache:
    """Bounded in-memory resize continuity, isolated by inventory scope."""

    def __init__(self, maximum_per_scope: int) -> None:
        if (
            isinstance(maximum_per_scope, bool)
            or not isinstance(maximum_per_scope, int)
            or maximum_per_scope <= 0
        ):
            raise ValueError("effective request cache bound must be positive")
        self._maximum_per_scope = maximum_per_scope
        self._requests: dict[
            tuple[str, str, str | None], OrderedDict[str, PodEffectiveRequest]
        ] = {}

    @staticmethod
    def _key(scope: InventoryScope) -> tuple[str, str, str | None]:
        return scope.key

    def get(self, scope: InventoryScope, uid: str) -> PodEffectiveRequest | None:
        requests = self._requests.get(self._key(scope))
        if requests is None:
            return None
        request = requests.get(uid)
        if request is not None:
            requests.move_to_end(uid)
        return request

    def peek(self, scope: InventoryScope, uid: str) -> PodEffectiveRequest | None:
        requests = self._requests.get(self._key(scope))
        return requests.get(uid) if requests is not None else None

    def put(
        self,
        scope: InventoryScope,
        uid: str,
        request: PodEffectiveRequest,
    ) -> None:
        key = self._key(scope)
        requests = self._requests.setdefault(key, OrderedDict())
        requests[uid] = request
        requests.move_to_end(uid)
        while len(requests) > self._maximum_per_scope:
            requests.popitem(last=False)

    def remove(self, scope: InventoryScope, uid: str) -> None:
        key = self._key(scope)
        requests = self._requests.get(key)
        if requests is None:
            return
        requests.pop(uid, None)
        if not requests:
            self._requests.pop(key, None)

    def reconcile_list(
        self,
        scope: InventoryScope,
        *,
        seen_uids: set[str],
        valid_updates: Mapping[str, PodEffectiveRequest],
        complete: bool,
    ) -> None:
        key = self._key(scope)
        requests = self._requests.get(key)
        if complete and requests is not None:
            for uid in tuple(requests):
                if uid not in seen_uids:
                    del requests[uid]
            if not requests:
                self._requests.pop(key, None)
        # Even an incomplete LIST provides useful positive observations. It
        # must never use unseen UIDs as an absence proof, but bounded updates
        # keep later resize fallbacks conservative for objects it did observe.
        for uid, request in valid_updates.items():
            self.put(scope, uid, request)

    def count(self, scope: InventoryScope) -> int:
        requests = self._requests.get(self._key(scope))
        return len(requests) if requests is not None else 0


def _raw_pod_uid(raw: Any) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    metadata = raw.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    uid = metadata.get("uid")
    if (
        not isinstance(uid, str)
        or not uid
        or len(uid) > 256
        or uid != uid.strip()
        or any(character.isspace() for character in uid)
    ):
        return None
    return uid


@dataclass(frozen=True, slots=True)
class CollectorCycleResult:
    snapshot_complete: bool
    relist_required: bool
    resource_version: str | None


class KubernetesPodCollectorRuntime:
    """Coordinates bounded Pod/PVC/PV LIST/WATCH scopes and ingestion.

    The historical class name remains a compatibility alias for tests and the
    image entry point; the runtime itself is resource-dispatched.
    """

    def __init__(
        self,
        *,
        settings: InfrastructureMeteringSettings,
        collector_id: str,
        kubernetes_client: Any,
        transport: IngestionTransport,
        volume_identity_key: str | bytes | None = None,
        controller_epoch: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        settings.validate()
        if not settings.collector_enabled:
            raise CollectorConfigurationError("collector gate is disabled")
        if not settings.namespace_allowlist:
            raise CollectorConfigurationError(
                "collector requires at least one namespace"
            )
        self._settings = settings
        self._collector_id = collector_id
        if collector_id not in _COLLECTOR_IDS:
            raise CollectorConfigurationError(
                "infrastructure metering collector identity is unsupported"
            )
        self._is_vm_collector = collector_id == VMI_COLLECTOR_ID
        self._is_vm_storage_collector = collector_id == VM_STORAGE_COLLECTOR_ID
        self._client = kubernetes_client
        self._transport = transport
        self._monotonic = monotonic
        if controller_epoch is not None:
            controller_epoch = controller_epoch.strip()
            if (
                not controller_epoch
                or len(controller_epoch) > 256
                or any(character.isspace() for character in controller_epoch)
            ):
                raise CollectorConfigurationError(
                    "collector controller epoch is invalid"
                )
        if self._is_vm_collector and not settings.vm_inventory_enabled:
            raise CollectorConfigurationError(
                "VMI collector identity requires VM inventory"
            )
        if self._is_vm_collector and controller_epoch is None:
            raise CollectorConfigurationError(
                "VM inventory requires a controller epoch"
            )
        if not self._is_vm_collector and controller_epoch is not None:
            raise CollectorConfigurationError(
                "only the VMI collector may carry a controller epoch"
            )
        if self._is_vm_storage_collector and not (
            settings.vm_pvc_inventory_enabled or settings.vm_pv_inventory_enabled
        ):
            raise CollectorConfigurationError(
                "VM storage collector identity requires VM storage inventory"
            )
        self._controller_epoch = controller_epoch
        self._scope_sequences: dict[tuple[str, str, str | None], int] = {}
        if isinstance(volume_identity_key, str):
            volume_identity_key = volume_identity_key.encode("utf-8")
        self._volume_identity_key = (
            None if volume_identity_key is None else bytes(volume_identity_key)
        )
        pv_inventory_enabled = (
            self._settings.vm_pv_inventory_enabled
            if self._is_vm_storage_collector
            else self._settings.pv_inventory_enabled
            if not self._is_vm_collector
            else False
        )
        if pv_inventory_enabled and (
            self._volume_identity_key is None or len(self._volume_identity_key) < 32
        ):
            raise CollectorConfigurationError(
                "PV inventory requires a 32+ byte volume identity key"
            )
        self._effective_requests = _EffectiveRequestCache(settings.max_snapshot_items)
        self._limits = CollectorLimits(
            list_page_size=settings.list_page_size,
            max_page_items=settings.list_page_size,
            max_pages=min(
                1_000,
                math.ceil(settings.max_snapshot_items / settings.list_page_size) + 1,
            ),
            max_page_bytes=min(8 * 1024 * 1024, settings.max_snapshot_bytes),
            max_snapshot_items=settings.max_snapshot_items,
            max_snapshot_bytes=settings.max_snapshot_bytes,
            max_watch_events=settings.watch_queue_size,
            max_watch_event_bytes=min(2 * 1024 * 1024, settings.max_snapshot_bytes),
            max_watch_bytes=settings.max_snapshot_bytes,
        )
        # Constructor validation prevents a malformed collector identity from
        # reaching logs or the first network request. This engine is discarded
        # without collecting; live engines always use the scope-bound
        # normalizer below so every Pod lookup supplies previous_request.
        KubernetesCollectionEngine(
            self._client,
            lambda _raw: None,
            collector_id=self._collector_id,
            limits=self._limits,
        )

    def _scope_configured(self, scope: InventoryScope) -> bool:
        if self._is_vm_collector:
            return (
                self._settings.vm_inventory_enabled
                and scope.source_cluster == self._settings.vm_stable_cluster_id
                and scope.api_resource == VMI_API_RESOURCE
                and not scope.cluster_scoped
                and scope.namespace == self._settings.vm_namespace
            )
        if self._is_vm_storage_collector:
            if scope.source_cluster != self._settings.vm_stable_cluster_id:
                return False
            if scope.api_resource == PVC_API_RESOURCE:
                return (
                    self._settings.vm_pvc_inventory_enabled
                    and not scope.cluster_scoped
                    and scope.namespace == self._settings.vm_namespace
                )
            if scope.api_resource == PV_API_RESOURCE:
                return (
                    self._settings.vm_pv_inventory_enabled
                    and self._settings.vm_pv_cluster_wide_rbac_acknowledged
                    and scope.cluster_scoped
                    and scope.namespace is None
                )
            return False
        if scope.source_cluster != self._settings.stable_cluster_id:
            return False
        if scope.api_resource == POD_API_RESOURCE:
            return (
                not scope.cluster_scoped
                and scope.namespace in self._settings.namespace_allowlist
            )
        if scope.api_resource == PVC_API_RESOURCE:
            return (
                self._settings.pvc_inventory_enabled
                and not scope.cluster_scoped
                and scope.namespace in self._settings.namespace_allowlist
            )
        if scope.api_resource == PV_API_RESOURCE:
            return (
                self._settings.pv_inventory_enabled
                and scope.cluster_scoped
                and scope.namespace is None
            )
        return False

    def _require_configured_scope(self, scope: InventoryScope) -> None:
        if not self._scope_configured(scope):
            raise CollectorConfigurationError(
                "inventory scope is not configured for this collector identity"
            )

    def _engine(
        self,
        scope: InventoryScope,
        *,
        normalized_requests: dict[str, PodEffectiveRequest],
        snapshot_id: UUID | None = None,
    ) -> KubernetesCollectionEngine:
        factory = (lambda: snapshot_id) if snapshot_id is not None else uuid4

        if scope.api_resource == POD_API_RESOURCE:

            def normalizer(raw: Any) -> Any:
                uid = _raw_pod_uid(raw)
                previous_request = (
                    self._effective_requests.get(scope, uid)
                    if uid is not None
                    else None
                )
                normalized = normalize_pod(raw, previous_request=previous_request)
                if (
                    normalized.valid_for_metering
                    and normalized.effective_request is not None
                ):
                    normalized_requests[normalized.uid] = normalized.effective_request
                else:
                    normalized_requests.pop(normalized.uid, None)
                return normalized

        elif scope.api_resource == PVC_API_RESOURCE:
            normalizer = normalize_pvc
        elif scope.api_resource == PV_API_RESOURCE:
            if self._volume_identity_key is None:
                raise CollectorConfigurationError(
                    "PV normalization requires its identity key"
                )

            def normalizer(raw: Any) -> Any:
                return normalize_pv(
                    raw,
                    source_cluster=scope.source_cluster,
                    identity_key=self._volume_identity_key,
                    identity_key_version=self._settings.volume_identity_key_version,
                )

        elif scope.api_resource == VMI_API_RESOURCE:
            normalizer = normalize_vmi

        else:
            raise CollectorConfigurationError(
                "unsupported infrastructure inventory API resource"
            )

        return KubernetesCollectionEngine(
            self._client,
            normalizer,
            collector_id=self._collector_id,
            limits=self._limits,
            snapshot_id_factory=factory,
        )

    def _scope_shadow_enabled(self, scope: InventoryScope) -> bool:
        if not self._scope_configured(scope):
            return False
        if scope.api_resource == POD_API_RESOURCE:
            return self._settings.shadow_enabled
        if scope.api_resource == PVC_API_RESOURCE:
            if self._is_vm_storage_collector:
                return self._settings.vm_pvc_shadow_enabled
            return self._settings.pvc_shadow_enabled
        if scope.api_resource == PV_API_RESOURCE:
            if self._is_vm_storage_collector:
                return self._settings.vm_pv_shadow_enabled
            return self._settings.pv_shadow_enabled
        if scope.api_resource == VMI_API_RESOURCE:
            return self._settings.vm_shadow_enabled
        return False

    def _snapshot_controller_identity(
        self, scope: InventoryScope
    ) -> tuple[str | None, int | None]:
        if scope.api_resource != VMI_API_RESOURCE:
            return None, None
        if self._controller_epoch is None:
            raise CollectorConfigurationError(
                "VM inventory requires a controller epoch"
            )
        key = (scope.source_cluster, scope.api_resource, scope.namespace)
        sequence = self._scope_sequences.get(key, -1) + 1
        self._scope_sequences[key] = sequence
        return self._controller_epoch, sequence

    async def _post_model(self, path: str, model: BaseModel) -> bytes:
        return await self._transport.post(path, _model_payload(model))

    async def _request_ticket(
        self,
        *,
        scope: InventoryScope,
        intent: str,
        snapshot_id: UUID | None = None,
        starting_resource_version: str | None = None,
        controller_epoch: str | None = None,
        sequence: int | None = None,
    ) -> InventoryTicketResponse:
        self._require_configured_scope(scope)
        request = _model_from_json(
            InventoryTicketRequest,
            {
                "scope": scope.to_wire(),
                "intent": intent,
                "snapshot_id": str(snapshot_id) if snapshot_id else None,
                "starting_resource_version": starting_resource_version,
                "controller_epoch": controller_epoch,
                "sequence": sequence,
            },
        )
        response = await self._post_model(TICKETS_PATH, request)
        try:
            ticket = InventoryTicketResponse.model_validate_json(response)
        except ValidationError as exc:
            raise IngestionTransportError("invalid-ticket-response") from exc
        if ticket.expires_at.tzinfo is None or ticket.expires_at.utcoffset() is None:
            raise IngestionTransportError("invalid-ticket-response")
        if ticket.expires_at <= datetime.now(timezone.utc):
            raise IngestionTransportError("invalid-ticket-response")
        if ticket.last_resource_version == "0":
            raise IngestionTransportError("invalid-ticket-response")
        return ticket

    async def collect_snapshot(self, scope: InventoryScope) -> InventorySnapshot:
        """LIST one exact scope, spool items, and phase them into ingestion."""

        snapshot_id = uuid4()
        controller_epoch, sequence = self._snapshot_controller_identity(scope)
        # Ticket acquisition precedes the Kubernetes LIST, fencing stale
        # collectors before they spend API-server or spool resources.
        ticket = await self._request_ticket(
            scope=scope,
            intent="snapshot",
            snapshot_id=snapshot_id,
            controller_epoch=controller_epoch,
            sequence=sequence,
        )
        with _SnapshotSpool(self._settings.max_snapshot_bytes) as spool:
            pod_scope = scope.api_resource == POD_API_RESOURCE
            normalized_requests: dict[str, PodEffectiveRequest] = {}
            valid_updates: dict[str, PodEffectiveRequest] = {}
            seen_uids: set[str] = set()

            async def stage_item(item: StagedInventoryItem) -> None:
                spool.append(item)
                seen_uids.add(item.uid)
                request = normalized_requests.pop(item.uid, None)
                if item.valid_for_metering and pod_scope:
                    if request is None:
                        raise CollectorRuntimeError("missing-effective-request")
                    valid_updates[item.uid] = request

            snapshot = await self._engine(
                scope,
                normalized_requests=normalized_requests,
                snapshot_id=snapshot_id,
            ).collect_list(
                scope,
                leader_generation=ticket.leader_generation,
                stage_item=stage_item,
                # Exact relists at the server-committed cursor close the
                # WATCH/LIST handoff without losing short-lived objects. The
                # first LIST of a new/recovery epoch has no prior cursor. In
                # collector-only mode there is no WATCH handoff to close, so
                # anchoring every periodic LIST would freeze inventory at one
                # historical state instead of observing current Kubernetes.
                resource_version=(
                    ticket.last_resource_version
                    if self._scope_shadow_enabled(scope)
                    else None
                ),
            )
            if controller_epoch is not None:
                snapshot = replace(
                    snapshot,
                    controller_epoch=controller_epoch,
                    sequence=sequence,
                )
            if pod_scope:
                self._effective_requests.reconcile_list(
                    scope,
                    seen_uids=seen_uids,
                    valid_updates=valid_updates,
                    complete=snapshot.complete,
                )
            if snapshot.item_count != spool.item_count:
                raise CollectorRuntimeError("snapshot-spool-count-mismatch")

            # A partial LIST is diagnostic metadata only. Uploading every safe
            # row observed before a repeated page/size/API failure would create
            # unbounded immutable staging churn without adding any absence or
            # interval authority. Keep the local positive request cache above,
            # but publish an intentionally empty manifest for this attempt.
            published_snapshot = (
                snapshot if snapshot.complete else replace(snapshot, item_count=0)
            )

            begin = _model_from_json(
                InventorySnapshotBegin,
                _snapshot_begin_wire(published_snapshot, ticket=ticket),
            )
            await self._post_model(SNAPSHOT_BEGIN_PATH, begin)

            batch_ordinal = 0
            if snapshot.complete:
                for items in spool.batches():
                    batch = InventorySnapshotItemBatch(
                        ticket_id=ticket.ticket_id,
                        ticket_token=ticket.ticket_token,
                        snapshot_id=snapshot.snapshot_id,
                        scope=begin.scope,
                        batch_ordinal=batch_ordinal,
                        items=items,
                    )
                    await self._post_model(SNAPSHOT_ITEMS_PATH, batch)
                    batch_ordinal += 1

            finalize = _model_from_json(
                InventorySnapshotFinalize,
                _snapshot_finalize_wire(
                    published_snapshot,
                    ticket=ticket,
                    shadow_enabled=self._scope_shadow_enabled(scope),
                ),
            )
            await self._post_model(SNAPSHOT_FINALIZE_PATH, finalize)
            return snapshot

    async def watch_once(
        self,
        scope: InventoryScope,
        *,
        resource_version: str,
        timeout_seconds: int,
    ) -> WatchOutcome:
        """Run one ticket-fenced, serial, atomically-published WATCH session."""

        ticket = await self._request_ticket(
            scope=scope,
            intent="watch-session",
            starting_resource_version=resource_version,
        )
        starting_cursor = ticket.last_resource_version or resource_version
        expected_cursor = starting_cursor
        pod_scope = scope.api_resource == POD_API_RESOURCE
        normalized_requests: dict[str, PodEffectiveRequest] = {}

        async def apply_observation(observation: WatchObservation) -> None:
            nonlocal expected_cursor
            request = _model_from_json(
                InventoryWatchApply,
                {
                    "ticket_id": str(ticket.ticket_id),
                    "ticket_token": ticket.ticket_token,
                    "leader_generation": ticket.leader_generation,
                    # Generated outside the HTTP transport retry loop: an
                    # ambiguous response is retried with the same durable
                    # idempotency identity and cursor precondition.
                    "event_id": str(uuid4()),
                    "expected_resource_version": expected_cursor,
                    "observation": observation.to_wire(),
                },
            )
            await self._post_model(WATCH_APPLY_PATH, request)
            if pod_scope and observation.item is not None:
                if observation.event_type == WatchEventType.DELETED:
                    self._effective_requests.remove(scope, observation.item.uid)
                    normalized_requests.pop(observation.item.uid, None)
                elif observation.item.valid_for_metering:
                    effective_request = normalized_requests.pop(
                        observation.item.uid, None
                    )
                    if effective_request is None:
                        raise CollectorRuntimeError("missing-effective-request")
                    self._effective_requests.put(
                        scope,
                        observation.item.uid,
                        effective_request,
                    )
            expected_cursor = observation.resource_version

        outcome = await self._engine(
            scope,
            normalized_requests=normalized_requests,
        ).watch(
            scope,
            resource_version=starting_cursor,
            apply_observation=apply_observation,
            timeout_seconds=timeout_seconds,
        )
        finish = _model_from_json(
            InventoryWatchFinish,
            _watch_finish_wire(outcome, ticket=ticket),
        )
        await self._post_model(WATCH_FINISH_PATH, finish)
        return outcome

    async def run_scope_cycle(
        self,
        scope: InventoryScope,
        *,
        stop: asyncio.Event,
        semaphore: asyncio.Semaphore | None = None,
    ) -> CollectorCycleResult:
        """Run one periodic LIST followed by short, reconnecting WATCH sessions."""

        if semaphore is None:
            snapshot = await self.collect_snapshot(scope)
        else:
            async with semaphore:
                snapshot = await self.collect_snapshot(scope)
        if not snapshot.complete:
            return CollectorCycleResult(False, True, None)

        cursor = snapshot.resource_version
        assert cursor is not None
        if not self._scope_shadow_enabled(scope):
            # Collector-only mode can inventory and seal snapshots, but WATCH
            # mutations require the shadow interval mutator.  Do not let a
            # collector gate alone mutate interval state.
            return CollectorCycleResult(True, True, cursor)
        deadline = self._monotonic() + self._settings.relist_interval_seconds
        while not stop.is_set():
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            timeout_seconds = max(
                1, min(MAX_WATCH_SESSION_SECONDS, math.ceil(remaining))
            )
            if semaphore is None:
                outcome = await self.watch_once(
                    scope,
                    resource_version=cursor,
                    timeout_seconds=timeout_seconds,
                )
            else:
                async with semaphore:
                    outcome = await self.watch_once(
                        scope,
                        resource_version=cursor,
                        timeout_seconds=timeout_seconds,
                    )
            cursor = outcome.committed_resource_version
            if outcome.relist_required:
                return CollectorCycleResult(True, True, cursor)
            # A broken or immediately-closed stream must not hot-loop against
            # ticket issuance and the API server.
            if outcome.processed_events == 0 and self._monotonic() < deadline:
                await _wait_or_stop(stop, min(0.25, deadline - self._monotonic()))

        return CollectorCycleResult(True, True, cursor)

    async def _scope_loop(
        self,
        scope: InventoryScope,
        *,
        stop: asyncio.Event,
        semaphore: asyncio.Semaphore,
    ) -> None:
        consecutive_failures = 0
        while not stop.is_set():
            try:
                result = await self.run_scope_cycle(
                    scope,
                    stop=stop,
                    semaphore=semaphore,
                )
                if result.snapshot_complete:
                    consecutive_failures = 0
                    if not self._scope_shadow_enabled(scope):
                        await _wait_or_stop(
                            stop,
                            float(self._settings.relist_interval_seconds),
                        )
                else:
                    consecutive_failures += 1
                    delay = min(
                        2 ** min(consecutive_failures - 1, 8),
                        float(min(self._settings.relist_interval_seconds, 300)),
                    )
                    await _wait_or_stop(stop, float(delay))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                # Namespace and exception type are safe bounded metadata; never
                # emit exception text because third-party clients may include a
                # raw response or request payload in it.
                logger.warning(
                    "infrastructure metering scope failed namespace=%s class=%s",
                    scope.namespace,
                    type(exc).__name__,
                )
                delay = min(
                    2 ** min(consecutive_failures - 1, 8),
                    float(min(self._settings.relist_interval_seconds, 300)),
                )
                await _wait_or_stop(stop, float(delay))

    async def run(self, stop: asyncio.Event) -> None:
        """Run every enabled exact resource scope with bounded concurrency."""

        semaphore = asyncio.Semaphore(self._settings.scope_concurrency)
        if self._is_vm_collector:
            scopes = [
                InventoryScope(
                    self._settings.vm_stable_cluster_id,
                    VMI_API_RESOURCE,
                    self._settings.vm_namespace,
                )
            ]
        elif self._is_vm_storage_collector:
            scopes = []
            if self._settings.vm_pvc_inventory_enabled:
                scopes.append(
                    InventoryScope(
                        self._settings.vm_stable_cluster_id,
                        PVC_API_RESOURCE,
                        self._settings.vm_namespace,
                    )
                )
            if self._settings.vm_pv_inventory_enabled:
                scopes.append(
                    InventoryScope(
                        self._settings.vm_stable_cluster_id,
                        PV_API_RESOURCE,
                        None,
                        cluster_scoped=True,
                    )
                )
        else:
            scopes = [
                InventoryScope(
                    self._settings.stable_cluster_id,
                    POD_API_RESOURCE,
                    namespace,
                )
                for namespace in self._settings.namespace_allowlist
            ]
        if (
            not self._is_vm_collector
            and not self._is_vm_storage_collector
            and self._settings.pvc_inventory_enabled
        ):
            scopes.extend(
                InventoryScope(
                    self._settings.stable_cluster_id,
                    PVC_API_RESOURCE,
                    namespace,
                )
                for namespace in self._settings.namespace_allowlist
            )
        if (
            not self._is_vm_collector
            and not self._is_vm_storage_collector
            and self._settings.pv_inventory_enabled
        ):
            scopes.append(
                InventoryScope(
                    self._settings.stable_cluster_id,
                    PV_API_RESOURCE,
                    None,
                    cluster_scoped=True,
                )
            )
        tasks = [
            asyncio.create_task(
                self._scope_loop(
                    scope,
                    stop=stop,
                    semaphore=semaphore,
                ),
                name=(
                    "infrastructure-metering-"
                    f"{scope.api_resource.rsplit('/', 1)[-1]}-"
                    f"{scope.namespace or 'cluster'}"
                ),
            )
            for scope in scopes
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> None:
    if delay <= 0 or stop.is_set():
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


def _load_kubernetes_client_module(deployment_mode: str) -> Any:
    """Load in-cluster auth, with kubeconfig fallback only when explicitly local."""

    try:
        from kubernetes import client as kubernetes_client
        from kubernetes import config as kubernetes_config
    except ImportError as exc:
        raise CollectorConfigurationError(
            "Kubernetes client dependency is unavailable"
        ) from exc

    try:
        kubernetes_config.load_incluster_config()
    except Exception as exc:
        if deployment_mode != "in-process":
            raise CollectorConfigurationError(
                "dedicated collector requires in-cluster Kubernetes auth"
            ) from exc
        try:
            kubernetes_config.load_kube_config()
        except Exception as fallback_exc:
            raise CollectorConfigurationError(
                "local collector could not load Kubernetes auth"
            ) from fallback_exc
    return kubernetes_client


def _load_kubernetes_core_api(deployment_mode: str) -> Any:
    """Compatibility helper for callers that only require core/v1."""

    return _load_kubernetes_client_module(deployment_mode).CoreV1Api()


def _load_kubernetes_apis(deployment_mode: str) -> tuple[Any, Any]:
    """Load the core and custom-object APIs under one auth decision."""

    kubernetes_client = _load_kubernetes_client_module(deployment_mode)
    return kubernetes_client.CoreV1Api(), kubernetes_client.CustomObjectsApi()


def _new_vm_controller_epoch(pod_uid: str) -> str:
    """Bind one remote authority epoch to both its Pod and process lifetime."""

    normalized = pod_uid.strip() if isinstance(pod_uid, str) else ""
    if (
        not normalized
        or len(normalized) > 128
        or any(character.isspace() for character in normalized)
    ):
        raise CollectorConfigurationError(
            "VM inventory requires a valid downward-API POD_UID"
        )
    return f"{normalized}:{uuid4()}"


def _build_runtime_from_env(
    settings: InfrastructureMeteringSettings,
) -> tuple[KubernetesPodCollectorRuntime, SignedIngestionHttpClient]:
    collector_id = os.environ.get(
        "INFRASTRUCTURE_METERING_COLLECTOR_ID", "kubernetes-pods"
    ).strip()
    orchestrator_url = os.environ.get(
        "INFRASTRUCTURE_METERING_ORCHESTRATOR_URL", ""
    ).strip()
    ingestion_key = os.environ.get("INFRASTRUCTURE_METERING_INGESTION_KEY", "")
    pv_inventory_enabled = (
        settings.vm_pv_inventory_enabled
        if collector_id == VM_STORAGE_COLLECTOR_ID
        else settings.pv_inventory_enabled
        if collector_id == PRIMARY_COLLECTOR_ID
        else False
    )
    http_transport = SignedIngestionHttpClient(
        base_url=orchestrator_url,
        collector_id=collector_id,
        ingestion_key=ingestion_key,
    )
    try:
        core_api, custom_objects_api = _load_kubernetes_apis(settings.deployment_mode)
        raw_client = RawKubernetesClient(
            core_api,
            custom_objects_api=custom_objects_api,
            max_page_bytes=min(8 * 1024 * 1024, settings.max_snapshot_bytes),
            max_watch_event_bytes=min(2 * 1024 * 1024, settings.max_snapshot_bytes),
        )
        runtime = KubernetesPodCollectorRuntime(
            settings=settings,
            collector_id=collector_id,
            kubernetes_client=raw_client,
            transport=http_transport,
            volume_identity_key=(
                os.environ.get("INFRASTRUCTURE_METERING_VOLUME_IDENTITY_KEY", "")
                if pv_inventory_enabled
                else None
            ),
            # A Pod UID alone survives a container restart while the in-memory
            # WATCH cursor/sequence does not.  Include a per-process nonce so
            # every restarted collector forces the server's recovery epoch and
            # records the resulting unknown range instead of reusing sequence 0
            # under an apparently unchanged authority.
            controller_epoch=(
                _new_vm_controller_epoch(os.environ.get("POD_UID", ""))
                if collector_id == VMI_COLLECTOR_ID
                else None
            ),
        )
    except Exception:
        # Construction happened before the async context can take ownership.
        # httpx has no sockets until first use, so no awaited cleanup is needed.
        raise
    return runtime, http_transport


async def _run_enabled_collector(
    settings: InfrastructureMeteringSettings,
) -> None:
    runtime, transport = _build_runtime_from_env(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    async with transport:
        await runtime.run(stop)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = InfrastructureMeteringSettings.from_env()
    except ValueError as exc:
        logger.error(
            "infrastructure metering collector configuration is invalid class=%s",
            type(exc).__name__,
        )
        return 2
    if not settings.collector_enabled:
        logger.info("infrastructure metering collector gate is disabled")
        return 0
    try:
        asyncio.run(_run_enabled_collector(settings))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error(
            "infrastructure metering collector stopped class=%s",
            type(exc).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the image command
    raise SystemExit(main())


__all__ = [
    "CollectorConfigurationError",
    "CollectorCycleResult",
    "CollectorRuntimeError",
    "IngestionTransportError",
    "KubernetesPodCollectorRuntime",
    "SignedIngestionHttpClient",
    "main",
]
