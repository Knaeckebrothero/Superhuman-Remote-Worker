"""Focused Slice 3A product-Pod and agent attribution contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from orchestrator.services.infrastructure_metering.agent_intervals import (
    AgentPodIntervalReconciler,
    PodProductClass,
    classify_product_pod,
    project_agent_pod,
    read_compute_metering_activation,
    resolve_agent_pod_attribution,
)
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryItem,
    SnapshotAbsenceMutationContext,
    SnapshotIntervalMutationContext,
    SnapshotObservationContext,
    SanitizedInventoryError,
    WatchDeletionMutationContext,
    WatchMutationAction,
    WatchTerminalMutationContext,
)


THREAD_ID = UUID("11111111-2222-4333-8444-555555555555")
JOB_ID = UUID("22222222-3333-4444-8555-666666666666")
AGENT_ID = UUID("33333333-4444-4555-8666-777777777777")
USER_ID = UUID("44444444-5555-4666-8777-888888888888")
PROJECT_ID = UUID("55555555-6666-4777-8888-999999999999")
RECEIVED_AT = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)


def _item(
    *,
    labels: dict[str, str],
    name: str = "srw-agent-j-deadbeef",
    source_uid: str = "pod-uid-1",
    owner_references: list[dict[str, str]] | None = None,
    accrues: bool = True,
) -> InventoryItem:
    return InventoryItem(
        source_kind="pod",
        source_uid=source_uid,
        revision_hash="a" * 64,
        valid_for_metering=True,
        normalized_item={
            "source_kind": "pod",
            "api_version": "v1",
            "namespace": "srw",
            "name": name,
            "uid": source_uid,
            "resource_version": "17",
            "labels": labels,
            "owner_references": owner_references or [],
            "lifecycle": {
                "accrues": accrues,
                "terminal": False,
                "creation_timestamp": "2026-08-06T11:59:00Z",
            },
            "capacity": {
                "cpu_millicores": 750,
                "memory_bytes": 2 * 1024**3,
                "capacity_quality": "exact",
                "measurement_algorithm": "pod-requests-fixture-v1",
            },
        },
    )


def _dynamic_job_item() -> InventoryItem:
    return _item(
        labels={
            "app": "srw-agent",
            "srw/component": "agent",
            "srw/managed-by": "agent-provisioner",
            "srw/purpose": "job",
        }
    )


def _dynamic_session_item() -> InventoryItem:
    return _item(
        name="srw-agent-s-deadbeef",
        labels={
            "app": "srw-agent",
            "srw/component": "agent",
            "srw/managed-by": "agent-provisioner",
            "srw/purpose": "session",
            "srw/thread-id": str(THREAD_ID)[:12],
            "srw.io/thread-id": str(THREAD_ID),
        },
    )


def _agent_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "agent_id": AGENT_ID,
        "agent_present": True,
        "pod_uid": "pod-uid-1",
        "hostname": "srw-agent-j-deadbeef",
        "identity_state": "valid",
        "attribution_scope": "shared-platform",
        "owner_kind": None,
        "owner_id": None,
        "user_id": None,
        "project_id": None,
        "reason_code": "unbound-agent",
        "revision": 1,
        "effective_at": RECEIVED_AT,
        "thread_agent_id": None,
        "thread_pod_name": None,
        "thread_pod_namespace": None,
    }
    row.update(updates)
    return row


SCOPE_EPOCH_ID = uuid4()
INVENTORY_SCOPE_ID = uuid4()


def _active_activation() -> ComputeActivation:
    return ComputeActivation(
        activation_key="agent_pod",
        state="active",
        activated_at=RECEIVED_AT - timedelta(days=1),
        database_time=RECEIVED_AT,
        authorized_scope_epoch_ids=frozenset({SCOPE_EPOCH_ID}),
    )


def _snapshot_context(
    *,
    received_at: datetime = RECEIVED_AT,
    existing_interval_id: UUID | None = None,
) -> SnapshotIntervalMutationContext:
    return SnapshotIntervalMutationContext(
        snapshot_id=uuid4(),
        scope_epoch_id=SCOPE_EPOCH_ID,
        inventory_scope_id=INVENTORY_SCOPE_ID,
        source_cluster="cluster-a",
        namespace="srw",
        received_at=received_at,
        existing_interval_id=existing_interval_id,
        existing_source_revision="a" * 64 if existing_interval_id else None,
    )


def _binding_event(
    revision: int,
    effective_at: datetime,
    *,
    scope: str,
    reason: str,
    pod_uid: str = "pod-uid-1",
) -> dict[str, object]:
    row = _agent_row(
        revision=revision,
        effective_at=effective_at,
        pod_uid=pod_uid,
        attribution_scope=scope,
        reason_code=reason,
    )
    row.update(
        {
            "id": uuid4(),
            "transition_source": "test-transition",
        }
    )
    if scope == "customer":
        row.update(
            {
                "owner_kind": "job",
                "owner_id": JOB_ID,
                "user_id": USER_ID,
                "project_id": PROJECT_ID,
            }
        )
    else:
        row.update(
            {
                "owner_kind": None,
                "owner_id": None,
                "user_id": None,
                "project_id": None,
            }
        )
    return row


def _open_interval_row(
    interval_id: UUID,
    lifecycle_id: UUID,
    *,
    binding_revision: int,
    binding_effective_at: datetime,
    scope: str = "shared-platform",
    started_at: datetime | None = None,
    last_confirmed_at: datetime | None = None,
) -> dict[str, object]:
    if scope == "customer":
        owner_kind = "job"
        owner_id = str(JOB_ID)
        user_id = USER_ID
        project_id = PROJECT_ID
        source = "app-db-agent-mutual-binding"
        quality = "exact"
        reason = "job-agent-mutual-binding"
    elif scope == "unknown":
        owner_kind = owner_id = user_id = project_id = None
        source = "app-db-agent-identity-conflict"
        quality = "ambiguous"
        reason = "job-binding-conflict"
    else:
        owner_kind = "platform"
        owner_id = user_id = project_id = None
        source = "app-db-agent-unbound"
        quality = "exact"
        reason = "warm-agent-unbound"
    start = started_at or binding_effective_at
    confirmed = last_confirmed_at or start
    return {
        "id": interval_id,
        "inventory_scope_id": INVENTORY_SCOPE_ID,
        "source_cluster": "cluster-a",
        "source_kind": "pod",
        "source_uid": "pod-uid-1",
        "source_api_version": "v1",
        "source_resource_version": "17",
        "source_lifecycle_id": lifecycle_id,
        "compute_scope_epoch_id": SCOPE_EPOCH_ID,
        "source_revision": "a" * 64,
        "started_at": start,
        "last_confirmed_at": confirmed,
        "attribution_scope": scope,
        "owner_kind": owner_kind,
        "owner_id": owner_id,
        "user_id": user_id,
        "project_id": project_id,
        "attribution_source": source,
        "attribution_quality": quality,
        "resource": "agent_pod",
        "namespace": "srw",
        "name": "srw-agent-j-deadbeef",
        "cpu_millicores": 750,
        "memory_bytes": 2 * 1024**3,
        "capacity_quality": "exact",
        "measurement_algorithm": "pod-requests-fixture-v1",
        "details": {
            "agent_id": str(AGENT_ID),
            "attribution_reason": reason,
            "binding_effective_at": binding_effective_at.isoformat(),
            "binding_revision": binding_revision,
            "classification_reason": "dynamic-job-agent",
            "identity_consistent": True,
            "product_class": "dynamic-agent",
            "purpose": "job",
            "thread_hint": None,
            "creation_timestamp": "2026-08-06T11:59:00+00:00",
            "start_time": None,
            "scheduled_transition_timestamp": None,
            "publication_enabled": False,
            "slice": "kubernetes-agent-shadow-v1",
        },
    }


def _journal_connection(
    existing: dict[str, object],
    events: list[dict[str, object]],
) -> AsyncMock:
    conn = AsyncMock()
    head = dict(events[-1])
    next_interval_revision = 1

    def fetchrow(sql: str, *args: object) -> object:
        if "FROM resource_intervals" in sql and "FOR UPDATE" in sql:
            return existing
        if "lock-agent-binding-head" in sql:
            return head
        if "UPDATE resource_intervals SET ended_at" in sql:
            return {
                "id": args[0],
                "source_lifecycle_id": existing["source_lifecycle_id"],
            }
        raise AssertionError(f"unexpected fetchrow: {sql}")

    def fetch(sql: str, *args: object) -> object:
        if "replay-agent-bindings" not in sql:
            raise AssertionError(f"unexpected fetch: {sql}")
        return events

    def fetchval(sql: str, *args: object) -> object:
        nonlocal next_interval_revision
        if "latest_revision_no=latest_revision_no+1" in sql:
            next_interval_revision += 1
            return next_interval_revision
        if sql.startswith("INSERT INTO resource_intervals"):
            return args[0]
        if "current_interval_id=NULL" in sql:
            return True
        if "SET current_interval_id=$2" in sql:
            return True
        if "last_confirmed_at=GREATEST" in sql:
            return args[0]
        raise AssertionError(f"unexpected fetchval: {sql}")

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.side_effect = fetch
    conn.fetchval.side_effect = fetchval
    return conn


@pytest.mark.parametrize(
    ("item", "expected", "resource"),
    [
        (
            _item(
                name="workspace-job",
                labels={
                    "app": "srw-workspace",
                    "srw/component": "workspace",
                    "srw/job-id": str(JOB_ID),
                },
            ),
            PodProductClass.EXISTING_WORKSPACE,
            "workspace_pod",
        ),
        (
            _item(
                name=f"ide-{str(JOB_ID)[:12]}",
                labels={
                    "app": "srw-workspace",
                    "srw/component": "ide-session",
                    "srw/job-id": str(JOB_ID),
                },
            ),
            PodProductClass.IDE_WORKSPACE,
            "workspace_pod",
        ),
        (_dynamic_job_item(), PodProductClass.DYNAMIC_AGENT, "agent_pod"),
        (
            _item(
                name=f"persistent-{str(THREAD_ID)[:12]}",
                labels={
                    "app": "srw-persistent-agent",
                    "srw/component": "persistent-agent",
                    "srw/thread-id": str(THREAD_ID),
                },
            ),
            PodProductClass.PERSISTENT_AGENT,
            "agent_pod",
        ),
        (
            _item(
                name="srw-agent-stateless-75fb684dbb-x1",
                labels={"app": "srw-agent-stateless", "srw/component": "agent"},
            ),
            PodProductClass.STATELESS_AGENT,
            "agent_pod",
        ),
        (
            _item(name="postgres", labels={"app": "postgres"}),
            PodProductClass.OTHER,
            None,
        ),
    ],
)
def test_product_classifier_keeps_workspace_agent_and_other_shapes_distinct(
    item: InventoryItem,
    expected: PodProductClass,
    resource: str | None,
) -> None:
    classification = classify_product_pod(item)

    assert classification.product_class == expected
    assert classification.resource == resource
    if expected == PodProductClass.IDE_WORKSPACE:
        assert classification.activation_key == "ide_workspace_pod"
        assert classification.product_class.value == "ide-session"
    elif expected in {
        PodProductClass.DYNAMIC_AGENT,
        PodProductClass.PERSISTENT_AGENT,
        PodProductClass.STATELESS_AGENT,
    }:
        assert classification.activation_key == "agent_pod"


def test_vmi_owner_reference_is_an_explicit_exclusion_even_with_agent_labels() -> None:
    item = _dynamic_job_item()
    payload = dict(item.normalized_item)
    payload["owner_references"] = [
        {"kind": "VirtualMachineInstance", "uid": "vmi-uid", "name": "vm-a"}
    ]
    item = InventoryItem(
        source_kind="pod",
        source_uid=item.source_uid,
        revision_hash=item.revision_hash,
        normalized_item=payload,
        valid_for_metering=True,
    )

    classification = classify_product_pod(item)

    assert classification.product_class == PodProductClass.VIRT_LAUNCHER
    assert classification.explicitly_excluded
    assert classification.reason_code == "virt-launcher-excluded"


def test_dynamic_session_requires_full_uuid_and_matching_short_hint() -> None:
    valid = classify_product_pod(_dynamic_session_item())
    assert valid.identity_consistent
    assert project_agent_pod(_dynamic_session_item()).thread_hint == THREAD_ID

    invalid_item = _dynamic_session_item()
    payload = dict(invalid_item.normalized_item)
    labels = dict(payload["labels"])
    labels["srw/thread-id"] = "different"
    payload["labels"] = labels
    invalid_item = InventoryItem(
        source_kind="pod",
        source_uid=invalid_item.source_uid,
        revision_hash=invalid_item.revision_hash,
        normalized_item=payload,
        valid_for_metering=True,
    )
    invalid = classify_product_pod(invalid_item)
    assert not invalid.identity_consistent
    assert invalid.reason_code == "dynamic-agent-identity-conflict"


def _stateless_item(extra_labels: dict[str, str] | None = None) -> InventoryItem:
    labels = {"app": "srw-agent-stateless", "srw/component": "agent"}
    labels.update(extra_labels or {})
    return _item(name="srw-agent-stateless-75fb684dbb-x1", labels=labels)


@pytest.mark.asyncio
async def test_stateless_pool_attributes_to_platform_without_registration() -> None:
    """The executor pool never registers per-pod identity, so attribution must
    resolve to shared-platform up front — no mutual-binding lookup, no
    binding cursor (the confirm path requires exactly that shape)."""
    conn = AsyncMock()

    attribution = await resolve_agent_pod_attribution(
        conn, project_agent_pod(_stateless_item())
    )

    assert attribution.scope == "shared-platform"
    assert attribution.owner_kind == "platform"
    assert attribution.reason_code == "stateless-executor-pool"
    assert attribution.agent_id is None
    assert attribution.binding_revision is None
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_pod_with_identity_labels_fails_toward_unknown() -> None:
    """Identity labels contradict the pool's contract (pods serve many units);
    a mislabeled pod must not be silently attributed to the platform."""
    item = _stateless_item({"srw/job-id": str(JOB_ID)})
    classification = classify_product_pod(item)
    assert not classification.identity_consistent
    assert classification.reason_code == "stateless-agent-identity-conflict"

    attribution = await resolve_agent_pod_attribution(
        AsyncMock(), project_agent_pod(item)
    )
    assert attribution.scope == "unknown"


@pytest.mark.asyncio
async def test_registered_unbound_job_agent_is_shared_platform() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_agent_row()]

    attribution = await resolve_agent_pod_attribution(
        conn, project_agent_pod(_dynamic_job_item())
    )

    assert attribution.scope == "shared-platform"
    assert attribution.owner_kind == "platform"
    assert attribution.agent_id == AGENT_ID
    assert attribution.reason_code == "warm-agent-unbound"


@pytest.mark.asyncio
async def test_job_customer_attribution_requires_mutual_agent_job_binding() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [
        _agent_row(
            attribution_scope="customer",
            owner_kind="job",
            owner_id=JOB_ID,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            reason_code="job-mutual-binding",
        )
    ]

    attribution = await resolve_agent_pod_attribution(
        conn, project_agent_pod(_dynamic_job_item())
    )

    assert attribution.scope == "customer"
    assert attribution.owner_kind == "job"
    assert attribution.owner_id == JOB_ID
    assert attribution.user_id == USER_ID
    assert attribution.project_id == PROJECT_ID


@pytest.mark.asyncio
async def test_one_sided_job_assignment_is_unknown_not_shared() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [
        _agent_row(
            attribution_scope="unknown",
            reason_code="job-binding-conflict",
        )
    ]

    attribution = await resolve_agent_pod_attribution(
        conn, project_agent_pod(_dynamic_job_item())
    )

    assert attribution.scope == "unknown"
    assert attribution.reason_code == "job-binding-conflict"


@pytest.mark.asyncio
async def test_session_attribution_requires_uid_name_thread_and_context_agreement() -> (
    None
):
    conn = AsyncMock()
    conn.fetch.return_value = [
        _agent_row(
            hostname="srw-agent-s-deadbeef",
            attribution_scope="customer",
            owner_kind="thread",
            owner_id=THREAD_ID,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            reason_code="thread-mutual-binding",
            thread_agent_id=AGENT_ID,
            thread_pod_name="srw-agent-s-deadbeef",
            thread_pod_namespace="srw",
        )
    ]

    attribution = await resolve_agent_pod_attribution(
        conn, project_agent_pod(_dynamic_session_item())
    )

    assert attribution.scope == "customer"
    assert attribution.owner_kind == "thread"
    assert attribution.owner_id == THREAD_ID


@pytest.mark.asyncio
async def test_duplicate_agent_rows_for_pod_uid_are_unknown() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_agent_row(), _agent_row(agent_id=uuid4())]

    attribution = await resolve_agent_pod_attribution(
        conn, project_agent_pod(_dynamic_job_item())
    )

    assert attribution.scope == "unknown"
    assert attribution.reason_code == "agent-pod-uid-ambiguous"


@pytest.mark.asyncio
async def test_missing_activation_schema_fails_closed() -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = asyncpg.UndefinedTableError("missing")

    assert await read_compute_metering_activation(conn) is None


@pytest.mark.asyncio
async def test_disabled_activation_does_not_resolve_or_open_agent_interval() -> None:
    conn = AsyncMock()
    reconciler = AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=ComputeActivation(
            activation_key="agent_pod",
            state="disabled",
            activated_at=None,
        ),
    )

    result = await reconciler.apply_snapshot(
        conn,
        SnapshotIntervalMutationContext(
            snapshot_id=uuid4(),
            scope_epoch_id=uuid4(),
            inventory_scope_id=uuid4(),
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            existing_interval_id=None,
            existing_source_revision=None,
        ),
        _dynamic_job_item(),
    )

    assert result is None
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_binding_transitions_between_relists_split_each_snapshot() -> (
    None
):
    interval_id = uuid4()
    lifecycle_id = uuid4()
    first_seen = RECEIVED_AT - timedelta(hours=1)
    customer_at = RECEIVED_AT - timedelta(minutes=45)
    unknown_at = RECEIVED_AT - timedelta(minutes=30)
    shared_at = RECEIVED_AT - timedelta(minutes=15)
    events = [
        _binding_event(1, first_seen, scope="shared-platform", reason="unbound-agent"),
        _binding_event(
            2,
            customer_at,
            scope="customer",
            reason="job-mutual-binding",
        ),
        _binding_event(
            3,
            unknown_at,
            scope="unknown",
            reason="job-binding-conflict",
        ),
        _binding_event(4, shared_at, scope="shared-platform", reason="unbound-agent"),
    ]
    conn = _journal_connection(
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=1,
            binding_effective_at=first_seen,
        ),
        events,
    )

    new_interval_id = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _dynamic_job_item(),
    )

    assert new_interval_id != interval_id
    close_calls = [
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE resource_intervals SET ended_at" in call.args[0]
    ]
    assert [call.args[2] for call in close_calls] == [
        customer_at,
        unknown_at,
        shared_at,
    ]
    assert [call.args[3] for call in close_calls] == [
        "binding-revision-2",
        "binding-revision-3",
        "binding-revision-4",
    ]
    assert all(call.args[4] == "app-db-agent-binding-event" for call in close_calls)
    assert all(call.args[5] == 0 for call in close_calls)
    inserts = [
        call
        for call in conn.fetchval.await_args_list
        if call.args[0].startswith("INSERT INTO resource_intervals")
    ]
    assert [call.args[3] for call in inserts] == [
        "customer",
        "unknown",
        "shared-platform",
    ]
    assert [call.args[4] for call in inserts] == ["job", None, "platform"]
    details = [json.loads(call.args[12]) for call in inserts]
    assert [value["binding_revision"] for value in details] == [2, 3, 4]
    assert all(value["publication_enabled"] is False for value in details)


@pytest.mark.asyncio
async def test_complete_list_absence_replays_binding_before_terminal_close() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    first_seen = RECEIVED_AT - timedelta(hours=1)
    bound_at = RECEIVED_AT - timedelta(minutes=10)
    events = [
        _binding_event(1, first_seen, scope="shared-platform", reason="unbound-agent"),
        _binding_event(
            2,
            bound_at,
            scope="customer",
            reason="job-mutual-binding",
        ),
    ]
    conn = _journal_connection(
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=1,
            binding_effective_at=first_seen,
        ),
        events,
    )

    consumed = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_absence(
        conn,
        SnapshotAbsenceMutationContext(
            snapshot_id=uuid4(),
            scope_epoch_id=SCOPE_EPOCH_ID,
            inventory_scope_id=INVENTORY_SCOPE_ID,
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
        ),
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=1,
            binding_effective_at=first_seen,
        ),
    )

    assert consumed is True
    closes = [
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE resource_intervals SET ended_at" in call.args[0]
    ]
    assert [call.args[3] for call in closes] == [
        "binding-revision-2",
        "absent-from-complete-snapshot",
    ]
    assert closes[-1].args[4] == "complete-inventory-absence"
    inserted = next(
        call
        for call in conn.fetchval.await_args_list
        if call.args[0].startswith("INSERT INTO resource_intervals")
    )
    assert json.loads(inserted.args[12])["binding_revision"] == 2


@pytest.mark.asyncio
async def test_watch_deletion_replays_binding_before_terminal_close() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    first_seen = RECEIVED_AT - timedelta(hours=1)
    bound_at = RECEIVED_AT - timedelta(minutes=10)
    row = _open_interval_row(
        interval_id,
        lifecycle_id,
        binding_revision=1,
        binding_effective_at=first_seen,
    )
    conn = _journal_connection(
        row,
        [
            _binding_event(
                1,
                first_seen,
                scope="shared-platform",
                reason="unbound-agent",
            ),
            _binding_event(
                2,
                bound_at,
                scope="customer",
                reason="job-mutual-binding",
            ),
        ],
    )

    result = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_deletion(
        conn,
        WatchDeletionMutationContext(
            scope_epoch_id=SCOPE_EPOCH_ID,
            inventory_scope_id=INVENTORY_SCOPE_ID,
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            source_kind="pod",
            source_uid="pod-uid-1",
        ),
        row,
    )

    assert result is not None
    action, affected = result
    assert action is WatchMutationAction.CLOSE
    assert isinstance(affected, UUID)
    closes = [
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE resource_intervals SET ended_at" in call.args[0]
    ]
    assert [call.args[3] for call in closes] == [
        "binding-revision-2",
        "watch-deleted",
    ]
    assert closes[-1].args[4] == "watch-deleted"


@pytest.mark.asyncio
async def test_terminal_watch_replays_binding_before_terminal_close() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    first_seen = RECEIVED_AT - timedelta(hours=1)
    bound_at = RECEIVED_AT - timedelta(minutes=10)
    row = _open_interval_row(
        interval_id,
        lifecycle_id,
        binding_revision=1,
        binding_effective_at=first_seen,
    )
    conn = _journal_connection(
        row,
        [
            _binding_event(
                1,
                first_seen,
                scope="shared-platform",
                reason="unbound-agent",
            ),
            _binding_event(
                2,
                bound_at,
                scope="customer",
                reason="job-mutual-binding",
            ),
        ],
    )

    result = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_terminal(
        conn,
        WatchTerminalMutationContext(
            scope_epoch_id=SCOPE_EPOCH_ID,
            inventory_scope_id=INVENTORY_SCOPE_ID,
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            source_kind="pod",
            source_uid="pod-uid-1",
        ),
        row,
    )

    assert result is not None and result[0] is WatchMutationAction.CLOSE
    closes = [
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE resource_intervals SET ended_at" in call.args[0]
    ]
    assert [call.args[3] for call in closes] == [
        "binding-revision-2",
        "terminal-object-event",
    ]
    assert closes[-1].args[4] == "watch-terminal"


@pytest.mark.asyncio
async def test_agent_terminal_hooks_fall_through_for_other_resources() -> None:
    reconciler = AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    )
    conn = AsyncMock()
    row = {"resource": "workspace_pod"}
    absence = await reconciler.apply_absence(
        conn,
        SnapshotAbsenceMutationContext(
            snapshot_id=uuid4(),
            scope_epoch_id=SCOPE_EPOCH_ID,
            inventory_scope_id=INVENTORY_SCOPE_ID,
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
        ),
        row,
    )
    deletion = await reconciler.apply_deletion(
        conn,
        WatchDeletionMutationContext(
            scope_epoch_id=SCOPE_EPOCH_ID,
            inventory_scope_id=INVENTORY_SCOPE_ID,
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            source_kind="pod",
            source_uid="pod-uid-1",
        ),
        row,
    )
    terminal = await reconciler.apply_terminal(
        conn,
        WatchTerminalMutationContext(
            scope_epoch_id=SCOPE_EPOCH_ID,
            inventory_scope_id=INVENTORY_SCOPE_ID,
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            source_kind="pod",
            source_uid="pod-uid-1",
        ),
        row,
    )

    assert absence is False
    assert deletion is None
    assert terminal is None
    conn.fetch.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_agent_interval_persists_binding_cursor_in_details() -> None:
    effective_at = RECEIVED_AT - timedelta(minutes=1)
    conn = AsyncMock()
    conn.fetch.return_value = [_agent_row(revision=7, effective_at=effective_at)]
    conn.fetchval.side_effect = [1, True]

    interval_id = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(conn, _snapshot_context(), _dynamic_job_item())

    assert isinstance(interval_id, UUID)
    insert = conn.execute.await_args_list[1]
    details = json.loads(insert.args[27])
    assert details["agent_id"] == str(AGENT_ID)
    assert details["binding_revision"] == 7
    assert details["binding_effective_at"] == effective_at.isoformat()
    assert details["publication_enabled"] is False


@pytest.mark.asyncio
async def test_first_agent_interval_persists_conservative_lifecycle_start() -> None:
    scheduled_at = RECEIVED_AT - timedelta(seconds=30)
    item = _dynamic_job_item()
    lifecycle = item.normalized_item["lifecycle"]
    assert isinstance(lifecycle, dict)
    lifecycle["pod_scheduled_condition"] = {
        "status": "True",
        "last_transition_time": scheduled_at.isoformat(),
    }

    conn = AsyncMock()
    conn.fetch.return_value = [
        _agent_row(revision=7, effective_at=RECEIVED_AT - timedelta(minutes=1))
    ]
    conn.fetchval.side_effect = [1, True]

    interval_id = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(conn, _snapshot_context(), item)

    assert isinstance(interval_id, UUID)
    insert = conn.execute.await_args_list[1]
    assert insert.args[23] == RECEIVED_AT
    assert insert.args[24] == "app-db-received"
    assert insert.args[25] == 30_000_000
    details = json.loads(insert.args[27])
    assert details["start_evidence_source"] == "pod-scheduled-transition"


@pytest.mark.asyncio
async def test_binding_journal_replay_is_idempotent_at_applied_revision() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    effective_at = RECEIVED_AT - timedelta(hours=1)
    event = _binding_event(
        4,
        effective_at,
        scope="shared-platform",
        reason="unbound-agent",
    )
    conn = _journal_connection(
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=4,
            binding_effective_at=effective_at,
        ),
        [event],
    )

    result = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _dynamic_job_item(),
    )

    assert result == interval_id
    assert not any(
        "UPDATE resource_intervals SET ended_at" in call.args[0]
        for call in conn.fetchrow.await_args_list
    )
    assert not any(
        call.args[0].startswith("INSERT INTO resource_intervals")
        for call in conn.fetchval.await_args_list
    )
    assert "last_confirmed_at=GREATEST" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_binding_time_clamps_to_last_proven_pod_presence() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    started_at = RECEIVED_AT - timedelta(hours=2)
    last_confirmed_at = RECEIVED_AT - timedelta(hours=1)
    stale_effective_at = RECEIVED_AT - timedelta(hours=1, minutes=30)
    events = [
        _binding_event(
            1,
            started_at,
            scope="shared-platform",
            reason="unbound-agent",
        ),
        _binding_event(
            2,
            stale_effective_at,
            scope="customer",
            reason="job-mutual-binding",
        ),
    ]
    conn = _journal_connection(
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=1,
            binding_effective_at=started_at,
            started_at=started_at,
            last_confirmed_at=last_confirmed_at,
        ),
        events,
    )

    await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _dynamic_job_item(),
    )

    close = next(
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE resource_intervals SET ended_at" in call.args[0]
    )
    assert close.args[2] == last_confirmed_at
    assert close.args[4] == "agent-binding-event-clamped"
    assert close.args[5] == int(
        (last_confirmed_at - stale_effective_at).total_seconds() * 1_000_000
    )
    insert = next(
        call
        for call in conn.fetchval.await_args_list
        if call.args[0].startswith("INSERT INTO resource_intervals")
    )
    assert insert.args[10] == last_confirmed_at
    assert insert.args[11] == "agent-binding-event-clamped"


@pytest.mark.asyncio
async def test_future_identity_move_is_deferred_without_leaking_backward() -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    baseline_at = RECEIVED_AT - timedelta(hours=1)
    events = [
        _binding_event(
            1,
            baseline_at,
            scope="shared-platform",
            reason="unbound-agent",
        ),
        _binding_event(
            2,
            RECEIVED_AT + timedelta(seconds=1),
            scope="customer",
            reason="job-mutual-binding",
            pod_uid="replacement-pod-uid",
        ),
    ]
    conn = _journal_connection(
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=1,
            binding_effective_at=baseline_at,
        ),
        events,
    )

    result = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _dynamic_job_item(),
    )

    assert result == interval_id
    assert not any(
        "UPDATE resource_intervals SET ended_at" in call.args[0]
        for call in conn.fetchrow.await_args_list
    )
    assert not any(
        call.args[0].startswith("INSERT INTO resource_intervals")
        for call in conn.fetchval.await_args_list
    )


@pytest.mark.asyncio
async def test_due_identity_move_splits_old_pod_to_unknown_and_remains_replayable() -> (
    None
):
    interval_id = uuid4()
    lifecycle_id = uuid4()
    baseline_at = RECEIVED_AT - timedelta(hours=1)
    moved_at = RECEIVED_AT - timedelta(minutes=10)
    baseline = _binding_event(
        1,
        baseline_at,
        scope="shared-platform",
        reason="unbound-agent",
    )
    moved = _binding_event(
        2,
        moved_at,
        scope="customer",
        reason="job-mutual-binding",
        pod_uid="replacement-pod-uid",
    )
    conn = _journal_connection(
        _open_interval_row(
            interval_id,
            lifecycle_id,
            binding_revision=1,
            binding_effective_at=baseline_at,
        ),
        [baseline, moved],
    )

    moved_interval_id = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _dynamic_job_item(),
    )

    insert = next(
        call
        for call in conn.fetchval.await_args_list
        if call.args[0].startswith("INSERT INTO resource_intervals")
    )
    assert insert.args[3] == "unknown"
    assert insert.args[4] is None
    details = json.loads(insert.args[12])
    assert details["binding_revision"] == 2
    assert details["attribution_reason"] == "agent-pod-identity-moved"
    assert details["publication_enabled"] is False

    assert isinstance(moved_interval_id, UUID)
    replay = _journal_connection(
        _open_interval_row(
            moved_interval_id,
            lifecycle_id,
            binding_revision=2,
            binding_effective_at=moved_at,
            scope="unknown",
            started_at=moved_at,
            last_confirmed_at=RECEIVED_AT,
        ),
        [moved],
    )
    replay_existing = replay.fetchrow.side_effect

    def moved_fetchrow(sql: str, *args: object) -> object:
        value = replay_existing(sql, *args)
        if "FROM resource_intervals" in sql and "FOR UPDATE" in sql:
            assert isinstance(value, dict)
            details_value = value["details"]
            assert isinstance(details_value, dict)
            details_value["attribution_reason"] = "agent-pod-identity-moved"
        return value

    replay.fetchrow.side_effect = moved_fetchrow

    replay_result = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        replay,
        _snapshot_context(
            received_at=RECEIVED_AT + timedelta(minutes=1),
            existing_interval_id=moved_interval_id,
        ),
        _dynamic_job_item(),
    )

    assert replay_result == moved_interval_id
    assert not any(
        "UPDATE resource_intervals SET ended_at" in call.args[0]
        for call in replay.fetchrow.await_args_list
    )


@pytest.mark.asyncio
async def test_unchanged_legacy_interval_bootstraps_cursor_not_attribution_change() -> (
    None
):
    interval_id = uuid4()
    lifecycle_id = uuid4()
    effective_at = RECEIVED_AT - timedelta(hours=1)
    existing = _open_interval_row(
        interval_id,
        lifecycle_id,
        binding_revision=1,
        binding_effective_at=effective_at,
    )
    details = existing["details"]
    assert isinstance(details, dict)
    details.pop("binding_revision")
    details.pop("binding_effective_at")
    conn = AsyncMock()

    def fetchrow(sql: str, *args: object) -> object:
        if "FROM resource_intervals" in sql and "FOR UPDATE" in sql:
            return existing
        if "UPDATE resource_intervals SET ended_at" in sql:
            return {"id": args[0], "source_lifecycle_id": lifecycle_id}
        raise AssertionError(f"unexpected fetchrow: {sql}")

    def fetchval(sql: str, *args: object) -> object:
        if "current_interval_id=NULL" in sql:
            return True
        if "latest_revision_no=latest_revision_no+1" in sql:
            return 2
        if "SET current_interval_id=$2" in sql:
            return True
        raise AssertionError(f"unexpected fetchval: {sql}")

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.return_value = [_agent_row(revision=1, effective_at=effective_at)]
    conn.fetchval.side_effect = fetchval

    result = await AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=_active_activation(),
    ).apply_snapshot(
        conn,
        _snapshot_context(existing_interval_id=interval_id),
        _dynamic_job_item(),
    )

    assert isinstance(result, UUID)
    close = next(
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE resource_intervals SET ended_at" in call.args[0]
    )
    assert close.args[3] == "binding-journal-bootstrap"
    insert = conn.execute.await_args_list[1]
    inserted_details = json.loads(insert.args[27])
    assert inserted_details["binding_revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["gap", "baseline-identity", "oversized", "stale-receipt"]
)
async def test_binding_replay_fails_closed_before_writes(failure: str) -> None:
    interval_id = uuid4()
    lifecycle_id = uuid4()
    baseline_at = RECEIVED_AT - timedelta(hours=1)
    existing = _open_interval_row(
        interval_id,
        lifecycle_id,
        binding_revision=1,
        binding_effective_at=baseline_at,
        last_confirmed_at=(
            RECEIVED_AT + timedelta(seconds=1)
            if failure == "stale-receipt"
            else baseline_at
        ),
    )
    baseline = _binding_event(
        1,
        baseline_at,
        scope="shared-platform",
        reason="unbound-agent",
    )
    if failure == "gap":
        events = [
            baseline,
            _binding_event(
                3,
                RECEIVED_AT - timedelta(minutes=1),
                scope="customer",
                reason="job-mutual-binding",
            ),
        ]
    elif failure == "baseline-identity":
        events = [
            _binding_event(
                1,
                baseline_at,
                scope="shared-platform",
                reason="unbound-agent",
                pod_uid="another-pod-uid",
            )
        ]
    elif failure == "oversized":
        events = [
            baseline,
            _binding_event(
                258,
                RECEIVED_AT - timedelta(minutes=1),
                scope="customer",
                reason="job-mutual-binding",
            ),
        ]
    else:
        events = [baseline]
    conn = _journal_connection(existing, events)

    with pytest.raises(InventoryConflictError):
        await AgentPodIntervalReconciler(
            shadow_enabled=True,
            activation=_active_activation(),
        ).apply_snapshot(
            conn,
            _snapshot_context(existing_interval_id=interval_id),
            _dynamic_job_item(),
        )

    assert not any(
        "UPDATE resource_intervals SET ended_at" in call.args[0]
        for call in conn.fetchrow.await_args_list
    )
    assert not any(
        call.args[0].startswith("INSERT INTO resource_intervals")
        for call in conn.fetchval.await_args_list
    )


@pytest.mark.asyncio
async def test_shadow_observation_matches_0103_shape_and_snapshots_customer() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [
        _agent_row(
            attribution_scope="customer",
            owner_kind="job",
            owner_id=JOB_ID,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            reason_code="job-mutual-binding",
        )
    ]
    reconciler = AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=ComputeActivation(
            activation_key="agent_pod",
            state="shadow",
            activated_at=None,
            database_time=RECEIVED_AT,
        ),
    )

    await reconciler.observe_snapshot(
        conn,
        SnapshotObservationContext(
            snapshot_id=uuid4(),
            scope_epoch_id=uuid4(),
            inventory_scope_id=uuid4(),
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            current_interval_id=None,
            current_source_revision=None,
        ),
        _dynamic_job_item(),
    )

    insert = conn.execute.await_args
    assert "activation_key" in insert.args[0]
    assert "product_class" in insert.args[0]
    assert insert.args[4] == "dynamic-agent"
    assert insert.args[7] == "customer"
    assert insert.args[8] == "job"
    assert insert.args[9] == JOB_ID
    assert insert.args[10] == USER_ID
    assert insert.args[11] == PROJECT_ID
    assert insert.args[12] == "eligible-unpriced"


@pytest.mark.asyncio
async def test_agent_shadow_writes_explicit_not_applicable_for_every_other_pod() -> (
    None
):
    conn = AsyncMock()
    reconciler = AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=ComputeActivation(
            activation_key="agent_pod",
            state="shadow",
            activated_at=None,
            database_time=RECEIVED_AT,
        ),
    )
    postgres = _item(name="postgres", labels={"app": "postgres"})

    await reconciler.observe_snapshot(
        conn,
        SnapshotObservationContext(
            snapshot_id=uuid4(),
            scope_epoch_id=uuid4(),
            inventory_scope_id=uuid4(),
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            current_interval_id=None,
            current_source_revision=None,
        ),
        postgres,
    )

    insert = conn.execute.await_args
    assert insert.args[4] == "other"
    assert insert.args[7] == "unknown"
    assert insert.args[12] == "not-applicable"
    assert insert.args[13] == "other-pod"
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_fallback_still_gets_item_for_item_shadow_row() -> None:
    conn = AsyncMock()
    reconciler = AgentPodIntervalReconciler(
        shadow_enabled=True,
        activation=ComputeActivation(
            activation_key="agent_pod",
            state="shadow",
            activated_at=None,
            database_time=RECEIVED_AT,
        ),
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

    await reconciler.observe_snapshot(
        conn,
        SnapshotObservationContext(
            snapshot_id=uuid4(),
            scope_epoch_id=uuid4(),
            inventory_scope_id=uuid4(),
            source_cluster="cluster-a",
            namespace="srw",
            received_at=RECEIVED_AT,
            current_interval_id=None,
            current_source_revision=None,
        ),
        invalid,
    )

    insert = conn.execute.await_args
    assert insert.args[3] == "bad-pod-uid"
    assert insert.args[4] == "other"
    assert insert.args[12] == "invalid"
    assert insert.args[13] == "invalid-pod-capacity"
