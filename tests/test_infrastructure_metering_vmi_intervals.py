"""Focused Slice 3B VMI attribution and interval adapter contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from orchestrator.services.infrastructure_metering.collectors.vmi_normalization import (
    normalize_virtual_machine_instance,
)
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryItem,
    SanitizedInventoryError,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
)
from orchestrator.services.infrastructure_metering.vmi_intervals import (
    VMIIntervalReconciler,
    project_vmi,
    resolve_vmi_attribution,
)


JOB_ID = UUID("22222222-3333-4444-8555-666666666666")
THREAD_ID = UUID("11111111-2222-4333-8444-555555555555")
USER_ID = UUID("44444444-5555-4666-8777-888888888888")
PROJECT_ID = UUID("55555555-6666-4777-8888-999999999999")
RECEIVED_AT = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
GIB = 1024**3


def _raw_vmi(
    *,
    owner_kind: str = "job",
    owner_id: UUID = JOB_ID,
) -> dict[str, object]:
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "metadata": {
            "uid": "vmi-uid-one",
            "namespace": "agent-vms",
            "name": "agent-vm-one",
            "resourceVersion": "12345",
            "creationTimestamp": "2026-08-07T10:00:00Z",
            "labels": {
                "srw.io/owner-kind": owner_kind,
                "srw.io/owner-id": str(owner_id),
            },
            "ownerReferences": [
                {
                    "apiVersion": "kubevirt.io/v1",
                    "kind": "VirtualMachine",
                    "name": "agent-vm-one",
                    "uid": "vm-uid-one",
                }
            ],
        },
        "spec": {
            "domain": {
                "cpu": {"cores": 2, "sockets": 2, "threads": 2},
                "memory": {"guest": "16Gi"},
            },
            "volumes": [
                {
                    "name": "rootdisk",
                    "dataVolume": {"name": "agent-vm-one-rootdisk"},
                }
            ],
        },
        "status": {
            "phase": "Running",
            "nodeName": "worker-a",
            "phaseTransitionTimestamps": [
                {
                    "phase": "Scheduled",
                    "phaseTransitionTimestamp": "2026-08-07T10:00:02Z",
                },
                {
                    "phase": "Running",
                    "phaseTransitionTimestamp": "2026-08-07T10:00:03Z",
                },
            ],
        },
    }


def _item(raw: dict[str, object] | None = None) -> InventoryItem:
    normalized = normalize_virtual_machine_instance(raw or _raw_vmi())
    return InventoryItem(
        source_kind="vmi",
        source_uid=normalized.uid,
        revision_hash=normalized.revision_hash,
        normalized_item=normalized.to_db_item(),
        valid_for_metering=normalized.valid_for_metering,
    )


def _owner_row(
    *,
    owner_kind: str = "job",
    owner_id: UUID = JOB_ID,
    owner_status: str = "processing",
    vm_status: str = "ready",
    vm_name: str = "agent-vm-one",
    namespace: str = "agent-vms",
    vm_uid: str | None = "vm-uid-one",
) -> dict[str, object]:
    vm_context: dict[str, object] = {
        "status": vm_status,
        "vm_name": vm_name,
        "namespace": namespace,
        "provision_generation": "00000000-0000-4000-8000-000000000001",
        "identity_authenticated": True,
        "identity_provision_generation": "00000000-0000-4000-8000-000000000001",
    }
    if vm_uid is not None:
        vm_context["vm_uid"] = vm_uid
    return {
        "owner_kind": owner_kind,
        "owner_id": owner_id,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "owner_status": owner_status,
        "vm_context": vm_context,
    }


SCOPE_EPOCH_ID = uuid4()


def _snapshot_context(
    *,
    received_at: datetime = RECEIVED_AT,
    existing_interval_id: UUID | None = None,
    existing_source_revision: str | None = None,
) -> SnapshotIntervalMutationContext:
    return SnapshotIntervalMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=SCOPE_EPOCH_ID,
        inventory_scope_id=uuid4(),
        source_cluster="vm-cluster-a",
        namespace="agent-vms",
        received_at=received_at,
        existing_interval_id=existing_interval_id,
        existing_source_revision=existing_source_revision,
    )


def _observation_context() -> SnapshotObservationContext:
    return SnapshotObservationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="vm-cluster-a",
        namespace="agent-vms",
        received_at=RECEIVED_AT,
        current_interval_id=None,
        current_source_revision=None,
    )


def _activation(
    state: str = "active",
    *,
    activated_at: datetime | None = None,
) -> ComputeActivation:
    boundary = activated_at or RECEIVED_AT - timedelta(days=1)
    return ComputeActivation(
        activation_key="workspace_vm",
        state=state,
        activated_at=boundary if state == "active" else None,
        database_time=RECEIVED_AT + timedelta(days=1),
        authorized_scope_epoch_ids=frozenset({SCOPE_EPOCH_ID}),
    )


def test_projection_uses_admitted_capacity_and_paused_migration_still_accrues() -> None:
    raw = _raw_vmi()
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "conditions": [{"type": "Paused", "status": "True"}],
        "migrationState": {
            "startTimestamp": "2026-08-07T10:03:00Z",
            "completed": False,
            "failed": False,
        },
    }

    projection = project_vmi(_item(raw))

    assert projection.accrues
    assert projection.paused
    assert projection.migrating
    assert projection.cpu_millicores == 8000
    assert projection.memory_bytes == 16 * GIB
    assert projection.measurement_algorithm == "kubevirt-vmi-current-guest-v2"


@pytest.mark.asyncio
async def test_customer_attribution_requires_owner_and_persisted_vm_identity() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]

    attribution = await resolve_vmi_attribution(conn, project_vmi(_item()))

    assert attribution.scope == "customer"
    assert attribution.owner_kind == "job"
    assert attribution.owner_id == JOB_ID
    assert attribution.user_id == USER_ID
    assert attribution.project_id == PROJECT_ID
    assert attribution.reason_code == "job-vm-identity"


@pytest.mark.asyncio
async def test_legacy_context_without_admitted_vm_uid_is_explicitly_unknown() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row(vm_uid=None)]

    attribution = await resolve_vmi_attribution(conn, project_vmi(_item()))

    assert attribution.scope == "unknown"
    assert attribution.quality == "ambiguous"
    assert attribution.user_id is None
    assert attribution.reason_code == "vm-uid-missing-legacy"


def test_projection_uses_current_capacity_during_hotplug_migration() -> None:
    raw = _raw_vmi()
    raw["spec"] = deepcopy(raw["spec"])
    raw["spec"]["domain"]["cpu"]["sockets"] = 4  # type: ignore[index]
    raw["spec"]["domain"]["memory"]["guest"] = "32Gi"  # type: ignore[index]
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "currentCPUTopology": {"cores": 2, "sockets": 2, "threads": 2},
        "memory": {"guestCurrent": "16Gi", "guestRequested": "32Gi"},
        "migrationState": {"completed": False, "failed": False},
    }

    projection = project_vmi(_item(raw))

    assert projection.migrating
    assert projection.cpu_millicores == 8000
    assert projection.memory_bytes == 16 * GIB
    assert projection.cpu_source == "vmi-status-current-topology"
    assert projection.memory_source == "vmi-status-guest-current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_update", "reason"),
    [
        ({"vm_name": "another-vm"}, "vm-name-mismatch"),
        ({"namespace": "another-namespace"}, "vm-namespace-mismatch"),
        ({"vm_uid": "another-uid"}, "vm-uid-mismatch"),
    ],
)
async def test_identity_mismatch_remains_unknown(
    row_update: dict[str, str], reason: str
) -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row(**row_update)]

    attribution = await resolve_vmi_attribution(conn, project_vmi(_item()))

    assert attribution.scope == "unknown"
    assert attribution.user_id is None
    assert attribution.project_id is None
    assert attribution.reason_code == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("vm_status", ["deleted", "suspended", "failed", "aborted"])
async def test_exact_identity_remains_attributable_during_vmi_deletion_tail(
    vm_status: str,
) -> None:
    """The admitted VMI, not an eager application status, ends allocation."""

    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row(vm_status=vm_status)]

    attribution = await resolve_vmi_attribution(conn, project_vmi(_item()))

    assert attribution.scope == "customer"
    assert attribution.reason_code == "job-vm-identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_status", ["awaiting_user", "suspended"])
async def test_exact_thread_identity_ignores_non_identity_thread_status(
    owner_status: str,
) -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [
        _owner_row(
            owner_kind="thread",
            owner_id=THREAD_ID,
            owner_status=owner_status,
        )
    ]

    attribution = await resolve_vmi_attribution(
        conn,
        project_vmi(_item(_raw_vmi(owner_kind="thread", owner_id=THREAD_ID))),
    )

    assert attribution.scope == "customer"
    assert attribution.owner_kind == "thread"
    assert attribution.owner_id == THREAD_ID


@pytest.mark.asyncio
async def test_pre_boundary_snapshot_cannot_open_a_vmi_interval() -> None:
    receipt = RECEIVED_AT - timedelta(hours=1)
    boundary = RECEIVED_AT
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchval.side_effect = [1, True]

    with pytest.raises(InventoryConflictError, match="precedes activation"):
        await VMIIntervalReconciler(
            shadow_enabled=True,
            activation=_activation(activated_at=boundary),
        ).apply_snapshot(conn, _snapshot_context(received_at=receipt), _item())

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_activation_never_opens_an_interval() -> None:
    conn = AsyncMock()

    result = await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("shadow"),
    ).apply_snapshot(conn, _snapshot_context(), _item())

    assert result is None
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_vmi_out_of_skew_transition_persists_receipt_start_and_uncertainty() -> (
    None
):
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchval.side_effect = [1, True]

    interval_id = await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
        max_lifecycle_clock_skew=timedelta(minutes=5),
    ).apply_snapshot(conn, _snapshot_context(), _item())

    assert isinstance(interval_id, UUID)
    insert = conn.execute.await_args_list[1]
    assert insert.args[23] == RECEIVED_AT
    assert insert.args[24] == "app-db-received"
    assert insert.args[25] == 2 * 60 * 60 * 1_000_000
    details = json.loads(insert.args[27])
    assert details["start_evidence_source"] == "object-creation-timestamp"


@pytest.mark.asyncio
async def test_terminal_vmi_closes_the_open_uid_at_receipt() -> None:
    raw = deepcopy(_raw_vmi())
    raw["status"] = {
        "phase": "Succeeded",
        "nodeName": "worker-a",
        "phaseTransitionTimestamps": [
            {
                "phase": "Succeeded",
                "phaseTransitionTimestamp": "2026-08-07T11:59:00Z",
            }
        ],
    }
    item = _item(raw)
    interval_id = uuid4()
    lifecycle_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": interval_id,
        "source_lifecycle_id": lifecycle_id,
    }
    conn.fetchval.return_value = True

    result = await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(
            existing_interval_id=interval_id,
            existing_source_revision="a" * 64,
        ),
        item,
    )

    assert result is None
    close = conn.fetchrow.await_args
    assert close.args[2] == RECEIVED_AT
    assert close.args[3] == "terminal"
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_vmi_revision_splits_without_stopping_accrual() -> None:
    raw = _raw_vmi()
    raw["status"] = {
        **raw["status"],  # type: ignore[arg-type]
        "conditions": [{"type": "Paused", "status": "True"}],
    }
    item = _item(raw)
    interval_id = uuid4()
    lifecycle_id = uuid4()
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchrow.side_effect = [
        {
            "id": interval_id,
            "compute_scope_epoch_id": SCOPE_EPOCH_ID,
            "source_revision": "a" * 64,
            "attribution_scope": "customer",
            "owner_kind": "job",
            "owner_id": str(JOB_ID),
            "user_id": USER_ID,
            "project_id": PROJECT_ID,
            "attribution_source": "app-db-vm-owner-identity",
            "attribution_quality": "exact",
        },
        {"id": interval_id, "source_lifecycle_id": lifecycle_id},
    ]
    conn.fetchval.side_effect = [True, 2, True]

    new_interval_id = await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(
            existing_interval_id=interval_id,
            existing_source_revision="a" * 64,
        ),
        item,
    )

    assert new_interval_id != interval_id
    assert conn.fetchrow.await_args_list[1].args[3] == "revision-changed"
    details = json.loads(conn.execute.await_args_list[1].args[27])
    assert details["paused"] is True


@pytest.mark.asyncio
async def test_completed_hotplug_splits_to_new_current_capacity_at_receipt() -> None:
    before_raw = _raw_vmi()
    before_raw["status"] = {
        **before_raw["status"],  # type: ignore[arg-type]
        "currentCPUTopology": {"cores": 2, "sockets": 2, "threads": 2},
        "memory": {"guestCurrent": "16Gi"},
        "migrationState": {"completed": False, "failed": False},
    }
    after_raw = deepcopy(before_raw)
    after_raw["status"]["currentCPUTopology"]["sockets"] = 4  # type: ignore[index]
    after_raw["status"]["memory"]["guestCurrent"] = "24Gi"  # type: ignore[index]
    after_raw["status"]["migrationState"] = {  # type: ignore[index]
        "completed": True,
        "failed": False,
    }
    before_item = _item(before_raw)
    after_item = _item(after_raw)
    interval_id = uuid4()
    lifecycle_id = uuid4()
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchrow.side_effect = [
        {
            "id": interval_id,
            "compute_scope_epoch_id": SCOPE_EPOCH_ID,
            "source_revision": before_item.revision_hash,
            "attribution_scope": "customer",
            "owner_kind": "job",
            "owner_id": str(JOB_ID),
            "user_id": USER_ID,
            "project_id": PROJECT_ID,
            "attribution_source": "app-db-vm-owner-identity",
            "attribution_quality": "exact",
        },
        {"id": interval_id, "source_lifecycle_id": lifecycle_id},
    ]
    conn.fetchval.side_effect = [True, 2, True]

    new_interval_id = await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(
            existing_interval_id=interval_id,
            existing_source_revision=before_item.revision_hash,
        ),
        after_item,
    )

    assert new_interval_id != interval_id
    assert conn.fetchrow.await_args_list[1].args[3] == "revision-changed"
    inserted = conn.execute.await_args_list[1]
    assert inserted.args[19] == 16000
    assert inserted.args[20] == 24 * GIB
    details = json.loads(inserted.args[27])
    assert details["cpu_source"] == "vmi-status-current-topology"
    assert details["memory_source"] == "vmi-status-guest-current"


@pytest.mark.asyncio
@pytest.mark.parametrize("activation_state", ["shadow", "active"])
async def test_shadow_writes_one_customer_row_per_active_vmi_item(
    activation_state: str,
) -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchval.return_value = True

    await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(activation_state),
    ).observe_snapshot(conn, _observation_context(), _item())

    assert conn.execute.await_count == 2
    insert = conn.execute.await_args_list[0]
    assert "'workspace_vm'" in insert.args[0]
    assert "'vmi'" in insert.args[0]
    assert insert.args[4] == "workspace-vm"
    assert insert.args[5] == 8000
    assert insert.args[6] == 16 * GIB
    assert insert.args[7] == "customer"
    assert insert.args[8] == "job"
    assert insert.args[9] == JOB_ID
    assert insert.args[12] == "eligible-unpriced"

    comparison = conn.execute.await_args_list[1]
    assert "resource_inventory_shadow_comparisons" in comparison.args[0]
    assert "'not-applicable'" in comparison.args[0]
    assert "'vmi-no-legacy-interval'" in comparison.args[0]
    assert "ON CONFLICT (snapshot_id,source_uid) DO NOTHING" in comparison.args[0]
    assert comparison.args[3] == "vmi-uid-one"
    assert comparison.args[4] == 8000
    assert comparison.args[5] == 16 * GIB
    assert comparison.args[6] == datetime(2026, 8, 7, 10, 0, 2, tzinfo=timezone.utc)
    assert comparison.args[7] == "vmi-scheduled-transition"
    assert comparison.args[8] == 0
    assert comparison.args[9] == RECEIVED_AT


@pytest.mark.asyncio
async def test_vmi_snapshot_comparison_replay_is_byte_stable_and_idempotent() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchval.return_value = True
    context = _observation_context()
    item = _item()
    reconciler = VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("shadow"),
    )

    await reconciler.observe_snapshot(conn, context, item)
    await reconciler.observe_snapshot(conn, context, item)

    comparisons = [
        call
        for call in conn.execute.await_args_list
        if "resource_inventory_shadow_comparisons" in call.args[0]
    ]
    assert len(comparisons) == 2
    assert comparisons[0].args == comparisons[1].args
    assert "ON CONFLICT (snapshot_id,source_uid) DO NOTHING" in comparisons[0].args[0]
    assert conn.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_vmi_snapshot_comparison_replay_fails_closed_on_content_drift() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_owner_row()]
    conn.fetchval.return_value = False

    with pytest.raises(InventoryConflictError, match="different content"):
        await VMIIntervalReconciler(
            shadow_enabled=True,
            activation=_activation("shadow"),
        ).observe_snapshot(conn, _observation_context(), _item())


@pytest.mark.asyncio
async def test_invalid_vmi_still_gets_exactly_one_shadow_row() -> None:
    invalid = InventoryItem(
        source_kind="vmi",
        source_uid="bad-vmi-uid",
        revision_hash=None,
        normalized_item={
            "source_kind": "vmi",
            "uid": "bad-vmi-uid",
            "valid_for_metering": False,
        },
        valid_for_metering=False,
        item_error=SanitizedInventoryError(code="invalid-vmi-capacity"),
    )
    conn = AsyncMock()

    await VMIIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("shadow"),
    ).observe_snapshot(conn, _observation_context(), invalid)

    conn.execute.assert_awaited_once()
    insert = conn.execute.await_args
    assert insert.args[3] == "bad-vmi-uid"
    assert insert.args[5] is None
    assert insert.args[6] is None
    assert insert.args[7] == "unknown"
    assert insert.args[12] == "invalid"
    assert insert.args[13] == "invalid-vmi-capacity"
    conn.fetch.assert_not_awaited()
