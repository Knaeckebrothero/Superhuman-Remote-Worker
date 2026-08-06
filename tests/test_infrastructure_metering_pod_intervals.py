from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from orchestrator.services.infrastructure_metering.inventory import (
    InventoryItem,
    WatchEventKind,
    WatchIntervalMutationContext,
)
from orchestrator.services.infrastructure_metering.pod_intervals import (
    PodIntervalReconciler,
    project_workspace_pod,
)


OWNER_ID = UUID("11111111-2222-4333-8444-555555555555")


def _item(*, component="workspace", labels=None, lifecycle=None):
    effective_labels = {
        "app": "srw-workspace",
        "srw/component": component,
        "srw/job-id": str(OWNER_ID),
    }
    if labels is not None:
        effective_labels = labels
    return InventoryItem(
        source_kind="pod",
        source_uid="pod-uid",
        revision_hash="a" * 64,
        valid_for_metering=True,
        normalized_item={
            "api_version": "v1",
            "resource_version": "rv-2",
            "namespace": "srw",
            "name": "workspace-11111111",
            "labels": effective_labels,
            "lifecycle": lifecycle or {"accrues": True, "terminal": False},
            "capacity": {
                "cpu_millicores": 750,
                "memory_bytes": 2 * 1024**3,
                "capacity_quality": "exact",
                "measurement_algorithm": "pod-requests-fixture-v1",
                "overhead_cpu_millicores": 50,
                "overhead_memory_bytes": 64 * 1024**2,
            },
        },
    )


def test_projects_exact_job_workspace_capacity_and_owner():
    projected = project_workspace_pod(_item())

    assert projected.applies
    assert projected.accrues
    assert projected.owner_kind == "job"
    assert projected.owner_id == OWNER_ID
    assert projected.cpu_millicores == 750
    assert projected.memory_bytes == 2 * 1024**3


def test_projects_thread_workspace_only_with_full_unambiguous_uuid():
    projected = project_workspace_pod(
        _item(
            component="thread-workspace",
            labels={
                "app": "srw-workspace",
                "srw/component": "thread-workspace",
                "srw/thread-id": str(OWNER_ID),
            },
        )
    )
    assert projected.owner_kind == "thread"
    assert projected.owner_id == OWNER_ID

    ambiguous = project_workspace_pod(
        _item(
            labels={
                "app": "srw-workspace",
                "srw/component": "workspace",
                "srw/job-id": str(OWNER_ID),
                "srw/thread-id": str(OWNER_ID),
            }
        )
    )
    assert ambiguous.applies
    assert ambiguous.owner_id is None


def test_foreign_and_unscheduled_pods_do_not_accrue():
    foreign = project_workspace_pod(
        _item(labels={"app": "postgres", "srw/job-id": str(OWNER_ID)})
    )
    assert not foreign.applies
    assert foreign.reason_code == "non-workspace-pod"

    pending = project_workspace_pod(
        _item(lifecycle={"accrues": False, "terminal": False})
    )
    assert pending.applies
    assert not pending.accrues


def test_projects_only_sane_normalized_lifecycle_timestamps():
    projected = project_workspace_pod(
        _item(
            lifecycle={
                "accrues": True,
                "terminal": False,
                "creation_timestamp": "2026-08-06T08:00:00Z",
                "start_time": "2026-08-06T08:00:04+00:00",
                "pod_scheduled_condition": {
                    "status": "True",
                    "last_transition_time": "2026-08-06T08:00:03Z",
                },
            }
        )
    )

    assert projected.creation_timestamp == datetime(2026, 8, 6, 8, tzinfo=timezone.utc)
    assert projected.start_time == datetime(2026, 8, 6, 8, 0, 4, tzinfo=timezone.utc)
    assert projected.pod_scheduled_transition_time == datetime(
        2026, 8, 6, 8, 0, 3, tzinfo=timezone.utc
    )

    not_scheduled = project_workspace_pod(
        _item(
            lifecycle={
                "accrues": True,
                "terminal": False,
                "creation_timestamp": "not-a-timestamp",
                "start_time": "2026-08-06T08:00:04",
                "pod_scheduled_condition": {
                    "status": "False",
                    "last_transition_time": "2026-08-06T08:00:03Z",
                },
            }
        )
    )
    assert not_scheduled.creation_timestamp is None
    assert not_scheduled.start_time is None
    assert not_scheduled.pod_scheduled_transition_time is None


@pytest.mark.asyncio
async def test_watch_does_not_infer_absence_from_expired_snapshot_items():
    received_at = datetime(2026, 8, 6, 9, tzinfo=timezone.utc)
    transition_at = received_at - timedelta(seconds=5)
    item = _item(
        lifecycle={
            "accrues": True,
            "terminal": False,
            "creation_timestamp": "2026-08-06T08:59:00Z",
            "pod_scheduled_condition": {
                "status": "True",
                "last_transition_time": transition_at.isoformat(),
            },
        }
    )
    projection = project_workspace_pod(item)
    scope_id, epoch_id = uuid4(), uuid4()
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {
            "continuity_health": "healthy",
            "continuous_since": received_at - timedelta(minutes=2),
            "snapshot_id": uuid4(),
            "proof_at": received_at - timedelta(seconds=10),
            "complete": True,
            "manifest_state": "items-expired",
        },
        None,
    ]

    start = await PodIntervalReconciler._watch_start(
        conn,
        WatchIntervalMutationContext(
            scope_epoch_id=epoch_id,
            inventory_scope_id=scope_id,
            source_cluster="cluster-a",
            namespace="srw",
            event_type=WatchEventKind.ADDED,
            received_at=received_at,
            existing_interval_id=None,
            existing_source_revision=None,
        ),
        item,
        projection,
    )

    assert start.started_at == received_at
    assert start.source == "app-db-received"
    assert start.uncertainty_us == 5_000_000
    assert conn.fetchval.await_count == 0
