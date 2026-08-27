from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.ingestion import (
    InfrastructureIngestionService,
    dispatch_ingestion_request,
    run_inventory_generation_loop,
)
from orchestrator.services.infrastructure_metering.ingestion_http import (
    IngestionRequestError,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventoryItemWire,
    InventoryScopeWire,
    InventorySnapshotFinalize,
    InventoryTicketRequest,
    InventoryWatchApply,
    InventoryWatchFinish,
)
from orchestrator.services.infrastructure_metering.collectors.contracts import (
    normalized_payload,
)
from orchestrator.services.infrastructure_metering.collectors.pod_normalization import (
    normalize_pod,
)
from orchestrator.services.infrastructure_metering.collectors.storage_normalization import (
    normalize_pv,
    normalize_pvc,
)
from orchestrator.services.infrastructure_metering.collectors.vmi_normalization import (
    normalize_vmi,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryFenceError,
    InventoryRecoveryRequired,
    InventoryScopeIdentity,
    TransportNonceClaim,
    WatchEventKind,
    WatchMutationAction,
)


def _settings(*, shadow_enabled: bool) -> InfrastructureMeteringSettings:
    return InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=shadow_enabled,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )


def _finalization(*, shadow_enabled: bool) -> InventorySnapshotFinalize:
    return InventorySnapshotFinalize.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "snapshot_id": uuid4(),
            "scope": {
                "source_cluster": "dev-cluster",
                "api_resource": "core/v1/pods",
                "namespace": "srw",
                "cluster_scoped": False,
            },
            "shadow_enabled": shadow_enabled,
            "collection_completed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            "complete": False,
            "resource_version": None,
            "item_count": 0,
            "item_digest": None,
            "fatal_errors": [],
        }
    )


def _meterable_item() -> dict:
    pod = normalize_pod(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "worker",
                "namespace": "srw",
                "uid": "pod-uid",
                "resourceVersion": "17",
                "creationTimestamp": "2026-08-05T11:59:00Z",
                "labels": {"srw/job-id": "job-1"},
            },
            "spec": {
                "nodeName": "node-a",
                "containers": [
                    {
                        "name": "agent",
                        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}},
                    }
                ],
            },
            "status": {"phase": "Running", "startTime": "2026-08-05T12:00:00Z"},
        }
    )
    return {
        "scope": {
            "source_cluster": "dev-cluster",
            "api_resource": "core/v1/pods",
            "namespace": "srw",
            "cluster_scoped": False,
        },
        "snapshot_id": uuid4(),
        "kind": "pod",
        "uid": pod.uid,
        "revision_hash": pod.revision_hash,
        "valid_for_metering": True,
        "normalized": normalized_payload(pod),
    }


