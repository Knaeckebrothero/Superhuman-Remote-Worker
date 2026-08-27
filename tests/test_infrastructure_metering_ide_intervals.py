"""Focused Slice 3 on-demand IDE Pod metering contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
)
from orchestrator.services.infrastructure_metering.ide_intervals import (
    IdePodIntervalReconciler,
    project_ide_pod,
    resolve_ide_pod_attribution,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryItem,
    SanitizedInventoryError,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
)


JOB_ID = UUID("11111111-2222-4333-8444-555555555555")
USER_ID = UUID("22222222-3333-4444-8555-666666666666")
PROJECT_ID = UUID("33333333-4444-4555-8666-777777777777")
RECEIVED_AT = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
ACTIVATED_AT = datetime(2026, 8, 7, tzinfo=timezone.utc)
IDE_NAME = f"ide-{str(JOB_ID)[:12]}"


def _item(
    *,
    labels: dict[str, str] | None = None,
    name: str = IDE_NAME,
    source_uid: str = "ide-pod-uid",
    revision_hash: str = "a" * 64,
    accrues: bool = True,
    terminal: bool = False,
) -> InventoryItem:
    return InventoryItem(
        source_kind="pod",
        source_uid=source_uid,
        revision_hash=revision_hash,
        valid_for_metering=True,
        normalized_item={
            "source_kind": "pod",
            "api_version": "v1",
            "namespace": "srw",
            "name": name,
            "uid": source_uid,
            "resource_version": "17",
            "labels": labels
            or {
                "app": "srw-workspace",
                "srw/component": "ide-session",
                "srw/job-id": str(JOB_ID),
                "srw.io/component": "agent-workspace",
            },
            "owner_references": [],
            "lifecycle": {
                "accrues": accrues,
                "terminal": terminal,
                "creation_timestamp": "2026-08-07T11:59:00Z",
            },
            "capacity": {
                "cpu_millicores": 375,
                "memory_bytes": 768 * 1024**2,
                "capacity_quality": "exact",
                "measurement_algorithm": "pod-requests-fixture-v1",
            },
        },
    )


def _owner_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": JOB_ID,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "ide_container_name": IDE_NAME,
        "ide_restore_type": "k8s_container",
    }
    row.update(updates)
    return row


SCOPE_EPOCH_ID = uuid4()


def _activation(
    state: str = "active",
    *,
    activated_at: datetime | None = ACTIVATED_AT,
    database_time: datetime | None = RECEIVED_AT,
) -> ComputeActivation:
    return ComputeActivation(
        activation_key="ide_workspace_pod",
        state=state,
        activated_at=activated_at,
        database_time=database_time,
        authorized_scope_epoch_ids=frozenset({SCOPE_EPOCH_ID}),
    )


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
        source_cluster="cluster-a",
        namespace="srw",
        received_at=received_at,
        existing_interval_id=existing_interval_id,
        existing_source_revision=existing_source_revision,
    )


def _observation_context() -> SnapshotObservationContext:
    return SnapshotObservationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace="srw",
        received_at=RECEIVED_AT,
        current_interval_id=None,
        current_source_revision=None,
    )


def _existing_customer(interval_id: UUID, revision: str = "a" * 64) -> dict:
    return {
        "id": interval_id,
        "compute_scope_epoch_id": SCOPE_EPOCH_ID,
        "source_revision": revision,
        "attribution_scope": "customer",
        "owner_kind": "job",
        "owner_id": str(JOB_ID),
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "attribution_source": "app-db-ide-context-binding",
        "attribution_quality": "exact",
    }


def test_projects_only_exact_ide_shape_with_admitted_capacity() -> None:
    projection = project_ide_pod(_item())

    assert projection.applies
    assert projection.identity_consistent
    assert projection.owner_hint == JOB_ID
    assert projection.cpu_millicores == 375
    assert projection.memory_bytes == 768 * 1024**2

    ordinary = project_ide_pod(
        _item(
            name="workspace-job",
            labels={
                "app": "srw-workspace",
                "srw/component": "workspace",
                "srw/job-id": str(JOB_ID),
            },
        )
    )
    assert not ordinary.applies


def test_conflicting_thread_or_agent_hints_are_never_exact_ide_identity() -> None:
    projection = project_ide_pod(
        _item(
            labels={
                "app": "srw-workspace",
                "srw/component": "ide-session",
                "srw/job-id": str(JOB_ID),
                "srw.io/thread-id": str(uuid4()),
                "srw/purpose": "session",
            }
        )
    )

    assert projection.applies
    assert not projection.identity_consistent


@pytest.mark.asyncio
async def test_exact_job_context_and_inventory_namespace_are_customer_authority() -> (
    None
):
    conn = AsyncMock()
    conn.fetchrow.return_value = _owner_row()

    attribution = await resolve_ide_pod_attribution(
        conn,
        project_ide_pod(_item()),
        expected_namespace="srw",
    )

    assert attribution.scope == "customer"
    assert attribution.owner_kind == "job"
    assert attribution.owner_id == JOB_ID
    assert attribution.user_id == USER_ID
    assert attribution.project_id == PROJECT_ID
    assert (
        "context->'ide_session'->>'container_name'" in conn.fetchrow.await_args.args[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_updates", "reason"),
    [
        ({"ide_container_name": "another-pod"}, "ide-context-mismatch"),
        ({"ide_restore_type": "container"}, "ide-context-mismatch"),
        ({"user_id": None}, "ide-job-owner-invalid"),
    ],
)
async def test_stale_or_local_ide_context_is_unknown(
    row_updates: dict[str, object], reason: str
) -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = _owner_row(**row_updates)

    attribution = await resolve_ide_pod_attribution(
        conn,
        project_ide_pod(_item()),
        expected_namespace="srw",
    )

    assert attribution.scope == "unknown"
    assert attribution.reason_code == reason


@pytest.mark.asyncio
async def test_name_or_namespace_mismatch_fails_before_database_attribution() -> None:
    conn = AsyncMock()
    wrong_name = await resolve_ide_pod_attribution(
        conn,
        project_ide_pod(_item(name="ide-wrong")),
        expected_namespace="srw",
    )
    wrong_namespace = await resolve_ide_pod_attribution(
        conn,
        project_ide_pod(_item()),
        expected_namespace="another-namespace",
    )

    assert wrong_name.reason_code == "ide-pod-name-mismatch"
    assert wrong_namespace.reason_code == "ide-namespace-mismatch"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_activation_does_not_resolve_or_open_interval() -> None:
    conn = AsyncMock()
    reconciler = IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("disabled", activated_at=None, database_time=None),
    )

    result = await reconciler.apply_snapshot(conn, _snapshot_context(), _item())

    assert result is None
    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_workspace_pod_is_never_mutated_by_ide_adapter() -> None:
    conn = AsyncMock()
    ordinary = _item(
        name="workspace-job",
        labels={
            "app": "srw-workspace",
            "srw/component": "workspace",
            "srw/job-id": str(JOB_ID),
        },
    )

    result = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(conn, _snapshot_context(), ordinary)

    assert result is None
    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_ide_opens_customer_workspace_interval_without_publication() -> (
    None
):
    conn = AsyncMock()
    conn.fetchrow.return_value = _owner_row()
    conn.fetchval.side_effect = [1, True]

    interval_id = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(conn, _snapshot_context(), _item())

    assert isinstance(interval_id, UUID)
    assert conn.execute.await_count == 2
    insert = conn.execute.await_args_list[1]
    assert "'workspace_pod'" in insert.args[0]
    assert "resource_publication" not in insert.args[0]
    assert insert.args[12] == "customer"
    assert insert.args[13] == "job"
    assert insert.args[14] == str(JOB_ID)
    assert insert.args[19] == 375
    assert insert.args[20] == 768 * 1024**2
    assert insert.args[23] == RECEIVED_AT
    details = json.loads(insert.args[27])
    assert details["product_class"] == "ide-session"
    assert details["publication_enabled"] is False


@pytest.mark.asyncio
async def test_first_ide_interval_persists_conservative_lifecycle_start() -> None:
    scheduled_at = RECEIVED_AT - timedelta(seconds=45)
    item = _item()
    lifecycle = item.normalized_item["lifecycle"]
    assert isinstance(lifecycle, dict)
    lifecycle["pod_scheduled_condition"] = {
        "status": "True",
        "last_transition_time": scheduled_at.isoformat(),
    }

    conn = AsyncMock()
    conn.fetchrow.return_value = _owner_row()
    conn.fetchval.side_effect = [1, True]

    interval_id = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(conn, _snapshot_context(), item)

    assert isinstance(interval_id, UUID)
    insert = conn.execute.await_args_list[1]
    assert insert.args[23] == RECEIVED_AT
    assert insert.args[24] == "app-db-received"
    assert insert.args[25] == 45_000_000
    details = json.loads(insert.args[27])
    assert details["start_evidence_source"] == "pod-scheduled-transition"


@pytest.mark.asyncio
async def test_pre_boundary_snapshot_cannot_open_an_ide_interval() -> None:
    received_before_boundary = ACTIVATED_AT - timedelta(seconds=1)
    conn = AsyncMock()
    conn.fetchrow.return_value = _owner_row()
    conn.fetchval.side_effect = [1, True]

    with pytest.raises(InventoryConflictError, match="precedes activation"):
        await IdePodIntervalReconciler(
            shadow_enabled=True,
            activation=_activation(database_time=RECEIVED_AT),
        ).apply_snapshot(
            conn,
            _snapshot_context(received_at=received_before_boundary),
            _item(),
        )

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_revision_and_owner_confirms_existing_ide_interval() -> None:
    interval_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_owner_row(), _existing_customer(interval_id)]
    conn.fetchval.return_value = interval_id

    result = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(
            existing_interval_id=interval_id,
            existing_source_revision="a" * 64,
        ),
        _item(),
    )

    assert result == interval_id
    assert conn.execute.await_count == 0
    assert "last_confirmed_at=GREATEST" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_capacity_revision_splits_ide_interval_at_receipt() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        _owner_row(),
        _existing_customer(interval_id),
        {"id": interval_id, "source_lifecycle_id": lifecycle_id},
    ]
    conn.fetchval.side_effect = [True, 2, True]

    replacement = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(
            existing_interval_id=interval_id,
            existing_source_revision="a" * 64,
        ),
        _item(revision_hash="b" * 64),
    )

    assert replacement != interval_id
    close = conn.fetchrow.await_args_list[2]
    assert close.args[3] == "revision-changed"
    insert = conn.execute.await_args_list[1]
    assert insert.args[9] == "b" * 64
    assert insert.args[23] == RECEIVED_AT


@pytest.mark.asyncio
async def test_app_db_attribution_change_splits_without_kubernetes_revision() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    new_project_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        _owner_row(project_id=new_project_id),
        _existing_customer(interval_id),
        {"id": interval_id, "source_lifecycle_id": lifecycle_id},
    ]
    conn.fetchval.side_effect = [True, 2, True]

    replacement = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(
            existing_interval_id=interval_id,
            existing_source_revision="a" * 64,
        ),
        _item(),
    )

    assert replacement != interval_id
    close = conn.fetchrow.await_args_list[2]
    assert close.args[3] == "attribution-changed"
    insert = conn.execute.await_args_list[1]
    assert insert.args[16] == new_project_id


@pytest.mark.asyncio
async def test_terminal_ide_closes_without_resolving_owner_or_reopening() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": interval_id,
        "source_lifecycle_id": lifecycle_id,
    }
    conn.fetchval.return_value = True

    result = await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _item(accrues=False, terminal=True),
    )

    assert result is None
    assert conn.fetchrow.await_count == 1
    assert conn.fetchrow.await_args.args[3] == "terminal-or-unscheduled"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_job_is_metered_unknown_not_assigned_to_label_owner() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.side_effect = [1, True]

    await IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation(),
    ).apply_snapshot(conn, _snapshot_context(), _item())

    insert = conn.execute.await_args_list[1]
    assert insert.args[12] == "unknown"
    assert insert.args[13] is None
    assert insert.args[14] is None
    details = json.loads(insert.args[27])
    assert details["attribution_reason"] == "ide-job-missing"


@pytest.mark.asyncio
async def test_shadow_writes_one_exact_customer_row_for_ide_item() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = _owner_row()
    reconciler = IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("shadow", activated_at=None),
    )

    await reconciler.observe_snapshot(conn, _observation_context(), _item())

    assert conn.execute.await_count == 1
    insert = conn.execute.await_args
    assert "'ide_workspace_pod'" in insert.args[0]
    assert "'ide-session'" in insert.args[0]
    assert insert.args[4] == 375
    assert insert.args[5] == 768 * 1024**2
    assert insert.args[6] == "customer"
    assert insert.args[7] == "job"
    assert insert.args[8] == JOB_ID
    assert insert.args[11] == "eligible-unpriced"
    assert insert.args[12] == "ide-job-context-binding"


@pytest.mark.asyncio
async def test_shadow_writes_explicit_not_applicable_for_every_non_ide_pod() -> None:
    conn = AsyncMock()
    reconciler = IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("shadow", activated_at=None),
    )
    ordinary = _item(
        name="workspace-job",
        labels={
            "app": "srw-workspace",
            "srw/component": "workspace",
            "srw/job-id": str(JOB_ID),
        },
    )

    await reconciler.observe_snapshot(conn, _observation_context(), ordinary)

    assert conn.execute.await_count == 1
    insert = conn.execute.await_args
    assert insert.args[4] is None
    assert insert.args[5] is None
    assert insert.args[6] == "unknown"
    assert insert.args[11] == "not-applicable"
    assert insert.args[12] == "existing-workspace-slice-1"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_fallback_still_gets_exactly_one_ide_shadow_row() -> None:
    conn = AsyncMock()
    reconciler = IdePodIntervalReconciler(
        shadow_enabled=True,
        activation=_activation("shadow", activated_at=None),
    )
    invalid = InventoryItem(
        source_kind="pod",
        source_uid="bad-pod-uid",
        revision_hash=None,
        normalized_item={
            "source_kind": "pod",
            "uid": "bad-pod-uid",
            "namespace": "srw",
            "valid_for_metering": False,
            "revision_hash": None,
            "normalization_error": "invalid-pod-capacity",
        },
        valid_for_metering=False,
        item_error=SanitizedInventoryError(code="invalid-pod-capacity"),
    )

    await reconciler.observe_snapshot(conn, _observation_context(), invalid)

    assert conn.execute.await_count == 1
    insert = conn.execute.await_args
    assert insert.args[3] == "bad-pod-uid"
    assert insert.args[11] == "invalid"
    assert insert.args[12] == "invalid-pod-capacity"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_activation_schema_fails_closed_without_shadow_row() -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = asyncpg.UndefinedTableError("missing")

    await IdePodIntervalReconciler(shadow_enabled=True).observe_snapshot(
        conn,
        _observation_context(),
        _item(),
    )

    conn.execute.assert_not_awaited()
