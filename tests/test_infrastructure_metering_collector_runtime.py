"""Focused tests for the database-free Pod collector process."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys
import types
from typing import Any
from uuid import UUID

import httpx
import pytest

from orchestrator.services.infrastructure_metering.collector_runtime import (
    SNAPSHOT_BEGIN_PATH,
    SNAPSHOT_FINALIZE_PATH,
    SNAPSHOT_ITEMS_PATH,
    TICKETS_PATH,
    WATCH_APPLY_PATH,
    WATCH_FINISH_PATH,
    PVC_API_RESOURCE,
    PV_API_RESOURCE,
    VM_STORAGE_COLLECTOR_ID,
    VMI_API_RESOURCE,
    CollectorConfigurationError,
    CollectorRuntimeError,
    IngestionTransportError,
    KubernetesPodCollectorRuntime,
    SignedIngestionHttpClient,
    _SnapshotSpool,
    _load_kubernetes_core_api,
    _new_vm_controller_epoch,
    _snapshot_begin_wire,
    _snapshot_finalize_wire,
    _watch_finish_wire,
    main,
)
from orchestrator.services.infrastructure_metering.collectors import (
    InventoryError,
    InventoryScope,
    InventorySnapshot,
    KubernetesListPage,
    KubernetesWatchEvent,
    StagedInventoryItem,
    WatchEventType,
    WatchOutcome,
)
from orchestrator.services.infrastructure_metering.collectors.contracts import (
    WatchGapReason,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    SNAPSHOT_BATCH_BODY_LIMIT,
    SNAPSHOT_METADATA_BODY_LIMIT,
    TICKET_BODY_LIMIT,
    WATCH_BODY_LIMIT,
    InventorySnapshotBegin,
    InventorySnapshotFinalize,
    InventoryTicketResponse,
    InventoryWatchFinish,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    volume_identity_key_fingerprint,
)
from orchestrator.services.infrastructure_metering.transport import (
    NONCE_HEADER,
    canonical_json_bytes,
    verify_transport_request,
)


SCOPE = InventoryScope("cluster-a", "core/v1/pods", "workers")
TICKET_TOKEN = "t" * 32


def test_vm_controller_epoch_changes_on_same_pod_process_restart() -> None:
    first = _new_vm_controller_epoch("collector-pod-uid")
    second = _new_vm_controller_epoch("collector-pod-uid")

    assert first.startswith("collector-pod-uid:")
    assert second.startswith("collector-pod-uid:")
    assert first != second
    with pytest.raises(CollectorConfigurationError, match="POD_UID"):
        _new_vm_controller_epoch("  ")


def _settings(
    *,
    namespaces: tuple[str, ...] = ("workers",),
    concurrency: int = 2,
    shadow: bool = True,
) -> InfrastructureMeteringSettings:
    return InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=shadow,
        stable_cluster_id="cluster-a",
        namespace_allowlist=namespaces,
        relist_interval_seconds=15,
        stale_after_seconds=30,
        list_page_size=500,
        scope_concurrency=concurrency,
        watch_queue_size=100,
        max_snapshot_items=1_000,
        max_snapshot_bytes=2 * 1024 * 1024,
    )


def _pod(
    uid: str,
    *,
    resource_version: str = "17",
    namespace: str = "workers",
    cpu_request: str = "250m",
    memory_request: str = "512Mi",
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"pod-{uid}",
            "namespace": namespace,
            "uid": uid,
            "resourceVersion": resource_version,
            "creationTimestamp": "2026-08-05T08:00:00Z",
            "labels": {"srw/component": "workspace"},
        },
        "spec": {
            "nodeName": "node-a",
            "containers": [
                {
                    "name": "app",
                    "env": [
                        {
                            "name": "NEVER_SERIALIZE",
                            "value": "raw-secret-marker",
                        }
                    ],
                    "resources": {
                        "requests": {
                            "cpu": cpu_request,
                            "memory": memory_request,
                        }
                    },
                }
            ],
        },
        "status": {
            "phase": "Running",
            "startTime": "2026-08-05T08:00:03Z",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "True",
                    "lastTransitionTime": "2026-08-05T08:00:02Z",
                }
            ],
        },
    }


def _vmi(*, resource_version: str = "21") -> dict[str, Any]:
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "metadata": {
            "name": "agent-vm-job-1",
            "namespace": "agent-vms",
            "uid": "vmi-uid-1",
            "resourceVersion": resource_version,
            "creationTimestamp": "2026-08-05T08:00:00Z",
            "labels": {
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": "00000000-0000-0000-0000-000000000001",
            },
        },
        "spec": {
            "domain": {
                "cpu": {"cores": 2, "sockets": 1, "threads": 1},
                "memory": {"guest": "4Gi"},
            }
        },
        "status": {"phase": "Running", "nodeName": "vm-node-a"},
    }


class _FakeKubernetesClient:
    def __init__(
        self,
        *,
        pages: dict[str | None, KubernetesListPage] | None = None,
        watch_events: list[KubernetesWatchEvent] | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.watch_events = watch_events or []
        self.trace = trace if trace is not None else []
        self.list_calls: list[dict[str, Any]] = []
        self.watch_calls: list[dict[str, Any]] = []

    async def list_resources(self, **kwargs: Any) -> KubernetesListPage:
        self.trace.append("kubernetes:list")
        self.list_calls.append(kwargs)
        result = self.pages[kwargs["continue_token"]]
        if isinstance(result, BaseException):
            raise result
        return result

    def watch_resources(self, **kwargs: Any) -> Any:
        self.trace.append("kubernetes:watch")
        self.watch_calls.append(kwargs)

        async def stream() -> Any:
            for event in self.watch_events:
                yield event

        return stream()


class _FakeIngestionTransport:
    def __init__(
        self,
        *,
        trace: list[str] | None = None,
        ticket_cursor: str | None = None,
    ) -> None:
        self.trace = trace if trace is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ticket_number = 0
        self.ticket_cursor = ticket_cursor

    async def post(self, path: str, payload: dict[str, Any]) -> bytes:
        copied = deepcopy(payload)
        self.calls.append((path, copied))
        self.trace.append(f"http:{path}")
        if path != TICKETS_PATH:
            return b"{}"
        self.ticket_number += 1
        ticket_id = UUID(int=self.ticket_number)
        response = {
            "ticket_id": str(ticket_id),
            "ticket_token": TICKET_TOKEN,
            "leader_generation": 9,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "last_resource_version": (
                self.ticket_cursor
                if self.ticket_cursor is not None
                else payload.get("starting_resource_version")
            ),
        }
        return canonical_json_bytes(response)


def _runtime(
    kubernetes_client: _FakeKubernetesClient,
    transport: _FakeIngestionTransport,
    *,
    settings: InfrastructureMeteringSettings | None = None,
) -> KubernetesPodCollectorRuntime:
    return KubernetesPodCollectorRuntime(
        settings=settings or _settings(),
        collector_id="kubernetes-pods",
        kubernetes_client=kubernetes_client,
        transport=transport,
    )


@pytest.mark.asyncio
async def test_snapshot_ticket_precedes_list_and_upload_is_phased_and_safe() -> None:
    trace: list[str] = []
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-a"), _pod("uid-b")],
                byte_count=2_000,
                resource_version="list-rv",
            )
        },
        trace=trace,
    )
    transport = _FakeIngestionTransport(trace=trace)

    snapshot = await _runtime(kubernetes_client, transport).collect_snapshot(SCOPE)

    assert snapshot.complete
    assert snapshot.item_count == 2
    assert trace[0:2] == [f"http:{TICKETS_PATH}", "kubernetes:list"]
    paths = [path for path, _payload in transport.calls]
    assert paths == [
        TICKETS_PATH,
        SNAPSHOT_BEGIN_PATH,
        SNAPSHOT_ITEMS_PATH,
        SNAPSHOT_FINALIZE_PATH,
    ]
    ticket_payload = transport.calls[0][1]
    begin = transport.calls[1][1]
    batch = transport.calls[2][1]
    finalize = transport.calls[3][1]
    assert ticket_payload["intent"] == "snapshot"
    assert ticket_payload["snapshot_id"] == begin["snapshot_id"]
    assert begin["complete"] is True
    assert begin["item_count"] == 2
    assert begin["items_streamed"] is True
    assert [item["uid"] for item in batch["items"]] == ["uid-a", "uid-b"]
    assert all(item["snapshot_id"] == begin["snapshot_id"] for item in batch["items"])
    assert finalize["complete"] is True
    assert finalize["resource_version"] == "list-rv"
    assert finalize["item_digest"] == begin["item_digest"]
    assert finalize["shadow_enabled"] is True
    assert "raw-secret-marker" not in json.dumps(transport.calls)


@pytest.mark.asyncio
async def test_vmi_snapshot_carries_controller_epoch_and_monotonic_sequence() -> None:
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        stable_cluster_id="vm-cluster",
        namespace_allowlist=("agent-vms",),
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
        relist_interval_seconds=15,
        stale_after_seconds=30,
        watch_queue_size=100,
        max_snapshot_items=1_000,
        max_snapshot_bytes=2 * 1024 * 1024,
    )
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_vmi()],
                byte_count=1_000,
                resource_version="vmi-list-rv",
            )
        }
    )
    transport = _FakeIngestionTransport()
    runtime = KubernetesPodCollectorRuntime(
        settings=settings,
        collector_id="kubevirt-vmis",
        kubernetes_client=kubernetes_client,
        transport=transport,
        controller_epoch="collector-pod-uid",
    )
    scope = InventoryScope("vm-cluster", VMI_API_RESOURCE, "agent-vms")

    first = await runtime.collect_snapshot(scope)
    await runtime.collect_snapshot(scope)

    assert first.controller_epoch == "collector-pod-uid"
    assert first.sequence == 0
    tickets = [payload for path, payload in transport.calls if path == TICKETS_PATH]
    begins = [
        payload for path, payload in transport.calls if path == SNAPSHOT_BEGIN_PATH
    ]
    assert [ticket["sequence"] for ticket in tickets] == [0, 1]
    assert all(ticket["controller_epoch"] == "collector-pod-uid" for ticket in tickets)
    assert [begin["sequence"] for begin in begins] == [0, 1]
    assert kubernetes_client.list_calls[0]["scope"].api_resource == VMI_API_RESOURCE


@pytest.mark.asyncio
async def test_remote_vm_server_config_does_not_replace_local_pod_scopes(
    monkeypatch,
) -> None:
    settings = replace(
        _settings(namespaces=("workers-a", "workers-b")),
        pvc_inventory_enabled=True,
        pv_inventory_enabled=True,
        volume_identity_key_version="test-v1",
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    runtime = KubernetesPodCollectorRuntime(
        settings=settings,
        collector_id="kubernetes-pods",
        kubernetes_client=_FakeKubernetesClient(),
        transport=_FakeIngestionTransport(),
        volume_identity_key=b"v" * 32,
    )
    visited: set[tuple[str, str | None]] = set()

    class _Incomplete:
        complete = False

    async def fake_collect(scope: InventoryScope) -> Any:
        visited.add((scope.api_resource, scope.namespace))
        return _Incomplete()

    monkeypatch.setattr(runtime, "collect_snapshot", fake_collect)
    stop = asyncio.Event()

    async def stop_later() -> None:
        await asyncio.sleep(0.01)
        stop.set()

    await asyncio.gather(runtime.run(stop), stop_later())

    assert visited == {
        ("core/v1/pods", "workers-a"),
        ("core/v1/pods", "workers-b"),
        ("core/v1/persistentvolumeclaims", "workers-a"),
        ("core/v1/persistentvolumeclaims", "workers-b"),
        ("core/v1/persistentvolumes", None),
    }


@pytest.mark.asyncio
async def test_vm_storage_collector_owns_only_exact_remote_pvc_and_pv_scopes(
    monkeypatch,
) -> None:
    settings = replace(
        _settings(namespaces=("workers-a", "workers-b")),
        pvc_inventory_enabled=True,
        vm_pvc_inventory_enabled=True,
        vm_pv_inventory_enabled=True,
        vm_pvc_shadow_enabled=True,
        vm_pv_shadow_enabled=True,
        vm_pv_cluster_wide_rbac_acknowledged=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
        volume_identity_key_version="storage-v1",
    )
    runtime = KubernetesPodCollectorRuntime(
        settings=settings,
        collector_id=VM_STORAGE_COLLECTOR_ID,
        kubernetes_client=_FakeKubernetesClient(),
        transport=_FakeIngestionTransport(),
        volume_identity_key=b"i" * 32,
    )
    visited: set[tuple[str, str | None, bool, str]] = set()

    class _Incomplete:
        complete = False

    async def fake_collect(scope: InventoryScope) -> Any:
        visited.add(
            (
                scope.api_resource,
                scope.namespace,
                scope.cluster_scoped,
                scope.source_cluster,
            )
        )
        return _Incomplete()

    monkeypatch.setattr(runtime, "collect_snapshot", fake_collect)
    stop = asyncio.Event()

    async def stop_later() -> None:
        await asyncio.sleep(0.01)
        stop.set()

    await asyncio.gather(runtime.run(stop), stop_later())

    assert visited == {
        (PVC_API_RESOURCE, "agent-vms", False, "vm-cluster"),
        (PV_API_RESOURCE, None, True, "vm-cluster"),
    }
    pvc_scope = InventoryScope("vm-cluster", PVC_API_RESOURCE, "agent-vms")
    pv_scope = InventoryScope("vm-cluster", PV_API_RESOURCE, None, cluster_scoped=True)
    assert runtime._scope_shadow_enabled(pvc_scope) is True
    assert runtime._scope_shadow_enabled(pv_scope) is True
    assert runtime._snapshot_controller_identity(pvc_scope) == (None, None)
    assert runtime._snapshot_controller_identity(pv_scope) == (None, None)
    assert (
        runtime._scope_configured(
            InventoryScope("vm-cluster", PVC_API_RESOURCE, "other")
        )
        is False
    )
    assert (
        runtime._scope_configured(
            InventoryScope("vm-cluster", VMI_API_RESOURCE, "agent-vms")
        )
        is False
    )


def test_vm_storage_collector_requires_its_gates_and_only_pv_requires_identity_key():
    pvc_settings = replace(
        _settings(),
        vm_pvc_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    runtime = KubernetesPodCollectorRuntime(
        settings=pvc_settings,
        collector_id=VM_STORAGE_COLLECTOR_ID,
        kubernetes_client=_FakeKubernetesClient(),
        transport=_FakeIngestionTransport(),
    )
    assert runtime._volume_identity_key is None

    pv_settings = replace(
        pvc_settings,
        vm_pv_inventory_enabled=True,
        vm_pv_cluster_wide_rbac_acknowledged=True,
        volume_identity_key_version="storage-v1",
    )
    with pytest.raises(CollectorConfigurationError, match="volume identity key"):
        KubernetesPodCollectorRuntime(
            settings=pv_settings,
            collector_id=VM_STORAGE_COLLECTOR_ID,
            kubernetes_client=_FakeKubernetesClient(),
            transport=_FakeIngestionTransport(),
        )
    with pytest.raises(CollectorConfigurationError, match="only the VMI collector"):
        KubernetesPodCollectorRuntime(
            settings=pvc_settings,
            collector_id=VM_STORAGE_COLLECTOR_ID,
            kubernetes_client=_FakeKubernetesClient(),
            transport=_FakeIngestionTransport(),
            controller_epoch="must-not-exist",
        )
    with pytest.raises(CollectorConfigurationError, match="identity is unsupported"):
        KubernetesPodCollectorRuntime(
            settings=pvc_settings,
            collector_id="typo-storage",
            kubernetes_client=_FakeKubernetesClient(),
            transport=_FakeIngestionTransport(),
        )


@pytest.mark.asyncio
async def test_vm_storage_pv_uses_volume_identity_key_not_ingestion_hmac() -> None:
    identity_key = b"volume-identity-key-material-32b!!"
    raw_handle = "provider-private-volume-handle"
    settings = replace(
        _settings(shadow=False),
        vm_pv_inventory_enabled=True,
        vm_pv_cluster_wide_rbac_acknowledged=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
        volume_identity_key_version="storage-v1",
    )
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[
                    {
                        "apiVersion": "v1",
                        "kind": "PersistentVolume",
                        "metadata": {
                            "uid": "pv-kubernetes-uid",
                            "name": "pv-a",
                            "resourceVersion": "21",
                        },
                        "spec": {
                            "capacity": {"storage": "8Gi"},
                            "persistentVolumeReclaimPolicy": "Retain",
                            "csi": {
                                "driver": "csi.example.test",
                                "volumeHandle": raw_handle,
                            },
                        },
                    }
                ],
                byte_count=1_000,
                resource_version="pv-list-rv",
            )
        }
    )
    transport = _FakeIngestionTransport()
    runtime = KubernetesPodCollectorRuntime(
        settings=settings,
        collector_id=VM_STORAGE_COLLECTOR_ID,
        kubernetes_client=kubernetes_client,
        transport=transport,
        volume_identity_key=identity_key,
    )

    await runtime.collect_snapshot(
        InventoryScope("vm-cluster", PV_API_RESOURCE, None, cluster_scoped=True)
    )

    batch = next(
        payload for path, payload in transport.calls if path == SNAPSHOT_ITEMS_PATH
    )
    normalized = batch["items"][0]["normalized"]
    identity = normalized["volume_identity"]
    assert identity["key_version"] == "storage-v1"
    assert identity["key_fingerprint"] == volume_identity_key_fingerprint(identity_key)
    assert raw_handle not in json.dumps(transport.calls)
    ticket = next(payload for path, payload in transport.calls if path == TICKETS_PATH)
    assert ticket["controller_epoch"] is None
    assert ticket["sequence"] is None


def test_vmi_collector_identity_requires_vm_gate_and_process_epoch() -> None:
    with pytest.raises(CollectorConfigurationError, match="requires VM inventory"):
        KubernetesPodCollectorRuntime(
            settings=_settings(),
            collector_id="kubevirt-vmis",
            kubernetes_client=_FakeKubernetesClient(),
            transport=_FakeIngestionTransport(),
            controller_epoch="pod:process",
        )
    with pytest.raises(CollectorConfigurationError, match="controller epoch"):
        KubernetesPodCollectorRuntime(
            settings=replace(
                _settings(),
                vm_inventory_enabled=True,
                vm_stable_cluster_id="vm-cluster",
                vm_namespace="agent-vms",
            ),
            collector_id="kubevirt-vmis",
            kubernetes_client=_FakeKubernetesClient(),
            transport=_FakeIngestionTransport(),
        )


@pytest.mark.asyncio
async def test_incomplete_list_is_finalized_without_cursor_or_absence_proof() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-partial")],
                byte_count=100,
                resource_version="list-rv",
                continue_token="next",
            ),
            "next": KubernetesListPage(
                items=[],
                byte_count=100,
                resource_version=None,
            ),
        }
    )
    transport = _FakeIngestionTransport()

    snapshot = await _runtime(kubernetes_client, transport).collect_snapshot(SCOPE)

    assert not snapshot.complete
    assert snapshot.item_count == 1
    paths = [path for path, _payload in transport.calls]
    assert paths == [TICKETS_PATH, SNAPSHOT_BEGIN_PATH, SNAPSHOT_FINALIZE_PATH]
    begin = transport.calls[1][1]
    finalize = transport.calls[2][1]
    assert begin["complete"] is False
    assert begin["item_count"] == 0
    assert begin["resource_version"] is None
    assert begin["item_digest"] is None
    assert finalize["complete"] is False
    assert finalize["item_count"] == 0
    assert finalize["fatal_errors"][0]["error_class"] == "resource-version"


@pytest.mark.asyncio
async def test_collector_only_mode_lists_but_does_not_start_watch() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-a")],
                byte_count=1_000,
                resource_version="list-rv",
            )
        },
        watch_events=[
            KubernetesWatchEvent(
                event_type=WatchEventType.MODIFIED,
                resource_version="18",
                byte_count=100,
                raw_object=_pod("uid-a", resource_version="18"),
            )
        ],
    )
    transport = _FakeIngestionTransport()
    runtime = _runtime(
        kubernetes_client,
        transport,
        settings=_settings(shadow=False),
    )

    result = await runtime.run_scope_cycle(SCOPE, stop=asyncio.Event())

    assert result.snapshot_complete
    assert result.resource_version == "list-rv"
    assert kubernetes_client.watch_calls == []
    assert not any(
        path in {WATCH_APPLY_PATH, WATCH_FINISH_PATH}
        for path, _payload in transport.calls
    )
    assert transport.calls[-1][1]["shadow_enabled"] is False


@pytest.mark.asyncio
async def test_collector_only_list_ignores_historical_ticket_cursor() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-current")],
                byte_count=1_000,
                resource_version="current-rv",
            )
        }
    )
    transport = _FakeIngestionTransport(ticket_cursor="historical-rv")
    runtime = _runtime(
        kubernetes_client,
        transport,
        settings=_settings(shadow=False),
    )

    snapshot = await runtime.collect_snapshot(SCOPE)

    assert snapshot.complete
    assert snapshot.resource_version == "current-rv"
    assert kubernetes_client.list_calls[0]["resource_version"] is None


@pytest.mark.asyncio
async def test_shadow_list_retains_exact_watch_handoff_cursor() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-a")],
                byte_count=1_000,
                resource_version="exact-rv",
            )
        }
    )
    runtime = _runtime(
        kubernetes_client,
        _FakeIngestionTransport(ticket_cursor="exact-rv"),
    )

    snapshot = await runtime.collect_snapshot(SCOPE)

    assert snapshot.complete
    assert kubernetes_client.list_calls[0]["resource_version"] == "exact-rv"


@pytest.mark.asyncio
async def test_watch_applies_events_serially_with_cursor_fences_and_finishes() -> None:
    kubernetes_client = _FakeKubernetesClient(
        watch_events=[
            KubernetesWatchEvent(
                event_type=WatchEventType.MODIFIED,
                resource_version="18",
                byte_count=321,
                raw_object=_pod("uid-a", resource_version="18"),
            ),
            KubernetesWatchEvent(
                event_type=WatchEventType.BOOKMARK,
                resource_version="19",
                byte_count=17,
                raw_object=None,
            ),
        ]
    )
    transport = _FakeIngestionTransport()

    outcome = await _runtime(kubernetes_client, transport).watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    assert outcome.committed_resource_version == "19"
    assert outcome.object_events == 1
    assert outcome.bookmarks == 1
    assert kubernetes_client.watch_calls[0]["resource_version"] == "17"
    applies = [payload for path, payload in transport.calls if path == WATCH_APPLY_PATH]
    assert [item["expected_resource_version"] for item in applies] == ["17", "18"]
    assert len({item["event_id"] for item in applies}) == 2
    assert [item["observation"]["source_event_bytes"] for item in applies] == [
        321,
        17,
    ]
    finish = [
        payload for path, payload in transport.calls if path == WATCH_FINISH_PATH
    ][0]
    assert finish["starting_resource_version"] == "17"
    assert finish["committed_resource_version"] == "19"
    assert finish["history_lost"] is False
    assert finish["gap_reason"] is None
    assert finish["ambiguous_resource_version"] is None
    assert finish["history_event_id"] is None


@pytest.mark.asyncio
async def test_watch_410_records_idempotent_history_gap_and_forces_relist() -> None:
    kubernetes_client = _FakeKubernetesClient(
        watch_events=[
            KubernetesWatchEvent(
                event_type=WatchEventType.ERROR,
                resource_version=None,
                byte_count=40,
                raw_object={},
                status_code=410,
            )
        ]
    )
    transport = _FakeIngestionTransport()

    outcome = await _runtime(kubernetes_client, transport).watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    assert outcome.history_lost
    assert outcome.relist_required
    assert outcome.gap_reason == WatchGapReason.RESOURCE_VERSION_EXPIRED
    assert not any(path == WATCH_APPLY_PATH for path, _payload in transport.calls)
    finish = [
        payload for path, payload in transport.calls if path == WATCH_FINISH_PATH
    ][0]
    assert finish["history_event_id"] is not None
    assert finish["gap_reason"] == "resource-version-expired"
    assert finish["ambiguous_resource_version"] is None
    assert finish["fatal_errors"][0]["error_class"] == "resource-version-expired"


@pytest.mark.asyncio
async def test_watch_apply_uncertainty_finishes_with_attempted_cursor() -> None:
    class _FailingApplyTransport(_FakeIngestionTransport):
        async def post(self, path: str, payload: dict[str, Any]) -> bytes:
            if path == WATCH_APPLY_PATH:
                self.calls.append((path, deepcopy(payload)))
                self.trace.append(f"http:{path}")
                raise CollectorRuntimeError("ambiguous-ingestion-response")
            return await super().post(path, payload)

    kubernetes_client = _FakeKubernetesClient(
        watch_events=[
            KubernetesWatchEvent(
                event_type=WatchEventType.MODIFIED,
                resource_version="18",
                byte_count=321,
                raw_object=_pod("uid-a", resource_version="18"),
            )
        ]
    )
    transport = _FailingApplyTransport()

    outcome = await _runtime(kubernetes_client, transport).watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    assert outcome.committed_resource_version == "17"
    assert outcome.history_lost
    assert outcome.gap_reason == WatchGapReason.AMBIGUOUS_APPLY
    assert outcome.ambiguous_resource_version == "18"
    finish = next(
        payload for path, payload in transport.calls if path == WATCH_FINISH_PATH
    )
    assert finish["gap_reason"] == "ambiguous-watch-apply"
    assert finish["ambiguous_resource_version"] == "18"
    assert finish["history_event_id"] is not None


@pytest.mark.asyncio
async def test_runtime_holds_1000m_during_incomplete_resize_to_500m() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[
                    _pod(
                        "uid-a",
                        cpu_request="1000m",
                        memory_request="1Gi",
                    )
                ],
                byte_count=1_000,
                resource_version="17",
            )
        }
    )
    transport = _FakeIngestionTransport()
    runtime = _runtime(kubernetes_client, transport)
    await runtime.collect_snapshot(SCOPE)

    resized = _pod(
        "uid-a",
        resource_version="18",
        cpu_request="500m",
        memory_request="1Gi",
    )
    resized["status"]["conditions"].append(
        {
            "type": "PodResizePending",
            "status": "True",
            "reason": "Deferred",
        }
    )
    kubernetes_client.watch_events = [
        KubernetesWatchEvent(
            event_type=WatchEventType.MODIFIED,
            resource_version="18",
            byte_count=321,
            raw_object=resized,
        )
    ]

    outcome = await runtime.watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    assert outcome.committed_resource_version == "18"
    apply = next(
        payload for path, payload in transport.calls if path == WATCH_APPLY_PATH
    )
    capacity = apply["observation"]["item"]["normalized"]["capacity"]
    assert capacity["cpu_millicores"] == 1_000
    assert capacity["capacity_quality"] == "resize-status-unavailable"
    cached = runtime._effective_requests.peek(SCOPE, "uid-a")
    assert cached is not None
    assert cached.cpu_millicores == 1_000


@pytest.mark.asyncio
async def test_runtime_without_resize_history_fails_closed() -> None:
    resized = _pod(
        "uid-a",
        resource_version="18",
        cpu_request="500m",
        memory_request="1Gi",
    )
    resized["status"]["conditions"].append(
        {
            "type": "PodResizePending",
            "status": "True",
            "reason": "Deferred",
        }
    )
    kubernetes_client = _FakeKubernetesClient(
        watch_events=[
            KubernetesWatchEvent(
                event_type=WatchEventType.MODIFIED,
                resource_version="18",
                byte_count=321,
                raw_object=resized,
            )
        ]
    )
    transport = _FakeIngestionTransport()
    runtime = _runtime(kubernetes_client, transport)

    await runtime.watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    apply = next(
        payload for path, payload in transport.calls if path == WATCH_APPLY_PATH
    )
    item = apply["observation"]["item"]
    assert item["valid_for_metering"] is False
    assert item["normalized"]["capacity"] is None
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is None


@pytest.mark.asyncio
async def test_complete_list_prunes_absent_cache_but_incomplete_list_does_not() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-a")],
                byte_count=1_000,
                resource_version="17",
            )
        }
    )
    runtime = _runtime(kubernetes_client, _FakeIngestionTransport())
    await runtime.collect_snapshot(SCOPE)
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is not None

    kubernetes_client.pages[None] = KubernetesListPage(
        items=[],
        byte_count=100,
        resource_version=None,
    )
    incomplete = await runtime.collect_snapshot(SCOPE)
    assert not incomplete.complete
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is not None

    kubernetes_client.pages[None] = KubernetesListPage(
        items=[],
        byte_count=100,
        resource_version="19",
    )
    complete = await runtime.collect_snapshot(SCOPE)
    assert complete.complete
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is None


@pytest.mark.asyncio
async def test_committed_delete_removes_cached_effective_request() -> None:
    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-a")],
                byte_count=1_000,
                resource_version="17",
            )
        }
    )
    runtime = _runtime(kubernetes_client, _FakeIngestionTransport())
    await runtime.collect_snapshot(SCOPE)
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is not None
    kubernetes_client.watch_events = [
        KubernetesWatchEvent(
            event_type=WatchEventType.DELETED,
            resource_version="18",
            byte_count=321,
            raw_object=_pod("uid-a", resource_version="18"),
        )
    ]

    outcome = await runtime.watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    assert outcome.committed_resource_version == "18"
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is None


@pytest.mark.asyncio
async def test_uncommitted_delete_keeps_cached_effective_request() -> None:
    class _ToggleApplyFailureTransport(_FakeIngestionTransport):
        fail_apply = False

        async def post(self, path: str, payload: dict[str, Any]) -> bytes:
            if path == WATCH_APPLY_PATH and self.fail_apply:
                self.calls.append((path, deepcopy(payload)))
                raise CollectorRuntimeError("ambiguous-ingestion-response")
            return await super().post(path, payload)

    kubernetes_client = _FakeKubernetesClient(
        pages={
            None: KubernetesListPage(
                items=[_pod("uid-a")],
                byte_count=1_000,
                resource_version="17",
            )
        }
    )
    transport = _ToggleApplyFailureTransport()
    runtime = _runtime(kubernetes_client, transport)
    await runtime.collect_snapshot(SCOPE)
    transport.fail_apply = True
    kubernetes_client.watch_events = [
        KubernetesWatchEvent(
            event_type=WatchEventType.DELETED,
            resource_version="18",
            byte_count=321,
            raw_object=_pod("uid-a", resource_version="18"),
        )
    ]

    outcome = await runtime.watch_once(
        SCOPE,
        resource_version="17",
        timeout_seconds=5,
    )

    assert outcome.committed_resource_version == "17"
    assert outcome.gap_reason == WatchGapReason.AMBIGUOUS_APPLY
    assert runtime._effective_requests.peek(SCOPE, "uid-a") is not None


@pytest.mark.asyncio
async def test_effective_request_cache_is_bounded_and_scope_isolated() -> None:
    settings = replace(
        _settings(namespaces=("workers", "other")),
        max_snapshot_items=2,
    )
    kubernetes_client = _FakeKubernetesClient(
        watch_events=[
            KubernetesWatchEvent(
                event_type=WatchEventType.ADDED,
                resource_version=str(index),
                byte_count=100,
                raw_object=_pod(f"uid-{index}", resource_version=str(index)),
            )
            for index in range(1, 4)
        ]
    )
    runtime = _runtime(
        kubernetes_client,
        _FakeIngestionTransport(),
        settings=settings,
    )
    await runtime.watch_once(
        SCOPE,
        resource_version="old",
        timeout_seconds=5,
    )

    assert runtime._effective_requests.count(SCOPE) == 2
    assert runtime._effective_requests.peek(SCOPE, "uid-1") is None
    assert runtime._effective_requests.peek(SCOPE, "uid-2") is not None
    assert runtime._effective_requests.peek(SCOPE, "uid-3") is not None

    other_scope = InventoryScope("cluster-a", "core/v1/pods", "other")
    kubernetes_client.watch_events = [
        KubernetesWatchEvent(
            event_type=WatchEventType.ADDED,
            resource_version="other-1",
            byte_count=100,
            raw_object=_pod(
                "uid-2",
                namespace="other",
                resource_version="other-1",
                cpu_request="1000m",
            ),
        )
    ]
    await runtime.watch_once(
        other_scope,
        resource_version="old",
        timeout_seconds=5,
    )

    assert runtime._effective_requests.count(SCOPE) == 2
    assert runtime._effective_requests.count(other_scope) == 1
    worker_request = runtime._effective_requests.peek(SCOPE, "uid-2")
    other_request = runtime._effective_requests.peek(other_scope, "uid-2")
    assert worker_request is not None and worker_request.cpu_millicores == 250
    assert other_request is not None and other_request.cpu_millicores == 1_000


def test_snapshot_spool_batches_at_500_items_and_enforces_byte_bound() -> None:
    snapshot_id = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    with _SnapshotSpool(1024 * 1024) as spool:
        for index in range(501):
            spool.append(
                StagedInventoryItem(
                    scope=SCOPE,
                    snapshot_id=snapshot_id,
                    kind="pod",
                    uid=f"uid-{index}",
                    revision_hash=None,
                    valid_for_metering=False,
                    normalized={},
                )
            )
        assert [len(batch) for batch in spool.batches()] == [500, 1]

    with _SnapshotSpool(10) as spool:
        with pytest.raises(
            CollectorRuntimeError, match="normalized-snapshot-too-large"
        ):
            spool.append(
                StagedInventoryItem(
                    scope=SCOPE,
                    snapshot_id=snapshot_id,
                    kind="pod",
                    uid="uid-too-large",
                    revision_hash=None,
                    valid_for_metering=False,
                    normalized={},
                )
            )


def test_snapshot_begin_deterministically_caps_diagnostic_item_errors() -> None:
    now = datetime.now(timezone.utc)
    errors = tuple(
        InventoryError(
            error_class="item-normalization",
            scope=SCOPE,
            message="An identifiable object is invalid for metering",
            kind="pod",
            uid=f"uid-{index:04d}",
        )
        for index in reversed(range(2_001))
    )
    snapshot = InventorySnapshot(
        collector_id="kubernetes-pods",
        scope=SCOPE,
        collection_started_at=now,
        collection_completed_at=now,
        complete=False,
        snapshot_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        leader_generation=9,
        resource_version=None,
        item_count=2_001,
        item_digest=None,
        pages_read=5,
        bytes_read=10_000,
        item_errors=errors,
    )
    ticket = InventoryTicketResponse(
        ticket_id=UUID(int=1),
        ticket_token=TICKET_TOKEN,
        leader_generation=9,
        expires_at=now + timedelta(minutes=10),
    )

    payload = _snapshot_begin_wire(snapshot, ticket=ticket)

    assert len(payload["item_errors"]) == 2_000
    assert payload["item_errors"][0]["uid"] == "uid-0000"
    assert payload["item_errors"][-1]["uid"] == "uid-1999"


def test_diagnostic_envelopes_are_deterministic_and_byte_bounded() -> None:
    now = datetime.now(timezone.utc)
    # Multi-byte values exercise encoded-byte accounting rather than character
    # or item-count approximations. At the model count caps these diagnostics
    # exceed the 2 MiB route budget and therefore must be prefix-truncated.
    errors = tuple(
        InventoryError(
            error_class="fatal-normalization",
            scope=SCOPE,
            message="💾" * 512,
            kind="k" * 128,
            uid=f"{index:04d}" + "ü" * 252,
        )
        for index in reversed(range(2_001))
    )
    snapshot = InventorySnapshot(
        collector_id="kubernetes-pods",
        scope=SCOPE,
        collection_started_at=now,
        collection_completed_at=now,
        complete=False,
        snapshot_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        leader_generation=9,
        resource_version=None,
        item_count=2_001,
        item_digest=None,
        pages_read=5,
        bytes_read=10_000,
        fatal_errors=errors,
        item_errors=errors,
    )
    ticket = InventoryTicketResponse(
        ticket_id=UUID(int=1),
        ticket_token=TICKET_TOKEN,
        leader_generation=9,
        expires_at=now + timedelta(minutes=10),
    )

    begin = _snapshot_begin_wire(snapshot, ticket=ticket)
    repeated_begin = _snapshot_begin_wire(snapshot, ticket=ticket)
    assert begin["fatal_errors"] == repeated_begin["fatal_errors"]
    assert begin["item_errors"] == repeated_begin["item_errors"]
    assert begin["fatal_errors"][0]["uid"].startswith("0000")
    assert len(begin["fatal_errors"]) < 1_000
    assert len(canonical_json_bytes(begin)) <= SNAPSHOT_METADATA_BODY_LIMIT
    InventorySnapshotBegin.model_validate_json(canonical_json_bytes(begin))

    item_only_begin = _snapshot_begin_wire(
        replace(snapshot, fatal_errors=()),
        ticket=ticket,
    )
    assert 0 < len(item_only_begin["item_errors"]) < 2_000
    assert item_only_begin["item_errors"][0]["uid"].startswith("0000")
    assert len(canonical_json_bytes(item_only_begin)) <= SNAPSHOT_METADATA_BODY_LIMIT
    InventorySnapshotBegin.model_validate_json(canonical_json_bytes(item_only_begin))

    finalize = _snapshot_finalize_wire(
        snapshot,
        ticket=ticket,
        shadow_enabled=True,
    )
    assert finalize["fatal_errors"][0]["uid"].startswith("0000")
    assert len(finalize["fatal_errors"]) < 1_000
    assert len(canonical_json_bytes(finalize)) <= SNAPSHOT_METADATA_BODY_LIMIT
    InventorySnapshotFinalize.model_validate_json(canonical_json_bytes(finalize))

    outcome = WatchOutcome(
        collector_id="kubernetes-pods",
        scope=SCOPE,
        started_at=now,
        completed_at=now,
        starting_resource_version="rv-1",
        committed_resource_version="rv-1",
        processed_events=0,
        object_events=0,
        bookmarks=0,
        bytes_read=0,
        reconnect_required=True,
        relist_required=True,
        history_lost=True,
        limit_reached=False,
        gap_reason=WatchGapReason.RESOURCE_VERSION_EXPIRED,
        fatal_errors=errors,
        item_errors=errors,
    )
    finish = _watch_finish_wire(outcome, ticket=ticket)
    assert finish["fatal_errors"][0]["uid"].startswith("0000")
    assert len(finish["fatal_errors"]) < 1_000
    assert len(canonical_json_bytes(finish)) <= WATCH_BODY_LIMIT
    InventoryWatchFinish.model_validate_json(canonical_json_bytes(finish))

    item_only_finish = _watch_finish_wire(
        replace(outcome, fatal_errors=()),
        ticket=ticket,
    )
    assert 0 < len(item_only_finish["item_errors"]) < 2_000
    assert item_only_finish["item_errors"][0]["uid"].startswith("0000")
    assert len(canonical_json_bytes(item_only_finish)) <= WATCH_BODY_LIMIT
    InventoryWatchFinish.model_validate_json(canonical_json_bytes(item_only_finish))


@pytest.mark.asyncio
async def test_signed_http_retry_reuses_body_but_rotates_transport_nonce() -> None:
    attempts: list[tuple[bytes, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        verify_transport_request(
            method="POST",
            path=request.url.path,
            headers=request.headers,
            body=body,
            key="k" * 32,
        )
        attempts.append((body, request.headers[NONCE_HEADER]))
        if len(attempts) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"{}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    transport = SignedIngestionHttpClient(
        base_url="http://orchestrator:8085",
        collector_id="kubernetes-pods",
        ingestion_key="k" * 32,
        client=http_client,
        sleep=sleep,
    )
    try:
        response = await transport.post(TICKETS_PATH, {"bounded": True})
    finally:
        await http_client.aclose()

    assert response == b"{}"
    assert attempts[0][0] == attempts[1][0]
    assert attempts[0][1] != attempts[1][1]
    assert delays == [0.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "maximum_bytes"),
    [
        (TICKETS_PATH, TICKET_BODY_LIMIT),
        (SNAPSHOT_BEGIN_PATH, SNAPSHOT_METADATA_BODY_LIMIT),
        (SNAPSHOT_ITEMS_PATH, SNAPSHOT_BATCH_BODY_LIMIT),
        (SNAPSHOT_FINALIZE_PATH, SNAPSHOT_METADATA_BODY_LIMIT),
        (WATCH_APPLY_PATH, WATCH_BODY_LIMIT),
        (WATCH_FINISH_PATH, WATCH_BODY_LIMIT),
    ],
)
async def test_signed_http_rejects_route_oversize_before_signing(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    maximum_bytes: int,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"{}")

    def unexpected_sign(**_kwargs: Any) -> dict[str, str]:
        raise AssertionError("oversized request must not be signed")

    monkeypatch.setattr(
        "orchestrator.services.infrastructure_metering.collector_runtime."
        "sign_transport_request",
        unexpected_sign,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = SignedIngestionHttpClient(
        base_url="http://orchestrator:8085",
        collector_id="kubernetes-pods",
        ingestion_key="k" * 32,
        client=http_client,
    )
    try:
        with pytest.raises(IngestionTransportError, match="request-too-large"):
            await transport.post(path, {"padding": "x" * maximum_bytes})
    finally:
        await http_client.aclose()

    assert requests == 0


@pytest.mark.asyncio
async def test_namespace_operations_respect_configured_concurrency(monkeypatch) -> None:
    settings = _settings(
        namespaces=("workers-a", "workers-b", "workers-c"),
        concurrency=2,
    )
    runtime = _runtime(
        _FakeKubernetesClient(),
        _FakeIngestionTransport(),
        settings=settings,
    )
    active = 0
    maximum_active = 0
    visited: set[str] = set()

    class _Incomplete:
        complete = False

    async def fake_collect(scope: InventoryScope) -> Any:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        visited.add(str(scope.namespace))
        await asyncio.sleep(0.01)
        active -= 1
        return _Incomplete()

    monkeypatch.setattr(runtime, "collect_snapshot", fake_collect)
    stop = asyncio.Event()

    async def stop_later() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(runtime.run(stop), stop_later())

    assert maximum_active == 2
    assert visited == {"workers-a", "workers-b", "workers-c"}


def test_kubeconfig_fallback_requires_explicit_in_process_mode(monkeypatch) -> None:
    # Stand up our own ``kubernetes`` modules instead of patching the installed
    # client: other test modules stub the package in ``sys.modules``, so whatever
    # the loader imports at runtime depends on which files were collected. The
    # two entry points below are the entire surface this loader touches.
    kubernetes_module = types.ModuleType("kubernetes")
    kubernetes_client = types.ModuleType("kubernetes.client")
    kubernetes_config = types.ModuleType("kubernetes.config")
    kubernetes_module.client = kubernetes_client  # type: ignore[attr-defined]
    kubernetes_module.config = kubernetes_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", kubernetes_client)
    monkeypatch.setitem(sys.modules, "kubernetes.config", kubernetes_config)

    fallback_calls: list[bool] = []

    def fail_incluster() -> None:
        raise RuntimeError("could contain local path or token details")

    def load_fallback() -> None:
        fallback_calls.append(True)

    sentinel = object()
    monkeypatch.setattr(
        kubernetes_config, "load_incluster_config", fail_incluster, raising=False
    )
    monkeypatch.setattr(
        kubernetes_config, "load_kube_config", load_fallback, raising=False
    )
    monkeypatch.setattr(kubernetes_client, "CoreV1Api", lambda: sentinel, raising=False)

    with pytest.raises(CollectorConfigurationError, match="requires in-cluster"):
        _load_kubernetes_core_api("dedicated")
    assert fallback_calls == []

    assert _load_kubernetes_core_api("in-process") is sentinel
    assert fallback_calls == [True]


def test_disabled_module_entrypoint_exits_cleanly(monkeypatch) -> None:
    monkeypatch.setenv("INFRASTRUCTURE_METERING_COLLECTOR_ENABLED", "false")
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda _coroutine: pytest.fail("disabled collector started an event loop"),
    )

    assert main() == 0