def _meterable_vmi_item() -> dict:
    vmi = normalize_vmi(
        {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachineInstance",
            "metadata": {
                "name": "agent-vm-job-1",
                "namespace": "agent-vms",
                "uid": "vmi-uid-1",
                "resourceVersion": "21",
                "creationTimestamp": "2026-08-05T11:59:00Z",
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
    )
    return {
        "scope": {
            "source_cluster": "vm-cluster",
            "api_resource": "kubevirt.io/v1/virtualmachineinstances",
            "namespace": "agent-vms",
            "cluster_scoped": False,
        },
        "snapshot_id": uuid4(),
        "kind": "vmi",
        "uid": vmi.uid,
        "revision_hash": vmi.revision_hash,
        "valid_for_metering": True,
        "normalized": normalized_payload(vmi),
    }


def test_server_accepts_only_the_allowlisted_normalized_pod_projection():
    valid = InventoryItemWire.model_validate(_meterable_item())
    projected = InfrastructureIngestionService._inventory_item(valid)
    assert projected.source_uid == "pod-uid"
    assert projected.valid_for_metering

    injections = (
        lambda payload: payload["normalized"].update(
            {"image": "registry.invalid/private-image"}
        ),
        lambda payload: payload["normalized"]["request_evidence"]["admitted_requests"][
            "containers"
        ][0].update({"command": ["leak-me"]}),
        lambda payload: payload["normalized"]["labels"].update(
            {"private-token": "leak-me"}
        ),
    )
    for inject in injections:
        payload = _meterable_item()
        inject(payload)
        wire = InventoryItemWire.model_validate(payload)
        with pytest.raises(IngestionRequestError, match="payload is invalid") as exc:
            InfrastructureIngestionService._inventory_item(wire)
        assert exc.value.status_code == 400


def test_server_accepts_strict_vmi_projection_and_exact_remote_scope() -> None:
    wire = InventoryItemWire.model_validate(_meterable_vmi_item())
    projected = InfrastructureIngestionService._inventory_item(wire)
    assert projected.source_kind == "vmi"

    service = object.__new__(InfrastructureIngestionService)
    service.settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    service._collector_id = "kubernetes-pods"
    scope = service._scope(wire.scope, "kubevirt-vmis")
    assert scope.source_cluster == "vm-cluster"
    assert scope.api_resource == "kubevirt.io/v1/virtualmachineinstances"
    with pytest.raises(IngestionRequestError, match="scope is not allowed"):
        service._scope(wire.scope, "kubernetes-pods")


def test_remote_collector_requires_a_distinct_hmac_key() -> None:
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    with pytest.raises(ValueError, match="keys must be distinct"):
        InfrastructureIngestionService(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            settings,
            ingestion_key=b"p" * 32,
            additional_ingestion_keys={"kubevirt-vmis": b"p" * 32},
        )


def test_vm_storage_hmac_and_scope_are_distinct_from_local_and_vmi_authorities():
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        pvc_inventory_enabled=True,
        pv_inventory_enabled=True,
        vm_inventory_enabled=True,
        vm_pvc_inventory_enabled=True,
        vm_pv_inventory_enabled=True,
        vm_pvc_shadow_enabled=True,
        vm_pv_shadow_enabled=True,
        vm_pv_cluster_wide_rbac_acknowledged=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
        volume_identity_key_version="storage-v1",
    )
    service = InfrastructureIngestionService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        settings,
        ingestion_key=b"p" * 32,
        additional_ingestion_keys={
            "kubevirt-vmis": b"v" * 32,
            "kubevirt-storage": b"s" * 32,
        },
    )
    assert set(service._ingestion_keys) == {
        "kubernetes-pods",
        "kubevirt-vmis",
        "kubevirt-storage",
    }
    assert service._vm_storage_reconciler.interval_mutations_enabled is True

    remote_pvc = InventoryScopeWire(
        source_cluster="vm-cluster",
        api_resource="core/v1/persistentvolumeclaims",
        namespace="agent-vms",
        cluster_scoped=False,
    )
    remote_pv = InventoryScopeWire(
        source_cluster="vm-cluster",
        api_resource="core/v1/persistentvolumes",
        namespace=None,
        cluster_scoped=True,
    )
    pvc_scope = service._scope(remote_pvc, "kubevirt-storage")
    pv_scope = service._scope(remote_pv, "kubevirt-storage")
    assert service._scope_shadow_enabled(pvc_scope) is True
    assert service._scope_shadow_enabled(pv_scope) is True

    rejected = (
        (remote_pvc, "kubevirt-vmis"),
        (remote_pv, "kubevirt-vmis"),
        (remote_pvc, "kubernetes-pods"),
        (
            InventoryScopeWire(
                source_cluster="vm-cluster",
                api_resource="core/v1/persistentvolumeclaims",
                namespace="other",
                cluster_scoped=False,
            ),
            "kubevirt-storage",
        ),
        (
            InventoryScopeWire(
                source_cluster="other-cluster",
                api_resource="core/v1/persistentvolumes",
                namespace=None,
                cluster_scoped=True,
            ),
            "kubevirt-storage",
        ),
    )
    for wire, collector_id in rejected:
        with pytest.raises(IngestionRequestError, match="scope is not allowed"):
            service._scope(wire, collector_id)

    # Local storage remains owned by the primary identity and its original
    # source/namespace gates.
    local_pvc = InventoryScopeWire(
        source_cluster="dev-cluster",
        api_resource="core/v1/persistentvolumeclaims",
        namespace="srw",
        cluster_scoped=False,
    )
    assert service._scope(local_pvc, "kubernetes-pods").collector_id == (
        "kubernetes-pods"
    )
    with pytest.raises(IngestionRequestError, match="scope is not allowed"):
        service._scope(local_pvc, "kubevirt-storage")

    with pytest.raises(ValueError, match="keys must be distinct"):
        InfrastructureIngestionService(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            settings,
            ingestion_key=b"p" * 32,
            additional_ingestion_keys={
                "kubevirt-vmis": b"x" * 32,
                "kubevirt-storage": b"x" * 32,
            },
        )


