"""Focused integration contracts for Slice 3 ingestion wiring."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from orchestrator.services.infrastructure_metering.capabilities import (
    REQUIRED_APP_INDEXES,
    REQUIRED_APP_TABLES,
    REQUIRED_APP_TRIGGERS,
    REQUIRED_SLICE1_APP_INDEXES,
    REQUIRED_SLICE1_APP_TABLES,
    REQUIRED_SLICE1_APP_TRIGGERS,
    REQUIRED_SLICE2_CLAIM_APP_CONSTRAINTS,
    REQUIRED_SLICE2_CLAIM_APP_INDEXES,
    REQUIRED_SLICE2_CLAIM_APP_TABLES,
    REQUIRED_SLICE2_CLAIM_APP_TRIGGERS,
    REQUIRED_SLICE2_VOLUME_APP_CONSTRAINTS,
    REQUIRED_SLICE2_VOLUME_APP_INDEXES,
    REQUIRED_SLICE2_VOLUME_APP_TABLES,
    REQUIRED_SLICE2_VOLUME_APP_TRIGGERS,
    REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINTS,
    REQUIRED_SLICE3_COMPUTE_APP_COLUMNS,
    REQUIRED_SLICE3_COMPUTE_APP_INDEXES,
    REQUIRED_SLICE3_COMPUTE_APP_TABLES,
    REQUIRED_SLICE3_COMPUTE_APP_TRIGGERS,
    REQUIRED_SLICE3_STORAGE_APP_CONSTRAINTS,
    REQUIRED_SLICE3_STORAGE_APP_INDEXES,
    REQUIRED_SLICE3_STORAGE_APP_TABLES,
    REQUIRED_SLICE3_STORAGE_APP_TRIGGERS,
    MeteringSchemaCapabilities,
)
from orchestrator.services.infrastructure_metering.collectors.contracts import (
    normalized_payload,
)
from orchestrator.services.infrastructure_metering.collectors.vmi_normalization import (
    normalize_vmi,
)
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.ingestion import (
    InfrastructureIngestionService,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventorySnapshotFinalize,
    InventoryWatchApply,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryItem,
    TransportNonceClaim,
    WatchMutationAction,
)


_JOB_ID = UUID("11111111-2222-4333-8444-555555555555")
_RECEIVED_AT = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
_EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _pod_settings() -> InfrastructureMeteringSettings:
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        agent_pod_shadow_enabled=True,
        ide_pod_shadow_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    settings.validate()
    return settings


def _vm_settings() -> InfrastructureMeteringSettings:
    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=True,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
        vm_inventory_enabled=True,
        vm_shadow_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    settings.validate()
    return settings


def _adapter(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        apply_absence=AsyncMock(return_value=False, name=f"{name}-absence"),
        apply_deletion=AsyncMock(return_value=None, name=f"{name}-deletion"),
        apply_snapshot=AsyncMock(return_value=uuid4(), name=f"{name}-snapshot"),
        apply_terminal=AsyncMock(return_value=None, name=f"{name}-terminal"),
        apply_watch=AsyncMock(return_value=uuid4(), name=f"{name}-watch"),
        observe_snapshot=AsyncMock(name=f"{name}-observation"),
    )


def _pod_service() -> InfrastructureIngestionService:
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _pod_settings()
    service._collector_id = "kubernetes-pods"
    service._pod_reconciler = _adapter("workspace")
    service._agent_pod_reconciler = _adapter("agent")
    service._ide_pod_reconciler = _adapter("ide")
    return service


def _pod_item(product: str) -> InventoryItem:
    if product == "agent":
        labels = {
            "app": "srw-agent",
            "srw/component": "agent",
            "srw/purpose": "job",
            "srw/managed-by": "agent-provisioner",
        }
        name = "agent-pod"
    elif product == "ide":
        labels = {
            "app": "srw-workspace",
            "srw/component": "ide-session",
            "srw/job-id": str(_JOB_ID),
        }
        name = f"ide-{str(_JOB_ID)[:12]}"
    else:
        labels = {
            "app": "srw-workspace",
            "srw/component": "workspace",
            "srw/job-id": str(_JOB_ID),
        }
        name = "workspace-pod"
    return InventoryItem(
        source_kind="pod",
        source_uid=f"{product}-uid",
        revision_hash="a" * 64,
        valid_for_metering=True,
        normalized_item={
            "source_kind": "pod",
            "api_version": "v1",
            "namespace": "srw",
            "name": name,
            "uid": f"{product}-uid",
            "resource_version": "17",
            "labels": labels,
            "owner_references": [],
            "lifecycle": {"accrues": True, "terminal": False},
            "capacity": {
                "cpu_millicores": 500,
                "memory_bytes": 1024**3,
                "capacity_quality": "exact",
                "measurement_algorithm": "pod-requests-fixture-v1",
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "adapter_name"),
    (
        ("workspace", "_pod_reconciler"),
        ("agent", "_agent_pod_reconciler"),
        ("ide", "_ide_pod_reconciler"),
    ),
)
async def test_composite_pod_lifecycle_routes_snapshot_and_watch_exclusively(
    product: str,
    adapter_name: str,
) -> None:
    service = _pod_service()
    conn = object()
    context = object()
    item = _pod_item(product)
    expected = getattr(service, adapter_name)

    snapshot_result = await service._apply_pod_snapshot(conn, context, item)
    watch_result = await service._apply_pod_watch(conn, context, item)

    assert snapshot_result == expected.apply_snapshot.return_value
    assert watch_result == expected.apply_watch.return_value
    expected.apply_snapshot.assert_awaited_once_with(conn, context, item)
    expected.apply_watch.assert_awaited_once_with(conn, context, item)
    for candidate_name in (
        "_pod_reconciler",
        "_agent_pod_reconciler",
        "_ide_pod_reconciler",
    ):
        if candidate_name == adapter_name:
            continue
        candidate = getattr(service, candidate_name)
        candidate.apply_snapshot.assert_not_awaited()
        candidate.apply_watch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product", "adapter_name"),
    (
        ("agent", "_agent_pod_reconciler"),
        ("ide", "_ide_pod_reconciler"),
    ),
)
async def test_active_compute_routing_does_not_depend_on_reversible_shadow_flags(
    product: str,
    adapter_name: str,
) -> None:
    service = _pod_service()
    service.settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=False,
        agent_pod_shadow_enabled=False,
        ide_pod_shadow_enabled=False,
        stable_cluster_id="dev-cluster",
        namespace_allowlist=("srw",),
    )
    expected = getattr(service, adapter_name)
    item = _pod_item(product)

    await service._apply_pod_snapshot(object(), object(), item)
    await service._apply_pod_watch(object(), object(), item)

    expected.apply_snapshot.assert_awaited_once()
    expected.apply_watch.assert_awaited_once()
    service._pod_reconciler.apply_snapshot.assert_not_awaited()
    service._pod_reconciler.apply_watch.assert_not_awaited()


@pytest.mark.asyncio
async def test_composite_pod_observation_fans_out_one_row_contract_per_class() -> None:
    service = _pod_service()
    conn = object()
    context = object()
    item = _pod_item("ide")

    await service._observe_pod_snapshot(conn, context, item)

    service._pod_reconciler.observe_snapshot.assert_awaited_once_with(
        conn, context, item
    )
    service._agent_pod_reconciler.observe_snapshot.assert_awaited_once_with(
        conn, context, item
    )
    service._ide_pod_reconciler.observe_snapshot.assert_awaited_once_with(
        conn, context, item
    )


@pytest.mark.asyncio
async def test_pod_snapshot_finalize_selects_composite_hooks_without_publication() -> (
    None
):
    model = InventorySnapshotFinalize.model_validate(
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
            "shadow_enabled": True,
            "collection_completed_at": _RECEIVED_AT,
            "complete": True,
            "resource_version": "17",
            "item_count": 0,
            "item_digest": _EMPTY_DIGEST,
            "fatal_errors": [],
        }
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
    service = _pod_service()
    service.store = store
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
    assert kwargs["interval_mutator"] == service._apply_pod_snapshot
    assert kwargs["observation_hook"] == service._observe_pod_snapshot
    assert kwargs["completion_hook"] == service._complete_pod_snapshot
    assert kwargs["absence_mutator"] == service._agent_pod_reconciler.apply_absence
    assert kwargs["require_shadow_comparison"] is True
    assert kwargs["reconcile_intervals"] is True
    assert not any("publication" in key or "usage" in key for key in kwargs)
    assert service.settings.publication_enabled is False
    assert service.settings.agent_pod_publication_enabled is False
    assert service.settings.ide_pod_publication_enabled is False


@pytest.mark.asyncio
async def test_pod_watch_wires_agent_terminal_and_deletion_repair_hooks() -> None:
    model = InventoryWatchApply.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "leader_generation": 11,
            "event_id": uuid4(),
            "expected_resource_version": "20",
            "observation": {
                "scope": {
                    "source_cluster": "dev-cluster",
                    "api_resource": "core/v1/pods",
                    "namespace": "srw",
                    "cluster_scoped": False,
                },
                "event_type": "BOOKMARK",
                "resource_version": "21",
                "source_event_bytes": 64,
                "collector_observed_at": _RECEIVED_AT,
                "confirms_presence": False,
                "item": None,
            },
        }
    )
    store = SimpleNamespace(
        apply_watch_event=AsyncMock(
            return_value=SimpleNamespace(
                event_id=model.event_id,
                resource_version="21",
                mutation_action=WatchMutationAction.BOOKMARK,
                session_consumed=False,
                replayed=False,
            )
        )
    )
    service = _pod_service()
    service.store = store
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubernetes-pods",
                transport_claim=TransportNonceClaim(
                    collector_id="kubernetes-pods",
                    request_nonce=uuid4(),
                    request_kind="watch-event",
                    request_digest="a" * 64,
                ),
            ),
        )
    )
    service._require_generation = AsyncMock()  # type: ignore[method-assign]

    await service.watch_apply(SimpleNamespace())  # type: ignore[arg-type]

    kwargs = store.apply_watch_event.await_args.kwargs
    assert kwargs["interval_mutator"] == service._apply_pod_watch
    assert kwargs["deletion_mutator"] == service._agent_pod_reconciler.apply_deletion
    assert kwargs["terminal_mutator"] == service._agent_pod_reconciler.apply_terminal


def _vmi_scope() -> dict[str, object]:
    return {
        "source_cluster": "vm-cluster",
        "api_resource": "kubevirt.io/v1/virtualmachineinstances",
        "namespace": "agent-vms",
        "cluster_scoped": False,
    }


def _vmi_item_wire() -> dict[str, object]:
    vmi = normalize_vmi(
        {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachineInstance",
            "metadata": {
                "name": "agent-vm-job-1",
                "namespace": "agent-vms",
                "uid": "vmi-uid-1",
                "resourceVersion": "21",
                "creationTimestamp": "2026-08-07T11:59:00Z",
                "labels": {
                    "srw.io/owner-kind": "job",
                    "srw.io/owner-id": str(_JOB_ID),
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
        "scope": _vmi_scope(),
        "snapshot_id": None,
        "kind": "vmi",
        "uid": vmi.uid,
        "revision_hash": vmi.revision_hash,
        "valid_for_metering": True,
        "normalized": normalized_payload(vmi),
    }


def _claim(request_kind: str) -> TransportNonceClaim:
    return TransportNonceClaim(
        collector_id="kubevirt-vmis",
        request_nonce=uuid4(),
        request_kind=request_kind,
        request_digest="b" * 64,
    )


def _vmi_service(store: SimpleNamespace) -> InfrastructureIngestionService:
    service = object.__new__(InfrastructureIngestionService)
    service.settings = _vm_settings()
    service.store = store
    service._collector_id = "kubernetes-pods"
    service._vmi_reconciler = _adapter("vmi")
    service._pod_reconciler = _adapter("workspace")
    service._storage_reconciler = _adapter("storage")
    return service


@pytest.mark.asyncio
async def test_vmi_snapshot_finalize_wires_only_vm_shadow_lifecycle_hooks() -> None:
    model = InventorySnapshotFinalize.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "snapshot_id": uuid4(),
            "scope": _vmi_scope(),
            "shadow_enabled": True,
            "collection_completed_at": _RECEIVED_AT,
            "complete": True,
            "resource_version": "21",
            "item_count": 0,
            "item_digest": _EMPTY_DIGEST,
            "controller_epoch": "vm-controller-pod-uid",
            "sequence": 7,
            "fatal_errors": [],
        }
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
    service = _vmi_service(store)
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubevirt-vmis",
                transport_claim=_claim("snapshot-finalize"),
            ),
        )
    )

    await service.snapshot_finalize(SimpleNamespace())  # type: ignore[arg-type]

    kwargs = store.finalize_snapshot.await_args.kwargs
    assert kwargs["interval_mutator"] == service._vmi_reconciler.apply_snapshot
    assert kwargs["observation_hook"] == service._vmi_reconciler.observe_snapshot
    assert kwargs["completion_hook"] == service._complete_vmi_snapshot
    assert kwargs["absence_mutator"] is None
    assert kwargs["reconcile_intervals"] is True
    assert kwargs["require_shadow_comparison"] is False
    assert not any("publication" in key or "usage" in key for key in kwargs)
    assert service.settings.publication_enabled is False
    assert service.settings.vm_publication_enabled is False


@pytest.mark.asyncio
async def test_vmi_watch_apply_wires_vm_mutator_without_publication_or_pv_delete() -> (
    None
):
    model = InventoryWatchApply.model_validate(
        {
            "ticket_id": uuid4(),
            "ticket_token": "t" * 32,
            "leader_generation": 11,
            "event_id": uuid4(),
            "expected_resource_version": "20",
            "observation": {
                "scope": _vmi_scope(),
                "event_type": "ADDED",
                "resource_version": "21",
                "source_event_bytes": 512,
                "collector_observed_at": _RECEIVED_AT,
                "confirms_presence": True,
                "item": _vmi_item_wire(),
            },
        }
    )
    store = SimpleNamespace(
        apply_watch_event=AsyncMock(
            return_value=SimpleNamespace(
                event_id=model.event_id,
                resource_version="21",
                mutation_action=WatchMutationAction.OPEN,
                session_consumed=False,
                replayed=False,
            )
        )
    )
    service = _vmi_service(store)
    service._authenticated = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            model,
            SimpleNamespace(
                collector_id="kubevirt-vmis",
                transport_claim=_claim("watch-event"),
            ),
        )
    )
    service._require_generation = AsyncMock()  # type: ignore[method-assign]

    result = await service.watch_apply(SimpleNamespace())  # type: ignore[arg-type]

    assert result["mutation_action"] == "open"
    kwargs = store.apply_watch_event.await_args.kwargs
    assert kwargs["interval_mutator"] == service._vmi_reconciler.apply_watch
    assert kwargs["deletion_mutator"] is None
    assert kwargs["terminal_mutator"] is None
    assert not any("publication" in key or "usage" in key for key in kwargs)
    event = store.apply_watch_event.await_args.args[5]
    assert event.item is not None
    assert event.item.source_kind == "vmi"
    service._pod_reconciler.apply_watch.assert_not_awaited()
    service._storage_reconciler.apply_watch.assert_not_awaited()


def _ready_slice3_capabilities() -> MeteringSchemaCapabilities:
    return MeteringSchemaCapabilities(
        app_tables=(
            REQUIRED_APP_TABLES
            | REQUIRED_SLICE1_APP_TABLES
            | REQUIRED_SLICE2_CLAIM_APP_TABLES
            | REQUIRED_SLICE2_VOLUME_APP_TABLES
            | REQUIRED_SLICE3_COMPUTE_APP_TABLES
            | REQUIRED_SLICE3_STORAGE_APP_TABLES
        ),
        app_indexes=(
            REQUIRED_APP_INDEXES
            | REQUIRED_SLICE1_APP_INDEXES
            | REQUIRED_SLICE2_CLAIM_APP_INDEXES
            | REQUIRED_SLICE2_VOLUME_APP_INDEXES
            | REQUIRED_SLICE3_COMPUTE_APP_INDEXES
            | REQUIRED_SLICE3_STORAGE_APP_INDEXES
        ),
        app_triggers=(
            REQUIRED_APP_TRIGGERS
            | REQUIRED_SLICE1_APP_TRIGGERS
            | REQUIRED_SLICE2_CLAIM_APP_TRIGGERS
            | REQUIRED_SLICE2_VOLUME_APP_TRIGGERS
            | REQUIRED_SLICE3_COMPUTE_APP_TRIGGERS
            | REQUIRED_SLICE3_STORAGE_APP_TRIGGERS
        ),
        app_constraints=(
            REQUIRED_SLICE2_CLAIM_APP_CONSTRAINTS
            | REQUIRED_SLICE2_VOLUME_APP_CONSTRAINTS
            | REQUIRED_SLICE3_COMPUTE_APP_CONSTRAINTS
            | REQUIRED_SLICE3_STORAGE_APP_CONSTRAINTS
        ),
        app_columns=REQUIRED_SLICE3_COMPUTE_APP_COLUMNS,
        app_seed_rows_ready=True,
        storage_activation_seed_rows_ready=True,
        compute_activation_seed_rows_ready=True,
    )


def test_slice3_capability_requires_complete_schema_and_fixed_activation_rows() -> None:
    ready = _ready_slice3_capabilities()
    assert ready.slice1_inventory_ready
    assert ready.slice3_compute_inventory_ready
    assert ready.slice3_storage_lifecycle_ready
    assert ready.diagnostics()["slice3_compute_inventory_ready"] is True
    assert ready.diagnostics()["slice3_storage_lifecycle_ready"] is True

    missing_seed = replace(ready, compute_activation_seed_rows_ready=False)
    assert not missing_seed.slice3_compute_inventory_ready

    trigger = next(iter(REQUIRED_SLICE3_COMPUTE_APP_TRIGGERS))
    missing_trigger = replace(ready, app_triggers=ready.app_triggers - {trigger})
    assert not missing_trigger.slice3_compute_inventory_ready
    assert missing_trigger.missing_slice3_compute_app_triggers == {trigger}

    storage_trigger = next(iter(REQUIRED_SLICE3_STORAGE_APP_TRIGGERS))
    missing_storage_trigger = replace(
        ready,
        app_triggers=ready.app_triggers - {storage_trigger},
    )
    assert not missing_storage_trigger.slice3_storage_lifecycle_ready
    assert missing_storage_trigger.missing_slice3_storage_app_triggers == {
        storage_trigger
    }


def test_slice3_schema_and_activation_readiness_do_not_enable_publication() -> None:
    import main as orchestrator_main

    settings = _vm_settings()
    ready = _ready_slice3_capabilities()
    activation = ComputeActivation(
        activation_key="agent_pod",
        state="active",
        activated_at=_RECEIVED_AT,
        database_time=_RECEIVED_AT,
    )

    resources = (
        orchestrator_main._capability_gated_infrastructure_publication_resources(
            settings,
            ready,
            compute_activations={
                "agent_pod": activation,
                "workspace_vm": replace(activation, activation_key="workspace_vm"),
            },
        )
    )

    assert resources == ("workspace_pod",)
    assert settings.publication_enabled is False
    assert settings.agent_pod_publication_enabled is False
    assert settings.ide_pod_publication_enabled is False
    assert settings.vm_publication_enabled is False


def test_durable_activation_keeps_collection_mutators_armed() -> None:
    import main as orchestrator_main

    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=False,
        stable_cluster_id="main-dev",
        namespace_allowlist=("srw",),
        pvc_inventory_enabled=True,
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    active = {
        key: ComputeActivation(
            activation_key=key,
            state="active",
            activated_at=_RECEIVED_AT,
            database_time=_RECEIVED_AT,
        )
        for key in ("agent_pod", "ide_workspace_pod", "workspace_vm")
    }
    claim = orchestrator_main.StorageActivation(
        measurement_basis="claim-requested",
        state="active",
        activated_at=_RECEIVED_AT,
        database_time=_RECEIVED_AT,
    )
    source = orchestrator_main.StorageSourceActivation(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="main-dev",
        state="active",
        activated_at=_RECEIVED_AT,
        database_time=_RECEIVED_AT,
    )

    runtime = orchestrator_main._durable_collection_settings(
        settings,
        compute_activations=active,
        claim_activation=claim,
        source_activations=(source,),
    )

    assert runtime.shadow_enabled is True
    assert runtime.agent_pod_shadow_enabled is True
    assert runtime.ide_pod_shadow_enabled is True
    assert runtime.vm_shadow_enabled is True
    assert runtime.pvc_shadow_enabled is True
    assert runtime.pv_shadow_enabled is False
    assert runtime.publication_enabled is False


def test_remote_only_durable_activation_does_not_enable_primary_pod_shadow() -> None:
    import main as orchestrator_main

    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=False,
        stable_cluster_id="main-dev",
        namespace_allowlist=("srw",),
        vm_inventory_enabled=True,
        vm_stable_cluster_id="vm-cluster",
        vm_namespace="agent-vms",
    )
    vm_activation = ComputeActivation(
        activation_key="workspace_vm",
        state="active",
        activated_at=_RECEIVED_AT,
        database_time=_RECEIVED_AT,
    )

    runtime = orchestrator_main._durable_collection_settings(
        settings,
        compute_activations={"workspace_vm": vm_activation},
    )

    assert runtime.shadow_enabled is False
    assert runtime.vm_shadow_enabled is True
    assert runtime.agent_pod_shadow_enabled is False
    assert runtime.ide_pod_shadow_enabled is False


def test_storage_only_durable_activation_does_not_enable_primary_pod_shadow() -> None:
    import main as orchestrator_main

    settings = InfrastructureMeteringSettings(
        collector_enabled=True,
        shadow_enabled=False,
        stable_cluster_id="main-dev",
        namespace_allowlist=("srw",),
        pvc_inventory_enabled=True,
    )
    claim = orchestrator_main.StorageActivation(
        measurement_basis="claim-requested",
        state="active",
        activated_at=_RECEIVED_AT,
        database_time=_RECEIVED_AT,
    )
    source = orchestrator_main.StorageSourceActivation(
        measurement_basis="claim-requested",
        collector_id="kubernetes-pods",
        source_cluster="main-dev",
        state="active",
        activated_at=_RECEIVED_AT,
        database_time=_RECEIVED_AT,
    )

    runtime = orchestrator_main._durable_collection_settings(
        settings,
        claim_activation=claim,
        source_activations=(source,),
    )

    assert runtime.shadow_enabled is False
    assert runtime.pvc_shadow_enabled is True
    assert runtime.agent_pod_shadow_enabled is False
    assert runtime.ide_pod_shadow_enabled is False


def test_active_compute_rollover_gate_is_not_reversed_by_helm(monkeypatch) -> None:
    import main as orchestrator_main

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        InfrastructureMeteringSettings(),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_durable_compute_activation_keys",
        frozenset({"agent_pod"}),
    )

    assert orchestrator_main._compute_class_shadow_enabled("agent_pod") is True
    assert orchestrator_main._compute_class_shadow_enabled("ide_workspace_pod") is False
