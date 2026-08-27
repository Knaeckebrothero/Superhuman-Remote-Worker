"""Focused Slice 2 storage projection and interval-reconciliation contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from orchestrator.services.infrastructure_metering.inventory import (
    InventoryItem,
    SnapshotAbsenceMutationContext,
    SnapshotCompletionContext,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
    WatchDeletionMutationContext,
    WatchMutationAction,
)
from orchestrator.services.infrastructure_metering.storage_assets import (
    StorageActivation,
    StorageActivationNotReady,
    VolumeAssetRecord,
)
from orchestrator.services.infrastructure_metering.storage_intervals import (
    StorageAttribution,
    StorageIntervalReconciler,
    project_storage_item,
)
from orchestrator.services.infrastructure_metering.storage_mapping import (
    StorageResourceMappingRule,
)


OWNER_ID = UUID("11111111-2222-4333-8444-555555555555")
USER_ID = UUID("22222222-3333-4444-8555-666666666666")
PROJECT_ID = UUID("33333333-4444-4555-8666-777777777777")
RECEIVED_AT = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)


def _pvc_item(
    *,
    name: str = "pvc-workspace-11111111-222",
    labels: dict[str, str] | None = None,
    source_uid: str = "claim-uid-1",
    phase: str = "Pending",
    deletion_requested: bool = False,
    storage_bytes: int = 4 * 1024**3,
) -> InventoryItem:
    return InventoryItem(
        source_kind="pvc",
        source_uid=source_uid,
        revision_hash="a" * 64,
        valid_for_metering=True,
        normalized_item={
            "source_kind": "pvc",
            "api_version": "v1",
            "namespace": "srw",
            "name": name,
            "uid": source_uid,
            "resource_version": "12",
            "labels": labels
            or {
                "app": "srw-workspace",
                "srw/component": "workspace-pvc",
                "srw/job-id": str(OWNER_ID),
            },
            "owner_references": [],
            "lifecycle": {
                "phase": phase,
                "accrues": True,
                "deletion_requested": deletion_requested,
                "creation_timestamp": "2026-08-06T11:59:55.000000Z",
                "deletion_timestamp": (
                    "2026-08-06T11:59:59.000000Z" if deletion_requested else None
                ),
            },
            "capacity": {
                "storage_bytes": storage_bytes,
                "source": "pvc-requested-storage",
                "measurement_algorithm": "pvc-request-storage-k8s-v1",
            },
            "capacity_evidence": {
                "original": "4Gi",
                "decimal_value": str(storage_bytes),
                "normalized_value": storage_bytes,
                "normalized_unit": "byte",
            },
            "storage_class": "longhorn-ephemeral",
            "access_modes": ["ReadWriteOnce"],
            "volume_mode": "Filesystem",
            "bound_volume_name": None,
            "measurement_basis": "claim-requested",
            "measurement_algorithm": "pvc-request-storage-k8s-v1",
            "diagnostics": [],
            "valid_for_metering": True,
            "revision_hash": "a" * 64,
        },
    )


def _vm_rootdisk_item(
    *,
    owner_kind: str = "job",
    owner_id: UUID = OWNER_ID,
    name: str | None = None,
    labels: dict[str, str] | None = None,
) -> InventoryItem:
    return _pvc_item(
        name=name or f"agent-vm-{owner_id}-rootdisk",
        labels=labels
        or {
            "srw.io/rootdisk": "true",
            "srw.io/owner-kind": owner_kind,
            "srw.io/owner-id": str(owner_id),
            # Historical controller label: identifier-only for both kinds.
            "job-id": str(owner_id),
        },
    )


def _vm_rootdisk_owner_row(
    *,
    owner_kind: str = "job",
    owner_id: UUID = OWNER_ID,
    vm_name: str | None = None,
    namespace: str = "srw",
    vm_status: str = "deleted",
    context_owner_kind: str | None = None,
    context_owner_id: UUID | None = None,
    rootdisk_pvc_uid: str | None = "claim-uid-1",
    identity_authenticated: bool | None = True,
    provision_generation: str | None = "00000000-0000-4000-8000-000000000001",
    identity_provision_generation: str | None = (
        "00000000-0000-4000-8000-000000000001"
    ),
) -> dict[str, object]:
    vm_context: dict[str, object] = {
        "status": vm_status,
        "vm_name": f"agent-vm-{owner_id}" if vm_name is None else vm_name,
        "namespace": namespace,
    }
    if identity_authenticated is not None:
        vm_context["identity_authenticated"] = identity_authenticated
    if provision_generation is not None:
        vm_context["provision_generation"] = provision_generation
    if identity_provision_generation is not None:
        vm_context["identity_provision_generation"] = identity_provision_generation
    if rootdisk_pvc_uid is not None:
        vm_context["rootdisk_pvc_uid"] = rootdisk_pvc_uid
    if context_owner_kind is not None:
        vm_context["owner_kind"] = context_owner_kind
    if context_owner_id is not None:
        vm_context["owner_id"] = str(context_owner_id)
    return {
        "owner_kind": owner_kind,
        "owner_id": owner_id,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "vm_context": vm_context,
    }


def _volume_item(
    *,
    source_uid: str = "c" * 64,
    pv_uid: str = "pv-object-uid-1",
    reclaim_policy: str = "Retain",
    claim_reference: dict[str, str] | None = None,
) -> InventoryItem:
    storage_bytes = 25 * 1024**3
    return InventoryItem(
        source_kind="volume",
        source_uid=source_uid,
        revision_hash="b" * 64,
        valid_for_metering=True,
        normalized_item={
            "source_kind": "volume",
            "api_version": "v1",
            "namespace": None,
            "name": "pvc-physical-1",
            "uid": source_uid,
            "resource_version": "19",
            "volume_identity": {
                "scheme": "csi-hmac-sha256-v1",
                "key_version": "storage-v1",
                "key_fingerprint": "d" * 64,
                "durable_asset_id": source_uid,
                "pv_uid": pv_uid,
            },
            "labels": {},
            "owner_references": [],
            "lifecycle": {
                "phase": "Bound",
                "accrues": True,
                "deletion_requested": False,
                "creation_timestamp": "2026-08-06T11:59:50.000000Z",
                "deletion_timestamp": None,
                "has_deletion_protection_finalizer": True,
            },
            "capacity": {
                "storage_bytes": storage_bytes,
                "source": "pv-provisioned-capacity",
                "measurement_algorithm": "pv-capacity-storage-k8s-v1",
            },
            "capacity_evidence": {
                "original": "25Gi",
                "decimal_value": str(storage_bytes),
                "normalized_value": storage_bytes,
                "normalized_unit": "byte",
            },
            "storage_class": "longhorn-ephemeral",
            "access_modes": ["ReadWriteOnce"],
            "volume_mode": "Filesystem",
            "reclaim_policy": reclaim_policy,
            "finalizers": ["external-provisioner.volume.kubernetes.io/finalizer"],
            "claim_reference": claim_reference,
            "csi_driver": "driver.longhorn.io",
            "measurement_basis": "volume-provisioned",
            "measurement_algorithm": "pv-capacity-storage-k8s-v1",
            "mapping_state": "unmapped",
            "resource": "unmapped_block_volume",
            "diagnostics": [],
            "valid_for_metering": True,
            "revision_hash": "b" * 64,
        },
    )


@pytest.mark.parametrize(
    ("app", "component", "owner_label", "name", "resource"),
    [
        (
            "srw-workspace",
            "workspace-pvc",
            "srw/job-id",
            "pvc-workspace-11111111-222",
            "workspace_pvc",
        ),
        (
            "srw-workspace",
            "workspace-pvc",
            "srw/thread-id",
            "pvc-ws-thread-11111111-222",
            "session_workspace_pvc",
        ),
        (
            "srw-agent",
            "agent-workspace-pvc",
            "srw/thread-id",
            "pvc-agent-s-11111111-222",
            "session_agent_pvc",
        ),
        (
            "srw-persistent-agent",
            "agent-workspace-pvc",
            "srw/thread-id",
            "pvc-persistent-11111111-222",
            "persistent_agent_pvc",
        ),
    ],
)
def test_exact_product_claim_shapes_keep_typed_resource_and_owner_hint(
    app: str,
    component: str,
    owner_label: str,
    name: str,
    resource: str,
) -> None:
    projection = project_storage_item(
        _pvc_item(
            name=name,
            labels={
                "app": app,
                "srw/component": component,
                owner_label: str(OWNER_ID),
            },
        )
    )

    assert projection.resource == resource
    assert projection.owner_hint is not None
    assert projection.owner_hint.id == OWNER_ID
    assert projection.owner_hint.kind == (
        "job" if owner_label == "srw/job-id" else "thread"
    )
    assert projection.static_attribution is None
    assert projection.valid_for_interval


@pytest.mark.parametrize(
    ("labels", "name", "reason"),
    [
        (
            {
                "app": "srw-workspace",
                "srw/component": "workspace-pvc",
                "srw/job-id": "11111111-222",
            },
            "pvc-workspace-11111111-222",
            "invalid-owner-label",
        ),
        (
            {
                "app": "srw-workspace",
                "srw/component": "workspace-pvc",
                "srw/job-id": str(OWNER_ID),
            },
            "pvc-workspace-wrong-name",
            "owner-name-mismatch",
        ),
        (
            {
                "app": "srw-workspace",
                "srw/component": "workspace-pvc",
                "srw/job-id": str(OWNER_ID),
                "srw/thread-id": str(OWNER_ID),
            },
            "pvc-workspace-11111111-222",
            "conflicting-owner-labels",
        ),
    ],
)
def test_untrusted_or_noncanonical_claim_owner_hints_remain_unknown(
    labels: dict[str, str], name: str, reason: str
) -> None:
    projection = project_storage_item(_pvc_item(name=name, labels=labels))

    assert projection.resource == "workspace_pvc"
    assert projection.owner_hint is None
    assert projection.static_attribution is not None
    assert projection.static_attribution.scope == "unknown"
    assert projection.classification_reason == reason
    assert projection.attribution_ambiguous


@pytest.mark.parametrize(
    ("name", "labels", "resource", "scope", "reason"),
    [
        (
            "agent-vm-11111111-rootdisk",
            {"srw.io/rootdisk": "true", "srw/job-id": str(OWNER_ID)},
            "vm_rootdisk_claim",
            "unknown",
            "vm-owner-hint-missing",
        ),
        (
            "golden-image-claim",
            {"srw.io/golden-image": "true"},
            "golden_image_pvc",
            "shared-platform",
            "golden-image-claim",
        ),
        (
            "postgres-data",
            {"app.kubernetes.io/managed-by": "Helm"},
            "platform_pvc",
            "shared-platform",
            "helm-platform-claim",
        ),
        (
            "foreign-claim",
            {"app": "foreign"},
            "unclassified_pvc",
            "unknown",
            "unclassified-pvc",
        ),
    ],
)
def test_vm_platform_and_unknown_claim_classification_is_explicit(
    name: str,
    labels: dict[str, str],
    resource: str,
    scope: str,
    reason: str,
) -> None:
    projection = project_storage_item(_pvc_item(name=name, labels=labels))

    assert projection.resource == resource
    assert projection.static_attribution is not None
    assert projection.static_attribution.scope == scope
    assert projection.classification_reason == reason


@pytest.mark.parametrize("owner_kind", ["job", "thread"])
@pytest.mark.parametrize("rootdisk_marker", [True, False])
def test_vm_rootdisk_requires_canonical_owner_and_exact_deterministic_name(
    owner_kind: str,
    rootdisk_marker: bool,
) -> None:
    labels = {
        "srw.io/owner-kind": owner_kind,
        "srw.io/owner-id": str(OWNER_ID),
        "job-id": str(OWNER_ID),
    }
    if rootdisk_marker:
        labels["srw.io/rootdisk"] = "true"

    projection = project_storage_item(
        _vm_rootdisk_item(owner_kind=owner_kind, labels=labels)
    )

    assert projection.source_kind == "pvc"
    assert projection.resource == "vm_rootdisk_claim"
    assert projection.owner_hint is not None
    assert projection.owner_hint.kind == owner_kind
    assert projection.owner_hint.id == OWNER_ID
    assert projection.static_attribution is None
    assert not projection.attribution_ambiguous


@pytest.mark.parametrize(
    ("labels", "name", "reason"),
    [
        (
            {"srw.io/rootdisk": "true", "srw.io/owner-id": str(OWNER_ID)},
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-owner-hint-missing",
        ),
        (
            {
                "srw.io/rootdisk": "true",
                "srw.io/owner-kind": "customer",
                "srw.io/owner-id": str(OWNER_ID),
            },
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-owner-hint-invalid",
        ),
        (
            {
                "srw.io/rootdisk": "true",
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": "11111111-2222-4333-8444-55555555555",
            },
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-owner-hint-invalid",
        ),
        (
            {
                "srw.io/rootdisk": "true",
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": str(OWNER_ID),
            },
            "agent-vm-spoofed-rootdisk",
            "vm-rootdisk-name-mismatch",
        ),
        (
            {
                "srw.io/rootdisk": "true",
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": str(OWNER_ID),
                "job-id": str(USER_ID),
            },
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-owner-hint-conflict",
        ),
        (
            {
                "srw.io/rootdisk": "true",
                "srw.io/owner-kind": "thread",
                "srw.io/owner-id": str(OWNER_ID),
                "srw/job-id": str(OWNER_ID),
            },
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-owner-hint-conflict",
        ),
        (
            {
                "srw.io/rootdisk": "false",
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": str(OWNER_ID),
            },
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-rootdisk-classification-conflict",
        ),
        (
            {
                "srw.io/rootdisk": "true",
                "srw.io/golden-image": "true",
                "srw.io/owner-kind": "job",
                "srw.io/owner-id": str(OWNER_ID),
            },
            f"agent-vm-{OWNER_ID}-rootdisk",
            "vm-rootdisk-classification-conflict",
        ),
    ],
)
def test_missing_invalid_or_conflicting_vm_rootdisk_hints_remain_unknown(
    labels: dict[str, str], name: str, reason: str
) -> None:
    projection = project_storage_item(_vm_rootdisk_item(name=name, labels=labels))

    assert projection.resource == "vm_rootdisk_claim"
    assert projection.owner_hint is None
    assert projection.static_attribution is not None
    assert projection.static_attribution.scope == "unknown"
    assert projection.classification_reason == reason
    assert projection.attribution_ambiguous


def test_pending_unmounted_and_deletion_requested_claims_still_accrue() -> None:
    pending = project_storage_item(_pvc_item(phase="Pending"))
    deleting = project_storage_item(_pvc_item(phase="Bound", deletion_requested=True))

    assert pending.valid_for_interval
    assert pending.bound_volume_name is None
    assert pending.storage_bytes == 4 * 1024**3
    assert deleting.valid_for_interval
    assert deleting.deletion_requested


def test_zero_byte_storage_remains_a_valid_counted_lifecycle() -> None:
    projection = project_storage_item(_pvc_item(storage_bytes=0))

    assert projection.storage_bytes == 0
    assert projection.valid_for_interval


@pytest.mark.asyncio
async def test_claim_customer_attribution_requires_live_app_database_owner() -> None:
    reconciler = StorageIntervalReconciler()
    projection = project_storage_item(_pvc_item())
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": OWNER_ID,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
    }

    attribution = await reconciler._resolve_claim_owner(conn, projection)

    assert attribution.scope == "customer"
    assert attribution.owner_kind == "job"
    assert attribution.owner_id == OWNER_ID
    assert attribution.user_id == USER_ID
    assert attribution.project_id == PROJECT_ID
    assert "FROM jobs" in conn.fetchrow.await_args.args[0]

    conn.fetchrow.return_value = None
    unresolved = await reconciler._resolve_claim_owner(conn, projection)
    assert unresolved.scope == "unknown"
    assert unresolved.reason_code == "owner-row-missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_kind", "vm_status"),
    [("job", "deleted"), ("thread", "suspended")],
)
async def test_vm_rootdisk_owner_uses_persisted_context_without_vmi_liveness(
    owner_kind: str,
    vm_status: str,
) -> None:
    projection = project_storage_item(_vm_rootdisk_item(owner_kind=owner_kind))
    conn = AsyncMock()
    conn.fetchrow.return_value = _vm_rootdisk_owner_row(
        owner_kind=owner_kind,
        vm_status=vm_status,
        context_owner_kind=owner_kind,
        context_owner_id=OWNER_ID,
    )

    attribution = await StorageIntervalReconciler._resolve_claim_owner(conn, projection)

    assert attribution.scope == "customer"
    assert attribution.owner_kind == owner_kind
    assert attribution.owner_id == OWNER_ID
    assert attribution.user_id == USER_ID
    assert attribution.project_id == PROJECT_ID
    assert attribution.source == "app-db-vm-rootdisk-owner-identity"
    assert attribution.reason_code == f"{owner_kind}-vm-rootdisk-identity"
    query = conn.fetchrow.await_args.args[0]
    assert f"FROM {'jobs' if owner_kind == 'job' else 'threads'}" in query
    assert "virtualmachine" not in query.lower()
    assert "datavolume" not in query.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (None, "owner-row-missing"),
        (
            _vm_rootdisk_owner_row(owner_id=USER_ID),
            "owner-row-identity-mismatch",
        ),
        (
            {**_vm_rootdisk_owner_row(), "vm_context": None},
            "vm-context-missing",
        ),
        (
            _vm_rootdisk_owner_row(identity_authenticated=False),
            "vm-identity-unauthenticated",
        ),
        (
            _vm_rootdisk_owner_row(provision_generation=None),
            "vm-identity-generation-missing",
        ),
        (
            _vm_rootdisk_owner_row(
                identity_provision_generation=("00000000-0000-4000-8000-000000000002")
            ),
            "vm-identity-generation-mismatch",
        ),
        (
            _vm_rootdisk_owner_row(vm_name="agent-vm-spoofed"),
            "vm-name-mismatch",
        ),
        (_vm_rootdisk_owner_row(vm_name=""), "vm-name-mismatch"),
        (
            _vm_rootdisk_owner_row(namespace="another-namespace"),
            "vm-namespace-mismatch",
        ),
        (
            _vm_rootdisk_owner_row(rootdisk_pvc_uid=None),
            "rootdisk-pvc-uid-missing",
        ),
        (
            _vm_rootdisk_owner_row(rootdisk_pvc_uid="replaced-claim-uid"),
            "rootdisk-pvc-uid-mismatch",
        ),
        (
            _vm_rootdisk_owner_row(rootdisk_pvc_uid="invalid uid"),
            "rootdisk-pvc-uid-invalid",
        ),
        (
            _vm_rootdisk_owner_row(context_owner_kind="thread"),
            "vm-owner-identity-mismatch",
        ),
        (
            _vm_rootdisk_owner_row(context_owner_kind=""),
            "vm-owner-identity-mismatch",
        ),
        (
            _vm_rootdisk_owner_row(context_owner_id=USER_ID),
            "vm-owner-identity-mismatch",
        ),
    ],
)
async def test_spoofed_vm_rootdisk_owner_or_context_remains_unknown(
    row: dict[str, object] | None,
    reason: str,
) -> None:
    projection = project_storage_item(_vm_rootdisk_item())
    conn = AsyncMock()
    conn.fetchrow.return_value = row

    attribution = await StorageIntervalReconciler._resolve_claim_owner(conn, projection)

    assert attribution.scope == "unknown"
    assert attribution.owner_id is None
    assert attribution.user_id is None
    assert attribution.project_id is None
    assert attribution.reason_code == reason


@pytest.mark.asyncio
async def test_unresolved_vm_rootdisk_hint_never_queries_owner_or_vm_state() -> None:
    projection = project_storage_item(
        _vm_rootdisk_item(
            labels={
                "srw.io/rootdisk": "true",
                "srw.io/owner-kind": "job",
            }
        )
    )
    conn = AsyncMock()

    attribution = await StorageIntervalReconciler._resolve_claim_owner(conn, projection)

    assert attribution.scope == "unknown"
    assert attribution.reason_code == "vm-owner-hint-missing"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_inherits_only_a_current_exact_claim_interval() -> None:
    claim_reference = {
        "uid": "claim-uid-1",
        "namespace": "srw",
        "name": "pvc-workspace-11111111-222",
    }
    projection = project_storage_item(_volume_item(claim_reference=claim_reference))
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "attribution_scope": "customer",
        "owner_kind": "job",
        "owner_id": str(OWNER_ID),
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
    }

    attribution = await StorageIntervalReconciler._resolve_volume_attribution(
        conn,
        source_cluster="cluster-a",
        projection=projection,
    )

    assert attribution.scope == "customer"
    assert attribution.owner_id == OWNER_ID
    assert attribution.source == "validated-claim-interval"
    sql, cluster, uid, namespace, name = conn.fetchrow.await_args.args
    assert "ended_at IS NULL" in sql
    assert "measurement_basis='claim-requested'" in sql
    assert (cluster, uid, namespace, name) == (
        "cluster-a",
        "claim-uid-1",
        "srw",
        "pvc-workspace-11111111-222",
    )

    conn.fetchrow.return_value = None
    unresolved = await StorageIntervalReconciler._resolve_volume_attribution(
        conn,
        source_cluster="cluster-a",
        projection=projection,
    )
    assert unresolved.scope == "unknown"
    assert unresolved.reason_code == "claim-interval-missing"


@pytest.mark.asyncio
async def test_volume_mapping_is_server_owned_and_persisted_as_provenance() -> None:
    rule = StorageResourceMappingRule(
        source_cluster="cluster-a",
        storage_class_name="longhorn-ephemeral",
        csi_driver="driver.longhorn.io",
        volume_mode="filesystem",
        resource="block_volume_longhorn_ephemeral",
        mapping_version="longhorn-ephemeral-v1",
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "source_cluster": rule.source_cluster,
        "storage_class_name": rule.storage_class_name,
        "csi_driver": rule.csi_driver,
        "volume_mode": rule.volume_mode,
        "resource": rule.resource,
        "mapping_version": rule.mapping_version,
        "rule_fingerprint": rule.fingerprint,
        "registered_at": RECEIVED_AT,
    }
    projection = await StorageIntervalReconciler._resolve_volume_mapping(
        conn,
        source_cluster="cluster-a",
        projection=project_storage_item(_volume_item()),
    )

    assert projection.resource == rule.resource
    assert projection.mapping_version == rule.mapping_version
    assert projection.mapping_fingerprint == rule.fingerprint
    assert "storage-resource-mapping:resolve-exact" in conn.fetchrow.await_args.args[0]

    conn.reset_mock()
    conn.fetchval.side_effect = [1, True]
    asset = VolumeAssetRecord(
        id=uuid4(),
        source_cluster="cluster-a",
        asset_digest="c" * 64,
        identity_scheme="csi-hmac-sha256-v1",
        identity_key_version="storage-v1",
        csi_driver="driver.longhorn.io",
        source_lifecycle_id=uuid4(),
        lifecycle_state="visible",
        first_observed_at=RECEIVED_AT,
        last_observed_at=RECEIVED_AT,
        replayed=False,
    )
    await StorageIntervalReconciler._open_interval(
        conn,
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        received_at=RECEIVED_AT,
        started_at=RECEIVED_AT,
        item=_volume_item(),
        projection=projection,
        attribution=StorageAttribution(
            scope="unknown",
            owner_kind=None,
            owner_id=None,
            user_id=None,
            project_id=None,
            source="kubernetes-identity-unresolved",
            quality="ambiguous",
            reason_code="claim-interval-missing",
        ),
        asset=asset,
    )
    details = json.loads(conn.execute.await_args_list[1].args[32])
    assert details["mapping_version"] == rule.mapping_version
    assert details["mapping_fingerprint"] == rule.fingerprint

    unmapped = replace(
        projection,
        resource="unmapped_block_volume",
        mapping_version=None,
        mapping_fingerprint=None,
    )
    assert unmapped.resource == "unmapped_block_volume"


@pytest.mark.asyncio
async def test_shadow_snapshot_writes_dedicated_and_generic_rows_without_interval() -> (
    None
):
    context = SnapshotObservationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace="srw",
        received_at=RECEIVED_AT,
        current_interval_id=None,
        current_source_revision=None,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": OWNER_ID,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
    }

    await StorageIntervalReconciler(shadow_enabled=True).observe_snapshot(
        conn, context, _pvc_item()
    )

    assert conn.execute.await_count == 2
    storage_shadow = conn.execute.await_args_list[0].args
    assert "INSERT INTO storage_shadow_observations" in storage_shadow[0]
    assert storage_shadow[3:9] == (
        "pvc",
        "claim-uid-1",
        "claim-requested",
        None,
        4 * 1024**3,
        "workspace_pvc",
    )
    assert storage_shadow[9:11] == (None, None)
    assert storage_shadow[11:15] == (
        "customer",
        "job",
        str(OWNER_ID),
        "eligible-unpriced",
    )
    generic_shadow = conn.execute.await_args_list[1].args
    assert "resource_inventory_shadow_comparisons" in generic_shadow[0]
    assert "'not-applicable'" in generic_shadow[0]
    assert "'dedicated-storage-shadow'" in generic_shadow[0]
    assert not any(
        "INSERT INTO resource_intervals" in str(call.args[0])
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_disabled_activation_leaves_valid_claim_inventory_only() -> None:
    context = SnapshotIntervalMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace="srw",
        received_at=RECEIVED_AT,
        existing_interval_id=None,
        existing_source_revision=None,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": OWNER_ID,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
    }

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "read_storage_activation",
        AsyncMock(side_effect=StorageActivationNotReady("shadow only")),
    ):
        interval_id = await StorageIntervalReconciler().apply_snapshot(
            conn, context, _pvc_item()
        )

    assert interval_id is None
    assert conn.execute.await_count == 0
    assert conn.fetchval.await_count == 0


@pytest.mark.asyncio
async def test_dark_remote_reconciler_never_consults_activation_or_opens_interval():
    context = SnapshotIntervalMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="vm-cluster",
        namespace="agent-vms",
        received_at=RECEIVED_AT,
        existing_interval_id=None,
        existing_source_revision=None,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": OWNER_ID,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
    }
    read_activation = AsyncMock(
        side_effect=AssertionError("remote dark source consulted activation")
    )

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "read_storage_activation",
        read_activation,
    ):
        interval_id = await StorageIntervalReconciler(
            interval_mutations_enabled=False
        ).apply_snapshot(conn, context, _pvc_item())

    assert interval_id is None
    read_activation.assert_not_awaited()
    assert not any(
        "resource_intervals" in str(call.args[0])
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_unchanged_storage_crosses_activation_at_exact_boundary() -> None:
    boundary = datetime(2026, 8, 7, tzinfo=timezone.utc)
    received_at = boundary + timedelta(minutes=5)
    context = SnapshotIntervalMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace="srw",
        received_at=received_at,
        existing_interval_id=None,
        existing_source_revision=None,
    )
    epoch = {
        "required_for_rollup": True,
        "required_from": boundary,
        "reliable_from": boundary - timedelta(hours=1),
        "continuous_since": boundary - timedelta(hours=1),
        "continuity_health": "healthy",
        "crossed_gap": False,
    }
    evidence = {"state": "present", "revision_hash": "a" * 64}
    conn = AsyncMock()
    conn.fetchrow.side_effect = [epoch, evidence]
    activation = StorageActivation(
        measurement_basis="claim-requested",
        state="active",
        activated_at=boundary,
        database_time=received_at,
    )
    with (
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "read_storage_activation",
            AsyncMock(return_value=activation),
        ),
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "lock_storage_activation",
            AsyncMock(return_value=boundary),
        ),
    ):
        started_at = await StorageIntervalReconciler._first_active_start(
            conn,
            context=context,
            item=_pvc_item(),
            projection=project_storage_item(_pvc_item()),
        )

    assert started_at == boundary

    conn.fetchrow.side_effect = [
        epoch,
        {"state": "modified", "revision_hash": "f" * 64},
    ]
    with (
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "read_storage_activation",
            AsyncMock(return_value=activation),
        ),
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "lock_storage_activation",
            AsyncMock(return_value=boundary),
        ),
    ):
        fallback = await StorageIntervalReconciler._first_active_start(
            conn,
            context=context,
            item=_pvc_item(),
            projection=project_storage_item(_pvc_item()),
        )
    assert fallback == received_at


@pytest.mark.asyncio
async def test_active_claim_interval_uses_locked_dimensions_and_capacity() -> None:
    item = _pvc_item()
    projection = project_storage_item(item)
    conn = AsyncMock()
    conn.fetchval.side_effect = [1, True]
    attribution = await StorageIntervalReconciler._resolve_claim_owner(
        AsyncMock(
            fetchrow=AsyncMock(
                return_value={
                    "id": OWNER_ID,
                    "user_id": USER_ID,
                    "project_id": PROJECT_ID,
                }
            )
        ),
        projection,
    )

    interval_id = await StorageIntervalReconciler._open_interval(
        conn,
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        received_at=RECEIVED_AT,
        started_at=RECEIVED_AT,
        item=item,
        projection=projection,
        attribution=attribution,
        asset=None,
    )

    assert isinstance(interval_id, UUID)
    insert = conn.execute.await_args_list[1].args
    assert "INSERT INTO resource_intervals" in insert[0]
    assert insert[4:8] == ("pvc", "claim-uid-1", "v1", "12")
    assert insert[13:18] == (
        "workspace_pvc",
        "claim-requested",
        "workload-allocation",
        "persistent-volume-claim",
        "customer",
    )
    assert insert[18:24] == (
        "job",
        str(OWNER_ID),
        USER_ID,
        PROJECT_ID,
        "app-db-owner-binding",
        "exact",
    )
    assert insert[25:28] == (
        4 * 1024**3,
        "pvc-requested-storage",
        "pvc-request-storage-k8s-v1",
    )
    assert insert[28] == RECEIVED_AT
    details = json.loads(insert[32])
    assert details["classification_reason"] == "workspace-job-claim"
    assert details["publication_gate_required"] is True


@pytest.mark.asyncio
async def test_retain_snapshot_absence_freezes_at_last_proof_and_opens_gap() -> None:
    interval_id, lifecycle_id, asset_id = uuid4(), uuid4(), uuid4()
    last_confirmed = RECEIVED_AT - timedelta(minutes=1)
    interval = {
        "id": interval_id,
        "source_kind": "volume",
        "source_lifecycle_id": lifecycle_id,
        "last_confirmed_at": last_confirmed,
        "details": json.dumps(
            {"storage_asset_id": str(asset_id), "reclaim_policy": "retain"}
        ),
    }
    context = SnapshotAbsenceMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace=None,
        received_at=RECEIVED_AT,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": interval_id,
        "source_lifecycle_id": lifecycle_id,
    }
    conn.fetchval.return_value = True
    gap = AsyncMock()

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "open_backend_unverified_gap",
        gap,
    ):
        consumed = await StorageIntervalReconciler().apply_absence(
            conn, context, interval
        )

    assert consumed
    gap.assert_awaited_once_with(
        conn,
        asset_id=asset_id,
        scope_epoch_id=context.scope_epoch_id,
        gap_start=last_confirmed,
        reason_code="retain-pv-disappeared",
    )
    close = conn.fetchrow.await_args.args
    assert close[1:] == (
        interval_id,
        last_confirmed,
        "backend-unverified",
        "retain-pv-backend-unverified",
        True,
    )


@pytest.mark.asyncio
async def test_dark_remote_absence_hook_cannot_close_supplied_interval() -> None:
    interval = {
        "id": uuid4(),
        "source_kind": "volume",
        "source_lifecycle_id": uuid4(),
        "last_confirmed_at": RECEIVED_AT,
        "details": json.dumps(
            {"storage_asset_id": str(uuid4()), "reclaim_policy": "retain"}
        ),
    }
    context = SnapshotAbsenceMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="vm-cluster",
        namespace=None,
        received_at=RECEIVED_AT,
    )
    conn = AsyncMock()

    consumed = await StorageIntervalReconciler(
        interval_mutations_enabled=False
    ).apply_absence(conn, context, interval)

    assert consumed is False
    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_policy_absence_stays_unknown_without_versioned_proof() -> None:
    interval_id, lifecycle_id, asset_id = uuid4(), uuid4(), uuid4()
    last_confirmed = RECEIVED_AT - timedelta(minutes=1)
    interval = {
        "id": interval_id,
        "source_kind": "volume",
        "source_lifecycle_id": lifecycle_id,
        "last_confirmed_at": last_confirmed,
        "details": json.dumps(
            {
                "storage_asset_id": str(asset_id),
                "reclaim_policy": "delete",
                "backend_deletion_finalizer_observed": True,
            }
        ),
    }
    context = SnapshotAbsenceMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace=None,
        received_at=RECEIVED_AT,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": interval_id,
        "source_lifecycle_id": lifecycle_id,
    }
    conn.fetchval.return_value = True
    gap = AsyncMock()

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "open_backend_unverified_gap",
        gap,
    ):
        assert await StorageIntervalReconciler().apply_absence(conn, context, interval)

    gap.assert_awaited_once_with(
        conn,
        asset_id=asset_id,
        scope_epoch_id=context.scope_epoch_id,
        gap_start=last_confirmed,
        reason_code="delete-finalizer-unverified",
    )
    assert conn.fetchrow.await_args.args[3:5] == (
        "backend-unverified",
        "delete-pv-backend-unverified",
    )


@pytest.mark.asyncio
async def test_complete_volume_snapshot_detaches_absent_retain_incarnation() -> None:
    incarnation_id, asset_id = uuid4(), uuid4()
    last_observed = RECEIVED_AT - timedelta(minutes=2)
    context = SnapshotCompletionContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace=None,
        received_at=RECEIVED_AT,
    )
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [
            {
                "incarnation_id": incarnation_id,
                "asset_id": asset_id,
                "pv_uid": "pv-object-uid-1",
                "reclaim_policy": "retain",
                "backend_deletion_finalizer_observed": False,
                "last_observed_at": last_observed,
                "identity_scheme": "csi-hmac-sha256-v1",
                "asset_digest": "c" * 64,
            }
        ],
        [],
    ]
    conn.fetchval.return_value = True
    gap = AsyncMock()

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "open_backend_unverified_gap",
        gap,
    ):
        await StorageIntervalReconciler().complete_snapshot(conn, context)

    detach = conn.fetchval.await_args.args
    assert "storage_volume_incarnations" in detach[0]
    assert detach[1:] == (incarnation_id, last_observed)
    gap.assert_awaited_once_with(
        conn,
        asset_id=asset_id,
        scope_epoch_id=context.scope_epoch_id,
        gap_start=last_observed,
        reason_code="retain-pv-disappeared",
    )


@pytest.mark.asyncio
async def test_dark_remote_deletion_detaches_asset_but_cannot_close_interval() -> None:
    incarnation_id, asset_id, supplied_interval_id = uuid4(), uuid4(), uuid4()
    last_observed = RECEIVED_AT - timedelta(seconds=10)
    context = WatchDeletionMutationContext(
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="vm-cluster",
        namespace=None,
        received_at=RECEIVED_AT,
        source_kind="volume",
        source_uid="c" * 64,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "incarnation_id": incarnation_id,
        "asset_id": asset_id,
        "pv_uid": "pv-object-uid-1",
        "reclaim_policy": "retain",
        "backend_deletion_finalizer_observed": False,
        "last_observed_at": last_observed,
        "identity_scheme": "csi-hmac-sha256-v1",
        "asset_digest": "c" * 64,
    }
    conn.fetchval.return_value = True
    gap = AsyncMock()
    supplied_last_confirmed = RECEIVED_AT - timedelta(minutes=1)
    supplied_interval = {
        "id": supplied_interval_id,
        "last_confirmed_at": supplied_last_confirmed,
    }

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "open_backend_unverified_gap",
        gap,
    ):
        action, interval_id = await StorageIntervalReconciler(
            interval_mutations_enabled=False
        ).apply_deletion(conn, context, supplied_interval)

    assert action is WatchMutationAction.ALREADY_ABSENT
    assert interval_id is None
    assert conn.fetchrow.await_count == 1
    assert conn.fetchval.await_args.args[1:] == (
        incarnation_id,
        supplied_last_confirmed,
    )
    assert all(
        "UPDATE resource_intervals" not in str(call.args[0])
        for call in conn.fetchrow.await_args_list
    )
    gap.assert_awaited_once_with(
        conn,
        asset_id=asset_id,
        scope_epoch_id=context.scope_epoch_id,
        gap_start=supplied_last_confirmed,
        reason_code="retain-pv-disappeared",
    )


@pytest.mark.asyncio
async def test_retain_deletion_without_open_interval_still_updates_asset_registry() -> (
    None
):
    incarnation_id, asset_id = uuid4(), uuid4()
    last_observed = RECEIVED_AT - timedelta(seconds=10)
    context = WatchDeletionMutationContext(
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace=None,
        received_at=RECEIVED_AT,
        source_kind="volume",
        source_uid="c" * 64,
    )
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "incarnation_id": incarnation_id,
        "asset_id": asset_id,
        "pv_uid": "pv-object-uid-1",
        "reclaim_policy": "retain",
        "backend_deletion_finalizer_observed": False,
        "last_observed_at": last_observed,
        "identity_scheme": "csi-hmac-sha256-v1",
        "asset_digest": "c" * 64,
    }
    conn.fetchval.return_value = True
    gap = AsyncMock()

    with patch(
        "orchestrator.services.infrastructure_metering.storage_intervals."
        "open_backend_unverified_gap",
        gap,
    ):
        action, interval_id = await StorageIntervalReconciler().apply_deletion(
            conn, context, None
        )

    assert action is WatchMutationAction.ALREADY_ABSENT
    assert interval_id is None
    assert conn.fetchval.await_args.args[1:] == (incarnation_id, last_observed)
    gap.assert_awaited_once_with(
        conn,
        asset_id=asset_id,
        scope_epoch_id=context.scope_epoch_id,
        gap_start=last_observed,
        reason_code="retain-pv-disappeared",
    )


@pytest.mark.asyncio
async def test_reobserved_retained_asset_resolves_gap_and_keeps_lifecycle() -> None:
    lifecycle_id, asset_id = uuid4(), uuid4()
    asset = VolumeAssetRecord(
        id=asset_id,
        source_cluster="cluster-a",
        asset_digest="c" * 64,
        identity_scheme="csi-hmac-sha256-v1",
        identity_key_version="storage-v1",
        csi_driver="driver.longhorn.io",
        source_lifecycle_id=lifecycle_id,
        lifecycle_state="backend-unverified",
        first_observed_at=RECEIVED_AT - timedelta(days=1),
        last_observed_at=RECEIVED_AT,
        replayed=True,
    )
    context = SnapshotObservationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace=None,
        received_at=RECEIVED_AT,
        current_interval_id=None,
        current_source_revision=None,
    )
    register = AsyncMock()
    ensure = AsyncMock(return_value=asset)
    resolve = AsyncMock()
    observe = AsyncMock()

    with (
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "register_storage_identity_key",
            register,
        ),
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "ensure_volume_asset",
            ensure,
        ),
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "resolve_backend_gap_reobserved",
            resolve,
        ),
        patch(
            "orchestrator.services.infrastructure_metering.storage_intervals."
            "observe_volume_incarnation",
            observe,
        ),
    ):
        result = await StorageIntervalReconciler._ensure_volume_asset(
            AsyncMock(),
            context=context,
            projection=project_storage_item(
                _volume_item(pv_uid="pv-object-uid-reimported")
            ),
        )

    assert result.source_lifecycle_id == lifecycle_id
    register.assert_awaited_once()
    resolve.assert_awaited_once_with(
        ensure.await_args.args[0],
        asset_id=asset_id,
        observed_at=RECEIVED_AT,
    )
    observe.assert_awaited_once()