@pytest.mark.asyncio
async def test_vm_storage_shadow_wires_source_activation_reconciler() -> None:
    model = InventorySnapshotFinalize.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "snapshot_id": uuid4(),
            "scope": {
                "source_cluster": "vm-cluster",
                "api_resource": "core/v1/persistentvolumeclaims",
                "namespace": "agent-vms",
                "cluster_scoped": False,
            },
            "shadow_enabled": True,
            "collection_completed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            "complete": True,
            "resource_version": "17",
            "item_count": 0,
            "item_digest": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "fatal_errors": [],
        }
    )
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        vm_pvc_inventory_enabled=True,
        vm_pvc_shadow_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    store = SimpleNamespace(
        finalize_snapshot=AsyncMock(
            return_value=SimpleNamespace(
                snapshot_id=model.snapshot_id,
                complete=True,
                present_items=0,
                invalid_items=0,
                confirmed_intervals=0,
                closed_intervals=0,
                pending_valid_items=0,
                shadow_comparisons=0,
                replayed=False,
            )
        )
    )
    service = InfrastructureIngestionService(
        None,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        settings,
        ingestion_key=b"p" * 32,
        additional_ingestion_keys={"kubevirt-storage": b"s" * 32},
    )
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubevirt-storage",
                transport_claim=TransportNonceClaim(
                    collector_id="kubevirt-storage",
                    request_nonce=uuid4(),
                    request_kind="snapshot-finalize",
                    request_digest="a" * 64,
                ),
            ),
        )
    )

    await service.snapshot_finalize(SimpleNamespace())  # type: ignore[arg-type]

    kwargs = store.finalize_snapshot.await_args.kwargs
    assert kwargs["interval_mutator"] == service._vm_storage_reconciler.apply_snapshot
    assert kwargs["observation_hook"].__self__ is service._vm_storage_reconciler
    assert kwargs["absence_mutator"] is None
    assert kwargs["reconcile_intervals"] is True
    assert service._vm_storage_reconciler.interval_mutations_enabled is True
    assert settings.publication_enabled is False
    assert settings.pvc_publication_enabled is False
    assert settings.pv_publication_enabled is False


def test_server_binds_normalized_pod_identity_validity_and_revision():
    payload = _meterable_item()
    payload["normalized"]["namespace"] = "other"
    wire = InventoryItemWire.model_validate(payload)
    with pytest.raises(IngestionRequestError, match="identity mismatch"):
        InfrastructureIngestionService._inventory_item(wire)


def _storage_wire(normalized, *, api_resource: str, cluster_scoped: bool):
    return {
        "scope": {
            "source_cluster": "dev-cluster",
            "api_resource": api_resource,
            "namespace": None if cluster_scoped else "srw",
            "cluster_scoped": cluster_scoped,
        },
        "snapshot_id": uuid4(),
        "kind": normalized.kind,
        "uid": normalized.uid,
        "revision_hash": normalized.revision_hash,
        "valid_for_metering": normalized.valid_for_metering,
        "normalized": normalized_payload(normalized),
    }


def test_server_accepts_strict_pvc_and_raw_free_pv_projections():
    pvc = normalize_pvc(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "uid": "claim-uid",
                "name": "pvc-workspace-11111111-222",
                "namespace": "srw",
                "resourceVersion": "4",
                "labels": {"app": "srw-workspace"},
            },
            "spec": {
                "resources": {"requests": {"storage": "20Gi"}},
                "accessModes": ["ReadWriteOnce"],
            },
        }
    )
    pvc_wire = InventoryItemWire.model_validate(
        _storage_wire(
            pvc,
            api_resource="core/v1/persistentvolumeclaims",
            cluster_scoped=False,
        )
    )
    assert InfrastructureIngestionService._inventory_item(pvc_wire).source_kind == "pvc"

    pv = normalize_pv(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {
                "uid": "pv-uid",
                "name": "pv-name",
                "resourceVersion": "5",
            },
            "spec": {
                "capacity": {"storage": "25Gi"},
                "persistentVolumeReclaimPolicy": "Retain",
                "csi": {
                    "driver": "driver.example.test",
                    "volumeHandle": "provider-secret-handle",
                    "volumeAttributes": {"private": "must-not-persist"},
                },
            },
        },
        source_cluster="dev-cluster",
        identity_key=b"k" * 32,
        identity_key_version="key-v1",
    )
    payload = _storage_wire(
        pv,
        api_resource="core/v1/persistentvolumes",
        cluster_scoped=True,
    )
    encoded = json.dumps(payload, default=str)
    assert "provider-secret-handle" not in encoded
    assert "must-not-persist" not in encoded
    pv_wire = InventoryItemWire.model_validate(payload)
    projected = InfrastructureIngestionService._inventory_item(pv_wire)
    assert projected.source_kind == "volume"
    assert projected.source_uid == pv.uid

    payload["normalized"]["volume_identity"]["volumeHandle"] = "leak"
    with pytest.raises(ValidationError, match="forbidden"):
        InventoryItemWire.model_validate(payload)


def test_server_rejects_storage_resource_kind_and_scope_confusion():
    pvc = normalize_pvc(
        {
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "uid": "claim-uid",
                "name": "claim",
                "namespace": "srw",
            },
            "spec": {"resources": {"requests": {"storage": "1Gi"}}},
        }
    )
    payload = _storage_wire(
        pvc,
        api_resource="core/v1/persistentvolumes",
        cluster_scoped=True,
    )
    payload["scope"]["namespace"] = None
    payload["kind"] = "pvc"
    wire = InventoryItemWire.model_validate(payload)
    with pytest.raises(IngestionRequestError, match="payload is invalid"):
        InfrastructureIngestionService._inventory_item(wire)


@pytest.mark.asyncio
async def test_snapshot_finalize_rejects_mixed_shadow_rollout_before_mutation():
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            _finalization(shadow_enabled=False),
            SimpleNamespace(collector_id="kubernetes-pods"),
        )
    )

    with pytest.raises(IngestionRequestError, match="shadow mode") as raised:
        await service.snapshot_finalize(SimpleNamespace())  # type: ignore[arg-type]

    assert raised.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_resource", "namespace", "cluster_scoped", "physical_volume"),
    (
        ("core/v1/persistentvolumeclaims", "srw", False, False),
        ("core/v1/persistentvolumes", None, True, True),
    ),
)
async def test_storage_snapshot_finalize_wires_exact_lifecycle_hooks(
    api_resource: str,
    namespace: str | None,
    cluster_scoped: bool,
    physical_volume: bool,
):
    model = InventorySnapshotFinalize.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "snapshot_id": uuid4(),
            "scope": {
                "source_cluster": "dev-cluster",
                "api_resource": api_resource,
                "namespace": namespace,
                "cluster_scoped": cluster_scoped,
            },
            "shadow_enabled": True,
            "collection_completed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            "complete": True,
            "resource_version": "17",
            "item_count": 0,
            "item_digest": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "fatal_errors": [],
        }
    )
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        pvc_inventory_enabled=True,
        pv_inventory_enabled=True,
        pvc_shadow_enabled=True,
        pv_shadow_enabled=True,
        volume_identity_key_version="storage-v1",
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    store = SimpleNamespace(
        finalize_snapshot=AsyncMock(
            return_value=SimpleNamespace(
                snapshot_id=model.snapshot_id,
                complete=True,
                present_items=0,
                invalid_items=0,
                confirmed_intervals=0,
                closed_intervals=0,
                pending_valid_items=0,
                shadow_comparisons=0,
                replayed=False,
            )
        )
    )
    storage = SimpleNamespace(
        apply_snapshot=AsyncMock(),
        observe_snapshot=AsyncMock(),
        complete_snapshot=AsyncMock(),
        apply_absence=AsyncMock(),
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = settings
    service.store = store
    service._storage_reconciler = storage
    service._pod_reconciler = SimpleNamespace()
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=TransportNonceClaim(
                    collector_id="kubernetes-pods",
                    request_nonce=uuid4(),
                    request_kind="snapshot-finalize",
                    request_digest="a" * 64,
                ),
            ),
        )
    )

    await service.snapshot_finalize(SimpleNamespace())  # type: ignore[arg-type]

    kwargs = store.finalize_snapshot.await_args.kwargs
    assert kwargs["interval_mutator"] == storage.apply_snapshot
    assert kwargs["observation_hook"] == storage.observe_snapshot
    assert kwargs["reconcile_intervals"] is True
    assert kwargs["require_shadow_comparison"] is True
    assert (kwargs["completion_hook"] == storage.complete_snapshot) is physical_volume
    assert (kwargs["absence_mutator"] == storage.apply_absence) is physical_volume


@pytest.mark.asyncio
async def test_pvc_inventory_only_snapshot_cannot_mutate_lifecycles():
    model = InventorySnapshotFinalize.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "snapshot_id": uuid4(),
            "scope": {
                "source_cluster": "dev-cluster",
                "api_resource": "core/v1/persistentvolumeclaims",
                "namespace": "srw",
                "cluster_scoped": False,
            },
            "shadow_enabled": False,
            "collection_completed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            "complete": True,
            "resource_version": "17",
            "item_count": 0,
            "item_digest": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "fatal_errors": [],
        }
    )
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        pvc_inventory_enabled=True,
        pvc_shadow_enabled=False,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    store = SimpleNamespace(
        finalize_snapshot=AsyncMock(
            return_value=SimpleNamespace(
                snapshot_id=model.snapshot_id,
                complete=True,
                present_items=0,
                invalid_items=0,
                confirmed_intervals=0,
                closed_intervals=0,
                pending_valid_items=0,
                shadow_comparisons=0,
                replayed=False,
            )
        )
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = settings
    service.store = store
    service._storage_reconciler = SimpleNamespace()
    service._pod_reconciler = SimpleNamespace()
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=TransportNonceClaim(
                    collector_id="kubernetes-pods",
                    request_nonce=uuid4(),
                    request_kind="snapshot-finalize",
                    request_digest="a" * 64,
                ),
            ),
        )
    )

    await service.snapshot_finalize(SimpleNamespace())  # type: ignore[arg-type]

    kwargs = store.finalize_snapshot.await_args.kwargs
    assert kwargs["interval_mutator"] is None
    assert kwargs["observation_hook"] is None
    assert kwargs["completion_hook"] is None
    assert kwargs["absence_mutator"] is None
    assert kwargs["reconcile_intervals"] is False
    assert kwargs["require_shadow_comparison"] is False


@pytest.mark.asyncio
async def test_dispatch_is_unavailable_without_service_and_sanitizes_fences():
    with pytest.raises(IngestionRequestError, match="unavailable") as unavailable:
        await dispatch_ingestion_request(None, "ticket", SimpleNamespace())  # type: ignore[arg-type]
    assert unavailable.value.status_code == 503

    service = SimpleNamespace(
        ticket=AsyncMock(side_effect=InventoryFenceError("secret cursor detail"))
    )
    with pytest.raises(IngestionRequestError, match="generation conflict") as fenced:
        await dispatch_ingestion_request(  # type: ignore[arg-type]
            service, "ticket", SimpleNamespace()
        )
    assert fenced.value.status_code == 409
    assert "secret cursor detail" not in str(fenced.value)

    service.ticket = AsyncMock(
        side_effect=InventoryRecoveryRequired("private abandoned session detail")
    )
    with pytest.raises(IngestionRequestError, match="recovery required") as recovery:
        await dispatch_ingestion_request(  # type: ignore[arg-type]
            service, "ticket", SimpleNamespace()
        )
    assert recovery.value.status_code == 409
    assert "private abandoned session detail" not in str(recovery.value)


@pytest.mark.asyncio
async def test_snapshot_ticket_rechecks_continuity_at_store_admission():
    scope = InventoryScopeIdentity(
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        api_resource="core/v1/pods",
        namespace="srw",
    )
    epoch_id = uuid4()
    model = InventoryTicketRequest.model_validate(
        {
            "scope": {
                "source_cluster": scope.source_cluster,
                "api_resource": scope.api_resource,
                "namespace": scope.namespace,
                "cluster_scoped": False,
            },
            "intent": "snapshot",
            "snapshot_id": uuid4(),
        }
    )
    claim = TransportNonceClaim(
        collector_id=scope.collector_id,
        request_nonce=uuid4(),
        request_kind="snapshot-ticket",
        request_digest="9" * 64,
    )
    grant = SimpleNamespace(
        id=uuid4(),
        token="t" * 32,
        leader_generation=7,
        expires_at=datetime(2026, 8, 5, 13, tzinfo=timezone.utc),
    )
    store = SimpleNamespace(issue_ingest_ticket=AsyncMock(return_value=grant))
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service.store = store
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id=scope.collector_id,
                body=b"{}",
                transport_claim=claim,
            ),
        )
    )
    service._scope = lambda *_args: scope  # type: ignore[method-assign]
    service._ensure_scope_epoch = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": epoch_id,
            "continuity_health": "healthy",
            "last_resource_version": "rv-list",
        }
    )

    response = await service.ticket(SimpleNamespace())  # type: ignore[arg-type]

    assert response["ticket_id"] == str(grant.id)
    store.issue_ingest_ticket.assert_awaited_once()
    assert (
        store.issue_ingest_ticket.await_args.kwargs["require_healthy_continuity"]
        is True
    )


@pytest.mark.asyncio
async def test_vmi_ticket_fences_controller_restart_into_recovery_epoch():
    scope = InventoryScopeIdentity(
        collector_id="kubevirt-vmis",
        source_cluster="vm-cluster",
        api_resource="kubevirt.io/v1/virtualmachineinstances",
        namespace="agent-vms",
    )
    model = InventoryTicketRequest.model_validate(
        {
            "scope": {
                "source_cluster": scope.source_cluster,
                "api_resource": scope.api_resource,
                "namespace": scope.namespace,
                "cluster_scoped": False,
            },
            "intent": "snapshot",
            "snapshot_id": uuid4(),
            "controller_epoch": "new-collector-pod-uid",
            "sequence": 0,
        }
    )
    claim = TransportNonceClaim(
        collector_id=scope.collector_id,
        request_nonce=uuid4(),
        request_kind="snapshot-ticket",
        request_digest="9" * 64,
    )
    epoch_id = uuid4()
    store = SimpleNamespace(
        start_controller_epoch_recovery=AsyncMock(),
        issue_ingest_ticket=AsyncMock(),
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    service.store = store
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id=scope.collector_id,
                body=b"{}",
                transport_claim=claim,
            ),
        )
    )
    service._scope = lambda *_args: scope  # type: ignore[method-assign]
    service._ensure_scope_epoch = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": epoch_id,
            "continuity_health": "healthy",
            "controller_epoch": "old-collector-pod-uid",
            "last_sequence": 12,
        }
    )

    with pytest.raises(IngestionRequestError, match="recovery started") as raised:
        await service.ticket(SimpleNamespace())  # type: ignore[arg-type]

    assert raised.value.status_code == 409
    store.start_controller_epoch_recovery.assert_awaited_once()
    recovery_claim = store.start_controller_epoch_recovery.await_args.kwargs[
        "transport"
    ]
    assert recovery_claim.request_kind == "controller-epoch-change"
    store.issue_ingest_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_watch_finish_forwards_typed_ambiguous_gap_to_store():
    event_id = uuid4()
    model = InventoryWatchFinish.model_validate_json(
        json.dumps(
            {
                "ticket_id": str(uuid4()),
                "ticket_token": "t" * 32,
                "leader_generation": 7,
                "scope": {
                    "source_cluster": "dev-cluster",
                    "api_resource": "core/v1/pods",
                    "namespace": "srw",
                    "cluster_scoped": False,
                },
                "started_at": "2026-08-05T12:00:00Z",
                "completed_at": "2026-08-05T12:00:01Z",
                "starting_resource_version": "17",
                "committed_resource_version": "17",
                "processed_events": 0,
                "object_events": 0,
                "bookmarks": 0,
                "bytes_read": 10,
                "reconnect_required": True,
                "relist_required": True,
                "history_lost": True,
                "limit_reached": False,
                "gap_reason": "ambiguous-watch-apply",
                "ambiguous_resource_version": "18",
                "history_event_id": str(event_id),
                "fatal_errors": [],
                "item_errors": [],
            }
        )
    )
    claim = TransportNonceClaim(
        collector_id="kubernetes-pods",
        request_nonce="n" * 32,
        request_kind="watch-finish",
        request_digest="a" * 64,
    )
    store = SimpleNamespace(
        record_watch_gap=AsyncMock(
            return_value=SimpleNamespace(coverage_gap_id=uuid4(), replayed=False)
        )
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service.store = store
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=claim,
            ),
        )
    )
    service._require_generation = AsyncMock()  # type: ignore[method-assign]

    result = await service.watch_finish(SimpleNamespace())  # type: ignore[arg-type]

    assert result["history_lost"] is True
    kwargs = store.record_watch_gap.await_args.kwargs
    assert kwargs["gap_reason"] == "ambiguous-watch-apply"
    assert kwargs["alternate_expected_resource_version"] == "18"
    assert kwargs["transport"].request_kind == "watch-history-lost"


@pytest.mark.asyncio
async def test_watch_apply_maps_uppercase_kubernetes_event_to_durable_kind():
    model = InventoryWatchApply.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "leader_generation": 7,
            "event_id": uuid4(),
            "expected_resource_version": "17",
            "observation": {
                "scope": {
                    "source_cluster": "dev-cluster",
                    "api_resource": "core/v1/pods",
                    "namespace": "srw",
                    "cluster_scoped": False,
                },
                "event_type": "BOOKMARK",
                "resource_version": "18",
                "source_event_bytes": 64,
                "collector_observed_at": datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
                "confirms_presence": False,
                "item": None,
            },
        }
    )
    scope = InventoryScopeIdentity(
        collector_id="kubernetes-pods",
        source_cluster="dev-cluster",
        api_resource="core/v1/pods",
        namespace="srw",
    )
    claim = TransportNonceClaim(
        collector_id="kubernetes-pods",
        request_nonce=uuid4(),
        request_kind="watch-event",
        request_digest="a" * 64,
    )
    store = SimpleNamespace(
        apply_watch_event=AsyncMock(
            return_value=SimpleNamespace(
                event_id=model.event_id,
                resource_version="18",
                mutation_action=WatchMutationAction.BOOKMARK,
                session_consumed=False,
                replayed=False,
            )
        )
    )
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _settings(shadow_enabled=True)
    service.store = store
    service._pod_reconciler = SimpleNamespace(apply_watch=AsyncMock())
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=claim,
            ),
        )
    )
    service._require_generation = AsyncMock()  # type: ignore[method-assign]
    service._scope = lambda *_args: scope  # type: ignore[method-assign]

    result = await service.watch_apply(SimpleNamespace())  # type: ignore[arg-type]

    assert result["resource_version"] == "18"
    event = store.apply_watch_event.await_args.args[5]
    assert event.event_type is WatchEventKind.BOOKMARK


@pytest.mark.asyncio
async def test_generation_loop_drains_bounded_cleanup_and_forwards_retention() -> None:
    stop = asyncio.Event()
    first = SimpleNamespace(might_have_more=True)
    final = SimpleNamespace(might_have_more=False)
    diagnostic_calls = 0

    async def finish_cleanup(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal diagnostic_calls
        diagnostic_calls += 1
        if diagnostic_calls == 1:
            return first
        stop.set()
        return final

    store = SimpleNamespace(
        activate_generation=AsyncMock(return_value=17),
        deactivate_generation=AsyncMock(),
        purge_expired_transport_nonces=AsyncMock(side_effect=(1_000, 0)),
        purge_diagnostics=AsyncMock(side_effect=finish_cleanup),
    )

    await run_inventory_generation_loop(
        stop,
        store,
        cleanup_interval_seconds=30,
        snapshot_item_retention=timedelta(days=9),
        diagnostic_retention=timedelta(days=40),
    )

    store.deactivate_generation.assert_awaited_once_with(17)
    assert store.purge_expired_transport_nonces.await_count == 2
    assert store.purge_diagnostics.await_count == 2
    kwargs = store.purge_diagnostics.await_args.kwargs
    assert kwargs["snapshot_item_retention"] == timedelta(days=9)
    assert kwargs["diagnostic_retention"] == timedelta(days=40)
    assert kwargs["abandoned_staging_retention"] == timedelta(hours=24)
    assert kwargs["limit"] == 1_000
